"""Email approval bucket, compliance, sending and engagement endpoints."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.core import audit
from app.core.logging import get_logger
from app.models.models import Email, EmailEvent, EmailOptOut, Job
from app.schemas.schemas import EmailOut
from app.services.job_queue import enqueue
from app.services.outreach import compliance_report, draft_email, send_email, suppress, tracking_urls

router = APIRouter(prefix="/emails", tags=["emails"])
log = get_logger("app.emails")


def _email_or_404(db, user_id: int, email_id: int) -> Email:
    row = db.query(Email).filter(Email.id == email_id, Email.user_id == user_id).first()
    if not row:
        raise HTTPException(404, "Email not found")
    return row


@router.get("", response_model=List[EmailOut])
def list_emails(user: CurrentUser, db: DbSession, status: Optional[str] = None, limit: int = Query(200, le=500)):
    query = db.query(Email).filter(Email.user_id == user.id)
    if status:
        query = query.filter(Email.status == status)
    return query.order_by(Email.created_at.desc()).limit(limit).all()


class EmailGenerate(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    job_id: Optional[int] = None
    department: str = "engineering"
    founder: bool = False
    recipient_email: Optional[str] = None
    recipient_name: Optional[str] = None
    context: str = ""
    queue: bool = False


@router.post("/generate")
async def create_email(payload: EmailGenerate, request: Request, user: CurrentUser, db: DbSession):
    """Draft outreach (never sends) and put it in the approval bucket."""
    job = None
    if payload.job_id:
        job = db.query(Job).filter(Job.id == payload.job_id, Job.user_id == user.id).first()
        if not job:
            raise HTTPException(404, "Job not found")

    recipient = None
    if payload.recipient_email:
        recipient = {"name": payload.recipient_name or "", "email": payload.recipient_email,
                     "title": "", "confidence": 1.0, "source": "manual", "verified": False}

    try:
        result = await draft_email(db, user, company=payload.company, job=job, founder=payload.founder,
                                   department=payload.department, recipient=recipient,
                                   extra_context=payload.context)
    except ValueError as exc:
        # User-fixable service errors must surface as 4xx with a machine code,
        # never as a 500.
        reason = str(exc)
        messages = {
            "profile_missing": "Upload your resume first so outreach can be personalised",
            "no_recipient": "No contact could be found — provide recipient_email explicitly",
            "suppressed": "That recipient is on your suppression list",
        }
        raise HTTPException(400, {"code": reason, "message": messages.get(reason, reason)}) from exc
    email_row: Email = result["email"]
    if payload.queue:
        enqueue(db, user_id=user.id, pipeline="email",
                payload={"send": True, "email_id": email_row.id}, priority=4,
                dedupe_key=f"email:{email_row.id}")
    audit.audit(db, "email.generated", user=user, target=email_row.to_email,
                detail={"company": payload.company, "source": email_row.source,
                        "confidence": email_row.confidence}, request=request)
    return {
        "email": {"id": email_row.id, "to": email_row.to_email, "subject": email_row.subject,
                  "body": email_row.body, "status": email_row.status, "source": email_row.source,
                  "confidence": email_row.confidence},
        "decision_maker": result["decision"],
        "ai": {"used": result["ai"].get("ai_used"), "fact_guard": result["ai"].get("fact_guard")},
    }


@router.get("/contacts")
async def preview_contacts(user: CurrentUser, db: DbSession, company: str = Query(...),
                          department: str = "engineering"):
    """Show who we can find *before* drafting anything (transparency + trust)."""
    from app.services.contact_discovery import discover_decision_makers

    return await discover_decision_makers(company, department=department, limit=8)


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
def send_now(email_id: int, payload: SendRequest, request: Request, user: CurrentUser, db: DbSession):
    row = _email_or_404(db, user.id, email_id)
    result = send_email(db, user, row, otp=payload.otp, allow_real=payload.allow_real)
    if result.get("sent"):
        audit.audit(db, "email.sent", user=user, target=row.to_email, detail={"email_id": row.id}, request=request)
    elif result.get("blocked"):
        audit.audit(db, "email.blocked", user=user, target=row.to_email,
                    detail={"email_id": row.id, "blockers": result["compliance"]["blockers"]}, request=request)
    return result


@router.post("/{email_id}/check")
def check_compliance(email_id: int, user: CurrentUser, db: DbSession):
    """Dry-run the compliance gate so the UI can explain blockers up front."""
    row = _email_or_404(db, user.id, email_id)
    return {**compliance_report(db, user, row), "tracking": tracking_urls(row)}


@router.get("/{email_id}/events")
def email_events(email_id: int, user: CurrentUser, db: DbSession):
    _email_or_404(db, user.id, email_id)
    rows = (
        db.query(EmailEvent)
        .filter(EmailEvent.email_id == email_id, EmailEvent.user_id == user.id)
        .order_by(EmailEvent.created_at.desc())
        .all()
    )
    return [{"id": r.id, "kind": r.kind, "detail": r.detail, "meta": r.meta, "created_at": r.created_at}
            for r in rows]


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
    row = (
        db.query(EmailOptOut)
        .filter(EmailOptOut.id == suppression_id, EmailOptOut.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(404, "Entry not found")
    db.delete(row)
    db.commit()
    audit.audit(db, "email.unsubscribed", user=user, target=row.email, detail={"removed": True})
    return {"ok": True}
