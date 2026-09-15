"""
Account, consent, privacy and audit endpoints.

Implements the data-subject rights a launch product needs: full export, hard
delete, consent capture with timestamps, and an audit trail for both.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession, client_ip
from app.core import audit
from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import (
    ApiKey,
    AuditLog,
    Email,
    EmailEvent,
    EmailOptOut,
    ErrorLog,
    FundingCompany,
    Job,
    JobEvent,
    PipelineJob,
    Profile,
    RefreshToken,
    Resume,
    SettingsModel,
    User,
    UserInputRequest,
    VaultEntry,
)

router = APIRouter(prefix="/account", tags=["account"])
log = get_logger("app.account")

DISCLOSURES = {
    "terms": {
        "title": "Terms of service",
        "summary": "You are responsible for the accuracy of the profile data and resumes this "
                   "system submits on your behalf.",
    },
    "automation": {
        "title": "Automated applications",
        "summary": "Automation acts with your credentials on third-party job portals. Some portals "
                   "prohibit automated submissions; you confirm you have the right to use each "
                   "portal this way and accept that accounts may be limited.",
    },
    "outreach": {
        "title": "Cold outreach",
        "summary": "Sending mail to hiring contacts must comply with anti-spam law (CAN-SPAM/GDPR). "
                   "You confirm you will only contact people you may lawfully contact, that your "
                   "postal address and unsubscribe link are configured, and that you will honour "
                   "opt-outs.",
    },
    "data_processing": {
        "title": "Data processing",
        "summary": "Your resume, profile and credentials are stored encrypted at rest. Credentials "
                   "use a per-user encryption key. You can export or delete all data at any time.",
    },
}


class ConsentPayload(BaseModel):
    terms: Optional[bool] = None
    automation: Optional[bool] = None
    outreach: Optional[bool] = None
    data_processing: Optional[bool] = None


@router.get("/disclosures")
def disclosures():
    """Public: the exact text the user is asked to accept."""
    return {"disclosures": DISCLOSURES, "environment": settings.environment}


@router.get("/consent")
def get_consent(user: CurrentUser):
    consents = user.consents or {}
    return {
        "consents": consents,
        "granted": {key: bool(consents.get(f"{key}_accepted_at")) for key in DISCLOSURES},
    }


@router.post("/consent")
def set_consent(payload: ConsentPayload, request: Request, user: CurrentUser, db: DbSession):
    consents = dict(user.consents or {})
    now = datetime.utcnow().isoformat()
    for key, value in payload.model_dump(exclude_none=True).items():
        field = f"{key}_accepted_at"
        if value:
            consents[field] = consents.get(field) or now
            audit.audit(db, "consent.accepted", user=user, target=key, request=request, ip=client_ip(request))
        else:
            consents.pop(field, None)
            consents[f"{key}_revoked_at"] = now
            audit.audit(db, "consent.revoked", user=user, target=key, request=request, ip=client_ip(request))
    user.consents = consents
    db.commit()
    return {"ok": True, "consents": consents}


@router.get("/audit")
def list_audit(user: CurrentUser, db: DbSession, limit: int = 100, action: Optional[str] = None):
    query = db.query(AuditLog).filter(AuditLog.user_id == user.id)
    if action:
        query = query.filter(AuditLog.action == action)
    rows = query.order_by(AuditLog.created_at.desc()).limit(min(500, max(1, limit))).all()
    return [
        {"id": r.id, "action": r.action, "target": r.target, "detail": r.detail,
         "ip": r.ip, "request_id": r.request_id, "created_at": r.created_at}
        for r in rows
    ]


@router.get("/export")
def export_account(request: Request, user: CurrentUser, db: DbSession):
    """GDPR-style machine-readable export of everything owned by the account."""
    from app.services.vault import VaultDecryptionError, reveal_password

    def vault_row(entry):
        """One vault entry, with an explicit state when it cannot be decrypted."""
        row: Dict[str, Any] = {"domain": entry.domain, "username": entry.username,
                               "created_at": entry.created_at.isoformat()}
        try:
            row["password"] = reveal_password(entry, db=db)
        except VaultDecryptionError as exc:
            # ``null`` + a named error, never ``""``: a blank password in a data
            # export is indistinguishable from a credential that was never set,
            # which is exactly how a rotated VAULT_KEY used to go unnoticed.
            row["password"] = None
            row["password_error"] = "undecryptable"
            log.error("GDPR export could not decrypt vault entry %s (%s): %s",
                      entry.id, entry.domain, exc)
        return row

    def rows(model, order=None):
        return db.query(model).filter(model.user_id == user.id).order_by(order or model.id).all()

    payload: Dict[str, Any] = {
        "exported_at": datetime.utcnow().isoformat(),
        "format_version": "2.0",
        "account": {"email": user.email, "name": user.name, "role": user.role,
                    "created_at": user.created_at.isoformat(), "consents": user.consents or {}},
        "profile": [
            {"id": p.id, "data": p.data, "layout": p.layout, "created_at": p.created_at.isoformat()}
            for p in rows(Profile)
        ],
        "resumes": [
            {"id": r.id, "filename": r.filename, "type": r.type, "status": r.status,
             "tags": r.tags, "created_at": r.created_at.isoformat()}
            for r in rows(Resume)
        ],
        "jobs": [
            {"id": j.id, "title": j.title, "company": j.company, "url": j.url, "source": j.source,
             "status": j.status, "score": j.score, "applied_at": j.applied_at.isoformat() if j.applied_at else None}
            for j in rows(Job)
        ],
        "job_events": [
            {"job_id": e.job_id, "stage": e.stage, "status": e.status, "message": e.message,
             "created_at": e.created_at.isoformat()}
            for e in rows(JobEvent)
        ],
        "vault": [vault_row(v) for v in rows(VaultEntry)],
        "emails": [
            {"id": e.id, "to": e.to_email, "subject": e.subject, "status": e.status,
             "sent_at": e.sent_at.isoformat() if e.sent_at else None, "opens": e.opens}
            for e in rows(Email)
        ],
        "email_events": [
            {"email_id": e.email_id, "kind": e.kind, "detail": e.detail, "created_at": e.created_at.isoformat()}
            for e in rows(EmailEvent)
        ],
        "suppressions": [{"email": s.email, "reason": s.reason} for s in rows(EmailOptOut)],
        "funding_companies": [
            {"name": c.name, "stage": c.stage, "source": c.source, "verified": c.verified}
            for c in rows(FundingCompany)
        ],
        "settings": [
            {"category": s.category, "key": s.key, "value": "***" if s.category == "email" and s.key == "password" else s.value}
            for s in rows(SettingsModel)
        ],
        "pipeline_jobs": [
            {"pipeline": p.pipeline, "status": p.status, "created_at": p.created_at.isoformat()}
            for p in rows(PipelineJob)
        ],
        "logs": [{"pipeline": row.pipeline, "level": row.level, "message": row.message,
                  "timestamp": row.timestamp.isoformat()} for row in
                 db.query(ErrorLog).filter(ErrorLog.user_id == user.id).all()],
        "audit": [{"action": a.action, "target": a.target, "created_at": a.created_at.isoformat()}
                  for a in rows(AuditLog)],
    }
    audit.audit(db, "account.exported", user=user, request=request, ip=client_ip(request))
    filename = f"jobhunter-export-{user.id}-{datetime.utcnow().strftime('%Y%m%d')}.json"
    return JSONResponse(content=json.loads(json.dumps(payload, default=str)),
                        headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.delete("")
def delete_account(request: Request, user: CurrentUser, db: DbSession, confirm: str = ""):
    """Hard-delete the account and every row it owns. Irreversible."""
    if confirm != user.email:
        raise HTTPException(400, {"code": "confirmation_required",
                                  "message": "Pass ?confirm=<your email> to delete the account permanently"})

    # Remove uploaded/generated files too — "delete forever" must mean it.
    for resume in db.query(Resume).filter(Resume.user_id == user.id).all():
        for path in {resume.filepath, resume.filepath.rsplit(".", 1)[0] + ".pdf"}:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError as exc:  # pragma: no cover
                log.warning("could not delete %s: %s", path, exc)

    user_id = user.id
    for model in (JobEvent, EmailEvent, UserInputRequest, PipelineJob, Email, Job, Resume, Profile,
                  VaultEntry, FundingCompany, EmailOptOut, SettingsModel, ApiKey, RefreshToken, ErrorLog):
        db.query(model).filter(model.user_id == user_id).delete(synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.user_id == user_id).delete(synchronize_session=False)
    db.query(User).filter(User.id == user_id).delete(synchronize_session=False)
    db.commit()

    audit.audit(db, "account.deleted", user_id=None, target=f"user:{user_id}",
                detail={"email": user.email}, request=request, ip=client_ip(request))
    return {"ok": True, "deleted": True, "message": "Account and all associated data deleted"}


@router.get("/runtime")
def runtime_settings(user: CurrentUser, db: DbSession):
    """Effective (non-secret) platform configuration for the UI's status panel."""
    from app.services.autofill import autofill_available
    from app.services.funding_sources import provider_status
    from app.services.sources import list_sources

    return {
        **settings.public_settings(),
        "autofill_runtime": autofill_available(),
        "funding_providers": provider_status(),
        "sources": list_sources(),
        "consents": {key: bool((user.consents or {}).get(f"{key}_accepted_at")) for key in DISCLOSURES},
    }


