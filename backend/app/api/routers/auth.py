"""Authentication, session and API-key endpoints."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.api.deps import CurrentUser, DbSession, client_ip
from app.core import audit
from app.core.auth import issue_session_tokens
from app.core.config import settings
from app.core.security import (
    api_key_prefix,
    hash_password,
    new_api_key,
    password_problems,
    sha256_hex,
    verify_password,
)
from app.models.models import ApiKey, RefreshToken, User
from app.services.user_settings import grouped

router = APIRouter(prefix="/auth", tags=["auth"])

# Brute-force protection: per-email attempt window (in-process; a shared store is
# the next step for multi-replica deployments).
_attempts: Dict[str, List[float]] = {}
_MAX_ATTEMPTS = 8
_WINDOW_SECONDS = 300


class Credentials(BaseModel):
    email: EmailStr
    # Login keeps the permissive floor: an account created under an older
    # PASSWORD_MIN_LENGTH must still be able to sign in.
    password: str = Field(min_length=8, max_length=256)


class RegisterRequest(Credentials):
    name: str = Field(default="", max_length=200)

    @field_validator("password")
    @classmethod
    def _meets_password_policy(cls, value: str) -> str:
        """Fail with the *configured* policy, not with pydantic's floor of 8."""
        problems = password_problems(value)
        if problems:
            raise ValueError("; ".join(problems))
        return value


class SessionUser(BaseModel):
    """The caller's own account — never contains anything secret."""

    id: int
    email: str
    name: str = ""
    role: str = "member"
    is_owner: bool = False
    is_active: bool = True
    created_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    # Included so the SPA can render the signed-in shell without a second call.
    user: Optional[SessionUser] = None


class ApiKeyCreate(BaseModel):
    name: str = Field(default="default", max_length=120)
    scopes: List[str] = Field(default_factory=list)


def _throttle(email: str) -> None:
    now = time.time()
    window = [t for t in _attempts.get(email.lower(), []) if now - t < _WINDOW_SECONDS]
    if len(window) >= _MAX_ATTEMPTS:
        retry = int(_WINDOW_SECONDS - (now - window[0]))
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            detail={"code": "too_many_attempts", "retry_after": max(1, retry)},
                            headers={"Retry-After": str(max(1, retry))})
    window.append(now)
    _attempts[email.lower()] = window
    if len(_attempts) > 2000:
        _attempts.clear()


@router.get("/status")
def auth_status(db: DbSession):
    """
    Public: tells the SPA whether to show setup, login or register.

    ``registration_open`` is the key the SPA reads; ``allow_registration`` is
    kept so older clients (and scripts) that read the raw setting keep working.
    """
    return {
        "bootstrap_required": db.query(User).count() == 0,
        "auth_required": settings.auth_required,
        "allow_registration": settings.allow_registration,
        "registration_open": settings.allow_registration,
        "password_min_length": settings.password_min_length,
        "environment": settings.environment,
        "app_name": settings.app_name,
        "version": settings.version,
    }


