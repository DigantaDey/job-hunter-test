"""
Discovery pipeline: source fan-out → filtering → scoring → classification →
form detection → deduplicated persistence.

Cost control is explicit: heuristics score everything, and only the top
candidates get (rate-limited) AI calls, so a discovery run cannot blow the
configured RPM budget or the AI bill.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc
from app.models.models import Job, User
from app.services import sources as source_registry
from app.services.ai_guardrails import AIUnavailableError
from app.services.classifier import ai_company_size, heuristic_company_size
from app.services.demo_pool import demo_jobs
from app.services.events import record_job_event
from app.services.form_detector import detect_form_structure
from app.services.scoring import heuristic_score, score_job
from app.services.user_settings import get_setting

log = get_logger("app.discovery")

AI_RESCORE_TOP = 12
AI_CLASSIFY_TOP = 5
FORM_DETECT_TOP = 8


def _demo_pool_enabled() -> bool:
    """
    Demo seed data is opt-in — in *every* environment.

    This used to default to enabled outside production and to read
    ``os.getenv`` directly, which meant the documented ``INCLUDE_DEMO_POOL``
    setting in ``.env`` was ignored and a plain local install silently mixed
    fabricated postings into real results.
    """
    return bool(settings.include_demo_pool)


async def _score_candidates(profile_data: Dict[str, Any], candidates: List[Dict[str, Any]], use_ai: bool,
                            *, db=None, user_id: Optional[int] = None,
                            persona: Optional[Dict[str, Any]] = None) -> None:
    """
    Preliminary score for all; guardrailed AI verdict for the strongest.

    Provenance is recorded on every candidate: scoring 200 postings with the
    model is not viable, so the bulk of a discovery run carries a deterministic
    *preliminary* score, and only the top slice gets the real AI verdict. The
    UI reads ``score_source`` so a keyword estimate is never presented as an
    AI match score.
    """
    for candidate in candidates:
        score, reason = heuristic_score(profile_data, candidate["description"])
        candidate["score"] = score
        candidate["score_reason"] = f"Preliminary keyword estimate: {reason}"
        candidate["score_source"] = "preliminary"

    if not use_ai or not profile_data:
        return
    ranked = sorted(candidates, key=lambda c: c["score"], reverse=True)[:AI_RESCORE_TOP]
    ai_outage: Optional[Dict[str, Any]] = None
    for candidate in ranked:
        try:
            result = await score_job(profile_data, candidate["description"], db=db, user_id=user_id,
                                     persona=persona, allow_preliminary=False)
            candidate["score"] = result["score"]
            candidate["score_reason"] = result["reason"]
            candidate["score_source"] = result["score_source"]
            candidate["score_detail"] = result.get("detail") or {}
        except AIUnavailableError as exc:
            # Do not keep hammering a provider that is down — report it once
            # and leave the remaining candidates on their preliminary score.
            ai_outage = exc.payload()
            log.warning("AI scoring unavailable during discovery (%s): %s", exc.reason, exc.detail)
            break
        except Exception as exc:  # a single bad JD must not fail discovery
            log.debug("ai scoring skipped: %s", exc)
    if ai_outage:
        for candidate in candidates:
            candidate.setdefault("ai_error", ai_outage)


async def discover_for_user(
    db: Session,
    user: User,
    *,
    keywords: List[str],
    freshness_hours: int,
    limit: int = 30,
    live_enabled: bool = True,
    source_ids: Optional[List[str]] = None,
    board_tokens: Optional[List[str]] = None,
    profile: Optional[Dict[str, Any]] = None,
    persona_id: Optional[int] = None,
    persona_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one discovery pass for a user (optionally scoped to one persona)."""

    profile_data = profile or {}
    started = datetime.utcnow()
    report: Dict[str, Any] = {"started_at": started.isoformat(), "keywords": keywords}

    # ---------------- fetch ----------------
    postings: List[Dict[str, Any]] = []
    source_report: Dict[str, Any] = {}
    if live_enabled:
        fetched, source_report = await source_registry.fetch_all(
            keywords,
            limit=max(limit * 2, 40),
            # Fetch a wider window than the user asked for, then filter locally:
            # it lets us report "nothing fresh" honestly instead of "no results".
            since_hours=max(24 * 7, freshness_hours),
            sources=source_ids,
            board_tokens=board_tokens,
        )
        postings.extend(posting.to_dict() for posting in fetched)
    report["sources"] = source_report

    if _demo_pool_enabled():
        postings.extend(demo_jobs(keywords, freshness_hours, limit=max(6, limit // 2)))
        report["demo_pool"] = True

    # ---------------- de-duplicate within the batch ----------------
    unique: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for posting in postings:
        key = (posting.get("external_id") or f"{posting['company']}|{posting['title']}").lower()[:280]
        posting["dedupe_key"] = key
        if key in seen:
            continue
        seen.add(key)
        unique.append(posting)

    # ---------------- freshness window ----------------
    cutoff = started - timedelta(hours=max(1, freshness_hours))
    fresh = [p for p in unique if not p.get("posted_at") or p["posted_at"] >= cutoff]
    older = [p for p in unique if p.get("posted_at") and p["posted_at"] < cutoff]
    if len(fresh) < max(3, limit // 3) and older:
        shortfall = max(3, limit // 3) - len(fresh)
        for posting in older[:shortfall]:
            posting["freshness_relaxed"] = True
            fresh.append(posting)
        report["freshness_relaxed"] = True

    fresh.sort(key=lambda p: (p["score"] if "score" in p else 0, p.get("posted_at") or datetime.min), reverse=False)
    fresh.sort(key=lambda p: p.get("posted_at") or datetime.min, reverse=True)
    candidates = fresh[: max(limit * 2, 40)]

    # Drop exact duplicates that already exist for this user.
    existing_keys = {
        row[0]
        for row in db.query(Job.dedupe_key).filter(Job.user_id == user.id, Job.dedupe_key != "").all()
    }
    candidates = [c for c in candidates if c["dedupe_key"] not in existing_keys]

    # ---------------- score / classify ----------------
    await _score_candidates(profile_data, candidates, use_ai=bool(profile_data),
                            db=db, user_id=user.id, persona=persona_context)

    ranked = sorted(candidates, key=lambda c: c.get("score", 0), reverse=True)[:limit * 2]
    for index, candidate in enumerate(ranked):
        if index < AI_CLASSIFY_TOP and profile_data:
            size, confidence = await ai_company_size(candidate["company"], candidate["description"])
        else:
            size = heuristic_company_size({"size": candidate.get("company_size")}, candidate)
            confidence = 0.55
        candidate["company_size"] = size
        candidate["company_size_confidence"] = confidence

    # ---------------- persist ----------------
    created: List[Job] = []
    for candidate in ranked[:limit]:
        job = Job(
            user_id=user.id,
            title=candidate["title"],
            company=candidate["company"],
            location=candidate.get("location", ""),
            description=candidate.get("description", ""),
            url=candidate.get("url", ""),
            source=candidate.get("source", "unknown"),
            external_id=candidate.get("external_id", ""),
            dedupe_key=candidate["dedupe_key"],
            status="discovered",
            score=float(candidate.get("score", 0.0)),
            score_reason=candidate.get("score_reason", ""),
            score_source=str(candidate.get("score_source") or "preliminary"),
            score_detail=candidate.get("score_detail") or ({"ai_error": candidate["ai_error"]}
                                                           if candidate.get("ai_error") else {}),
            persona_id=persona_id,
            company_size=candidate.get("company_size", "unknown"),
            company_info={"confidence": candidate.get("company_size_confidence", 0.0),
                          "industry": candidate.get("industry", "")},
            posted_at=candidate.get("posted_at"),
            freshness_hours=freshness_hours,
            extra={"salary": candidate.get("salary", ""), "remote": candidate.get("remote", False),
                   "freshness_relaxed": candidate.get("freshness_relaxed", False), "forms": {}},
        )
        db.add(job)
        created.append(job)

    try:
        db.commit()
    except Exception as exc:  # unique constraint race → reload, keep going
        db.rollback()
        log.warning("discovery insert conflict: %s", exc)
        created = []

    # Increment usage by actual inserted count (more accurate than trigger-time 1)
    try:
        from app.core.entitlements import increment_usage
        if created:
            increment_usage(db, user.id, "jobs_discovered_per_month", len(created))
    except Exception as e:
        log.debug("usage increment failed: %s", e)

    for job in created:
        db.refresh(job)
        record_job_event(
            db, user_id=user.id, job_id=job.id, stage="discovered", status="success",
            message=f"Discovered from {job.source} (score {job.score:.0f})",
            meta={"source": job.source, "posted_at": job.posted_at.isoformat() if job.posted_at else None},
        )

    # ---------------- form structure for the best new jobs ----------------
    form_tasks = [detect_form_structure(job.url, job.source, use_ai=False) for job in created[:FORM_DETECT_TOP]]
    if form_tasks:
        results = await asyncio.gather(*form_tasks, return_exceptions=True)
        for job, schema in zip(created[:FORM_DETECT_TOP], results, strict=False):
            if isinstance(schema, Exception):
                continue
            extra = dict(job.extra or {})
            extra["forms"] = schema
            job.extra = extra
            record_job_event(
                db, user_id=user.id, job_id=job.id, stage="form_detected",
                status="success" if schema.get("detection_source") == "html" else "warning",
                message=f"Form structure via {schema.get('detection_source')} "
                        f"({len(schema.get('fields') or [])} fields, confidence {schema.get('ai_confidence')})",
                meta={"detection_source": schema.get("detection_source"), "portal": schema.get("portal_type")},
            )
        db.commit()

    elapsed = (datetime.utcnow() - started).total_seconds()
    inc("jobhunter_discovery_runs_total", result="ok")
    summary = {
        "scanned": len(unique),
        "fresh": len(fresh),
        "inserted": len(created),
        "sources": source_report,
        "elapsed_seconds": round(elapsed, 2),
        "jobs": [
            {"id": job.id, "title": job.title, "company": job.company, "source": job.source,
             "score": job.score, "location": job.location, "url": job.url}
            for job in created
        ],
    }
    log.info(
        "discovery: %s scanned, %s inserted in %.1fs",
        summary["scanned"], summary["inserted"], elapsed,
        extra={"extra_fields": {"user_id": user.id, **{k: summary[k] for k in ("scanned", "fresh", "inserted")}}},
    )
    return summary


def discovery_sources_config(db: Session, user_id: int) -> Dict[str, Any]:
    """Resolve the effective discovery configuration for a user."""
    sources = get_setting(db, user_id, "scraping", "sources", None)
    if sources is None:
        sources = [s["id"] for s in source_registry.list_sources() if s["available"] and s.get("enabled_by_default")]
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]
    board_tokens = get_setting(db, user_id, "scraping", "board_tokens", []) or []
    if isinstance(board_tokens, str):
        board_tokens = [t.strip() for t in board_tokens.split(",") if t.strip()]
    return {
        "sources": [s for s in sources if s in source_registry.ADAPTERS],
        "board_tokens": board_tokens,
        "live_enabled": bool(get_setting(db, user_id, "scraping", "live_enabled", settings.live_scraping_enabled)),
    }
