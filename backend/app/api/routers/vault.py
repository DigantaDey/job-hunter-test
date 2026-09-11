"""Credential vault endpoints (list / export / reveal / delete forever) — monetized."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.api.deps import CurrentUser, DbSession
from app.core import audit
from app.core.entitlements import enforce
from app.models.models import VaultEntry
from app.schemas.schemas import VaultOut
from app.services.vault import (
    delete_all_vault,
    delete_entry,
    export_apple_csv,
    export_chrome_csv,
    list_vault_entries,
    reveal_password,
    save_vault_entry,
)

router = APIRouter(prefix="/vault", tags=["vault"])


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
    return {"id": entry.id, "domain": entry.domain, "username": entry.username, "password": reveal_password(entry)}


@router.get("/export/chrome")
def export_chrome(request: Request, user: CurrentUser, db: DbSession):
    entries = list_vault_entries(db, user.id)
    audit.audit(db, "vault.exported", user=user, target="chrome", detail={"entries": len(entries)}, request=request)
    return PlainTextResponse(
        export_chrome_csv(entries),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=jobhunter_chrome_passwords.csv", "Cache-Control": "no-store"},
    )


@router.get("/export/apple")
def export_apple(request: Request, user: CurrentUser, db: DbSession):
    entries = list_vault_entries(db, user.id)
    audit.audit(db, "vault.exported", user=user, target="apple", detail={"entries": len(entries)}, request=request)
    return PlainTextResponse(
        export_apple_csv(entries),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=jobhunter_apple_passwords.csv", "Cache-Control": "no-store"},
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
