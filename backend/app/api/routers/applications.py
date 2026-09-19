"""Reviewable application packets — preparation only (no auto-submit)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.core import audit
from app.core.entitlements import enforce
from app.core.logging import get_logger
from app.models.models import ApplicationPacket, ApplicationPacketEvent, Job, User
from app.services.ai_guardrails import AIUnavailableError, GuardrailError
from app.services import application_packet as packet_service

router = APIRouter(prefix="/applications", tags=["applications"])
log = get_logger("app.applications")


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class PrepareRequest(BaseModel):
    job_id: int = Field(..., description="Job to prepare packet for")
    persona_id: Optional[int] = None
    strict_skeleton: bool = False


class EditRequest(BaseModel):
    tailored_resume: Optional[Dict[str, Any]] = None
    cover_note: Optional[str] = None
    short_answers: Optional[List[Dict[str, Any]]] = None
    outreach_draft: Optional[Dict[str, Any]] = None
    summary: Optional[str] = None
    checklist: Optional[List[Dict[str, Any]]] = None


class RejectRequest(BaseModel):
    reason: str = ""


def _get_job_or_404(db, user_id: int, job_id: int) -> Job:
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == user_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    return job


def _get_packet_or_404(db, user_id: int, packet_id: int) -> ApplicationPacket:
    pkt = db.query(ApplicationPacket).filter(ApplicationPacket.id == packet_id, ApplicationPacket.user_id == user_id).first()
    if not pkt:
        raise HTTPException(404, "Application packet not found")
    return pkt


# --------------------------------------------------------------------------- #
# Prepare — synchronous generation (guardrailed, grounded, no auto-submit)
# --------------------------------------------------------------------------- #


@router.post("/prepare")
async def prepare_packet(body: PrepareRequest, request: Request, user: CurrentUser, db: DbSession):
    """
    Generate a reviewable packet for one job.

    - Preparation only: the packet is created in ``pending_approval`` and never
      auto-submitted. The caller must review, optionally edit, then approve.
    - Grounding: only confirmed candidate facts are used. The fact ledger blocks
      invented employers, titles, years, certifications, skills and work
      authorization. Unsupported / ambiguous fields are routed to the checklist
      (``needs_review=true``) instead of guessed.
    - Master profile is snapshotted and preserved separately from the tailored
      artifacts.
    - Versioned: every call creates a new version; old versions are retained
      and marked superseded but never deleted.
    - JD version: the packet stores the JD hash and text at generation time.
    - Guardrails + provider-reported token accounting are stored on the packet.
    """
    enforce(db, user.id, "can_generate_resume")

    job = _get_job_or_404(db, user.id, body.job_id)
    if not (job.description or "").strip():
        raise HTTPException(422, "This job has no description — paste the JD before preparing a packet")

    # Quick profile existence check so the user gets a clear message without an AI call
    from app.services.application_packet import _get_master_profile

    master_doc, _, _, _ = _get_master_profile(db, user.id, body.persona_id or job.persona_id)
    if not master_doc or not any(v for v in master_doc.values() if v not in (None, "", [], {})):
        raise HTTPException(400, "Upload and confirm your profile before preparing an application packet")

    try:
        packet = await packet_service.generate_packet(
            db,
            user=user,
            job=job,
            persona_id=body.persona_id,
            strict_skeleton=body.strict_skeleton,
        )
    except AIUnavailableError as exc:
        # Use the guardrail's own diagnosis — the SPA renders reason/fix verbatim.
        payload = exc.payload() if hasattr(exc, "payload") else {"code": "ai_unavailable", "message": str(exc)}
        raise HTTPException(status_code=exc.http_status if hasattr(exc, "http_status") else 503, detail=payload) from exc
    except GuardrailError as exc:
        payload = exc.payload() if hasattr(exc, "payload") else {"code": "guardrail_failed", "message": str(exc)}
        # Guardrail failures are 422 and carry the violation list so the user/designer can see why.
        raise HTTPException(status_code=exc.http_status if hasattr(exc, "http_status") else 422, detail=payload) from exc

    job_row = _get_job_or_404(db, user.id, packet.job_id)
    d = packet_service.packet_to_dict(packet, job_row)
    audit.audit(db, "application_packet.prepared", user=user, target=f"job:{job.id}", detail={"packet_id": packet.id, "version": packet.version, "job_id": job.id}, request=request)
    return d


@router.post("/prepare-sync")
async def prepare_packet_alias(body: PrepareRequest, request: Request, user: CurrentUser, db: DbSession):
    """Alias for older clients/tests that POST to /prepare-sync."""
    return await prepare_packet(body, request, user, db)


# --------------------------------------------------------------------------- #
# List / get
# --------------------------------------------------------------------------- #


@router.get("")
def list_packets(
    user: CurrentUser,
    db: DbSession,
    job_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    is_current: Optional[bool] = Query(None),
    include_superseded: bool = Query(True),
):
    rows = packet_service.list_packets(db, int(user.id), job_id=job_id, status=status, include_superseded=include_superseded)
    if is_current is not None:
        rows = [r for r in rows if bool(r.is_current) is is_current]
    # Enrich with staleness vs current job
    out = []
    for pkt in rows:
        job = db.query(Job).filter(Job.id == pkt.job_id, Job.user_id == user.id).first()
        out.append(packet_service.packet_to_dict(pkt, job))
    return {"packets": out, "total": len(out)}


@router.get("/{packet_id}")
def get_packet(packet_id: int, user: CurrentUser, db: DbSession):
    pkt = _get_packet_or_404(db, user.id, packet_id)
    job = db.query(Job).filter(Job.id == pkt.job_id, Job.user_id == user.id).first()
    d = packet_service.packet_to_dict(pkt, job)
    # Include event trail
    events = (
        db.query(ApplicationPacketEvent)
        .filter(ApplicationPacketEvent.packet_id == pkt.id, ApplicationPacketEvent.user_id == user.id)
        .order_by(ApplicationPacketEvent.occurred_at.asc())
        .all()
    )
    d["events"] = [
        {
            "id": e.id,
            "event_type": e.event_type,
            "from_status": e.from_status,
            "to_status": e.to_status,
            "detail": e.detail,
            "meta": e.meta or {},
            "actor_type": e.actor_type,
            "occurred_at": e.occurred_at.isoformat() + "Z" if e.occurred_at else None,
        }
        for e in events
    ]
    return d


@router.get("/job/{job_id}/current")
def get_current_for_job(job_id: int, user: CurrentUser, db: DbSession):
    _get_job_or_404(db, user.id, job_id)
    pkt = packet_service.get_current_packet(db, int(user.id), job_id)
    if not pkt:
        raise HTTPException(404, "No packet for this job yet — prepare one first")
    job = db.query(Job).filter(Job.id == pkt.job_id, Job.user_id == user.id).first()
    d = packet_service.packet_to_dict(pkt, job)
    return d


@router.get("/job/{job_id}/versions")
def list_versions_for_job(job_id: int, user: CurrentUser, db: DbSession):
    _get_job_or_404(db, user.id, job_id)
    rows = packet_service.list_packets(db, int(user.id), job_id=job_id, include_superseded=True)
    # Already ordered newest first; keep all versions (retained)
    out = []
    for pkt in rows:
        job = db.query(Job).filter(Job.id == pkt.job_id, Job.user_id == user.id).first()
        out.append(packet_service.packet_to_dict(pkt, job))
    return {"job_id": job_id, "versions": out, "total": len(out)}


# --------------------------------------------------------------------------- #
# Edit before use
# --------------------------------------------------------------------------- #


@router.put("/{packet_id}")
def edit_packet(packet_id: int, body: EditRequest, request: Request, user: CurrentUser, db: DbSession):
    pkt = _get_packet_or_404(db, user.id, packet_id)
    # Only current packet is editable (keeps history immutable)
    if not pkt.is_current:
        raise HTTPException(409, "Only the current packet version can be edited — regenerate to create a new editable version")
    if pkt.status not in ("pending_approval", "draft", "rejected"):
        raise HTTPException(409, f"Packet in status '{pkt.status}' cannot be edited — only pending_approval/draft/rejected are editable")
    updated = packet_service.update_packet(
        db,
        packet=pkt,
        actor_user_id=int(user.id),
        tailored_resume=body.tailored_resume,
        cover_note=body.cover_note,
        short_answers=body.short_answers,
        outreach_draft=body.outreach_draft,
        summary=body.summary,
        checklist=body.checklist,
    )
    job = db.query(Job).filter(Job.id == updated.job_id, Job.user_id == user.id).first()
    audit.audit(db, "application_packet.edited", user=user, target=f"packet:{updated.id}", detail={"packet_id": updated.id}, request=request)
    return packet_service.packet_to_dict(updated, job)


@router.patch("/{packet_id}")
def edit_packet_patch(packet_id: int, body: EditRequest, request: Request, user: CurrentUser, db: DbSession):
    return edit_packet(packet_id, body, request, user, db)


# --------------------------------------------------------------------------- #
# Approval gate
# --------------------------------------------------------------------------- #


@router.post("/{packet_id}/approve")
def approve_packet(packet_id: int, request: Request, user: CurrentUser, db: DbSession):
    pkt = _get_packet_or_404(db, user.id, packet_id)
    if not pkt.is_current:
        raise HTTPException(409, "Only the current packet version can be approved")
    try:
        approved = packet_service.approve_packet(db, packet=pkt, actor_user_id=int(user.id))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    job = db.query(Job).filter(Job.id == approved.job_id, Job.user_id == user.id).first()
    audit.audit(db, "application_packet.approved", user=user, target=f"packet:{approved.id}", detail={"packet_id": approved.id}, request=request)
    return packet_service.packet_to_dict(approved, job)


@router.post("/{packet_id}/reject")
def reject_packet(packet_id: int, body: RejectRequest, request: Request, user: CurrentUser, db: DbSession):
    pkt = _get_packet_or_404(db, user.id, packet_id)
    if not pkt.is_current:
        raise HTTPException(409, "Only the current packet version can be rejected")
    rejected = packet_service.reject_packet(db, packet=pkt, actor_user_id=int(user.id), reason=body.reason or "Rejected by user")
    job = db.query(Job).filter(Job.id == rejected.job_id, Job.user_id == user.id).first()
    audit.audit(db, "application_packet.rejected", user=user, target=f"packet:{rejected.id}", detail={"packet_id": rejected.id}, request=request)
    return packet_service.packet_to_dict(rejected, job)


# --------------------------------------------------------------------------- #
# Regenerate after correction / JD change — creates a new retained version
# --------------------------------------------------------------------------- #


@router.post("/{packet_id}/regenerate")
async def regenerate_packet(packet_id: int, request: Request, user: CurrentUser, db: DbSession, strict_skeleton: bool = False):
    """
    Regenerate the tailored artifacts after a profile correction or JD update.

    Creates a new version; previous versions are retained (status superseded,
    is_current flipped). The new packet goes through the same grounding and
    guardrail path as the initial prepare.
    """
    pkt = _get_packet_or_404(db, user.id, packet_id)
    job = _get_job_or_404(db, user.id, int(pkt.job_id))
    try:
        new_pkt = await packet_service.generate_packet(
            db,
            user=user,
            job=job,
            persona_id=pkt.persona_id,
            strict_skeleton=strict_skeleton,
        )
    except AIUnavailableError as exc:
        payload = exc.payload() if hasattr(exc, "payload") else {"code": "ai_unavailable", "message": str(exc)}
        raise HTTPException(status_code=exc.http_status if hasattr(exc, "http_status") else 503, detail=payload) from exc
    except GuardrailError as exc:
        payload = exc.payload() if hasattr(exc, "payload") else {"code": "guardrail_failed", "message": str(exc)}
        raise HTTPException(status_code=exc.http_status if hasattr(exc, "http_status") else 422, detail=payload) from exc

    job2 = db.query(Job).filter(Job.id == new_pkt.job_id, Job.user_id == user.id).first()
    audit.audit(db, "application_packet.regenerated", user=user, target=f"job:{job.id}", detail={"from_packet_id": pkt.id, "new_packet_id": new_pkt.id, "version": new_pkt.version}, request=request)
    return packet_service.packet_to_dict(new_pkt, job2)


@router.post("/job/{job_id}/regenerate")
async def regenerate_for_job(job_id: int, request: Request, user: CurrentUser, db: DbSession, strict_skeleton: bool = False, persona_id: Optional[int] = None):
    """Regenerate without needing a prior packet id — uses the current job state."""
    job = _get_job_or_404(db, user.id, job_id)
    if not (job.description or "").strip():
        raise HTTPException(422, "This job has no description")
    try:
        new_pkt = await packet_service.generate_packet(db, user=user, job=job, persona_id=persona_id, strict_skeleton=strict_skeleton)
    except AIUnavailableError as exc:
        payload = exc.payload() if hasattr(exc, "payload") else {"code": "ai_unavailable", "message": str(exc)}
        raise HTTPException(status_code=exc.http_status if hasattr(exc, "http_status") else 503, detail=payload) from exc
    except GuardrailError as exc:
        payload = exc.payload() if hasattr(exc, "payload") else {"code": "guardrail_failed", "message": str(exc)}
        raise HTTPException(status_code=exc.http_status if hasattr(exc, "http_status") else 422, detail=payload) from exc
    job2 = db.query(Job).filter(Job.id == new_pkt.job_id, Job.user_id == user.id).first()
    audit.audit(db, "application_packet.regenerated", user=user, target=f"job:{job_id}", detail={"new_packet_id": new_pkt.id, "version": new_pkt.version}, request=request)
    return packet_service.packet_to_dict(new_pkt, job2)


# --------------------------------------------------------------------------- #
# Events / changelog for a packet
# --------------------------------------------------------------------------- #


@router.get("/{packet_id}/events")
def packet_events(packet_id: int, user: CurrentUser, db: DbSession):
    pkt = _get_packet_or_404(db, user.id, packet_id)
    rows = (
        db.query(ApplicationPacketEvent)
        .filter(ApplicationPacketEvent.packet_id == pkt.id, ApplicationPacketEvent.user_id == user.id)
        .order_by(ApplicationPacketEvent.occurred_at.asc())
        .all()
    )
    return {
        "packet_id": pkt.id,
        "events": [
            {
                "id": r.id,
                "event_type": r.event_type,
                "from_status": r.from_status,
                "to_status": r.to_status,
                "detail": r.detail,
                "meta": r.meta or {},
                "actor_type": r.actor_type,
                "occurred_at": r.occurred_at.isoformat() + "Z" if r.occurred_at else None,
            }
            for r in rows
        ],
    }


@router.get("/{packet_id}/stale")
def packet_stale_check(packet_id: int, user: CurrentUser, db: DbSession):
    pkt = _get_packet_or_404(db, user.id, packet_id)
    job = db.query(Job).filter(Job.id == pkt.job_id, Job.user_id == user.id).first()
    if not job:
        raise HTTPException(404, "Job for packet not found")
    stale = packet_service.is_stale(pkt, job)
    return {
        "packet_id": pkt.id,
        "version": pkt.version,
        "is_stale": stale,
        "stored_jd_hash": pkt.jd_hash,
        "current_jd_hash": packet_service._hash_jd(job),
        "jd_text_snapshot": pkt.jd_text_snapshot,
        "current_jd": job.description,
    }
