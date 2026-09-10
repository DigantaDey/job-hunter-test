"""Shared FastAPI dependencies and tenant-scoped helpers."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Dict, Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.auth import CurrentUser, get_current_user, require_consent  # noqa: F401  (re-export)
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

    Cached per user; the heuristic extractor guarantees a non-empty result even
    with no AI configured.
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
    context = await ai_extract_context(profile_data, resume_text=resume_text, extra_context=extra_context)
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
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


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