@router.post("/bootstrap", response_model=TokenResponse, status_code=201)
def bootstrap(payload: RegisterRequest, request: Request, db: DbSession):
    """Create the first (owner) account. Only possible while no user exists."""
    if db.query(User).count() > 0:
        raise HTTPException(409, "Bootstrap already completed — sign in instead")
    problems = password_problems(payload.password)
    if problems:
        raise HTTPException(400, {"code": "weak_password", "problems": problems})

    user = User(
        email=payload.email.lower(),
        name=payload.name or payload.email.split("@")[0],
        password_hash=hash_password(payload.password),
        role="owner",
        consents={},
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    audit.audit(db, "auth.bootstrap", user=user, request=request, target=user.email)
    return issue_session_tokens(db, user, user_agent=request.headers.get("user-agent", ""))


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(payload: RegisterRequest, request: Request, db: DbSession):
    if db.query(User).count() == 0:
        raise HTTPException(400, "Use /auth/bootstrap to create the first account")
    if not settings.allow_registration:
        raise HTTPException(403, {"code": "registration_disabled",
                                  "message": "Self-registration is disabled. Ask the owner to invite you."})
    email = payload.email.lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(409, "Email already registered")
    problems = password_problems(payload.password)
    if problems:
        raise HTTPException(400, {"code": "weak_password", "problems": problems})
    user = User(email=email, name=payload.name or email.split("@")[0],
                password_hash=hash_password(payload.password), role="member", consents={})
    db.add(user)
    db.commit()
    db.refresh(user)
    audit.audit(db, "auth.register", user=user, request=request, target=email)
    return issue_session_tokens(db, user, user_agent=request.headers.get("user-agent", ""))


@router.post("/login", response_model=TokenResponse)
def login(payload: Credentials, request: Request, db: DbSession):
    email = payload.email.lower()
    _throttle(email)
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        audit.audit(db, "auth.login_failed", request=request, target=email, ip=client_ip(request))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(403, "Account disabled")
    _attempts.pop(email, None)
    audit.audit(db, "auth.login", user=user, request=request, ip=client_ip(request))
    return issue_session_tokens(db, user, user_agent=request.headers.get("user-agent", ""))


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: Request, db: DbSession, payload: dict = Body(...)):
    raw = str(payload.get("refresh_token") or "")
    if not raw:
        raise HTTPException(400, "refresh_token required")
    row = (
        db.query(RefreshToken)
        .filter(RefreshToken.token_hash == sha256_hex(raw), RefreshToken.revoked_at.is_(None))
        .first()
    )
    if not row or row.expires_at < datetime.utcnow():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token invalid or expired")
    user = db.query(User).filter(User.id == row.user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account unavailable")
    row.revoked_at = datetime.utcnow()  # rotate: one-shot refresh tokens
    db.commit()
    audit.audit(db, "auth.refresh", user=user, request=request)
    return issue_session_tokens(db, user, user_agent=request.headers.get("user-agent", ""))


@router.post("/logout")
def logout(request: Request, db: DbSession, payload: Optional[dict] = Body(None)):
    raw = str((payload or {}).get("refresh_token") or "")
    revoked = 0
    if raw:
        row = db.query(RefreshToken).filter(RefreshToken.token_hash == sha256_hex(raw)).first()
        if row and not row.revoked_at:
            row.revoked_at = datetime.utcnow()
            db.commit()
            revoked = 1
            audit.audit(db, "auth.logout", user_id=row.user_id, request=request)
    return {"ok": True, "revoked": revoked}


@router.get("/me")
def me(user: CurrentUser, db: DbSession):
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "is_active": user.is_active,
        "consents": user.consents or {},
        "last_login_at": user.last_login_at,
        "created_at": user.created_at,
        "settings": grouped(db, user),
    }


@router.post("/password")
def change_password(payload: dict, request: Request, user: CurrentUser, db: DbSession):
    current = str(payload.get("current_password") or "")
    new = str(payload.get("new_password") or "")
    if not verify_password(current, user.password_hash):
        raise HTTPException(403, "Current password is incorrect")
    problems = password_problems(new)
    if problems:
        raise HTTPException(400, {"code": "weak_password", "problems": problems})
    user.password_hash = hash_password(new)
    # Invalidate every device session on password change.
    db.query(RefreshToken).filter(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)).update(
        {RefreshToken.revoked_at: datetime.utcnow()}, synchronize_session=False
    )
    db.commit()
    audit.audit(db, "auth.password_changed", user=user, request=request)
    return {"ok": True, "message": "Password changed — other sessions were signed out"}


# --------------------------------------------------------------------------- #
# API keys (machine access)
# --------------------------------------------------------------------------- #
@router.get("/api-keys")
def list_api_keys(user: CurrentUser, db: DbSession):
    rows = db.query(ApiKey).filter(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc()).all()
    return [
        {"id": r.id, "name": r.name, "prefix": r.prefix, "scopes": r.scopes,
         "created_at": r.created_at, "last_used_at": r.last_used_at,
         "revoked": r.revoked_at is not None,
         # Expose the timestamp too: the UI needs to know *when* a key was
         # revoked, and a bare boolean cannot answer that.
         "revoked_at": r.revoked_at}
        for r in rows
    ]


@router.post("/api-keys", status_code=201)
def create_api_key(payload: ApiKeyCreate, request: Request, user: CurrentUser, db: DbSession):
    raw = new_api_key()
    row = ApiKey(user_id=user.id, name=payload.name, prefix=api_key_prefix(raw),
                 key_hash=sha256_hex(raw), scopes=payload.scopes or ["read", "write"])
    db.add(row)
    db.commit()
    db.refresh(row)
    audit.audit(db, "auth.api_key_created", user=user, target=row.prefix, request=request)
    return {"id": row.id, "name": row.name, "api_key": raw,
            "message": "Store this key now — it cannot be retrieved again"}


@router.delete("/api-keys/{key_id}")
def revoke_api_key(key_id: int, request: Request, user: CurrentUser, db: DbSession):
    row = db.query(ApiKey).filter(ApiKey.id == key_id, ApiKey.user_id == user.id).first()
    if not row:
        raise HTTPException(404, "API key not found")
    row.revoked_at = datetime.utcnow()
    db.commit()
    audit.audit(db, "auth.api_key_revoked", user=user, target=row.prefix, request=request)
    return {"ok": True}
