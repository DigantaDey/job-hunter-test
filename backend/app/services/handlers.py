"""
Pipeline handlers.

One function per pipeline; each takes a claimed ``PipelineJob`` and a session and
returns a JSON-serialisable result. Keeping them here (rather than inline in the
API layer) means the API and the standalone worker execute *exactly* the same
code path.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import Email, FundingCompany, Job, PipelineJob, User
from app.services import funding_radar
from app.services.ai_client import is_ai_error
from app.services.ai_guardrails import describe_ai_error
from app.services.apply_flow import execute_application, prepare_application
from app.services.auto_scheduler import FUNDING_MATCH_NOTIFICATION_KIND, HIGH_MATCH_NOTIFICATION_KIND
from app.services.discovery import discover_for_user
from app.services.outreach import draft_email, send_email
from app.services.user_settings import get_setting

log = get_logger("app.handlers")


def _user(db: Session, item: PipelineJob) -> Optional[User]:
    return db.query(User).filter(User.id == item.user_id).first()


def _is_auto(item: PipelineJob) -> bool:
    """Was this item enqueued by the auto-mode scheduler?

    The marker is the *only* difference between auto work and clicked work — the
    pipeline, the handler, the entitlements and the failure semantics are all the
    same. It drives two things: the in-app notifications auto mode owes the user
    (nobody reads a queue to find out a match arrived) and the quota charge the
    worker makes on completion.
    """
    return str(dict(item.payload or {}).get("trigger") or "") == "auto"


def _notify_run(db: Session, item: PipelineJob, kind: str, title: str, body: str,
                link: str, meta: Optional[Dict[str, Any]] = None) -> bool:
    """One in-app notification per (run, kind) — never email, never SMS.

    Dedupe lives in the item payload rather than in a query: a retried or
    re-run item is the *same* run, so it must not notify twice, and marking the
    row is the one record that survives a worker restart.
    """
    payload = dict(item.payload or {})
    sent = [str(k) for k in (payload.get("auto_notified") or [])]
    if kind in sent:
        return False
    from app.api.routers.notifications import create_notification

    create_notification(db, int(item.user_id), kind, title[:200], body=body[:2000], link=link,
                        meta={"pipeline_job_id": int(item.id), **(meta or {})})
    item.payload = {**payload, "auto_notified": sent + [kind]}  # type: ignore[assignment]
    db.commit()
    return True


def _notify_auto_discovery(db: Session, item: PipelineJob, user: User,
                           result: Dict[str, Any]) -> None:
    """Surface the run's high-match finds: one notification, top three, to /jobs."""
    threshold = float(get_setting(db, int(user.id), "general", "generate_min_score", 65) or 0)
    surfaced = [job for job in (result.get("jobs") or [])
                if isinstance(job, dict) and float(job.get("score") or 0) >= threshold][:3]
    if not surfaced:
        return
    ids = [int(job["id"]) for job in surfaced if job.get("id") is not None]
    rows = db.query(Job).filter(Job.id.in_(ids), Job.user_id == user.id).all() if ids else []
    # Honest provenance: a queued discovery run scores with the deterministic
    # keyword estimate unless the AI verdict was computed, so the notification
    # must not call an estimate a match the model made. (The gap itself — the
    # discovery handler never passes the profile the AI scoring needs — is
    # recorded in the 2.2.0 PR as out-of-scope.)
    provenance = {str(row.score_source or "preliminary") for row in rows} or {"preliminary"}
    ai_scored = provenance == {"ai"}
    lines = [f"• {(job.get('company') or '?')} — {job.get('title') or 'Untitled'} "
             f"(score {float(job.get('score') or 0):.0f})" for job in surfaced]
    if not ai_scored:
        lines.append("Scored by keyword overlap — open a job for the AI verdict.")
    title = (f"{len(surfaced)} new high-match job{'s' if len(surfaced) != 1 else ''} "
             f"at or above {threshold:.0f}")
    _notify_run(db, item, HIGH_MATCH_NOTIFICATION_KIND, title, "\n".join(lines), "/jobs",
                meta={"threshold": threshold, "job_ids": ids, "score_source": sorted(provenance)})


