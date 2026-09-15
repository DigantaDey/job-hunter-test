"""Credential vault endpoints (list / export / reveal / delete forever) — monetized."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.api.deps import CurrentUser, DbSession
from app.core import audit
from app.core.entitlements import enforce
from app.core.logging import get_logger
from app.models.models import VaultEntry
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
def export_chrome(request: Request, user: CurrentUser, db: DbSession):
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
def export_apple(request: Request, user: CurrentUser, db: DbSession):
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
