"""Authentication, session, API-key and multi-tenant isolation tests."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.core.security import hash_password, verify_password
from app.models.models import ApiKey, Job, RefreshToken, User


def test_bootstrap_creates_owner_and_is_single_shot(client):
    first = client.post("/api/auth/bootstrap",
                        json={"email": "a@example.com", "password": "long-enough-password"})
    assert first.status_code == 201
    assert first.json()["access_token"]
    second = client.post("/api/auth/bootstrap",
                         json={"email": "b@example.com", "password": "long-enough-password"})
    assert second.status_code == 409


def test_weak_password_rejected(client):
    response = client.post("/api/auth/bootstrap", json={"email": "a@example.com", "password": "short"})
    assert response.status_code in (400, 422)


def test_login_and_me(client, owner):
    login = client.post("/api/auth/login",
                        json={"email": owner["email"], "password": owner["password"]})
    assert login.status_code == 200, login.text
    # The session payload carries the account so the SPA renders without a second call.
    assert login.json()["user"]["email"] == owner["email"]
    assert login.json()["user"]["is_owner"] is True
    assert "password" not in str(login.json()["user"]).lower()
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == owner["email"]
    assert body["role"] == "owner"
    assert "settings" in body


def test_login_wrong_password_is_401_and_audited(client, owner, db):
    from app.models.models import AuditLog

    response = client.post("/api/auth/login", json={"email": owner["email"], "password": "wrong-password-123"})
    assert response.status_code == 401
    assert db.query(AuditLog).filter(AuditLog.action == "auth.login_failed").count() == 1


def test_unauthenticated_requests_are_rejected(client):
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/resumes").status_code == 401
    assert client.get("/api/settings").status_code == 401
    # public endpoints stay open
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/status").status_code == 200
    assert client.get("/api/account/disclosures").status_code == 200


def test_auth_status_reports_bootstrap_requirement(client):
    assert client.get("/api/auth/status").json()["bootstrap_required"] is True
    client.post("/api/auth/bootstrap", json={"email": "a@example.com", "password": "long-enough-password"})
    assert client.get("/api/auth/status").json()["bootstrap_required"] is False


def test_refresh_tokens_rotate_and_cannot_be_reused(client, owner):
    first = client.post("/api/auth/refresh", json={"refresh_token": owner["refresh_token"]})
    assert first.status_code == 200
    replay = client.post("/api/auth/refresh", json={"refresh_token": owner["refresh_token"]})
    assert replay.status_code == 401, "one-shot refresh tokens must not be replayable"


def test_logout_revokes_refresh_token(client, owner):
    assert client.post("/api/auth/logout", json={"refresh_token": owner["refresh_token"]}).status_code == 200
    assert client.post("/api/auth/refresh", json={"refresh_token": owner["refresh_token"]}).status_code == 401


def test_password_change_invalidates_sessions(client, owner, auth):
    response = client.post("/api/auth/password",
                           json={"current_password": owner["password"], "new_password": "brand-new-password-1"},
                           headers=auth)
    assert response.status_code == 200, response.text
    assert client.post("/api/auth/refresh", json={"refresh_token": owner["refresh_token"]}).status_code == 401
    assert client.post("/api/auth/login", json={"email": owner["email"], "password": "brand-new-password-1"}).status_code == 200
    assert client.post("/api/auth/login", json={"email": owner["email"], "password": owner["password"]}).status_code == 401


def test_api_key_authentication_and_revocation(client, auth):
    created = client.post("/api/auth/api-keys", json={"name": "ci"}, headers=auth)
    assert created.status_code == 201, created.text
    raw_key = created.json()["api_key"]
    key_id = created.json()["id"]
    assert raw_key.startswith("jh_")

    header_auth = client.get("/api/jobs", headers={"Authorization": f"Bearer {raw_key}"})
    assert header_auth.status_code == 200
    x_api_key = client.get("/api/jobs", headers={"X-API-Key": raw_key})
    assert x_api_key.status_code == 200

    assert client.delete(f"/api/auth/api-keys/{key_id}", headers=auth).status_code == 200
    assert client.get("/api/jobs", headers={"X-API-Key": raw_key}).status_code == 401


def test_api_keys_are_hashed_at_rest(client, auth, db):
    raw_key = client.post("/api/auth/api-keys", json={"name": "hash-check"}, headers=auth).json()["api_key"]
    stored = db.query(ApiKey).order_by(ApiKey.id.desc()).first()
    assert stored.key_hash != raw_key
    assert raw_key not in stored.key_hash


def test_tenancy_isolation(client, auth, member_auth, db, owner, member):
    """Two accounts must never see each other's data."""
    owner_user = db.query(User).filter(User.email == owner["email"]).first()
    member_user = db.query(User).filter(User.email == member["email"]).first()

    db.add(Job(user_id=owner_user.id, title="Owner Job", company="OwnerCo", dedupe_key="owner:1",
               description="python"))
    db.add(Job(user_id=member_user.id, title="Member Job", company="MemberCo", dedupe_key="member:1",
               description="python"))
    db.commit()

    owner_jobs = client.get("/api/jobs", headers=auth).json()
    member_jobs = client.get("/api/jobs", headers=member_auth).json()
    assert [j["title"] for j in owner_jobs] == ["Owner Job"]
    assert [j["title"] for j in member_jobs] == ["Member Job"]

    other_job_id = member_jobs[0]["id"]
    assert client.get(f"/api/jobs/{other_job_id}", headers=auth).status_code == 404
    assert client.post(f"/api/jobs/{other_job_id}/apply", json={}, headers=auth).status_code == 404

    # Settings are per-user too.
    client.put("/api/settings", json={"general": {"strict_skeleton": True}}, headers=auth)
    member_settings = client.get("/api/settings", headers=member_auth).json()
    assert member_settings["general"]["strict_skeleton"] is False


def test_expired_access_token_is_rejected(client, owner, db):
    from app.core.security import create_access_token

    user = db.query(User).filter(User.email == owner["email"]).first()
    token, _ = create_access_token(user.id, expires_minutes=-5)
    assert client.get("/api/jobs", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_password_hashing_roundtrip():
    digest = hash_password("a-very-long-passphrase-that-exceeds-bcrypts-limit-" + "x" * 100)
    assert digest.startswith(("bcrypt-sha256$", "pbkdf2-sha256$"))
    assert verify_password("a-very-long-passphrase-that-exceeds-bcrypts-limit-" + "x" * 100, digest)
    assert not verify_password("wrong", digest)
    assert not verify_password("", digest)


def test_registration_can_be_disabled(client, owner, monkeypatch, auth):
    from app.core.config import settings

    monkeypatch.setattr(settings, "allow_registration", False)
    response = client.post("/api/auth/register",
                           json={"email": "nope@example.com", "password": "long-enough-password"})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "registration_disabled"


def test_member_cannot_use_owner_only_ops(client, member_auth):
    response = client.post("/api/ops/queue/recover", headers=member_auth)
    assert response.status_code == 403