def _notify_auto_funding(db: Session, item: PipelineJob, user: User, started_at: datetime) -> None:
    """One notification when the radar actually gained verified companies."""
    fresh = (
        db.query(func.count(FundingCompany.id))
        .filter(FundingCompany.user_id == user.id, FundingCompany.verified.is_(True),
                FundingCompany.discovered_at >= started_at)
        .scalar()
    )
    count = int(fresh or 0)
    if count < 1:
        return
    names = [
        row.name for row in (
            db.query(FundingCompany)
            .filter(FundingCompany.user_id == user.id, FundingCompany.verified.is_(True),
                    FundingCompany.discovered_at >= started_at)
            .order_by(FundingCompany.raised_at.desc())
            .limit(3)
            .all()
        )
    ]
    _notify_run(db, item, FUNDING_MATCH_NOTIFICATION_KIND,
                f"{count} new verified {'company' if count == 1 else 'companies'} on your radar",
                "\n".join(f"• {name}" for name in names), "/funding",
                meta={"added": count})


async def handle_discovery(db: Session, item: PipelineJob) -> Dict[str, Any]:
    user = _user(db, item)
    if not user:
        raise RuntimeError("user_missing")
    payload = item.payload or {}
    from app.services import persona as persona_service
    from app.services.discovery import discovery_sources_config

    config = discovery_sources_config(db, user.id)
    persona = persona_service.get_persona(db, user.id, payload.get("persona_id"))
    persona_context = None
    if persona:
        memory = persona_service.memory_summary(persona)
        persona_context = {"name": persona.name, "target_role": persona.target_role,
                           "observed_skills": list((memory.get("observed_skills") or {}).keys())[:12]}
    result = await discover_for_user(
        db,
        user,
        keywords=payload.get("keywords") or [],
        freshness_hours=int(payload.get("freshness_hours") or settings.default_freshness_hours),
        limit=int(payload.get("limit") or 30),
        live_enabled=bool(payload.get("live_enabled", config["live_enabled"])),
        source_ids=payload.get("sources") or config["sources"],
        board_tokens=payload.get("board_tokens") or config["board_tokens"],
        persona_id=payload.get("persona_id") or (persona.id if persona else None),
        persona_context=persona_context,
    )
    # One signal per run is enough to mark the track as active in memory.
    # (v2.2 fix: this read ``result["job_ids"]`` — a key ``discover_for_user``
    # has never returned; the summary carries ``jobs``. So a queued discovery
    # run, the only kind there is, never fed the persona memory the interactive
    # paths do, and the track never learned.)
    if persona and (result.get("jobs") or []):
        persona_service.record_signal(db, user.id, persona.id, "job_scored", {"note": "discovery run"})
    if _is_auto(item):
        _notify_auto_discovery(db, item, user, result)
    return result


async def handle_application(db: Session, item: PipelineJob) -> Dict[str, Any]:
    user = _user(db, item)
    job = db.query(Job).filter(Job.id == item.job_id, Job.user_id == item.user_id).first()
    if not user or not job:
        raise RuntimeError("job_or_user_missing")
    payload = item.payload or {}

    prepared = await prepare_application(
        db, user, job,
        resume_choice=payload.get("resume_choice", "auto"),
        resume_id=payload.get("resume_id"),
        answers=payload.get("answers") or {},
    )
    if prepared.get("status") == "needs_input":
        return {"status": "needs_input", **prepared}

    result = await execute_application(db, user, job, answers=payload.get("answers") or {})
    return {**prepared, **result}


async def handle_email(db: Session, item: PipelineJob) -> Dict[str, Any]:
    user = _user(db, item)
    if not user:
        raise RuntimeError("user_missing")
    payload = item.payload or {}

    if payload.get("send"):
        email_row = db.query(Email).filter(Email.id == payload["email_id"], Email.user_id == user.id).first()
        if not email_row:
            raise RuntimeError("email_missing")
        return send_email(db, user, email_row, otp=payload.get("otp"), allow_real=payload.get("allow_real", True))

    if payload.get("company"):
        drafted = await draft_email(
            db, user,
            company=payload["company"],
            job=db.query(Job).filter(Job.id == payload["job_id"]).first() if payload.get("job_id") else None,
            founder=bool(payload.get("founder")),
            department=payload.get("department", "engineering"),
            extra_context=payload.get("context", ""),
        )
        email_row = drafted["email"]
        if bool(get_setting(db, user.id, "general", "auto_approve_email", False)):
            email_row.status = "queued"
            db.commit()
            return send_email(db, user, email_row, allow_real=True)
        return {"status": "pending_approval", "email_id": email_row.id, "to": email_row.to_email}

    return {"status": "noop"}


