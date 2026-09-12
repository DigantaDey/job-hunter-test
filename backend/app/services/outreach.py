"""
Outreach service: drafting, compliance gating, sending and engagement tracking.

Compliance is enforced in code, not in the UI:

* the suppression list (opt-outs) is checked before *every* send;
* the ``terms``, ``outreach`` and ``data_processing`` consents must be recorded;
* real sends require a postal address, an unsubscribe URL and a configured
  mailbox (CAN-SPAM/GDPR), plus an explicit ``allow_real`` confirmation;
* a per-user daily cap applies, and every state change is written to
  ``email_events`` for the audit trail;
* dry-run is the default — an unconfigured install cannot send anything, and it
  says so instead of pretending.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc
from app.core.security import new_tracking_token
from app.models.models import Email, EmailOptOut, Job, User
from app.services.contact_discovery import find_decision_maker
from app.services.email_pipeline import generate_cold_email, send_via_smtp
from app.services.events import record_email_event
from app.services.user_settings import get_setting

log = get_logger("app.outreach")

DEFAULT_SENDER_NAME = "Hiring Team"

#: Mailbox local-parts that mean "a department", not a person. Outreach to these
#: converts badly, so the UI must say so instead of presenting them as a find.
ROLE_MAILBOXES = {"hiring", "jobs", "careers", "recruiting", "talent", "people", "hr",
                  "info", "hello", "contact", "office", "team", "engineering", "admin"}


def is_role_mailbox(address: str) -> bool:
    local = (address or "").split("@", 1)[0].strip().lower()
    return bool(local) and (local in ROLE_MAILBOXES or local.split(".")[0] in ROLE_MAILBOXES
                            or local.split("+")[0] in ROLE_MAILBOXES)


# --------------------------------------------------------------------------- #
# Suppression list
# --------------------------------------------------------------------------- #
def is_suppressed(db: Session, user_id: int, email: str) -> bool:
    address = (email or "").strip().lower()
    if not address:
        return True
    exists = (
        db.query(EmailOptOut.id)
        .filter(EmailOptOut.user_id == user_id, func.lower(EmailOptOut.email) == address)
        .first()
    )
    return exists is not None


def suppress(db: Session, user_id: int, email: str, reason: str = "manual") -> EmailOptOut:
    """Add an address to the suppression list (idempotent)."""
    address = (email or "").strip().lower()
    existing = (
        db.query(EmailOptOut)
        .filter(EmailOptOut.user_id == user_id, func.lower(EmailOptOut.email) == address)
        .first()
    )
    if existing:
        return existing
    row = EmailOptOut(user_id=user_id, email=address, reason=reason[:200])
    db.add(row)
    db.commit()
    db.refresh(row)
    inc("jobhunter_suppressions_total", reason=reason[:40])
    return row


def sent_today(db: Session, user_id: int) -> int:
    start = datetime.combine(date.today(), datetime.min.time())
    return int(
        db.query(func.count(Email.id))
        .filter(Email.user_id == user_id, Email.status.in_(("sent", "opened")), Email.sent_at >= start)
        .scalar()
        or 0
    )


# --------------------------------------------------------------------------- #
# Tracking URLs
# --------------------------------------------------------------------------- #
def tracking_urls(email_row: Email) -> Dict[str, str]:
    """Public URLs embedded in the message for this email row."""
    base = (settings.public_base_url or "").rstrip("/")
    return {
        "open": f"{base}/api/track/open/{email_row.tracking_token}.png" if email_row.tracking_token else "",
        "unsubscribe": f"{base}/api/track/unsubscribe/{email_row.unsubscribe_token}"
        if email_row.unsubscribe_token else "",
    }


# --------------------------------------------------------------------------- #
# Drafting
# --------------------------------------------------------------------------- #
async def draft_email(
    db: Session,
    user: User,
    company: Any = "",
    job: Optional[Job] = None,
    founder: bool = False,
    department: str = "engineering",
    recipient: Optional[Dict[str, Any]] = None,
    extra_context: str = "",
    persona_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Create a draft in the approval bucket (never sends).

    ``recipient`` may be supplied explicitly (manual address or a contact picked
    in the UI); otherwise discovery runs and the result — including its honest
    ``confidence``/``verified`` flags — is stored on the row. The job's own
    description is passed to the writer so the message is about *that* posting,
    and the posting's context is stored on the row so the approval bucket can
    always show which job a draft was written for.
    """
    company_name = company if isinstance(company, str) else getattr(company, "name", "") or ""
    profile = get_setting(db, user.id, "application", "profile_snapshot", {}) or {}
    if not profile:
        from app.models.models import Profile  # local import avoids a cycle at module load

        profile_row = db.query(Profile).filter(Profile.user_id == user.id).first()
        profile = (profile_row.data if profile_row else {}) or {}
    if not profile:
        raise ValueError("profile_missing")
    if not str(profile.get("name") or "").strip():
        # A message signed "Unknown" is unreadable to the recipient, so refuse
        # rather than drafting something the user would have to rewrite anyway.
        raise ValueError("profile_name_missing")

    contact = dict(recipient or {})
    if not contact.get("email"):
        found = await find_decision_maker(company_name or (job.company if job else ""),
                                          department=department, ai_config=None)
        contact = {**found, **{k: v for k, v in contact.items() if v}}
    address = str(contact.get("email") or "").strip().lower()
    if not address or "@" not in address:
        raise ValueError("no_recipient")
    if is_suppressed(db, user.id, address):
        raise ValueError("suppressed")

    drafted = await generate_cold_email(
        profile,
        company_name or (job.company if job else "") or "your team",
        job_title=(job.title if job else "") or "",
        founder=founder,
        recipient_name=str(contact.get("name") or ""),
        extra_context=extra_context,
        jd=(job.description if job else "") or "",
        job_url=(job.url if job else "") or "",
        db=db,
        user_id=user.id,
    )

    row = Email(
        user_id=user.id,
        to_email=address,
        to_name=str(contact.get("name") or "")[:200],
        subject=str(drafted.get("subject") or "")[:500],
        body=str(drafted.get("body") or ""),
        status="pending_approval",
        job_id=job.id if job else None,
        job_title=(job.title if job else "")[:300],
        job_url=(job.url if job else "")[:500],
        jd_excerpt=(job.description if job else "")[:2000],
        persona_id=persona_id or (job.persona_id if job else None),
        company=(company_name or (job.company if job else "") or "")[:200],
        recipient_type="founder" if founder else "hiring_manager",
        source=str(contact.get("source") or "manual")[:40],
        confidence=float(contact.get("confidence") or 0.0),
        verified=bool(contact.get("verified")) or bool((contact.get("verification") or {}).get("mx_ok")),
        ai_used=bool(drafted.get("ai_used")),
        guardrail_report=drafted.get("fact_guard", {}) or {},
        tracking_token=new_tracking_token(),
        unsubscribe_token=new_tracking_token(),
        dry_run=not settings.email_real_sending,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    record_email_event(db, user_id=user.id, email_id=row.id, kind="queued",
                       detail="draft created",
                       meta={"contact": {k: v for k, v in contact.items() if k != "email"},
                             "ai_used": bool(drafted.get("ai_used")),
                             "fact_guard": drafted.get("fact_guard", {}),
                             "job_id": row.job_id, "job_title": row.job_title})
    inc("jobhunter_emails_drafted_total", audience=row.recipient_type)
    from app.services import persona as persona_service

    persona_service.record_signal(db, user.id, row.persona_id, "email_drafted",
                                  {"title": row.job_title, "company": row.company})
    return {"email": row, "decision": contact, "contact": contact,
            "ai": {"ai_used": bool(drafted.get("ai_used")),
                   "fact_guard": drafted.get("fact_guard", {})}}


# --------------------------------------------------------------------------- #
# Compliance gate
# --------------------------------------------------------------------------- #
def _sender_config(db: Session, user_id: int) -> Dict[str, Any]:
    return {
        "host": get_setting(db, user_id, "email", "host", settings.email_smtp_host) or "",
        "port": int(get_setting(db, user_id, "email", "port", settings.email_smtp_port) or 587),
        "username": get_setting(db, user_id, "email", "username", settings.email_smtp_username) or "",
        "password": get_setting(db, user_id, "email", "password", settings.email_smtp_password) or "",
        "use_tls": bool(get_setting(db, user_id, "email", "use_tls", settings.email_smtp_use_tls)),
        "from_name": (get_setting(db, user_id, "email", "from_name", settings.email_from_name)
                      or get_setting(db, user_id, "general", "display_name", "")
                      or DEFAULT_SENDER_NAME),
        "from_address": get_setting(db, user_id, "email", "username", settings.email_smtp_username) or "",
        "postal_address": get_setting(db, user_id, "email", "postal_address",
                                      settings.email_postal_address) or "",
    }


def _unsubscribe_base(db: Session, user_id: int) -> str:
    configured = get_setting(db, user_id, "email", "unsubscribe_base_url", settings.email_unsubscribe_base_url)
    return (configured or settings.public_base_url or "").rstrip("/")


def compliance_report(db: Session, user: User, email_row: Email) -> Dict[str, Any]:
    """Evaluate every gate before a real send; used by the API and the worker."""
    consents = user.consents or {}
    sender = _sender_config(db, user.id)
    daily_limit = int(get_setting(db, user.id, "email", "daily_limit", settings.email_daily_limit) or 0)
    used = sent_today(db, user.id)
    address = (email_row.to_email or "").strip()
    base = _unsubscribe_base(db, user.id)

    checks = {
        "recipient_valid": "@" in address and "." in address.split("@")[-1],
        "recipient_suppressed": is_suppressed(db, user.id, address),
        "terms_accepted": bool(consents.get("terms_accepted_at")),
        "outreach_consent": bool(consents.get("outreach_accepted_at")),
        "data_processing": bool(consents.get("data_processing_accepted_at")),
        "smtp_configured": bool(sender["host"] and sender["username"]),
        "postal_address": bool(sender["postal_address"]),
        "unsubscribe_base": bool(base),
        "unsubscribe_reachable": bool(base) and "localhost" not in base and "127.0.0.1" not in base,
        "daily_limit": daily_limit,
        "sent_today": used,
        "under_daily_limit": used < daily_limit if daily_limit else True,
        "already_sent": email_row.status in ("sent", "opened"),
        "sending_enabled": settings.email_sending_enabled,
        "dry_run": not settings.email_real_sending,
        "email_body": bool((email_row.body or "").strip()),
    }

    blockers = []
    if not checks["recipient_valid"]:
        blockers.append("recipient_invalid")
    if checks["recipient_suppressed"]:
        blockers.append("recipient_suppressed")
    if not checks["terms_accepted"]:
        blockers.append("terms_not_accepted")
    if not checks["outreach_consent"]:
        blockers.append("outreach_consent_missing")
    if not checks["data_processing"]:
        blockers.append("data_processing_consent_missing")
    if not checks["smtp_configured"]:
        blockers.append("smtp_not_configured")
    if not checks["postal_address"]:
        blockers.append("postal_address_missing")
    if not checks["unsubscribe_base"]:
        blockers.append("unsubscribe_not_configured")
    if not checks["under_daily_limit"]:
        blockers.append("daily_limit_reached")
    if checks["already_sent"]:
        blockers.append("already_sent")
    if not checks["email_body"]:
        blockers.append("empty_body")

    warnings = []
    if not checks["unsubscribe_reachable"] and not checks["dry_run"]:
        warnings.append(
            "PUBLIC_BASE_URL points at localhost — recipients cannot reach the unsubscribe link."
        )
    if email_row.confidence and email_row.confidence < 0.5:
        warnings.append(
            f"This address came from a low-confidence guess ({email_row.source}, "
            f"{email_row.confidence:.0%}) — verify it before a real send."
        )
    if (email_row.source or "") == "manual":
        warnings.append("Address was entered manually — double-check the domain.")
    if not email_row.verified:
        warnings.append(
            f"This address is unverified ({email_row.source or 'unknown source'}) — "
            "confirm it is a real mailbox before sending."
        )
    if is_role_mailbox(email_row.to_email):
        warnings.append(
            "This is a generic role mailbox (e.g. hiring@/info@), not a named person — "
            "reply rates are much lower. Use Contacts to find a real decision maker."
        )
    if not email_row.job_title:
        warnings.append("This draft is not attached to a job posting — the recipient will not know which role you mean.")

    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "mode": "send" if settings.email_real_sending else "dry_run",
        "tracking": tracking_urls(email_row),
    }


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def send_email(
    db: Session,
    user: User,
    email_row: Email,
    otp: Optional[str] = None,
    allow_real: bool = True,
) -> Dict[str, Any]:
    """
    Attempt delivery. Dry-run installs return ``sent=False, dry_run=True`` — the
    message is stored with the reason, and nothing is transmitted.

    ``allow_real=False`` means "evaluate only" even on a fully configured
    install (the UI uses it for the preview button).
    """
    report = compliance_report(db, user, email_row)
    if not report["ok"]:
        email_row.status = "suppressed" if "recipient_suppressed" in report["blockers"] else email_row.status
        email_row.error = ",".join(report["blockers"])[:2000]
        db.commit()
        record_email_event(db, user_id=user.id, email_id=email_row.id, kind="blocked",
                           detail=email_row.error, meta={"blockers": report["blockers"]})
        inc("jobhunter_emails_blocked_total")
        return {"sent": False, "blocked": True, "dry_run": False, "needs_otp": False,
                "compliance": report, "status": email_row.status,
                "error": report["blockers"][0] if report["blockers"] else "blocked"}

    real = bool(settings.email_real_sending and allow_real)
    if not real:
        email_row.status = "dry_run"
        email_row.dry_run = True
        email_row.error = ("Dry run — nothing was transmitted. "
                           "Enable sending in Settings → Email when you are ready.")
        db.commit()
        record_email_event(db, user_id=user.id, email_id=email_row.id, kind="dry_run",
                           detail=email_row.error, meta={"otp_supplied": bool(otp)})
        inc("jobhunter_emails_dry_run_total")
        return {"sent": False, "blocked": False, "dry_run": True, "needs_otp": False,
                "compliance": report, "status": email_row.status, "error": None,
                "message": email_row.error}

    sender = _sender_config(db, user.id)
    sender["unsubscribe_url"] = f"{_unsubscribe_base(db, user.id)}/api/track/unsubscribe/{email_row.unsubscribe_token}"
    sender["tracking_pixel_url"] = tracking_urls(email_row)["open"] if settings.email_tracking_enabled else ""

    email_row.status = "sending"
    email_row.dry_run = False
    db.commit()
    record_email_event(db, user_id=user.id, email_id=email_row.id, kind="queued",
                       detail="transmitting", meta={"host": sender["host"]})

    outcome = send_via_smtp(
        email_row.to_email,
        email_row.subject,
        email_row.body,
        sender,
        otp=otp,
        from_email=sender["from_address"] or None,
    )

    if outcome.get("success"):
        email_row.status = "sent"
        email_row.sent_at = datetime.utcnow()
        email_row.error = ""
        db.commit()
        record_email_event(db, user_id=user.id, email_id=email_row.id, kind="sent",
                           detail="delivered to the SMTP relay",
                           meta={"message_id": outcome.get("message_id", "")})
        inc("jobhunter_emails_sent_total")
        return {"sent": True, "blocked": False, "dry_run": False, "needs_otp": False,
                "compliance": report, "status": email_row.status, "error": None}

    needs_otp = bool(outcome.get("needs_otp"))
    email_row.status = "needs_otp" if needs_otp else "failed"
    email_row.error = str(outcome.get("error") or "send failed")[:2000]
    db.commit()
    record_email_event(db, user_id=user.id, email_id=email_row.id,
                       kind="needs_otp" if needs_otp else "failed", detail=email_row.error)
    inc("jobhunter_emails_needs_otp_total" if needs_otp else "jobhunter_emails_failed_total")
    return {"sent": False, "blocked": False, "dry_run": False, "needs_otp": needs_otp,
            "compliance": report, "status": email_row.status, "error": email_row.error}


