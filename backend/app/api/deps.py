"""Shared FastAPI dependencies and tenant-scoped helpers."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Dict, Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.auth import CurrentUser, get_current_user, require_consent  # noqa: F401  (re-export)
from app.core.middleware import resolve_client_ip
from app.db import get_db
from app.models.models import Job, Profile

DbSession = Annotated[Session, Depends(get_db)]

# In-process search-context cache, keyed per user.
_context_cache: Dict[int, Dict[str, Any]] = {}


def invalidate_context(user_id: int) -> None:
    _context_cache.pop(user_id, None)


async def search_context(
    db: Session,
    user_id: int,
    *,
    extra_context: str = "",
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """
    AI-extracted search context (keywords/roles/industries/funding focus) built
    from the user's profile, master resume text and free-form context.

    Cached per user. AI is a hard dependency: when the model is unavailable
    this raises (sync callers → pausable 503, queued callers → the item is
    paused and re-run when the provider is back) — a stale or mined-only
    context is never served as the extraction.
    """
    from app.services.keyword_extractor import ai_extract_context

    profile = db.query(Profile).filter(Profile.user_id == user_id).order_by(Profile.created_at.desc()).first()
    profile_id = profile.id if profile else None
    cached = _context_cache.get(user_id)
    if (
        not force_refresh
        and cached
        and cached.get("profile_id") == profile_id
        and cached.get("extra_context", "") == (extra_context or "")
    ):
        return cached

    profile_data = (profile.data if profile else {}) or {}
    resume_text = profile_data.get("raw_text", "") or ""
    context = await ai_extract_context(profile_data, resume_text=resume_text, extra_context=extra_context,
                                       db=db, user_id=user_id)
    context["profile_id"] = profile_id
    context["generated_at"] = datetime.utcnow().isoformat()
    _context_cache[user_id] = context
    context["extra_context"] = extra_context or ""
    return context


def get_owned_job(db: Session, user_id: int, job_id: int) -> Job:
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == user_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    return job


def get_owned_profile(db: Session, user_id: int) -> Optional[Profile]:
    return db.query(Profile).filter(Profile.user_id == user_id).order_by(Profile.created_at.desc()).first()


def client_ip(request: Request) -> str:
    """
    The client IP this request is attributed to (audit rows, login failures).

    Delegated to :func:`app.core.middleware.resolve_client_ip`, which believes
    ``X-Forwarded-For`` only from a configured proxy and then takes the rightmost
    hop that is not itself a proxy. Taking the *leftmost* entry unconditionally —
    what this used to do — let any direct client pick the address written into
    the audit trail and used for per-IP throttling.
    """
    return resolve_client_ip(request.scope)


__all__ = [
    "DbSession",
    "CurrentUser",
    "get_current_user",
    "require_consent",
    "search_context",
    "invalidate_context",
    "get_owned_job",
    "get_owned_profile",
    "client_ip",
]
