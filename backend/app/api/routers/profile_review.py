"""
Candidate profile extraction and review API.

Implements contract 02 + 13-api-response-shapes:
- GET /api/profile/review — list fields with provenance, evidence, confidence
- POST /api/profile/review/resolve — confirm/correct/reject/defer per field
- GET /api/profile/completeness — completeness calculation
- GET /api/profile/history — field history audit
- POST /api/profile/extract — trigger extraction from text or resume

All endpoints tenant-scoped.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession
from app.core import audit
from app.models.models import CandidateProfile, ProfileFieldProvenance, User
from app.services import candidate_profile as cp_service

router = APIRouter(prefix="/profile", tags=["candidate-profile"])


def _after_review_hook(db: Session, user: User, profile_id: int) -> None:
    """Post-commit pool hook shared by the review endpoints.

    Imported lazily so the router does not pull the discovery graph in unless a
    review actually happens. The hook itself never raises.
    """
    from app.services import job_pool

    job_pool.maybe_instant_match_after_review(db, user, profile_id)


class ExtractRequest(BaseModel):
    text: Optional[str] = None
    resume_document_id: Optional[int] = None
    persona_id: Optional[int] = None


class ResolveRequest(BaseModel):
    profile_id: int
    path: str
    action: str  # confirm | correct | reject | defer
    value: Optional[Any] = None
    reason: Optional[str] = None


class CorrectRequest(BaseModel):
    profile_id: int
    path: str
    value: Any
    reason: Optional[str] = None


@router.get("/review")
def get_profile_review(
    user: CurrentUser,
    db: DbSession,
    profile_id: Optional[int] = None,
    only_needs_review: bool = False,
    persona_id: Optional[int] = None,
):
    """Get profile review document — whole document return per contract 13."""
    if profile_id is not None:
        profile, provenance = cp_service.get_profile_with_provenance(db, user.id, profile_id)
        if not profile:
            raise HTTPException(404, {"code": "profile_missing", "message": "Profile not found"})
    else:
        profile = cp_service.get_current_profile(db, user.id, persona_id=persona_id)
        if not profile:
            raise HTTPException(404, {"code": "profile_missing", "message": "No current profile"})
        provenance = cp_service.list_provenance_for_review(db, user.id, profile.id, only_needs_review=only_needs_review)

    if only_needs_review:
        provenance = [p for p in provenance if p.review_required]

    return cp_service.build_review_response(profile, provenance)


@router.post("/review/resolve")
def resolve_field(request: Request, user: CurrentUser, db: DbSession, body: ResolveRequest):
    """Resolve a field that needs review — confirm/correct/reject/defer."""
    if body.action not in ("confirm", "correct", "reject", "defer"):
        raise HTTPException(400, {"code": "validation_error", "message": f"Invalid action {body.action}"})

    profile, _ = cp_service.get_profile_with_provenance(db, user.id, body.profile_id)
    if not profile:
        raise HTTPException(404, {"code": "profile_missing", "message": "Profile not found"})

    provenance = (
        db.query(ProfileFieldProvenance)
        .filter(
            ProfileFieldProvenance.user_id == user.id,
            ProfileFieldProvenance.profile_id == body.profile_id,
            ProfileFieldProvenance.path == body.path,
        )
        .first()
    )
    if not provenance:
        raise HTTPException(404, {"code": "field_needs_review", "message": f"Field {body.path} not found"})

    # Restricted fields cannot be deferred
    if body.action == "defer" and provenance.sensitivity == "restricted":
        raise HTTPException(400, {"code": "restricted_field", "message": "Restricted fields cannot be deferred"})

    try:
        if body.action == "confirm":
            cp_service.confirm_field(db, user.id, body.profile_id, body.path, actor_id=user.id)
        elif body.action == "correct":
            if body.value is None:
                raise HTTPException(400, {"code": "validation_error", "message": "Value required for correct action"})
            cp_service.correct_field(db, user.id, body.profile_id, body.path, body.value, actor_type="user", actor_id=user.id, reason=body.reason)
        elif body.action == "reject":
            cp_service.reject_field(db, user.id, body.profile_id, body.path, actor_id=user.id, reason=body.reason)
        elif body.action == "defer":
            # Deferred keeps the value but removes it from the required queue;
            # still needs to recalc completeness so the percent does not stay stale.
            import uuid
            from datetime import datetime

            from app.services.candidate_profile import ProfileFieldHistory
            # Append history for audit (defer is a review decision)
            deferred_hist = ProfileFieldHistory(
                user_id=user.id,
                provenance_id=provenance.id,
                path=provenance.path,
                previous_value_hash=provenance.value_hash,
                previous_value_preview=provenance.value_preview,
                new_value_hash=provenance.value_hash,
                new_value_preview=provenance.value_preview,
                origin_before=provenance.origin,
                origin_after=provenance.origin,
                review_status_before=provenance.review_status,
                review_status_after="deferred",
                actor_type="user",
                actor_id=user.id,
                reason=body.reason or "user deferred",
                event_id=str(uuid.uuid4()),
                occurred_at=datetime.utcnow(),
            )
            db.add(deferred_hist)
            provenance.review_status = "deferred"
            provenance.review_required = False
            provenance.reviewed_by = user.id
            provenance.reviewed_at = datetime.utcnow()
            provenance.needs_answer_for = []
            db.add(provenance)
            db.flush()
            # Recalculate completeness/state — defer counts separately
            try:
                from app.services.candidate_profile import _recalculate_profile_state
                prof = db.query(CandidateProfile).filter(CandidateProfile.id == body.profile_id, CandidateProfile.user_id == user.id).first()
                if prof:
                    _recalculate_profile_state(db, prof)
                    db.flush()
            except Exception:
                pass
    except ValueError as exc:
        raise HTTPException(404, {"code": "field_needs_review", "message": str(exc)}) from exc

    db.commit()

    # Onboarding is finished the moment the last field is resolved: hand the
    # profile to the shared pool for instant matches and queue the live run.
    # Idempotent per (profile, document), and failure-isolated by the hook.
    _after_review_hook(db, user, body.profile_id)

    # Audit
    audit.audit(db, "profile.review_completed", user=user, target=body.path, request=request, detail={"action": body.action, "profile_id": body.profile_id})

    # Return updated review doc
    updated_profile, provenance_list = cp_service.get_profile_with_provenance(db, user.id, body.profile_id)
    if not updated_profile:
        raise HTTPException(404, {"code": "profile_missing", "message": "Profile not found after update"})
    response = cp_service.build_review_response(updated_profile, provenance_list)
    # Add invalidated answers per contract 13
    response["invalidated_answers"] = []  # would list application answers that depended on this field
    return response


@router.post("/review/correct")
def correct_field_endpoint(request: Request, user: CurrentUser, db: DbSession, body: CorrectRequest):
    """Shortcut for correction — user overrides AI value without deleting evidence."""
    profile, _ = cp_service.get_profile_with_provenance(db, user.id, body.profile_id)
    if not profile:
        raise HTTPException(404, {"code": "profile_missing", "message": "Profile not found"})

    try:
        cp_service.correct_field(db, user.id, body.profile_id, body.path, body.value, actor_type="user", actor_id=user.id, reason=body.reason)
    except ValueError as exc:
        raise HTTPException(404, {"code": "field_needs_review", "message": str(exc)}) from exc

    db.commit()

    # A correction can be the change that activates the profile — match the
    # corrected document against the pool before answering.
    _after_review_hook(db, user, body.profile_id)

    audit.audit(db, "profile.updated", user=user, target=body.path, request=request, detail={"profile_id": body.profile_id, "field": body.path})

    updated_profile, provenance_list = cp_service.get_profile_with_provenance(db, user.id, body.profile_id)
    if not updated_profile:
        raise HTTPException(404, {"code": "profile_missing", "message": "Profile not found after update"})
    return cp_service.build_review_response(updated_profile, provenance_list)


@router.get("/completeness")
def get_completeness(user: CurrentUser, db: DbSession, profile_id: Optional[int] = None, persona_id: Optional[int] = None):
    """Get completeness calculation for current or specific profile."""
    if profile_id is not None:
        profile = db.query(CandidateProfile).filter(CandidateProfile.id == profile_id, CandidateProfile.user_id == user.id).first()
        if not profile:
            raise HTTPException(404, {"code": "profile_missing", "message": "Profile not found"})
    else:
        profile = cp_service.get_current_profile(db, user.id, persona_id=persona_id)
        if not profile:
            raise HTTPException(404, {"code": "profile_missing", "message": "No current profile"})

    completeness = profile.completeness or {}
    # Compute completion requirements from provenance + canonical document
    # (document is source of truth for sensitive masked previews — using preview
    # alone marked every sensitive field as missing, causing the stale 63% etc.)
    provenance = cp_service.list_provenance_for_review(db, user.id, profile.id)
    # Use the stored completeness' missing/uncertain only as stored value is already
    # authoritative. For requirements we need field-level value existence —
    # rebuild from document + provenance via the same helpers the service uses.
    fields = {}
    doc = profile.document or {}
    for prov in provenance:
        fk = prov.path.lstrip("/").split("/")[0]
        # Resolve actual value from document (masked previews are None for sensitive)
        try:
            from app.services.candidate_profile import _get_doc_value, _hash_value
            val = _get_doc_value(doc, fk)
            if val in (None, "", [], {}):
                if prov.sensitivity not in ("sensitive", "restricted") and prov.value_preview not in (None, "", [], {}):
                    val = prov.value_preview
                elif prov.value_hash != _hash_value(None) and prov.review_status not in ("rejected", "missing"):
                    val = "__sensitive_filled__"
                else:
                    val = None
        except Exception:
            val = prov.value_preview if prov.value_preview is not None else None
        # Derive status consistently with _recalculate_profile_state
        if prov.review_status in ("confirmed", "corrected", "auto_accepted"):
            status = "confirmed"
        elif prov.review_status == "rejected":
            status = "missing"
            val = None
        elif getattr(prov, "ambiguity", "none") == "conflicting_sources":
            status = "conflicting"
        elif val in (None, "", [], {}):
            status = "missing"
        elif prov.review_status in ("needs_review", "deferred") or prov.confidence_band in ("low", "none"):
            status = "uncertain"
        else:
            status = prov.review_status or "uncertain"
        fields[fk] = {
            "value": val,
            "confidence": prov.confidence or 0.0,
            "confidence_band": prov.confidence_band,
            "status": status,
            "review_required": prov.review_required,
            "evidence": prov.evidence,
            "evidence_quotes": [ev.get("quote") for ev in (prov.evidence or []) if isinstance(ev, dict)],
        }
    # Fill missing definitions
    from app.services.candidate_profile import FIELD_DEFINITIONS
    for k, _defn in FIELD_DEFINITIONS.items():
        if k not in fields:
            fields[k] = {"value": None, "confidence": 0.0, "confidence_band": "none", "status": "missing", "review_required": True, "evidence": [], "evidence_quotes": []}

    requirements = cp_service.build_completion_requirements(fields)

    return {
        "profile_id": profile.id,
        "version": profile.version,
        "state": profile.state,
        "completeness": completeness,
        "completion_requirements": requirements,
        "required_fields": cp_service.REQUIRED_APPLICATION_FIELDS,
    }


@router.get("/history")
def get_history(user: CurrentUser, db: DbSession, profile_id: int, path: Optional[str] = None):
    """Get field history — who changed what and when."""
    profile = db.query(CandidateProfile).filter(CandidateProfile.id == profile_id, CandidateProfile.user_id == user.id).first()
    if not profile:
        raise HTTPException(404, {"code": "profile_missing", "message": "Profile not found"})

    query = db.query(ProfileFieldProvenance).filter(ProfileFieldProvenance.profile_id == profile_id, ProfileFieldProvenance.user_id == user.id)
    if path:
        query = query.filter(ProfileFieldProvenance.path == path)
    provenances = query.all()

    history_items: list[dict[str, Any]] = []
    for prov in provenances:
        hist = cp_service.get_field_history(db, user.id, prov.id)
        for h in hist:
            history_items.append({
                "provenance_id": prov.id,
                "path": h.path,
                "previous_value_preview": h.previous_value_preview,
                "new_value_preview": h.new_value_preview,
                "previous_hash": h.previous_value_hash,
                "new_hash": h.new_value_hash,
                "origin_before": h.origin_before,
                "origin_after": h.origin_after,
                "review_status_before": h.review_status_before,
                "review_status_after": h.review_status_after,
                "actor_type": h.actor_type,
                "actor_id": h.actor_id,
                "reason": h.reason,
                "event_id": h.event_id,
                "occurred_at": h.occurred_at.isoformat() if h.occurred_at else None,
            })

    def _history_sort_key(item: dict[str, Any]) -> str:
        occ = item.get("occurred_at")
        return occ if isinstance(occ, str) else ""

    history_items.sort(key=_history_sort_key, reverse=True)
    return {"profile_id": profile_id, "history": history_items}


@router.get("")
def get_current_profile(user: CurrentUser, db: DbSession, persona_id: Optional[int] = None):
    """Get current candidate profile document."""
    profile = cp_service.get_current_profile(db, user.id, persona_id=persona_id)
    if not profile:
        raise HTTPException(404, {"code": "profile_missing", "message": "No current profile"})
    return {
        "id": profile.id,
        "version": profile.version,
        "state": profile.state,
        "is_current": profile.is_current,
        "document": profile.document,
        "completeness": profile.completeness,
        "review": profile.review,
        "source_document_id": profile.source_document_id,
        "source_extraction_id": profile.source_extraction_id,
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


@router.post("/extract")
async def trigger_extraction(request: Request, user: CurrentUser, db: DbSession, body: ExtractRequest):
    """Trigger candidate profile extraction from text or resume document."""
    from app.models.models import ResumeDocument
    from app.services.ai_guardrails import AIUnavailableError as GuardrailAIUnavailable

    text = body.text or ""
    source_doc_id = body.resume_document_id

    if not text and source_doc_id:
        doc = db.query(ResumeDocument).filter(ResumeDocument.id == source_doc_id, ResumeDocument.user_id == user.id).first()
        if not doc:
            raise HTTPException(404, {"code": "resume_missing", "message": "Resume document not found"})
        # Try to get text from profile_snapshot or file
        if doc.profile_snapshot and isinstance(doc.profile_snapshot, dict):
            # Use raw text if available
            text = doc.profile_snapshot.get("raw_text") or ""
        if not text:
            # Try to extract from file if exists
            try:
                from app.services.resume_parser import extract_text
                text = extract_text(doc.filepath) or ""
            except Exception:
                text = ""

    if not text:
        raise HTTPException(400, {"code": "validation_error", "message": "No text provided for extraction"})

    try:
        from app.services.user_settings import get_user_ai_config
        ai_config = get_user_ai_config(db, user.id)
        fields, document, meta = await cp_service.ai_extract_candidate_profile(
            text, ai_config=ai_config, db=db, user_id=user.id, source_document_id=source_doc_id
        )
    except GuardrailAIUnavailable as exc:
        # Safe defaults when AI unavailable
        fields, document, meta = cp_service.safe_default_extraction(text, source_document_id=source_doc_id)
        meta["error"] = str(exc)
        meta["ai_unavailable"] = True
    except Exception as exc:
        # Also safe default on any extraction failure
        fields, document, meta = cp_service.safe_default_extraction(text, source_document_id=source_doc_id)
        meta["error"] = f"{type(exc).__name__}: {exc}"
        meta["ai_unavailable"] = True

    # Persist
    profile = cp_service.persist_candidate_profile(
        db, user.id, fields, document, meta,
        source_document_id=source_doc_id,
        source_extraction_id=None,
        persona_id=body.persona_id,
    )
    db.commit()

    audit.audit(db, "profile.created", user=user, target=str(profile.id), request=request, detail={"source_document_id": source_doc_id})

    provenance = cp_service.list_provenance_for_review(db, user.id, profile.id)
    return cp_service.build_review_response(profile, provenance)
