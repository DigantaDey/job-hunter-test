"""Vault encryption, tenant isolation, exports, audit trail and re-keying."""
from __future__ import annotations

import pytest
from cryptography.fernet import InvalidToken

from app.core.security import (
    decrypt_secret,
    derive_key,
    encrypt_secret,
    needs_reencrypt,
    sha256_hex,
)
from app.models.models import AuditLog, VaultEntry
from app.services.vault import credential_for_application, reveal_password


def test_per_user_keys_are_isolated():
    token = encrypt_secret("hunter2-strong-password", "user:1:vault")
    assert decrypt_secret(token, "user:1:vault") == "hunter2-strong-password"
    with pytest.raises(InvalidToken):
        decrypt_secret(token, "user:2:vault", allow_legacy=False)


def test_legacy_global_key_values_still_readable_and_rekeyed(db, owner):
    from app.models.models import User

    user = db.query(User).order_by(User.id).first()
    legacy_ciphertext = encrypt_secret("legacy-password", "global")
    entry = VaultEntry(user_id=user.id, domain="legacy.example.com", username="legacy@example.com",
                       password_enc=legacy_ciphertext)
    db.add(entry)
    db.commit()

    assert reveal_password(entry) == "legacy-password"
    assert needs_reencrypt(entry.password_enc, f"user:{user.id}:vault") is False
    db.commit()
    assert decrypt_secret(entry.password_enc, f"user:{user.id}:vault") == "legacy-password"


def test_derived_keys_are_deterministic_and_scoped():
    assert derive_key("master", "jobhunter:user:1:vault:v1") == derive_key("master", "jobhunter:user:1:vault:v1")
    assert derive_key("master", "jobhunter:user:1:vault:v1") != derive_key("master", "jobhunter:user:2:vault:v1")


def test_vault_api_flow_is_audited(client, auth, db):
    created = client.post("/api/vault", json={"domain": "jobs.lever.co", "username": "me@example.com",
                                              "password": "a-strong-password"}, headers=auth)
    assert created.status_code == 201, created.text
    entry_id = created.json()["id"]

    listing = client.get("/api/vault", headers=auth).json()
    assert len(listing) == 1
    assert "password" not in listing[0]

    revealed = client.get(f"/api/vault/{entry_id}/reveal", headers=auth).json()
    assert revealed["password"] == "a-strong-password"

    chrome = client.get("/api/vault/export/chrome", headers=auth)
    assert chrome.status_code == 200
    assert "name,url,username,password" in chrome.text
    assert "me@example.com" in chrome.text
    apple = client.get("/api/vault/export/apple", headers=auth)
    assert "OTPAuth" in apple.text

    actions = {row.action for row in db.query(AuditLog).all()}
    assert {"vault.credential_created", "vault.viewed", "vault.exported"} <= actions


def test_vault_is_tenant_scoped(client, auth, member_auth):
    client.post("/api/vault", json={"domain": "jobs.lever.co", "username": "owner@example.com",
                                    "password": "owner-password-1"}, headers=auth)
    assert client.get("/api/vault", headers=member_auth).json() == []
    assert "owner@example.com" not in client.get("/api/vault/export/chrome", headers=member_auth).text


def test_delete_forever_removes_everything(client, auth, db):
    client.post("/api/vault", json={"domain": "a.example.com", "username": "u@example.com",
                                    "password": "password-1234"}, headers=auth)
    client.post("/api/vault", json={"domain": "b.example.com", "username": "u@example.com",
                                    "password": "password-1234"}, headers=auth)
    response = client.delete("/api/vault", headers=auth)
    assert response.json()["deleted"] == 2
    assert db.query(VaultEntry).count() == 0
    assert client.get("/api/vault", headers=auth).json() == []
    assert db.query(AuditLog).filter(AuditLog.action == "vault.deleted").count() >= 1


def test_credential_creation_prefers_profile_email(db, client, auth, uploaded_resume):
    from app.models.models import User

    user = db.query(User).order_by(User.id).first()
    credential, created = credential_for_application(db, user_id=user.id, domain="boards.greenhouse.io",
                                                     company="Acme")
    assert created is True
    assert credential["username"] == "test.candidate@example.com"
    assert len(credential["password"]) >= 16

    again, created_again = credential_for_application(db, user_id=user.id, domain="boards.greenhouse.io",
                                                     company="Acme")
    assert created_again is False
    assert again["username"] == credential["username"]


def test_ciphertexts_differ_for_same_password_across_users():
    first = encrypt_secret("identical-password", "user:1:vault")
    second = encrypt_secret("identical-password", "user:2:vault")
    assert first != second


def test_api_key_hash_is_not_reversible():
    assert sha256_hex("jh_abc") != "jh_abc"
    assert len(sha256_hex("jh_abc")) == 64


def test_security_headers_present(client):
    response = client.get("/api/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in response.headers
    assert response.headers.get("X-Request-ID")


def test_request_id_is_echoed(client):
    response = client.get("/api/health", headers={"X-Request-ID": "trace-me-123"})
    assert response.headers["X-Request-ID"] == "trace-me-123"