# --------------------------------------------------------------------------- #
# Engagement tracking
# --------------------------------------------------------------------------- #
def record_open(db: Session, token: str) -> bool:
    """Record an open for the tracking pixel (mail clients may prefetch images)."""
    if not token:
        return False
    row = db.query(Email).filter(Email.tracking_token == token).first()
    if not row:
        return False
    row.opens = int(row.opens or 0) + 1
    row.last_opened_at = datetime.utcnow()
    if row.status == "sent":
        row.status = "opened"
    db.commit()
    record_email_event(db, user_id=row.user_id, email_id=row.id, kind="open",
                       detail=f"open #{row.opens}", meta={"user_agent_proxied": True})
    inc("jobhunter_emails_opened_total")
    return True


def handle_unsubscribe(db: Session, token: str) -> Optional[EmailOptOut]:
    """Honour an unsubscribe link: suppress the recipient immediately."""
    if not token:
        return None
    row = db.query(Email).filter(Email.unsubscribe_token == token).first()
    if not row:
        return None
    opt_out = suppress(db, row.user_id, row.to_email, reason="unsubscribe_link")
    if row.status not in ("sent", "opened", "dry_run"):
        row.status = "suppressed"
    row.error = "recipient unsubscribed"
    db.commit()
    record_email_event(db, user_id=row.user_id, email_id=row.id, kind="unsubscribe",
                       detail="one-click unsubscribe honoured")
    inc("jobhunter_emails_unsubscribed_total")
    return opt_out


