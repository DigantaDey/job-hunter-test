"""
Pipeline handlers.

One function per pipeline; each takes a claimed ``PipelineJob`` and a session and
returns a JSON-serialisable result. Keeping them here (rather than inline in the
API layer) means the API and the standalone worker execute *exactly* the same
code path.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.entitlements import can
from app.core.logging import get_logger
from app.models.models import Email, FundingCompany, Job, PipelineJob, User
from app.services import funding_radar
from app.services.ai_client import is_ai_error
from app.services.ai_guardrails import describe_ai_error
from app.services.apply_flow import execute_application, latest_profile, prepare_application
from app.services.auto_scheduler import FUNDING_MATCH_NOTIFICATION_KIND, HIGH_MATCH_NOTIFICATION_KIND
from app.services.discovery import AI_RESCORE_NO_PROFILE, AI_RESCORE_PLAN_FREE, discover_for_user
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
    # Honest provenance. Since v2.2.3 a queued run *can* carry the model's
    # verdict (Pro + a profile on file → the top AI_RESCORE_TOP rows), so the
    # caveat is dropped only when every surfaced row really is an AI verdict.
    # A mixed run keeps it — the top slice is 12 rows while the notification
    # surfaces the three highest scorers, which need not all be in it — and so
    # does a free-tier or profile-less run, where every row is a keyword
    # estimate (see ``ai_rescore.skipped`` in the run's report for which).
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


def _payload_sources(payload: Any, config: Dict[str, Any]) -> Optional[List[str]]:
    """Which sources this queued run fetches: ``None`` = defaults, ``[]`` = none.

    The queue payload's ``sources`` key is optional, and *absent* is not the same
    claim as *empty*:

    * absent or ``None`` — the enqueuer had no opinion (older rows, or a caller
      that wants the user's configuration) → use ``discovery_sources_config``,
      which itself resolves ``None`` to the enabled-by-default adapters;
    * an explicit list, including ``[]`` — the enqueuer's choice, honoured
      verbatim. ``[]`` means fetch nothing, which is how a run can honestly
      report ``why_empty="no_sources_configured"``.

    ``config["sources"]`` may itself be ``[]`` — the settings layer has always
    distinguished unset from empty — and that is passed through untouched. A
    non-list value (a hand-written payload, a corrupted row) falls back to the
    configuration rather than to "everything", so a bad row cannot silently widen
    a run.
    """
    selected = payload.get("sources") if isinstance(payload, dict) else None
    if selected is None:
        return list(config["sources"])
    if isinstance(selected, str):
        # The settings layer accepts a comma-separated string for the same key.
        return [token.strip() for token in selected.split(",") if token.strip()]
    if isinstance(selected, (list, tuple, set)):
        return [str(source_id) for source_id in selected]
    return list(config["sources"])


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

    # ---------------- the AI top slice (v2.2.3) ----------------
    # ``discover_for_user`` gives the top ``AI_RESCORE_TOP`` candidates the
    # guardrailed AI verdict *if and only if* it is handed a profile — and this
    # handler never handed it one, so every queued discovery row was a keyword
    # estimate and the AI verdict existed only per job
    # (``GET /api/jobs/{job_id}/intelligence``, Pro). Two gates, both the
    # product's own:
    #
    # 1. the user's latest ``Profile`` row — the identical query
    #    ``/intelligence`` runs (``apply_flow.latest_profile``). No profile (or
    #    an empty one) means there is nothing to score against, and the model
    #    must not be asked to invent a match.
    # 2. ``can_use_advanced_matching`` — the entitlement ``/intelligence``
    #    enforces per job. It is a *permission* check, not a quota: no
    #    ``enforce()``, no new entitlement, no counter, nothing consumed.
    #
    # Withholding the profile (rather than passing it with ``use_ai=False``) is
    # what makes a free-tier run cost exactly **zero** AI calls: the same
    # profile also switches on the AI company-size verdict for the top
    # ``AI_CLASSIFY_TOP`` rows. A free user's board is not a back door around
    # the plan gate. The reason is reported in the run's ``ai_rescore`` block,
    # because "your board is all estimates" has two different fixes.
    profile_row = latest_profile(db, int(user.id))
    profile_data: Dict[str, Any] = dict((profile_row.data if profile_row else {}) or {})
    ai_rescore_skipped: Optional[str] = None
    if not profile_data:
        ai_rescore_skipped = AI_RESCORE_NO_PROFILE
    elif not can(db, int(user.id), "can_use_advanced_matching"):
        ai_rescore_skipped = AI_RESCORE_PLAN_FREE
        profile_data = {}

    result = await discover_for_user(
        db,
        user,
        keywords=payload.get("keywords") or [],
        freshness_hours=int(payload.get("freshness_hours") or settings.default_freshness_hours),
        limit=int(payload.get("limit") or 30),
        live_enabled=bool(payload.get("live_enabled", config["live_enabled"])),
        # ``None``/absent means "use the user's configuration"; an explicit ``[]``
        # means "fetch nothing". The old ``payload.get("sources") or
        # config["sources"]`` collapsed the two, and ``fetch_all`` collapsed them
        # again — so a user who had deliberately saved zero sources was still
        # fetched from every available adapter, and an empty board could never be
        # reported as ``no_sources_configured``.
        source_ids=_payload_sources(payload, config),
        board_tokens=payload.get("board_tokens") or config["board_tokens"],
        profile=profile_data or None,
        ai_rescore_skipped=ai_rescore_skipped,
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
    try:
        funding_radar.persist_funding_scan(db, int(user.id), report, companies if report.get("scan_status") == funding_radar.SCAN_OK else [])
    except Exception as exc:  # noqa: BLE001
        log.warning("funding history persist (queued) failed for user %s: %s", user.id, exc)
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
