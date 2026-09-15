"""Email approval bucket, compliance, sending and engagement endpoints — monetized."""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.core import audit
from app.core.auth import require_consent
from app.core.entitlements import enforce, increment_usage
from app.core.logging import get_logger
from app.models.models import Email, EmailEvent, EmailOptOut, Job, Profile, User
from app.services import persona as persona_service
from app.services.handlers import DRAFT_REJECTIONS
from app.services.job_queue import enqueue_or_existing
from app.services.outreach import (
    compliance_report,
    is_role_mailbox,
    is_suppressed,
    send_email,
    suppress,
    tracking_urls,
)

router = APIRouter(prefix="/emails", tags=["emails"])
log = get_logger("app.emails")


def _email_or_404(db, user_id: int, email_id: int) -> Email:
    row = db.query(Email).filter(Email.id == email_id, Email.user_id == user_id).first()
    if not row:
        raise HTTPException(404, "Email not found")
    return row


def _bucket_row(row: Email, *, jobs: Dict[int, Job]) -> Dict[str, Any]:
    """
    One approval-bucket card.

    The bucket is where the user decides what actually gets sent, so it has to
    answer "which job was this written for, and is this address real?" without
    the user leaving the page.
    """
    job = jobs.get(row.job_id) if row.job_id else None
    excerpt = row.jd_excerpt or (job.description if job else "") or ""
    return {
        "id": row.id,
        "to_email": row.to_email,
        "to": row.to_email,  # alias kept for existing API clients
        "to_name": row.to_name,
        "subject": row.subject,
        "body": row.body,
        "status": row.status,
        "company": row.company,
        "recipient_type": row.recipient_type,
        "source": row.source,
        "confidence": row.confidence,
        "verified": bool(row.verified),
        "role_mailbox": is_role_mailbox(row.to_email),
        "ai_used": bool(row.ai_used),
        "guardrail": row.guardrail_report or {},
        "opens": row.opens,
        "dry_run": row.dry_run,
        "created_at": row.created_at,
        "sent_at": row.sent_at,
        "job": ({
            "id": row.job_id,
            "title": row.job_title or (job.title if job else ""),
            "company": row.company,
            "url": row.job_url or (job.url if job else ""),
            "jd_excerpt": excerpt[:600],
            "jd_length": len(excerpt),
        } if row.job_id else None),
    }


@router.get("")
def list_emails(user: CurrentUser, db: DbSession, status: Optional[str] = None, limit: int = Query(200, le=500)):
    """Approval bucket, with the job/JD context each draft was written for."""
    query = db.query(Email).filter(Email.user_id == user.id)
    if status:
        query = query.filter(Email.status == status)
    rows = query.order_by(Email.created_at.desc()).limit(limit).all()
    job_ids = {row.job_id for row in rows if row.job_id}
    jobs: Dict[int, Job] = {}
    if job_ids:
        jobs = {job.id: job for job in db.query(Job).filter(Job.id.in_(job_ids)).all()}
    return [_bucket_row(row, jobs=jobs) for row in rows]


@router.get("/contacts")
async def preview_contacts(user: CurrentUser, db: DbSession, company: str = Query(...), department: str = "engineering"):
    """Show who we can find *before* drafting anything (transparency + trust)."""
    enforce(db, user.id, "contact_discovery_per_month")
    from app.services.contact_discovery import discover_decision_makers

    result = await discover_decision_makers(company, department=department, limit=8)
    for contact in result.get("contacts") or []:
        contact["role_mailbox"] = is_role_mailbox(str(contact.get("email") or ""))
    increment_usage(db, user.id, "contact_discovery_per_month", 1)
    return result