def record_webhook_event(db: Session, provider_token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Handle provider webhooks (bounce/complaint/open) — suppresses on hard bounce."""
    event = str(payload.get("event") or payload.get("type") or "").strip().lower()
    address = str(payload.get("email") or payload.get("recipient") or "").strip().lower()
    token = str(payload.get("token") or payload.get("tracking_token") or "").strip()
    row: Optional[Email] = None
    if token:
        row = (db.query(Email)
               .filter((Email.tracking_token == token) | (Email.unsubscribe_token == token))
               .first())
    if row is None and address:
        row = (db.query(Email)
               .filter(func.lower(Email.to_email) == address)
               .order_by(Email.id.desc())
               .first())

    if event in ("hard_bounce", "bounce", "permanent_fail", "dropped"):
        if row:
            row.status = "failed"
            row.error = f"provider bounce: {event}"
            db.commit()
            record_email_event(db, user_id=row.user_id, email_id=row.id, kind="bounce",
                               detail=event, meta={"provider": provider_token})
            suppress(db, row.user_id, row.to_email, reason=f"bounce:{event}")
        return {"handled": True, "action": "suppressed"}

    if event in ("spam_complaint", "complaint", "abuse"):
        if row:
            record_email_event(db, user_id=row.user_id, email_id=row.id, kind="complaint", detail=event)
            suppress(db, row.user_id, row.to_email, reason="spam_complaint")
        return {"handled": True, "action": "suppressed"}

    if event in ("open", "opened"):
        return {"handled": bool(row) and record_open(db, row.tracking_token if row else ""),
                "action": "opened"}

    if event in ("unsubscribe", "unsubscribed"):
        return {"handled": bool(row) and bool(handle_unsubscribe(db, row.unsubscribe_token if row else "")),
                "action": "unsubscribed"}

    return {"handled": False, "action": "ignored", "reason": f"unknown event '{event or 'empty'}'"}


def followups_due(db: Session, user_id: int, hours: int = 72) -> int:
    """Sent emails old enough to deserve a follow-up and never opened."""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    return int(
        db.query(func.count(Email.id))
        .filter(Email.user_id == user_id, Email.status == "sent", Email.sent_at <= cutoff)
        .scalar()
        or 0
    )


__all__ = [
    "is_suppressed",
    "is_role_mailbox",
    "suppress",
    "sent_today",
    "tracking_urls",
    "draft_email",
    "compliance_report",
    "send_email",
    "record_open",
    "handle_unsubscribe",
    "record_webhook_event",
    "followups_due",
]
