"""
Authentication & authorisation dependencies.

Three credential types are supported:

* ``Authorization: Bearer <JWT>`` — the SPA session (short-lived access token).
* ``Authorization: Bearer jh_…`` / ``X-API-Key: jh_…`` — machine API keys.
* ``AUTH_REQUIRED=false`` — explicit local-dev escape hatch that binds requests
  to the first active user. Refused outright in production (config validation).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Callable, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWTError as JWTError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import user_id_var
from app.core.security import api_key_prefix, decode_token, sha256_hex
from app.db import get_db
from app.models.models import ApiKey, User

bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token or JobHunter API key")

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def _active_user(db: Session, user_id: int) -> Optional[User]:
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_active:
        return None
    return user


def _user_from_api_key(db: Session, raw_key: str) -> Optional[ApiKey]:
    row = (
        db.query(ApiKey)
        .filter(ApiKey.key_hash == sha256_hex(raw_key), ApiKey.revoked_at.is_(None))
        .first()
    )
    if not row:
        return None
    row.last_used_at = datetime.utcnow()
    db.commit()
    return row


_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SCOPE_DENIED = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail={"code": "insufficient_scope",
            "message": "This API key does not allow write operations — create a key with the 'write' scope."},
)


def _api_key_allows(method: str, row: ApiKey) -> bool:
    """
    Enforce the scopes stored on the key.

    Scopes are recorded when a key is created, so honouring them is what makes a
    read-only machine credential read-only. Keys created before scopes existed
    (empty list) stay unrestricted rather than silently breaking clients.
    """
    scopes = {str(scope).strip().lower() for scope in (row.scopes or []) if str(scope).strip()}
    if not scopes or "admin" in scopes:
        return True
    required = "write" if method.upper() in _WRITE_METHODS else "read"
    return required in scopes


def get_current_user(
    request: Request,
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)] = None,
    db: Session = Depends(get_db),
) -> User:
    header_key = request.headers.get("x-api-key")

    if credentials and credentials.credentials.startswith("jh_"):
        row = _user_from_api_key(db, credentials.credentials)
        if row:
            user = _active_user(db, row.user_id)
            if user and not _api_key_allows(request.method, row):
                raise _SCOPE_DENIED
            if user:
                user_id_var.set(int(user.id))
                request.state.user_id = user.id
                return user
        raise _UNAUTHENTICATED

    if header_key and header_key.startswith("jh_"):
        row = _user_from_api_key(db, header_key)
        if row:
            user = _active_user(db, row.user_id)
            if user and not _api_key_allows(request.method, row):
                raise _SCOPE_DENIED
            if user:
                user_id_var.set(int(user.id))
                request.state.user_id = user.id
                return user
        raise _UNAUTHENTICATED

    if credentials and credentials.credentials:
        try:
            payload = decode_token(credentials.credentials)
        except JWTError as err:
            raise _UNAUTHENTICATED from err
        if payload.get("typ") != "access":
            raise _UNAUTHENTICATED
        user = _active_user(db, int(payload.get("sub", 0)))
        if not user:
            raise _UNAUTHENTICATED
        user_id_var.set(int(user.id))
        request.state.user_id = user.id
        return user

    if not settings.auth_required:
        # Explicit single-user/local mode (never allowed in production).
        user = db.query(User).filter(User.is_active.is_(True)).order_by(User.id).first()
        if user:
            request.state.user_id = user.id
            user_id_var.set(int(user.id))
            return user
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="No account exists yet — create the owner account via POST /api/auth/bootstrap",
        )

    raise _UNAUTHENTICATED


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_consent(kind: str) -> Callable[..., User]:
    """
    Guard for third-party-impacting actions.

    ``kind`` is one of ``terms``, ``automation`` (browser autofill / auto-apply)
    or ``outreach`` (cold email). Returns a dependency that 403s with an
    actionable code when the user has not accepted the matching disclosure.
    """

    def _dep(user: CurrentUser) -> User:
        consents = user.consents or {}
        if not consents.get(f"{kind}_accepted_at"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "consent_required",
                    "consent": kind,
                    "message": (
                        f"Accept the '{kind}' disclosure to use this feature "
                        f"(POST /api/account/consent with {{\"{kind}\": true}})."
                    ),
                },
            )
        return user

    return _dep


RequireConsent = require_consent


def require_owner(user: CurrentUser) -> User:
    if (user.role or "").lower() != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner role required")
    return user


def issue_session_tokens(db: Session, user: User, *, user_agent: str = "") -> dict:
    """Create an access token + a persisted (hashed) refresh token."""
    from app.core.security import new_refresh_token
    from app.models.models import RefreshToken

    access_token, expires_in = create_access_token(user)
    refresh_raw = new_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=sha256_hex(refresh_raw),
            expires_at=datetime.utcnow() + timedelta(days=settings.refresh_token_days),
            user_agent=(user_agent or "")[:300],
        )
    )
    user.last_login_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    return {
        "access_token": access_token,
        "refresh_token": refresh_raw,
        "token_type": "bearer",
        "expires_in": expires_in,
        # Sent inline so the SPA can render immediately without a second round trip.
        "user": user_payload(user),
    }


def user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name or "",
        "role": user.role or "member",
        "is_owner": (user.role or "") == "owner",
        "is_active": bool(user.is_active),
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
    }


def create_access_token(user: User) -> tuple[str, int]:
    from app.core.security import create_access_token as _create

    return _create(user.id, email=user.email, role=user.role or "member")


def api_key_fingerprint(raw_key: str) -> str:
    return api_key_prefix(raw_key)