class EmailGenerate(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    job_id: Optional[int] = None
    department: str = "engineering"
    founder: bool = False
    recipient_email: Optional[str] = None
    recipient_name: Optional[str] = None
    context: str = ""
    queue: bool = False
    persona_id: Optional[int] = None


@router.post("/generate")
async def create_email(payload: EmailGenerate, request: Request, user: CurrentUser, db: DbSession):
    """Queue an outreach draft (never sends) for the approval bucket.

    v2.2.8: answers ``{queued, pipeline_job_id}`` — the draft is written by the
    ``email`` pipeline and the row's ``result`` carries ``email_id``/``to`` (or
    ``status: rejected`` with the reason), so the bucket and the job drawer can
    show it on any route and after a refresh. The user-fixable preconditions
    (no profile, unsigned profile, suppressed recipient) are still checked
    here so a click fails fast with the same 400 as before.
    """
    enforce(db, user.id, "can_send_outreach")
    enforce(db, user.id, "outreach_per_month")
    enforce(db, user.id, "contact_discovery_per_month")

    job = None
    if payload.job_id:
        job = db.query(Job).filter(Job.id == payload.job_id, Job.user_id == user.id).first()
        if not job:
            raise HTTPException(404, "Job not found")

    # The profile lives in the Profile table — there is no settings-based
    # snapshot to read first (the old `application.profile_snapshot` fallback
    # was never writable and therefore always empty).
    profile_row = db.query(Profile).filter(Profile.user_id == user.id).first()
    profile = (profile_row.data if profile_row else {}) or {}
    if not profile:
        raise HTTPException(400, {"code": "profile_missing", "message": DRAFT_REJECTIONS["profile_missing"]})
    if not str(profile.get("name") or "").strip():
        raise HTTPException(400, {"code": "profile_name_missing", "message": DRAFT_REJECTIONS["profile_name_missing"]})
    if payload.recipient_email and is_suppressed(db, user.id, payload.recipient_email.strip().lower()):
        raise HTTPException(400, {"code": "suppressed", "message": DRAFT_REJECTIONS["suppressed"]})

    recipient = None
    if payload.recipient_email:
        recipient = {"name": payload.recipient_name or "", "email": payload.recipient_email, "title": "",
                     "confidence": 1.0, "source": "manual", "verified": False}

    persona = persona_service.get_persona(db, user.id, payload.persona_id or (job.persona_id if job else None))
    fingerprint = hashlib.sha256(
        f"{payload.company.strip().lower()}|{payload.department}|{int(payload.founder)}|"
        f"{(payload.recipient_email or '').lower()}|{payload.context.strip()}".encode()
    ).hexdigest()[:12]
    item, duplicate = enqueue_or_existing(
        db,
        user_id=user.id,
        pipeline="email",
        job_id=job.id if job else None,
        payload={"company": payload.company, "job_id": job.id if job else None, "founder": payload.founder,
                 "department": payload.department, "context": payload.context, "recipient": recipient,
                 "persona_id": persona.id if persona else None, "queue": bool(payload.queue)},
        priority=4,
        dedupe_key=f"draft:{job.id if job else 0}:{fingerprint}",
    )
    if not duplicate:
        audit.audit(db, "email.generate_queued", user=user, target=payload.company,
                    detail={"company": payload.company, "job_id": job.id if job else None,
                            "pipeline_job_id": item.id if item else None}, request=request)
    return {
        "status": "queued",
        "queued": not duplicate,
        "duplicate": duplicate,
        "pipeline_job_id": item.id if item else None,
        "queue_status": item.status if item else None,
        "company": payload.company,
        "job_id": job.id if job else None,
        "persona": persona_service.to_dict(persona, include_memory=False) if persona else None,
        "message": ("A draft for this company is already being written — showing its live status."
                    if duplicate else "Draft queued — it lands in the approval bucket when the writer is done."),
    }


@router.put("/{email_id}")
def update_email(email_id: int, payload: dict, user: CurrentUser, db: DbSession):
    row = _email_or_404(db, user.id, email_id)
    for key in ("subject", "body", "to_email", "to_name"):
        if key in payload:
            setattr(row, key, str(payload[key])[:8000])
    db.commit()
    return {"ok": True}


@router.post("/{email_id}/approve")
def approve_email(email_id: int, request: Request, user: CurrentUser, db: DbSession):
    row = _email_or_404(db, user.id, email_id)
    row.status = "queued"
    db.commit()
    audit.audit(db, "email.approved", user=user, target=row.to_email, detail={"email_id": row.id}, request=request)
    return {"ok": True, "status": row.status}


class SendRequest(BaseModel):
    otp: Optional[str] = None
    allow_real: bool = True


@router.post("/{email_id}/send")
def send_now(
    email_id: int,
    payload: SendRequest,
    request: Request,
    user: Annotated[User, Depends(require_consent("outreach"))],
    db: DbSession,
):
    enforce(db, user.id, "outreach_per_day")
    row = _email_or_404(db, user.id, email_id)
    result = send_email(db, user, row, otp=payload.otp, allow_real=payload.allow_real)
    if result.get("sent"):
        increment_usage(db, user.id, "outreach_per_day", 1)
        audit.audit(db, "email.sent", user=user, target=row.to_email, detail={"email_id": row.id}, request=request)
    elif result.get("blocked"):
        audit.audit(db, "email.blocked", user=user, target=row.to_email, detail={"email_id": row.id, "blockers": result["compliance"]["blockers"]}, request=request)
    return result


@router.post("/{email_id}/check")
def check_compliance(email_id: int, user: CurrentUser, db: DbSession):
    """Dry-run the compliance gate so the UI can explain blockers up front."""
    row = _email_or_404(db, user.id, email_id)
    return {**compliance_report(db, user, row), "tracking": tracking_urls(row)}


@router.get("/{email_id}/events")
def email_events(email_id: int, user: CurrentUser, db: DbSession):
    _email_or_404(db, user.id, email_id)
    rows = db.query(EmailEvent).filter(EmailEvent.email_id == email_id, EmailEvent.user_id == user.id).order_by(EmailEvent.created_at.desc()).all()
    return [{"id": r.id, "kind": r.kind, "detail": r.detail, "meta": r.meta, "created_at": r.created_at} for r in rows]


@router.delete("/{email_id}")
def delete_email(email_id: int, user: CurrentUser, db: DbSession):
    row = _email_or_404(db, user.id, email_id)
    db.query(EmailEvent).filter(EmailEvent.email_id == row.id).delete(synchronize_session=False)
    db.delete(row)
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Suppression list
# --------------------------------------------------------------------------- #
@router.get("/suppressions")
def list_suppressions(user: CurrentUser, db: DbSession):
    rows = db.query(EmailOptOut).filter(EmailOptOut.user_id == user.id).order_by(EmailOptOut.created_at.desc()).all()
    return [{"id": r.id, "email": r.email, "reason": r.reason, "created_at": r.created_at} for r in rows]


@router.post("/suppressions")
def add_suppression(payload: dict, user: CurrentUser, db: DbSession):
    email = str(payload.get("email") or "").strip().lower()
    if "@" not in email:
        raise HTTPException(400, "A valid email address is required")
    row = suppress(db, user.id, email, reason=str(payload.get("reason") or "manual"))
    return {"ok": True, "id": row.id, "email": row.email}


@router.delete("/suppressions/{suppression_id}")
def remove_suppression(suppression_id: int, user: CurrentUser, db: DbSession):
    row = db.query(EmailOptOut).filter(EmailOptOut.id == suppression_id, EmailOptOut.user_id == user.id).first()
    if not row:
        raise HTTPException(404, "Entry not found")
    db.delete(row)
    db.commit()
    audit.audit(db, "email.unsubscribed", user=user, target=row.email, detail={"removed": True})
    return {"ok": True}


@router.get("/{email_id}")
def get_email(email_id: int, user: CurrentUser, db: DbSession):
    """One approval-bucket card — how the SPA fetches a draft the queue just wrote.

    Declared last on purpose: ``/{email_id}`` would otherwise shadow the
    literal ``/suppressions`` and ``/contacts`` routes above it.
    """
    row = _email_or_404(db, user.id, email_id)
    jobs: Dict[int, Job] = {}
    if row.job_id:
        job = db.query(Job).filter(Job.id == row.job_id, Job.user_id == user.id).first()
        if job:
            jobs[job.id] = job
    return _bucket_row(row, jobs=jobs)