async def handle_funding(db: Session, item: PipelineJob) -> Dict[str, Any]:
    """Run a queued funding scan.

    AI is a hard dependency (v2.1): a transient outage raises and the worker
    *pauses* the item (drained when the provider is back), a blocked outcome
    dead-letters it. Either way the reason is persisted to the user's
    ``funding.last_report`` first, so the Funding page can say why the radar did
    not refresh instead of showing a silently stale list.
    """
    user = _user(db, item)
    if not user:
        raise RuntimeError("user_missing")
    payload = item.payload or {}
    # ``discovered_at`` is first-seen and never rewritten, so a timestamp taken
    # before the scan is exactly "what this run added to the radar".
    started_at = datetime.utcnow()

    context = payload.get("context")
    if not context:
        from app.api.deps import search_context

        context = await search_context(db, user.id, extra_context=payload.get("user_context", ""), force_refresh=True)

    window = int(payload.get("window_days") or get_setting(db, user.id, "funding", "freshness_days",
                                                          settings.funding_freshness_days))
    provider = payload.get("provider") or get_setting(db, user.id, "funding", "provider", settings.funding_provider)
    try:
        companies, report = await funding_radar.scan_funded_companies(
            context, stages=payload.get("stages"), window_days=window,
            limit=int(payload.get("limit") or settings.funding_limit), provider=provider,
            db=db, user_id=int(user.id),
        )
    except Exception as exc:
        if is_ai_error(exc):
            outage = describe_ai_error(exc, workflow="funding_scan")
            funding_radar.store_scan_report(
                db, int(user.id), funding_radar.outage_report(outage),
                scan_status=funding_radar.SCAN_PAUSED if outage.pausable else funding_radar.SCAN_BLOCKED,
                reason=outage.reason,
            )
        raise
    # A scan that fetched nothing must not prune the radar (see sync_funding_db).
    sync = (funding_radar.sync_funding_db(db, user.id, companies, window)
            if report.get("scan_status") == funding_radar.SCAN_OK else {})
    funding_radar.store_scan_report(db, int(user.id), report)
    if _is_auto(item):
        # Auto runs are held to the same per-action budget the interactive radar
        # charges (``POST /funding/companies`` counts ``len(companies)`` per scan).
        # Checking it before enqueueing is only meaningful if the run also pays
        # for what it took, so a scheduled scan can never be a way around the cap.
        if report.get("scan_status") == funding_radar.SCAN_OK and companies:
            from app.core.entitlements import increment_usage

            try:
                increment_usage(db, int(user.id), "funding_companies_per_month", len(companies))
            except Exception as exc:  # noqa: BLE001 - never lose the scan over the ledger
                log.warning("could not charge funding quota for item %s: %s", item.id, exc)
        _notify_auto_funding(db, item, user, started_at)
    return {"scanned": len(companies), "scan_status": report.get("scan_status"),
            "reason": report.get("reason"), "provider_report": report, **sync}


async def handle_ai(db: Session, item: PipelineJob) -> Dict[str, Any]:
    """Generic AI work: currently resume tagging (the queue keeps it observable)."""
    user = _user(db, item)
    payload = item.payload or {}
    if payload.get("task") == "tag_resume":
        from app.models.models import Resume
        from app.services.resume_generator import tag_resume

        resume = db.query(Resume).filter(Resume.id == payload.get("resume_id"), Resume.user_id == item.user_id).first()
        if not resume:
            raise RuntimeError("resume_missing")
        tags = await tag_resume(resume.profile_snapshot or {})
        resume.tags = tags
        db.commit()
        return {"resume_id": resume.id, "tags": tags}
    return {"status": "noop", "user": user.id if user else None}


HANDLERS = {
    "discovery": handle_discovery,
    "application": handle_application,
    "email": handle_email,
    "funding": handle_funding,
    "ai": handle_ai,
}