@router.get("/billing-usage")
def usage(user: CurrentUser, db: DbSession):
    """Simple usage counters (the basis for plan limits / billing)."""
    from app.services.ai_client import usage_snapshot
    from app.services.outreach import utc_day_bounds

    # Same UTC calendar day the outreach daily cap is measured over — one
    # definition of "today" for the whole app, not one per call site.
    day_start, day_end = utc_day_bounds()
    return {
        "jobs": {
            "total": db.query(Job).filter(Job.user_id == user.id).count(),
            "today": db.query(Job).filter(Job.user_id == user.id, Job.discovered_at >= day_start).count(),
            "applied": db.query(Job).filter(Job.user_id == user.id, Job.status == "applied").count(),
        },
        "emails": {
            "sent_today": db.query(Email).filter(Email.user_id == user.id, Email.status == "sent",
                                                 Email.sent_at >= day_start,
                                                 Email.sent_at < day_end).count(),
            "pending_approval": db.query(Email).filter(Email.user_id == user.id,
                                                       Email.status == "pending_approval").count(),
        },
        "resumes": db.query(Resume).filter(Resume.user_id == user.id).count(),
        "vault_entries": db.query(VaultEntry).filter(VaultEntry.user_id == user.id).count(),
        "ai_tokens": usage_snapshot(),
    }
