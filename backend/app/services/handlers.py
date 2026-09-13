"""
Pipeline handlers.

One function per pipeline; each takes a claimed ``PipelineJob`` and a session and
returns a JSON-serialisable result. Keeping them here (rather than inline in the
API layer) means the API and the standalone worker execute *exactly* the same
code path.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import Email, Job, PipelineJob, User
from app.services import funding_radar
from app.services.ai_client import is_ai_error
from app.services.ai_guardrails import describe_ai_error
from app.services.apply_flow import execute_application, prepare_application
from app.services.discovery import discover_for_user
from app.services.outreach import draft_email, send_email
from app.services.user_settings import get_setting

log = get_logger("app.handlers")


def _user(db: Session, item: PipelineJob) -> Optional[User]:
    return db.query(User).filter(User.id == item.user_id).first()


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
    if persona and (result.get("job_ids") or []):
        persona_service.record_signal(db, user.id, persona.id, "job_scored", {"note": "discovery run"})
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
