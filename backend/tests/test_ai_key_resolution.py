"""Regression tests for the "invalid_api_key / AI offline after adding a key" bug.

Root causes fixed:
1. Real AI calls (scoring, parsing, emails, …) resolved config from *env only*
   — the per-user key saved in Settings → AI API was ignored, so every
   pipeline silently fell back to heuristics ("AI still offline").
2. The status probe equated a 401/403 on ``GET /models`` with an invalid key,
   even when the provider only restricts the models listing (or doesn't
   implement it) and chat completions work fine.
3. A stored key that can no longer be decrypted silently fell back to the env
   key while the UI kept claiming a key was configured.
4. Per-workflow override keys were stored in plaintext and shared globally
   across users.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List

import pytest

VALID_KEY = "sk-repro-valid-key-123"


class FakeOpenAIHandler(BaseHTTPRequestHandler):
    """OpenAI-compatible provider with configurable /models behaviour."""

    behavior: Dict[str, Any] = {"models_status": 200, "auth_log": []}

    def log_message(self, *a):  # silence
        pass

    def _authed(self) -> bool:
        return self.headers.get("Authorization", "") == f"Bearer {VALID_KEY}"

    def do_GET(self):
        self.behavior["auth_log"].append(("GET", self.path, self.headers.get("Authorization")))
        if self.path.endswith("/models"):
            status = self.behavior["models_status"] if self._authed() else 401
            body = json.dumps({"data": [{"id": "gpt-4o-mini"}]} if status == 200 else
                              {"error": {"message": "models listing not allowed"}}).encode()
            self.send_response(status)
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.behavior["auth_log"].append(("POST", self.path, self.headers.get("Authorization")))
        if not self._authed():
            body = json.dumps({"error": {"message": "Incorrect API key provided"}}).encode()
            self.send_response(401)
        else:
            body = json.dumps({
                "choices": [{"message": {"content": json.dumps({
                    # Schema-valid for the scoring contract: the guardrail now
                    # requires a substantive reason, grounded strengths, evidence
                    # and a recommendation that matches the score band.
                    "score": 88,
                    "reason": "Strong python overlap with the core requirements of this role.",
                    "missing_skills": [], "strengths": ["python"],
                    "breakdown": {}, "recommendation": "HIGH PRIORITY",
                    "recommendation_reason": "core stack matches",
                    "evidence": ["python"]})}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def provider():
    FakeOpenAIHandler.behavior = {"models_status": 200, "auth_log": []}
    server = HTTPServer(("127.0.0.1", 0), FakeOpenAIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


@pytest.fixture()
def auth(client):
    r = client.post("/api/auth/bootstrap",
                    json={"email": "owner@example.com", "password": "LongPassphrase123!", "name": "Owner"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _owner_id(db):
    from app.models.models import User

    return db.query(User).filter(User.email == "owner@example.com").first().id


def _save_key(client, auth, provider):
    r = client.put("/api/settings", json={
        "ai": {"base_url": provider, "model": "gpt-4o-mini", "rpm": 60, "api_key": VALID_KEY}
    }, headers=auth)
    assert r.status_code == 200, r.text


def test_saved_key_powers_real_ai_calls_with_ambient_user(client, auth, provider, db):
    """The user's saved key must reach actual pipeline AI calls (defect 1).

    Pipelines don't thread db/user_id; the AI client now picks up the ambient
    request/worker user (``user_id_var``) — exactly what the worker's
    ``LogContext`` provides.
    """
    import asyncio

    _save_key(client, auth, provider)

    from app.core.logging import LogContext
    from app.services.ai_client import chat_completion
    from app.services.scoring import ai_score_detailed

    with LogContext(request_id="test", user_id=_owner_id(db)):
        detail = asyncio.run(ai_score_detailed({"skills": ["python"]}, "Looking for a python dev", ai_config=None))

    assert detail.get("source") != "heuristic", "AI call fell back to heuristic — the user's key was not used!"
    assert detail.get("overall") == 88
    # The provider must have received the *user's* key, not an env fallback.
    posts = [entry for entry in FakeOpenAIHandler.behavior["auth_log"] if entry[0] == "POST"]
    assert posts and posts[-1][2] == f"Bearer {VALID_KEY}"

    # chat_completion itself (no db/user_id args) resolves the same way.
    with LogContext(request_id="test", user_id=_owner_id(db)):
        result = asyncio.run(chat_completion("scoring", "say hi"))
    assert result["score"] == 88


def test_status_online_when_only_chat_works(client, auth, provider):
    """Provider restricts /models → status must verify via chat, not cry invalid_api_key (defect 2)."""
    FakeOpenAIHandler.behavior["models_status"] = 401
    _save_key(client, auth, provider)

    status = client.get("/api/settings/ai/status", headers=auth).json()
    assert status["online"] is True, f"false negative: {status.get('reason')}"
    assert status["probe"] == "chat_completions"
    assert status["key_source"] == "user"
    assert status["key_preview"].startswith("sk-rep")


def test_status_reports_invalid_key_with_provider_detail(client, auth, provider):
    """A key rejected for chat too → invalid_api_key with the provider's message."""
    # Provider rejects every auth (including VALID_KEY) by returning 401 for both endpoints:
    # simulate by saving a DIFFERENT key than the one the provider accepts.
    r = client.put("/api/settings", json={
        "ai": {"base_url": provider, "model": "gpt-4o-mini", "api_key": "sk-wrong-key-999"}
    }, headers=auth)
    assert r.status_code == 200

    status = client.get("/api/settings/ai/status", headers=auth).json()
    assert status["online"] is False
    assert status["reason"] == "invalid_api_key"
    assert "Incorrect API key provided" in (status.get("detail") or "")
    assert status["key_source"] == "user"


