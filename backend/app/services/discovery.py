"""
Discovery pipeline: source fan-out → filtering → scoring → classification →
form detection → deduplicated persistence.

Cost control is explicit: a deterministic pre-rank scores everything, and only
the top candidates get (rate-limited) AI calls, so a discovery run cannot blow
the configured RPM budget or the AI bill. The AI verdicts are a hard
dependency for the top slice — an outage pauses the queued run (v2.1), which
re-executes when the provider is back.

The AI top slice is also **plan-gated** (v2.2.3): the caller passes a profile
only when the user has ``can_use_advanced_matching``, so a free-tier run makes
zero AI calls and stays fully preliminary — the same gate
``GET /api/jobs/{job_id}/intelligence`` enforces per job. Every run reports what
happened in its ``ai_rescore`` block, so a board full of estimates says why.

An *empty* board is reported the same way (v2.2.4): a run that adds nothing says
which of the three honest reasons it was — no sources were attempted, every
source that was attempted failed, or the sources were healthy and simply had
nothing inside the freshness window — plus a ``summary`` block with the counts
behind that verdict. ``discover_for_user`` is the single source of truth for
both: the endpoint that serves them
(``GET /api/jobs/discovery/last-run``) only reads the stored report, so the
queue row, the log line and the UI can never disagree about why a board is
empty. Reporting only — no fetching, scoring or quota behaviour changes.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc
from app.models.models import Job, User
from app.services import sources as source_registry
from app.services.classifier import ai_company_size, deterministic_company_size
from app.services.company_normalize import normalize_company_name
from app.services.demo_pool import demo_jobs
from app.services.events import record_job_event
from app.services.form_detector import detect_form_structure
from app.services.scoring import preliminary_score, score_job
from app.services.user_settings import get_setting

log = get_logger("app.discovery")

AI_RESCORE_TOP = 12
AI_CLASSIFY_TOP = 5
FORM_DETECT_TOP = 8

#: The only two reasons the AI top slice may be skipped, reported verbatim in
#: the run's ``ai_rescore.skipped`` (``None`` means the slice ran). They are a
#: vocabulary, not free text: an operator — or the empty-board work — reads one
#: of these to find out *why* a board is full of keyword estimates, and the two
#: fixes are different ("upload a resume" vs "upgrade the plan").
AI_RESCORE_NO_PROFILE = "no_profile"
AI_RESCORE_PLAN_FREE = "plan_free"

#: The only four reasons a discovery run can add nothing to the board,
#: reported verbatim as the run's ``why_empty``. Like the ``ai_rescore``
#: reasons above they are a vocabulary, not free text: "no fresh jobs" used to
#: be the whole story, and a user could not tell a configuration problem (fix
#: it in Settings) from a source outage (retry) from a quiet market (widen the
#: window). Exactly one of these is set, and only when the run added zero jobs
#: — a run that found jobs has no ``why_empty`` key at all (not ``null``, not
#: ``""``), so an absent key is meaningful.
#:
#: There is deliberately no fourth reason for "candidates were found but every
#: one was already on the board": such a run adds nothing *because* the board
#: already has those jobs, so the board is not empty and no banner is rendered.
WHY_NO_SOURCES_CONFIGURED = "no_sources_configured"
WHY_ALL_SOURCES_FAILED = "all_sources_failed"
WHY_NO_FRESH_POSTINGS = "no_fresh_postings"
#: The board is at its plan's ``jobs_max`` storage cap (v2.2.10): the run is a
#: no-op *by design* — not an outage, not a quiet market, and never an
#: over-limit insert. The fix is delete jobs or upgrade the plan.
WHY_JOBS_CAP_REACHED = "jobs_cap_reached"


def _canonical_key(posting: Dict[str, Any]) -> str:
    """Canonical job identity used to merge duplicates.

    Prefer the adapter's ``canonical_id`` / ``dedupe_key`` (``source:external_id``
    when the source has a stable id). Fall back to the historical
    ``external_id`` / ``company|title`` shapes so a row written before this
    release still matches a re-fetch of the same posting.
    """
    for candidate in (posting.get("canonical_id"), posting.get("dedupe_key")):
        text = str(candidate or "").strip().lower()
        if text:
            return text[:280]
    source = str(posting.get("source") or "").strip()
    external = str(posting.get("external_id") or "").strip()
    if external:
        return (f"{source}:{external}" if source else external).lower()[:280]
    company = str(posting.get("company") or "").strip()
    title = str(posting.get("title") or "").strip()
    return f"{company}|{title}".lower()[:280]


def _lookup_keys(posting: Dict[str, Any]) -> List[str]:
    """Canonical key plus the two historical shapes, for matching existing rows."""
    canonical = _canonical_key(posting)
    keys = [canonical]
    external = str(posting.get("external_id") or "").strip().lower()[:280]
    if external and external not in keys:
        keys.append(external)
    company = str(posting.get("company") or "").strip()
    title = str(posting.get("title") or "").strip()
    legacy = f"{company}|{title}".lower()[:280]
    if legacy not in ("|", "") and legacy not in keys:
        keys.append(legacy)
    return keys


def _is_expired_candidate(posting: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    if posting.get("expired"):
        return True
    expires_at = posting.get("expires_at")
    if isinstance(expires_at, datetime) and expires_at <= (now or datetime.utcnow()):
        return True
    return False


def _store_raw_payload(job: Job, candidate: Dict[str, Any]) -> None:
    """Keep the compact source payload on the job, never inside ``extra``."""
    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else None
    if raw:
        job.raw_payload = dict(raw)


def _merge_existing_job(job: Job, candidate: Dict[str, Any], now: datetime) -> None:
    """Update timestamps / description on a job we already have. Never rewrite first_seen."""
    job.last_seen_at = now
    job.last_verified_at = now
    if _is_expired_candidate(candidate, now):
        job.expired = True
        job.expired_at = job.expired_at or now
    elif job.expired:
        job.expired = False
        job.expired_at = None
    incoming_description = str(candidate.get("description") or "")
    if len(incoming_description) > len(job.description or ""):
        job.description = incoming_description[:6000]
    if candidate.get("url") and not job.url:
        job.url = str(candidate["url"])[:1000]
    if candidate.get("content_hash") and not job.content_hash:
        job.content_hash = str(candidate["content_hash"])[:64]
    if candidate.get("source_kind") and not job.source_kind:
        job.source_kind = str(candidate["source_kind"])[:16]
    _store_raw_payload(job, candidate)
    attribution = (candidate.get("extra") or {}).get("search_discovery")
    if attribution:
        job.extra = {**(job.extra or {}), "search_discovery": attribution}


def _demo_pool_enabled() -> bool:
    """
    Demo seed data is opt-in — in *every* environment.

    This used to default to enabled outside production and to read
    ``os.getenv`` directly, which meant the documented ``INCLUDE_DEMO_POOL``
    setting in ``.env`` was ignored and a plain local install silently mixed
    fabricated postings into real results.
    """
    return bool(settings.include_demo_pool)


def _classify_empty_run(source_report: Dict[str, Any]) -> str:
    """Which of the three honest reasons this run added nothing to the board.

    Reads only the per-source report :func:`app.services.sources.fetch_all`
    returns — ``{"requested": [...], "ok": {...}, "errors": {...}, "total": n}``
    — so the classification is a fact about the run, never a guess:

    * nothing was *attempted* (``requested`` empty — no sources configured, or
      ``live_enabled`` off, which skips the fan-out entirely) →
      :data:`WHY_NO_SOURCES_CONFIGURED`;
    * sources were attempted and **every** one of them is in ``errors``
      (individual source failures never raise; they land there) →
      :data:`WHY_ALL_SOURCES_FAILED` — an outage, not a quiet market;
    * otherwise the fan-out produced something to keep (at least one source in
      ``ok``) and nothing survived the freshness window and the dedupe against
      the board → :data:`WHY_NO_FRESH_POSTINGS`.

    The last branch is the default on purpose: a partial report (a source in
    neither ``ok`` nor ``errors``, which ``fetch_all`` cannot produce but a
    hand-built one could) must still yield exactly one reason rather than no
    reason at all, and "the sources ran and nothing was fresh" is the honest
    reading of it.
    """
    requested = [str(source_id) for source_id in (source_report.get("requested") or [])]
    errors = source_report.get("errors") or {}
    if not requested:
        return WHY_NO_SOURCES_CONFIGURED
    if all(source_id in errors for source_id in requested):
        return WHY_ALL_SOURCES_FAILED
    return WHY_NO_FRESH_POSTINGS


def _run_summary(source_report: Dict[str, Any], *, freshness_hours: int, fresh_count: int) -> Dict[str, Any]:
    """The counts behind :func:`_classify_empty_run`'s verdict.

    Every value is derived from the same per-source report the classification
    reads (plus two facts of this run), so the two can never tell different
    stories. ``needs_credentials`` is the registry's own
    :func:`~app.services.sources.unconfigured_source_ids` — available adapters
    whose credentials the operator never provided — which is what separates
    "these sources need keys" from "the network is down".
    """
    return {
        "configured_sources": [str(source_id) for source_id in (source_report.get("requested") or [])],
        "failed_sources": {str(source_id): str(error) for source_id, error in (source_report.get("errors") or {}).items()},
        "needs_credentials": list(source_registry.unconfigured_source_ids()),
        "demo_pool_enabled": _demo_pool_enabled(),
        "freshness_window_hours": int(freshness_hours),
        "fetched": int(source_report.get("total") or 0),
        "fresh": int(fresh_count),
    }


async def _score_candidates(profile_data: Dict[str, Any], candidates: List[Dict[str, Any]], use_ai: bool,
                            *, db=None, user_id: Optional[int] = None,
                            persona: Optional[Dict[str, Any]] = None) -> None:
    """
    Preliminary pre-rank for all (deterministic, provenance-labelled
    ``score_source='preliminary'``); guardrailed AI verdict for the strongest.

    Cost control is explicit: scoring 200 postings with the model is not
    viable, so the bulk of a discovery run carries the *preliminary* score and
    only the top slice gets the real AI verdict. The UI reads ``score_source``
    so a preliminary estimate is never presented as an AI match score.

    AI is a hard dependency for the top-slice verdicts: when the model is
    unavailable this RAISES (transient outage → the queued discovery run is
    paused and re-executed when the provider is back; needs-action → the run
    dead-letters with the diagnosis). Nothing is persisted before this point,
    so a paused-and-resumed run creates exactly the same rows as an
    uninterrupted one.

    ``use_ai`` is the caller's decision, not this function's: the queued run
    (``handlers.handle_discovery``) sets it from the user's profile *and* their
    ``can_use_advanced_matching`` entitlement, so a free-tier batch never
    reaches the loop below.
    """
    for candidate in candidates:
        score, reason = preliminary_score(profile_data, candidate["description"])
        candidate["score"] = score
        candidate["score_reason"] = f"Preliminary keyword estimate: {reason}"
        candidate["score_source"] = "preliminary"

    if not use_ai or not profile_data:
        return
    ranked = sorted(candidates, key=lambda c: c["score"], reverse=True)[:AI_RESCORE_TOP]
    for candidate in ranked:
        result = await score_job(profile_data, candidate["description"], db=db, user_id=user_id,
                                 persona=persona, allow_preliminary=False)
        candidate["score"] = result["score"]
        candidate["score_reason"] = result["reason"]
        candidate["score_source"] = result["score_source"]
        candidate["score_detail"] = result.get("detail") or {}


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
    ai_rescore_skipped: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one discovery pass for a user (optionally scoped to one persona).

    ``profile`` is the AI switch. Passing a non-empty profile turns on the
    guardrailed AI verdict for the top :data:`AI_RESCORE_TOP` candidates (and
    the AI company-size verdict for the top :data:`AI_CLASSIFY_TOP`); passing
    nothing keeps the whole run on the deterministic pre-rank. Callers therefore
    decide *who* gets the AI slice — ``handlers.handle_discovery`` withholds the
    profile from users without ``can_use_advanced_matching``, which is what makes
    a free-tier run cost zero AI calls.

    ``ai_rescore_skipped`` is the caller's reason for withholding it
    (:data:`AI_RESCORE_NO_PROFILE` / :data:`AI_RESCORE_PLAN_FREE`); it is
    reported verbatim in the run's ``ai_rescore`` block so an operator can tell
    a plan gate apart from a missing resume. It is only read when no profile was
    passed.

    ``source_ids`` follows the same None-vs-empty contract as
    :func:`app.services.sources.fetch_all`: ``None`` means "whatever the caller
    resolved for this user" (which ``fetch_all`` reads as every available
    adapter), an explicit ``[]`` means *fetch nothing* — a user who deliberately
    disabled every source must not be silently upgraded to all of them.

    Returns the run report: ``started_at``, ``keywords``, ``sources`` (the
    per-source report), ``scanned`` / ``fresh`` / ``inserted`` /
    ``elapsed_seconds``, ``ai_rescore``, ``summary``, ``jobs``, plus
    ``demo_pool`` / ``freshness_relaxed`` when they apply and ``why_empty`` when
    — and only when — the run added zero jobs.
    """

    profile_data = profile or {}
    started = datetime.utcnow()
    report: Dict[str, Any] = {"started_at": started.isoformat(), "keywords": keywords}

    # ---------------- storage cap (jobs_max) ----------------
    # The board has a real size now: the cap is enforced by *row count*, in
    # the worker as well as at the HTTP trigger (``POST /api/jobs/discover``
    # answers 429), so concurrent runs can never overshoot it. A full board
    # is an honest zero-job result with a reason — never a silent no-op and
    # never an over-limit insert. A nearly-full board is clamped so the run
    # fills exactly the remaining slots.
    from app.core.entitlements import check_storage

    _cap_allowed, jobs_used, jobs_limit, _cap_hint = check_storage(db, user.id, "jobs_max")
    # ``None`` = unlimited; otherwise the exact number of rows this run may add.
    jobs_remaining: Optional[int] = None if jobs_limit <= 0 else max(0, jobs_limit - jobs_used)
    if jobs_limit > 0:
        report["jobs_cap"] = {"used": jobs_used, "limit": jobs_limit}
    if jobs_remaining == 0:
        report["sources"] = {}
        report["scanned"] = 0
        report["fresh"] = 0
        report["inserted"] = 0
        report["elapsed_seconds"] = round((datetime.utcnow() - started).total_seconds(), 2)
        report["summary"] = _run_summary({}, freshness_hours=freshness_hours, fresh_count=0)
        report["why_empty"] = WHY_JOBS_CAP_REACHED
        report["jobs"] = []
        log.warning("discovery: user %s board at storage cap (%s/%s) — run is a no-op",
                    user.id, jobs_used, jobs_limit)
        return report

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
        if settings.job_search_providers.strip():
            from app.services.search import discover_search, preferences_for_user

            try:
                search_postings, search_report = await discover_search(
                    db, user.id, preferences_for_user(db, user.id, persona_id),
                    since_hours=freshness_hours, known_urls=[p.url for p in fetched],
                )
                postings.extend(p.to_dict() for p in search_postings)
                report["search"] = search_report
                source_report.setdefault("requested", []).append("search_discovery")
                if search_report.get("errors") and not search_postings:
                    source_report.setdefault("errors", {})["search_discovery"] = "search_provider_failed"
                elif search_report.get("skipped"):
                    source_report.setdefault("skipped", {})["search_discovery"] = search_report["skipped"]
                else:
                    source_report.setdefault("ok", {})["search_discovery"] = len(search_postings)
                source_report["total"] = int(source_report.get("total", 0)) + len(search_postings)
            except Exception:
                # Search is supplementary. A provider/cache failure must never
                # discard direct-adapter postings or leak provider exception text.
                report["search"] = {"errors": [{"code": "unavailable"}], "validated": 0}
    report["sources"] = source_report

    if _demo_pool_enabled():
        postings.extend(demo_jobs(keywords, freshness_hours, limit=max(6, limit // 2)))
        report["demo_pool"] = True

    # ---------------- de-duplicate within the batch ----------------
    unique: List[Dict[str, Any]] = []
    seen: set[str] = set()
    seen_hash: set[str] = set()
    for posting in postings:
        key = _canonical_key(posting)
        posting["dedupe_key"] = key
        posting.setdefault("canonical_id", key)
        if key in seen:
            continue
        content_hash = str(posting.get("content_hash") or "")
        if content_hash and content_hash in seen_hash:
            continue
        seen.add(key)
        if content_hash:
            seen_hash.add(content_hash)
        unique.append(posting)

    # ---------------- freshness window ----------------
    cutoff = started - timedelta(hours=max(1, freshness_hours))
    # Expired postings are never fresh matches, even if they still have a recent
    # posted_at (a source can list a closed role).
    live_unique = [p for p in unique if not _is_expired_candidate(p, started)]
    expired_dropped = len(unique) - len(live_unique)
    if expired_dropped:
        report["expired_dropped"] = expired_dropped
    fresh = [p for p in live_unique if not p.get("posted_at") or p["posted_at"] >= cutoff]
    older = [p for p in live_unique if p.get("posted_at") and p["posted_at"] < cutoff]
    if len(fresh) < max(3, limit // 3) and older:
        shortfall = max(3, limit // 3) - len(fresh)
        for posting in older[:shortfall]:
            posting["freshness_relaxed"] = True
            fresh.append(posting)
        report["freshness_relaxed"] = True

    # Newest first. This is the only ordering that is known *before* scoring —
    # ``score`` does not exist on a candidate until ``_score_candidates`` runs
    # below, so a sort by score here could only be a no-op on a constant key.
    fresh.sort(key=lambda p: p.get("posted_at") or datetime.min, reverse=True)
    candidates = fresh[: max(limit * 2, 40)]

    # Merge postings we already have (update last_seen / last_verified) rather
    # than presenting them as fresh matches. Look up by canonical key and by
    # content hash so a cross-source duplicate does not become a second row.
    existing_keys = {
        row[0]
        for row in db.query(Job.dedupe_key).filter(Job.user_id == user.id, Job.dedupe_key != "").all()
    }
    existing_hashes = {
        row[0]
        for row in db.query(Job.content_hash).filter(
            Job.user_id == user.id, Job.content_hash != "", Job.content_hash.isnot(None),
        ).all()
        if row[0]
    }
    to_merge = [
        c for c in candidates
        if any(key in existing_keys for key in _lookup_keys(c))
        or (c.get("content_hash") and c["content_hash"] in existing_hashes)
    ]
    candidates = [
        c for c in candidates
        if not any(key in existing_keys for key in _lookup_keys(c))
        and not (c.get("content_hash") and c["content_hash"] in existing_hashes)
    ]
    if to_merge:
        merge_keys = [key for c in to_merge for key in _lookup_keys(c)]
        merge_hashes = [str(c.get("content_hash") or "") for c in to_merge if c.get("content_hash")]
        clauses = [Job.dedupe_key.in_(merge_keys)]
        if merge_hashes:
            clauses.append(Job.content_hash.in_(merge_hashes))
        rows = (
            db.query(Job)
            .filter(Job.user_id == user.id)
            .filter(or_(*clauses))
            .all()
        )
        by_key = {row.dedupe_key: row for row in rows}
        by_hash = {row.content_hash: row for row in rows if row.content_hash}
        merged_now = started
        for candidate in to_merge:
            job = None
            for key in _lookup_keys(candidate):
                job = by_key.get(key)
                if job is not None:
                    break
            if job is None:
                job = by_hash.get(str(candidate.get("content_hash") or ""))
            if job is None:
                continue
            _merge_existing_job(job, candidate, merged_now)
        try:
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            log.warning("discovery merge of existing jobs failed: %s", exc)
        report["duplicates_merged"] = len(to_merge)

    # ---------------- score / classify ----------------
    # The batch slice gets exactly what the per-job path (``/intelligence``)
    # gives the model: this run's session, the owning user (so the AI gateway
    # resolves *their* key, budgets and ledger) and the persona context, so a
    # persona-scoped run scores against the track. ``use_ai`` is the plan gate
    # the caller applied by passing (or withholding) ``profile``.
    await _score_candidates(profile_data, candidates, use_ai=bool(profile_data),
                            db=db, user_id=user.id, persona=persona_context)

    ranked = sorted(candidates, key=lambda c: c.get("score", 0), reverse=True)[:limit * 2]
    for index, candidate in enumerate(ranked):
        if index < AI_CLASSIFY_TOP and profile_data:
            # AI verdict — a hard dependency: on outage this raises and the
            # queued run pauses (re-executed when the provider is back).
            size, confidence = await ai_company_size(candidate["company"], candidate["description"],
                                                     db=db, user_id=user.id)
        else:
            # Bulk slice (beyond the AI top-N): deterministic, source-data-first
            # labelling — provenance-labelled, never an AI substitute.
            size = deterministic_company_size({"size": candidate.get("company_size")}, candidate)
            confidence = 0.55
        candidate["company_size"] = size
        candidate["company_size_confidence"] = confidence

    # ---------------- persist ----------------
    # One commit per row, not one commit for the batch: the AI verdicts for
    # this batch have already been *spent*, so a single bad row (a uniqueness
    # race against a concurrent run is the common one) must be skipped — with
    # its failure recorded — and the rest of the batch must land. The old
    # batch commit discarded every good job because of one conflict.
    created: List[Job] = []
    insert_errors: List[Dict[str, Any]] = []
    # Clamp to the storage headroom (``None`` = unlimited): a nearly-full
    # board fills exactly its remaining slots and never overshoots jobs_max.
    batch_limit = limit if jobs_remaining is None else min(limit, jobs_remaining)
    seen_at = datetime.utcnow()
    for candidate in ranked[:batch_limit]:
        if _is_expired_candidate(candidate, seen_at):
            continue
        incoming_raw = candidate.get("raw")
        raw_payload: Dict[str, Any] = dict(incoming_raw) if isinstance(incoming_raw, dict) else {}
        job = Job(
            user_id=user.id,
            title=candidate["title"],
            company=candidate["company"],
            # SQL-side company filter identity (see Job model / migration
            # e5f6a7b8c9d0) — stored at import time, not derived per query.
            company_name_normalized=candidate.get("company_name_normalized")
            or normalize_company_name(candidate["company"]),
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
                   "freshness_relaxed": candidate.get("freshness_relaxed", False), "forms": {},
                   **({"search_discovery": candidate["extra"]["search_discovery"]}
                      if (candidate.get("extra") or {}).get("search_discovery") else {})},
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            last_verified_at=seen_at,
            expired=False,
            source_kind=str(candidate.get("source_kind") or "")[:16],
            content_hash=str(candidate.get("content_hash") or "")[:64],
            title_normalized=str(candidate.get("title_normalized") or candidate["title"]).strip().lower()[:300],
            raw_payload=dict(raw_payload),
        )
        db.add(job)
        try:
            db.commit()
        except Exception as exc:  # noqa: BLE001 - one bad row must never discard the batch
            db.rollback()
            log.warning("discovery insert failed for %s (%s): %s",
                        candidate.get("company"), candidate.get("title"), exc)
            insert_errors.append({
                "title": candidate.get("title"),
                "company": candidate.get("company"),
                "dedupe_key": candidate.get("dedupe_key"),
                "error": str(exc)[:500],
            })
            continue
        created.append(job)

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
    # v2.2.5 linkage — keep has_open_positions fresh via the shared matcher both directions.
    if created:
        try:
            from app.services.funding_radar import refresh_funding_has_open_positions
            refresh_funding_has_open_positions(db, int(user.id))
        except Exception as exc:  # noqa: BLE001
            log.warning("has_open_positions sync (discovery) failed for user %s: %s", user.id, exc)

    # ---------------- form structure for the best new jobs ----------------
    form_tasks = [detect_form_structure(job.url, job.source, use_ai=False) for job in created[:FORM_DETECT_TOP]]
    if form_tasks:
        results = await asyncio.gather(*form_tasks, return_exceptions=True)
        for job, schema in zip(created[:FORM_DETECT_TOP], results, strict=False):
            if isinstance(schema, BaseException):
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
    # Provenance for the whole run (v2.2.3). The AI top slice is gated by the
    # caller on the profile *and* the plan, so "every job on this board is a
    # keyword estimate" has two very different causes — and this block is the
    # only place an operator (or the empty-board work) can read which one
    # happened. It rides in the queue item's result JSON: no schema change, no
    # migration.
    ai_enabled = bool(profile_data)
    ai_rescore: Dict[str, Any] = {
        "enabled": ai_enabled,
        "skipped": None if ai_enabled else str(ai_rescore_skipped or AI_RESCORE_NO_PROFILE),
        # Rows, not calls: a candidate the model could not verdict (guardrail
        # rejection, no text to score) is not an AI-scored job, and a run whose
        # insert lost a uniqueness race created nothing to score.
        "scored": sum(1 for job in created if str(job.score_source or "") == "ai"),
    }
    # The returned report *is* the run report built at the top of this function
    # (v2.2.4). It used to be two dicts: ``report`` carried ``started_at``,
    # ``keywords``, ``demo_pool`` and ``freshness_relaxed`` and was then dropped
    # on the floor, while a separate ``summary`` was persisted — so nothing
    # downstream could say *when* a run happened, that the demo pool had
    # contributed to it, or that the freshness window had been backfilled.
    report["scanned"] = len(unique)
    report["fresh"] = len(fresh)
    report["inserted"] = len(created)
    report["elapsed_seconds"] = round(elapsed, 2)
    report["ai_rescore"] = ai_rescore
    # Why the board gained nothing (v2.2.4). Classified here, once, from the
    # per-source report above — the endpoint only reads it back. ``summary`` is
    # always present (it is the accounting behind the verdict, useful on a
    # successful run too); ``why_empty`` is present *only* when this run added
    # zero jobs, so an absent key means "this run found jobs".
    report["summary"] = _run_summary(source_report, freshness_hours=freshness_hours, fresh_count=len(fresh))
    # Per-row insert failures (v2.2.9): the batch no longer rolls back as a
    # whole, so a partially-landed run reports exactly which row(s) were
    # skipped and why. Absent key = nothing failed.
    if insert_errors:
        report["insert_errors"] = insert_errors
    why_empty = _classify_empty_run(source_report) if not created else None
    if why_empty:
        report["why_empty"] = why_empty
    report["jobs"] = [
        {"id": job.id, "title": job.title, "company": job.company, "source": job.source,
         "score": job.score, "location": job.location, "url": job.url}
        for job in created
    ]
    log.info(
        "discovery: %s scanned, %s inserted in %.1fs (ai_rescore enabled=%s skipped=%s scored=%s)%s%s",
        report["scanned"], report["inserted"], elapsed,
        ai_rescore["enabled"], ai_rescore["skipped"] or "-", ai_rescore["scored"],
        f" insert_errors={len(insert_errors)}" if insert_errors else "",
        f" why_empty={why_empty}" if why_empty else "",
        extra={"extra_fields": {"user_id": user.id, **{k: report[k] for k in ("scanned", "fresh", "inserted")},
                                "ai_rescore": ai_rescore,
                                **({"why_empty": why_empty} if why_empty else {})}},
    )
    return report


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
    from app.services.flags import is_enabled

    return {
        "sources": [s for s in sources if s in source_registry.ADAPTERS],
        "board_tokens": board_tokens,
        # Live fetching needs BOTH the user's own source setting and the
        # workspace-wide flag from the owner console — either off stops it.
        "live_enabled": bool(
            get_setting(db, user_id, "scraping", "live_enabled", settings.live_scraping_enabled)
        ) and is_enabled(db, "discovery.live_sources"),
    }
