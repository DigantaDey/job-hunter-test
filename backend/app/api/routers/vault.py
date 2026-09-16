"""Credential vault endpoints (list / export / reveal / delete forever) — monetized."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.api.deps import CurrentUser, DbSession
from app.core import audit
from app.core.config import settings
from app.core.entitlements import enforce
from app.core.logging import get_logger, user_id_var
from app.core.security import constant_time_equals
from app.models.models import User, VaultEntry
from app.schemas.schemas import VaultOut
from app.services.vault import (
    VaultDecryptionError,
    VaultExport,
    delete_all_vault,
    delete_entry,
    export_apple_csv,
    export_chrome_csv,
    list_vault_entries,
    reveal_password,
    save_vault_entry,
)

log = get_logger("app.vault.api")

router = APIRouter(prefix="/vault", tags=["vault"])

#: Response header carrying how many entries had to be left out of a CSV
#: because their ciphertext could not be decrypted. A download cannot render an
#: error, so this (and the audit row) is how a silently short export says so.
UNDECRYPTABLE_HEADER = "X-Vault-Undecryptable"


def _undecryptable_headers(result: VaultExport) -> Dict[str, str]:
    return {UNDECRYPTABLE_HEADER: str(len(result.undecryptable))}


def _audit_export(
    db: DbSession, user: Any, request: Request, flavour: str, entries: List[VaultEntry], result: VaultExport
) -> None:
    """Record an export, including the entries that could not be read."""
    detail: Dict[str, Any] = {"entries": len(entries), "written": len(entries) - len(result.undecryptable)}
    if result.undecryptable:
        detail["undecryptable"] = list(result.undecryptable)
        log.error(
            "vault %s export omitted %s entr%s that cannot be decrypted with the configured "
            "VAULT_KEY (entry ids: %s)",
            flavour, len(result.undecryptable),
            "y" if len(result.undecryptable) == 1 else "ies",
            ", ".join(str(i) for i in result.undecryptable),
        )
    audit.audit(db, "vault.exported", user=user, target=flavour, detail=detail, request=request)


# --------------------------------------------------------------------------- #
# Signed CSV download URLs (same pattern as resume downloads)
#
# ``GET /api/vault/export/chrome`` and ``/apple`` answer the CSV body directly,
# but the SPA's download buttons are plain ``<a href>`` links, which carry no
# Authorization header. Fetching a short-lived signed URL first keeps the
# click authenticated while still letting the browser trigger the native
# download. The CSV endpoints themselves only gain an optional signed ``token``
# query parameter the browser navigation can present.
# --------------------------------------------------------------------------- #
EXPORT_TOKEN_TTL_SECONDS = 900


def _sign_export(user_id: int, flavour: str, expires: int) -> str:
    message = f"{user_id}:{flavour}:{expires}".encode()
    return hmac.new(settings.secret_key.encode(), message, hashlib.sha256).hexdigest()


def create_export_token(user_id: int, flavour: str,
                        ttl: int = EXPORT_TOKEN_TTL_SECONDS) -> str:
    """``expires.user_id.signature`` — the user id stays in the clear so a
    token-only request can be attributed to its owner, while the signature
    (over user id + flavour + expiry, keyed by the app secret) makes it tamper
    evident and unusable for any other user or format."""
    expires = int(time.time()) + max(30, ttl)
    return f"{expires}.{user_id}.{_sign_export(user_id, flavour, expires)}"


def verify_export_token(token: Optional[str], flavour: str) -> tuple[bool, Optional[int]]:
    """Return (valid, user_id) for a signed export token, or (False, None)."""
    try:
        raw_expires, raw_user_id, signature = str(token).split(".", 2)
        expires = int(raw_expires)
        user_id = int(raw_user_id)
    except (ValueError, AttributeError):
        return False, None
    if expires < int(time.time()):
        return False, None
    expected = _sign_export(user_id, flavour, expires)
    return constant_time_equals(expected, signature), user_id


@router.get("/export/{flavour}/url")
def export_url(flavour: str, user: CurrentUser):
    """Issue a short-lived signed URL the CSV export endpoint will accept."""
    if flavour not in ("chrome", "apple"):
        raise HTTPException(400, "flavour must be 'chrome' or 'apple'")
    token = create_export_token(user.id, flavour)
    return {
        "url": f"/api/vault/export/{flavour}?token={token}",
        "filename": ("jobhunter_chrome_passwords.csv" if flavour == "chrome"
                     else "jobhunter_apple_passwords.csv"),
        "expires_in": EXPORT_TOKEN_TTL_SECONDS,
    }


def _resolve_exporter(request: Request, db: DbSession, flavour: str, token: Optional[str]) -> Any:
    """Bearer token first (API clients / dev mode); otherwise a valid signed
    token. The SPA's navigation sends no Authorization header, so the signed
    token is the only credential a browser download presents. When *both* are
    present they must agree — a bearer for one account combined with a signed
    token minted for another is refused rather than silently using whichever
    was checked first."""
    from fastapi.security import HTTPAuthorizationCredentials

    from app.core.auth import get_current_user

    header = request.headers.get("authorization") or ""
    credentials = None
    if header.lower().startswith("bearer "):
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=header[7:].strip())

    bearer_user: Any = None
    try:
        user = get_current_user(request=request, credentials=credentials, db=db)
        if user:
            bearer_user = user
    except HTTPException:
        pass

    token_user: Any = None
    if token:
        valid, user_id = verify_export_token(token, flavour)
        if valid and user_id is not None:
            token_user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()

    if bearer_user is not None and token_user is not None:
        if bearer_user.id != token_user.id:
            raise HTTPException(401, "Credential mismatch")
        return bearer_user
    resolved = token_user or bearer_user
    if resolved is not None:
        request.state.user_id = resolved.id
        user_id_var.set(int(resolved.id))
        return resolved
    raise HTTPException(401, "Not authenticated")


@router.get("", response_model=list[VaultOut])
def get_vault(request: Request, user: CurrentUser, db: DbSession):
    audit.audit(db, "vault.viewed", user=user, target="list", request=request)
    return list_vault_entries(db, user.id)


@router.post("", status_code=201)
def create_entry(request: Request, user: CurrentUser, db: DbSession, payload: dict):
    enforce(db, user.id, "vault_entries_max")
    domain = str(payload.get("domain") or "").strip()
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not domain or not username or len(password) < 8:
        raise HTTPException(400, "domain, username and a password of at least 8 characters are required")
    entry = save_vault_entry(db, user.id, domain, username, password, origin="manual")
    audit.audit(db, "vault.credential_created", user=user, target=domain, detail={"entry_id": entry.id, "origin": "manual"}, request=request)
    return {"id": entry.id, "domain": entry.domain, "username": entry.username}


@router.get("/{entry_id}/reveal")
def reveal(entry_id: int, request: Request, user: CurrentUser, db: DbSession):
    entry = db.query(VaultEntry).filter(VaultEntry.id == entry_id, VaultEntry.user_id == user.id).first()
    if not entry:
        raise HTTPException(404, "Entry not found")
    audit.audit(db, "vault.viewed", user=user, target=entry.domain, detail={"entry_id": entry.id, "revealed": True}, request=request)
    try:
        password = reveal_password(entry, db=db)
    except VaultDecryptionError as exc:
        # A 500, not an empty password: the entry exists and belongs to the
        # caller, so this is the server failing to read its own data (almost
        # always a rotated VAULT_KEY). Saying so is the difference between an
        # operator seeing the breakage and a user re-typing every credential.
        audit.audit(db, "vault.reveal_failed", user=user, target=entry.domain,
                    detail={"entry_id": entry.id, "reason": exc.reason}, request=request)
        raise HTTPException(
            500,
            detail={
                "code": "vault_entry_undecryptable",
                "entry_id": entry.id,
                "message": ("This credential cannot be decrypted with the server's current "
                            "VAULT_KEY. It was likely encrypted under a previous key; restore "
                            "that key or re-enter the credential."),
            },
        ) from exc
    return {"id": entry.id, "domain": entry.domain, "username": entry.username, "password": password}


@router.get("/export/chrome")
def export_chrome(request: Request, db: DbSession, token: Optional[str] = None):
    user = _resolve_exporter(request, db, "chrome", token)
    entries = list_vault_entries(db, user.id)
    result = export_chrome_csv(entries)
    _audit_export(db, user, request, "chrome", entries, result)
    return PlainTextResponse(
        result.text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=jobhunter_chrome_passwords.csv",
                 "Cache-Control": "no-store", **_undecryptable_headers(result)},
    )


@router.get("/export/apple")
def export_apple(request: Request, db: DbSession, token: Optional[str] = None):
    user = _resolve_exporter(request, db, "apple", token)
    entries = list_vault_entries(db, user.id)
    result = export_apple_csv(entries)
    _audit_export(db, user, request, "apple", entries, result)
    return PlainTextResponse(
        result.text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=jobhunter_apple_passwords.csv",
                 "Cache-Control": "no-store", **_undecryptable_headers(result)},
    )


@router.delete("/{entry_id}")
def delete_single(entry_id: int, request: Request, user: CurrentUser, db: DbSession):
    if not delete_entry(db, user.id, entry_id):
        raise HTTPException(404, "Entry not found")
    audit.audit(db, "vault.deleted", user=user, target=f"entry:{entry_id}", request=request)
    return {"ok": True}


@router.delete("")
def delete_vault(request: Request, user: CurrentUser, db: DbSession):
    count = delete_all_vault(db, user.id)
    audit.audit(db, "vault.deleted", user=user, target="all", detail={"entries": count}, request=request)
    return {"ok": True, "deleted": count}