def test_unreadable_stored_key_is_surfaced_not_silently_ignored(client, auth, provider, db):
    """ENCRYPTION_KEY rotation must not silently ping with a different key (defect 3)."""
    _save_key(client, auth, provider)
    # Corrupt the stored ciphertext (what a rotated ENCRYPTION_KEY looks like).
    from app.models.models import SettingsModel

    row = db.query(SettingsModel).filter(SettingsModel.category == "ai", SettingsModel.key == "api_key").first()
    row.value = "gAAAAA-corrupted-ciphertext-that-cannot-decrypt"
    db.commit()
    db.expire_all()

    status = client.get("/api/settings/ai/status", headers=auth).json()
    assert status["online"] is False
    assert status["reason"] == "stored_key_unreadable"
    assert status["stored_key_error"] is True
    assert "Re-enter" in (status.get("hint") or "")

    # The settings UI no longer lies that a usable key is configured.
    settings = client.get("/api/settings", headers=auth).json()
    assert settings["ai"]["api_key_error"] == "unreadable"

    # Re-entering the key fixes it.
    _save_key(client, auth, provider)
    status = client.get("/api/settings/ai/status", headers=auth).json()
    assert status["online"] is True


def test_masked_placeholder_is_rejected(client, auth):
    """Saving the masked '***' back must 400, not overwrite the real key (classic round-trip bug)."""
    for masked in ("***", "••••••••", "XXXXXXX"):
        r = client.put("/api/settings", json={"ai": {"api_key": masked}}, headers=auth)
        assert r.status_code == 400, masked
        assert r.json()["detail"]["code"] == "masked_api_key"
    r = client.post("/api/ai/config", json={"scoring": {"api_key": "***", "model": "gpt-4o"}}, headers=auth)
    assert r.status_code == 400


def test_key_whitespace_is_trimmed(client, auth, provider):
    """Paste artifacts (newline/space) must not corrupt the Authorization header."""
    r = client.put("/api/settings", json={
        "ai": {"base_url": f"  {provider}/  ", "model": " gpt-4o-mini ", "api_key": f"  {VALID_KEY}\n"}
    }, headers=auth)
    assert r.status_code == 200
    status = client.get("/api/settings/ai/status", headers=auth).json()
    assert status["online"] is True, status


def test_owner_key_is_fallback_for_member(client, auth, provider, db):
    """Member without own key uses the owner's key — and the status says where it comes from."""
    from app.models.models import User

    _save_key(client, auth, provider)
    client.post("/api/auth/register", json={"email": "member@example.com",
                                            "password": "member-password-123", "name": "Member"})
    login = client.post("/api/auth/login", json={"email": "member@example.com", "password": "member-password-123"})
    member_auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

    status = client.get("/api/settings/ai/status", headers=member_auth).json()
    assert status["online"] is True
    assert status["key_source"] == "owner"
    assert db.query(User).filter(User.email == "member@example.com").count() == 1


def test_workflow_override_is_per_user_and_encrypted(client, auth, provider, db):
    """One user's override must never hijack another user's AI calls (defect 4)."""
    from app.models.models import User

    _save_key(client, auth, provider)
    r = client.post("/api/ai/config", json={"scoring": {"api_key": "sk-owner-override-key"}}, headers=auth)
    assert r.status_code == 200

    # A member with no key of their own (falls back to the owner's default).
    client.post("/api/auth/register", json={"email": "member@example.com",
                                            "password": "member-password-123", "name": "Member"})

    from app.models.models import SettingsModel
    from app.services.ai_client import resolve_config_for_user

    row = db.query(SettingsModel).filter(SettingsModel.category == "ai_workflows", SettingsModel.key == "scoring").all()
    assert all("sk-owner-override-key" not in str(r_.value) for r_ in row), "override key stored in plaintext!"

    owner_cfg = resolve_config_for_user(db, _owner_id(db), "scoring")
    assert owner_cfg["api_key"] == "sk-owner-override-key"
    assert owner_cfg["key_source"] == "workflow_override"

    member = db.query(User).filter(User.email == "member@example.com").first()
    member_cfg = resolve_config_for_user(db, member.id, "scoring")
    assert member_cfg["api_key"] == VALID_KEY, "member inherited another user's override!"
    assert member_cfg["key_source"] == "owner"


def test_unreachable_provider_reason(client, auth):
    """No key → no_api_key; empty env, honest reason."""
    status = client.get("/api/settings/ai/status", headers=auth).json()
    assert status["online"] is False
    assert status["reason"] == "no_api_key"
