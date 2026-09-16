"""
User personas — one job-search track each (Data Analyst, Data Scientist, …).

Every persona drives its own discovery → scoring → resume → outreach funnel and
accumulates its own memory, so the system gets to know the user per track
instead of averaging everything into one generic profile.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession, get_owned_profile, invalidate_context, search_context
from app.core import audit
from app.core.entitlements import enforce
from app.core.logging import get_logger
from app.services import persona as persona_service

router = APIRouter(prefix="/personas", tags=["personas"])
log = get_logger("app.personas")


class PersonaCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    target_role: str = Field(default="", max_length=200)
    keywords: Optional[List[str]] = None
    industries: Optional[List[str]] = None
    roles: Optional[List[str]] = None
    locations: Optional[List[str]] = None
    seniority: Optional[str] = None
    preferences: Optional[Dict[str, Any]] = None


class PersonaUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    target_role: Optional[str] = Field(default=None, max_length=200)
    is_active: Optional[bool] = None
    keywords: Optional[List[str]] = None
    industries: Optional[List[str]] = None
    roles: Optional[List[str]] = None
    locations: Optional[List[str]] = None
    funding_focus: Optional[List[str]] = None
    seniority: Optional[str] = None
    preferences: Optional[Dict[str, Any]] = None


def _persona_or_404(db, user_id: int, persona_id: int):
    row = persona_service.get_persona(db, user_id, persona_id)
    if not row:
        raise HTTPException(404, "Persona not found")
    return row


def _context_payload(payload) -> Dict[str, Any]:
    context: Dict[str, Any] = {}
    for key in ("keywords", "industries", "roles", "locations", "funding_focus", "seniority"):
        value = getattr(payload, key, None)
        if value is not None:
            context[key] = value
    return context


@router.get("")
def list_personas(user: CurrentUser, db: DbSession):
    """All tracks for this user, with memory + portrait state."""
    rows = persona_service.list_personas(db, user.id)
    if not rows:
        # First visit after a resume upload: seed the default track from the
        # profile so the rest of the product always has a persona to bind to.
        profile = get_owned_profile(db, user.id)
        seeded = persona_service.ensure_default_persona(db, user.id, (profile.data if profile else {}) or {})
        rows = persona_service.list_personas(db, user.id) if seeded else []
    return {
        "personas": [persona_service.to_dict(row) for row in rows],
        "active_id": next((r.id for r in rows if r.is_active), rows[0].id if rows else None),
    }


@router.post("", status_code=201)
def create_persona(payload: PersonaCreate, request: Request, user: CurrentUser, db: DbSession):
    profile = get_owned_profile(db, user.id)
    try:
        row = persona_service.create_persona(
            db, user.id,
            name=payload.name,
            target_role=payload.target_role,
            search_context=_context_payload(payload),
            preferences=payload.preferences or {},
            source_resume_id=(profile.master_resume_id if profile else None),
        )
    except persona_service.PersonaExistsError as exc:
        raise HTTPException(409, f"You already have a track called '{exc.name}'. "
                                 "Open it, or pick a different name.") from exc
    persona_service.record_signal(db, user.id, row.id, "keyword_added",
                                  {"keywords": payload.keywords or [], "note": f"track created: {row.name}"})
    invalidate_context(user.id)
    audit.audit(db, "persona.created", user=user, target=row.name,
                detail={"persona_id": row.id, "target_role": row.target_role}, request=request)
    return persona_service.to_dict(row)


@router.get("/suggestions")
async def suggestions(user: CurrentUser, db: DbSession):
    """AI-suggested additional tracks the resume already supports."""
    profile = get_owned_profile(db, user.id)
    if not profile:
        raise HTTPException(400, "Upload a master resume first")
    tracks = await persona_service.suggest_tracks(db, user, profile.data or {})
    existing = {r.name.lower() for r in persona_service.list_personas(db, user.id)}
    return {"tracks": [t for t in tracks if t["name"].lower() not in existing]}


@router.get("/{persona_id}")
def get_persona(persona_id: int, user: CurrentUser, db: DbSession):
    row = _persona_or_404(db, user.id, persona_id)
    return persona_service.to_dict(row)


@router.put("/{persona_id}")
def update_persona(persona_id: int, payload: PersonaUpdate, request: Request, user: CurrentUser, db: DbSession):
    row = _persona_or_404(db, user.id, persona_id)
    body: Dict[str, Any] = {}
    for key in ("name", "target_role", "is_active", "preferences"):
        value = getattr(payload, key, None)
        if value is not None:
            body[key] = value
    context = _context_payload(payload)
    if context:
        body["search_context"] = context
    updated = persona_service.update_persona(db, user.id, row.id, body)
    persona_service.record_signal(db, user.id, row.id, "preference_set",
                                  {"changed": sorted(body.keys()), **(payload.preferences or {})})
    invalidate_context(user.id)
    audit.audit(db, "persona.updated", user=user, target=updated.name,
                detail={"persona_id": updated.id, "changed": sorted(body.keys())}, request=request)
    return persona_service.to_dict(updated)


@router.post("/{persona_id}/activate")
def activate_persona(persona_id: int, request: Request, user: CurrentUser, db: DbSession):
    row = persona_service.set_active_persona(db, user.id, persona_id)
    invalidate_context(user.id)
    audit.audit(db, "persona.activated", user=user, target=row.name,
                detail={"persona_id": row.id}, request=request)
    return persona_service.to_dict(row)


@router.post("/{persona_id}/reflect")
async def reflect(persona_id: int, request: Request, user: CurrentUser, db: DbSession, force: bool = False):
    """
    Rebuild the persona portrait from observed evidence.

    AI-required: when the model is unreachable this returns 503 with the exact
    reason instead of inventing a portrait from thin air.
    """
    enforce(db, user.id, "ai_operations_per_month")
    row = _persona_or_404(db, user.id, persona_id)
    if not force and not persona_service.portrait_is_stale(row):
        return {"portrait": persona_service.portrait_dict(row), "cached": True,
                "portrait_at": row.portrait_at.isoformat() if row.portrait_at else None}
    result = await persona_service.build_portrait(db, user, row, force=force)
    audit.audit(db, "persona.reflected", user=user, target=row.name,
                detail={"persona_id": row.id, "signals": row.portrait_evidence.get("signals_used")},
                request=request)
    return {"cached": False, **result}


@router.post("/{persona_id}/signals")
def add_signal(persona_id: int, payload: dict, user: CurrentUser, db: DbSession):
    """Record one behavioural signal (used by the UI's explicit feedback)."""
    row = _persona_or_404(db, user.id, persona_id)
    kind = str(payload.get("kind") or "preference_set")
    if kind not in persona_service.SIGNAL_KINDS:
        raise HTTPException(400, f"Unknown signal kind '{kind}'")
    data = {k: v for k, v in (payload.get("data") or {}).items() if isinstance(v, (str, int, float, bool, list))}
    persona_service.record_signal(db, user.id, row.id, kind, data)
    return {"ok": True, "memory": persona_service.memory_summary(row)}


@router.get("/{persona_id}/context")
async def persona_context(persona_id: int, user: CurrentUser, db: DbSession):
    """The effective search context this track drives discovery with."""
    row = _persona_or_404(db, user.id, persona_id)
    base = await search_context(db, user.id)
    return persona_service.context_for_persona(base, row)


@router.delete("/{persona_id}")
def delete_persona(persona_id: int, request: Request, user: CurrentUser, db: DbSession):
    row = _persona_or_404(db, user.id, persona_id)
    # Jobs/resumes/emails that were produced under this track are detached by the
    # service; anything the schema refuses to detach is reported, not forced.
    try:
        persona_service.delete_persona(db, user.id, persona_id)
    except persona_service.PersonaInUseError as exc:
        raise HTTPException(409, {"code": "persona_in_use",
                                  "message": f"The track '{exc.name}' is still referenced by "
                                             f"{', '.join(exc.blockers)} and cannot be deleted yet.",
                                  "blockers": exc.blockers}) from exc
    invalidate_context(user.id)
    audit.audit(db, "persona.deleted", user=user, target=row.name, request=request)
    return {"ok": True}
