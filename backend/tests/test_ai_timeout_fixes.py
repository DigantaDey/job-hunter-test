"""Regression tests for the "slow reasoning model fails as `unreachable` with a blank
`http_error:` detail" outage reported while AI-parsing an uploaded resume.

Root causes fixed:
1. The gateway waited a flat 30s per attempt, then labelled the resulting
   ``httpx.ReadTimeout`` — whose string form is literally ``""`` — as
   ``http_error: `` and mapped that prefix to ``unreachable``. A slow but
   healthy generation was reported as a DNS/TLS/connection failure.
2. Every expired wait was retried up to ``AI_MAX_RETRIES`` (3), so one upload
   spent — and the provider billed — three full generations while the ledger
   claimed ``0 tokens / $0.0000``.
3. ``reason_from_message`` short-circuited on the ``http_error`` prefix before
   checking timeout keywords.
4. ``describe_ai_error`` let a failed short-deadline probe overwrite a precise
   ``timeout`` with ``unreachable``.
5. The wait budget was env-only (and the parse path passed no timeout at all);
   per-user settings were ignored.
6. The browser gave up first (30s globally, 120s for uploads).
7. Timed-out attempts were never caveated as possibly billed.

Every test drives the REAL gateway (``@pytest.mark.real_ai``) against a local
scripted OpenAI-compatible provider — no stubs, no outbound network. "Very
long" is simulated with small real delays plus per-call/per-user overrides.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List

import pytest

VALID_KEY = "sk-timeout-fix-key-1"

GOOD_PROFILE = {
    "name": "Test Candidate",
    "email": "test.candidate@example.com",
    "phone": "+91 90000 00001",
    "location": "Bangalore, India",
    "summary": ("Backend engineer with 6 years building Python, FastAPI, PostgreSQL and "
                "Kubernetes services for fintech payments platforms."),
    "current_title": "Senior Software Engineer",
    "years_experience": "6",
    "skills": ["Python", "FastAPI", "PostgreSQL", "Docker", "Kubernetes", "AWS", "React"],
    "experience": [
        {"title": "Senior Software Engineer", "company": "FinCo", "duration": "2021 - present",
         "location": "Bangalore", "bullets": ["Payments platform serving 2M users."]},
        {"title": "Software Engineer", "company": "ShopStack", "duration": "2018 - 2021",
         "location": "", "bullets": ["Order pipeline on AWS."]},
    ],
    "education": [], "projects": [], "links": [],
}


# --------------------------------------------------------------------------- #
# Slow scripted provider: behaves like a heavy reasoning model — accepts the
# request, takes a long time, then returns a good answer (provider-side tokens
# are "consumed" whether or not the client is still waiting).
# --------------------------------------------------------------------------- #
class SlowAIHandler(BaseHTTPRequestHandler):
    behavior: Dict[str, Any] = {"delay": 0.0, "requests": []}

    def log_message(self, *a):  # silence
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "stub-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request_body = json.loads(self.rfile.read(length) or b"{}")
        self.behavior["requests"].append(request_body)
        # Heavy reasoning: sleep first (the client may give up meanwhile —
        # the provider keeps working anyway, exactly like the real incident).
        time.sleep(self.behavior["delay"])
        out = {
            "choices": [{"message": {"content": json.dumps(GOOD_PROFILE)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 2000,
                      "total_tokens": 3000},
        }
        body = json.dumps(out).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client already timed out — provider billed it anyway


@pytest.fixture()
def slow_provider():
    SlowAIHandler.behavior = {"delay": 0.0, "requests": []}
    server = HTTPServer(("127.0.0.1", 0), SlowAIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


@pytest.fixture()
def direct_ai(slow_provider):
    """Point the env-level gateway config at the slow provider."""
    import app.services.ai_client as ai_client

    ai_client._workflow_overrides.clear()
    ai_client._breakers.clear()
    ai_client.set_workflow_overrides(
        {"parse": {"base_url": slow_provider, "api_key": VALID_KEY,
                   "model": "z-ai/glm-5.3-free"}},
        user_id=0,
    )
    yield ai_client
    ai_client._workflow_overrides.clear()
    ai_client._breakers.clear()


def _wire_requests() -> List[Dict[str, Any]]:
    return SlowAIHandler.behavior["requests"]


def _is_parse_call(request_body: Dict[str, Any]) -> bool:
    contents = [str(m.get("content") or "") for m in request_body.get("messages") or []]
    return any("extract a complete, structured profile" in c.lower() for c in contents)


# --------------------------------------------------------------------------- #
# 1. The user's exact scenario: slow reasoning model → upload succeeds
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
def test_slow_reasoning_model_upload_succeeds_end_to_end(client, auth, slow_provider, db):
    """End-to-end: the provider takes longer than the old 30s ceiling would
    allow for a big generation — the upload must still succeed and persist the
    AI-extracted profile. (Delay stands in for "minutes"; the 300s default
    covers it with room to spare.)"""
    import app.services.ai_client as ai_client
    from tests.conftest import pdf_bytes

    ai_client._breakers.clear()
    try:
        saved = client.put("/api/settings", json={
            "ai": {"base_url": slow_provider, "model": "z-ai/glm-5.3-free",
                   "rpm": 600, "api_key": VALID_KEY},
        }, headers=auth)
        assert saved.status_code == 200, saved.text

        SlowAIHandler.behavior["delay"] = 1.2
        response = client.post(
            "/api/resume/upload",
            files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
            headers=auth,
        )
        assert response.status_code == 200, response.text

        from app.models.models import AICreditLedger, Profile, Resume

        profile = db.query(Profile).first()
        assert profile is not None, "the AI-extracted profile must be persisted"
        assert profile.data["name"] == "Test Candidate"
        assert profile.extraction_source == "ai"
        resume = db.query(Resume).filter(Resume.type == "master").first()
        assert resume is not None and resume.status == "approved"

        # The parse succeeded on its first attempt — no retry storm.
        parse_calls = [r for r in _wire_requests() if _is_parse_call(r)]
        assert len(parse_calls) == 1, f"expected 1 parse attempt, saw {len(parse_calls)}"

        # The ledger tells the truth about the spend (provider usage 3000).
        row = (db.query(AICreditLedger)
               .filter(AICreditLedger.workflow == "parse")
               .order_by(AICreditLedger.id.desc()).first())
        assert row is not None and row.success
        assert row.total_tokens == 3000, f"ledger must record provider usage, got {row.total_tokens}"
        assert row.meta.get("attempts") == 1
    finally:
        ai_client._breakers.clear()


@pytest.mark.real_ai
def test_slow_model_within_per_call_override_succeeds(direct_ai):
    """A per-call wait budget longer than the generation time succeeds —
    proving the ceiling is the client's wait, not the provider."""
    SlowAIHandler.behavior["delay"] = 1.0
    result = asyncio.run(direct_ai.chat_completion("parse", "extract the resume", timeout=5))
    assert result["name"] == "Test Candidate"
    assert len(_wire_requests()) == 1


# --------------------------------------------------------------------------- #
# 2. The honest-failure case: the wait genuinely expires
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
def test_expired_wait_reports_timeout_with_billing_caveat(direct_ai, monkeypatch):
    """Provider slower than the wait budget → `timeout` (never `unreachable`),
    non-blank actionable detail, exactly ONE wire attempt (no blind retries)."""
    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 3)
    SlowAIHandler.behavior["delay"] = 2.0
    started = time.monotonic()
    with pytest.raises(direct_ai.AIClientError) as excinfo:
        asyncio.run(direct_ai.chat_completion("parse", "extract the resume", timeout=0.4))
    elapsed = time.monotonic() - started

    err = excinfo.value
    assert err.reason == "timeout", f"must be timeout, got {err.reason}: {err}"
    detail = str(err)
    assert detail.split(":", 1)[1].strip(), "detail must not be blank after the colon"
    assert "still generating" in detail
    assert "0.4" in detail, "the wait budget must be named"
    assert "1 attempt" in detail and "may still have been billed" in detail
    assert "Timeout in Settings" in detail or "AI_TIMEOUT" in detail
    # One attempt only: 0.4s wait + no backoff storm (3 blind retries would take ~6s+).
    assert elapsed < 3, f"must fail after one wait, took {elapsed:.1f}s"
    assert len(_wire_requests()) == 1, "an expired generation wait must not be retried"


@pytest.mark.real_ai
def test_upload_timeout_is_honest_end_to_end_with_ledger_caveat(
        client, auth, slow_provider, db, monkeypatch):
    """Upload against a provider slower than the budget → 503 `timeout` with an
    actionable fix, and the ledger caveats the spend instead of claiming $0."""
    import app.services.ai_client as ai_client
    from tests.conftest import pdf_bytes

    ai_client._breakers.clear()
    try:
        monkeypatch.setattr("app.core.config.settings.ai_timeout", 0.4)
        monkeypatch.setattr("app.core.config.settings.ai_max_retries", 3)
        saved = client.put("/api/settings", json={
            "ai": {"base_url": slow_provider, "model": "z-ai/glm-5.3-free",
                   "rpm": 600, "api_key": VALID_KEY},
        }, headers=auth)
        assert saved.status_code == 200, saved.text

        SlowAIHandler.behavior["delay"] = 2.0
        response = client.post(
            "/api/resume/upload",
            files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
            headers=auth,
        )
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["code"] == "ai_unavailable"
        assert body["reason"] == "timeout", body
        assert body["workflow"] == "parse"
        assert "reached" in body["message"], "the message must say the endpoint was reached"
        assert body["fix"] and ("Timeout" in body["fix"] or "AI_TIMEOUT" in body["fix"])
        assert body["detail"].split(":", 1)[1].strip(), "detail must not be blank"
        assert "may still have been billed" in body["detail"]

        # Exactly one provider request: no retry storm behind the user's back.
        assert len(_wire_requests()) == 1

        from app.models.models import AICreditLedger

        row = (db.query(AICreditLedger)
               .filter(AICreditLedger.workflow == "parse")
               .order_by(AICreditLedger.id.desc()).first())
        assert row is not None and not row.success
        assert "may still have been billed" in (row.error or "")
        assert row.meta.get("attempts") == 1
        assert row.meta.get("possibly_billed") is True
        assert row.meta.get("usage_unknown") is True
    finally:
        ai_client._breakers.clear()


# --------------------------------------------------------------------------- #
# 3. Genuine connectivity failures are still `unreachable` — and fast
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
def test_connection_refused_is_unreachable_fast_and_nonblank(direct_ai, monkeypatch):
    """Nothing listening on the port → `unreachable` with a non-blank detail,
    failing fast (not after the long generation wait)."""
    import app.services.ai_client as ai_client

    ai_client.set_workflow_overrides(
        {"parse": {"base_url": "http://127.0.0.1:9/v1", "api_key": VALID_KEY,
                   "model": "z-ai/glm-5.3-free"}},
        user_id=0,
    )
    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 1)
    started = time.monotonic()
    with pytest.raises(ai_client.AIClientError) as excinfo:
        asyncio.run(ai_client.chat_completion("parse", "extract the resume", timeout=30))
    elapsed = time.monotonic() - started

    assert excinfo.value.reason == "unreachable"
    assert str(excinfo.value).split(":", 1)[1].strip(), "connect detail must be non-blank"
    assert elapsed < 5, f"a refused connection must fail fast, took {elapsed:.1f}s"


@pytest.mark.real_ai
def test_connect_errors_are_still_retried_but_timeouts_are_not(direct_ai, monkeypatch):
    """Connect-phase failures (provider never saw the request) keep the retry
    loop; generation waits do not. Proves the two paths diverge."""
    import app.services.ai_client as ai_client

    ai_client.set_workflow_overrides(
        {"parse": {"base_url": "http://127.0.0.1:9/v1", "api_key": VALID_KEY,
                   "model": "z-ai/glm-5.3-free"}},
        user_id=0,
    )
    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 2)
    started = time.monotonic()
    with pytest.raises(ai_client.AIClientError) as excinfo:
        asyncio.run(ai_client.chat_completion("parse", "hi", timeout=30))
    elapsed = time.monotonic() - started

    assert excinfo.value.reason == "unreachable"
    # 2 attempts + backoff sleep proves the connect path retried.
    assert elapsed >= 1.0, "connect failures must be retried with backoff"


@pytest.mark.real_ai
def test_gateway_maps_timeout_phase_to_reason(direct_ai, monkeypatch):
    """Hermetic classification guard: the *phase* of the wait decides the
    reason — a connect-phase wait means the endpoint was never reached
    (`unreachable`); a read wait means it was reached and is still generating
    (`timeout`). httpx stringifies both exceptions as ``""`` — the detail must
    still be non-blank in both cases."""
    import httpx

    import app.services.ai_client as ai_client

    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 1)

    async def _connect_timeout(self, *args, **kwargs):
        raise httpx.ConnectTimeout("")

    async def _read_timeout(self, *args, **kwargs):
        raise httpx.ReadTimeout("")

    async def _connect_timeout_stream(self, *a, **kw):
        raise httpx.ConnectTimeout("")
    async def _read_timeout_stream(self, *a, **kw):
        raise httpx.ReadTimeout("")

    monkeypatch.setattr(httpx.AsyncClient, "post", _connect_timeout)
    monkeypatch.setattr(httpx.AsyncClient, "stream", lambda self, *a, **kw: (_ for _ in ()).throw(httpx.ConnectTimeout("")) if False else None)
    # Proper stream mock for connect timeout
    def _stream_connect(self, *a, **kw):
        class _C:
            async def __aenter__(self):
                raise httpx.ConnectTimeout("")
            async def __aexit__(self, *a):
                return False
        return _C()
    monkeypatch.setattr(httpx.AsyncClient, "stream", _stream_connect)
    with pytest.raises(ai_client.AIClientError) as excinfo:
        asyncio.run(ai_client.chat_completion("parse", "hi", timeout=30, stream=True))
    assert excinfo.value.reason == "unreachable", f"got {excinfo.value}: {excinfo.value!r} stream"
    assert "connect" in str(excinfo.value).lower()
    assert str(excinfo.value).split(":", 1)[1].strip()
    # Also test non-stream still
    monkeypatch.setattr(httpx.AsyncClient, "post", _connect_timeout)
    with pytest.raises(ai_client.AIClientError) as excinfo:
        asyncio.run(ai_client.chat_completion("parse", "hi", timeout=30))
    assert excinfo.value.reason == "unreachable", f"got {excinfo.value}: {excinfo.value!r}"
    assert "connect" in str(excinfo.value).lower()
    assert str(excinfo.value).split(":", 1)[1].strip()

    def _stream_read(self, *a, **kw):
        class _C:
            async def __aenter__(self):
                raise httpx.ReadTimeout("")
            async def __aexit__(self, *a):
                return False
        return _C()
    monkeypatch.setattr(httpx.AsyncClient, "post", _read_timeout)
    monkeypatch.setattr(httpx.AsyncClient, "stream", _stream_read)
    with pytest.raises(ai_client.AIClientError) as excinfo:
        asyncio.run(ai_client.chat_completion("parse", "hi", timeout=30, stream=True))
    assert excinfo.value.reason == "timeout", f"got {excinfo.value}: {excinfo.value!r} stream"
    assert "still generating" in str(excinfo.value)
    assert str(excinfo.value).split(":", 1)[1].strip()
    monkeypatch.setattr(httpx.AsyncClient, "post", _read_timeout)
    with pytest.raises(ai_client.AIClientError) as excinfo:
        asyncio.run(ai_client.chat_completion("parse", "hi", timeout=30))
    assert excinfo.value.reason == "timeout", f"got {excinfo.value}: {excinfo.value!r}"
    assert "still generating" in str(excinfo.value)
    assert str(excinfo.value).split(":", 1)[1].strip()


# --------------------------------------------------------------------------- #
# 4. The wait budget is deliberate, generous and configurable
# --------------------------------------------------------------------------- #
def test_default_wait_covers_heavy_reasoning_models():
    """The shipped default must give a slow reasoning model minutes, not seconds."""
    from app.core.config import settings

    assert settings.ai_timeout >= 300, "default generation wait must be >= 300s"
    assert settings.ai_connect_timeout <= 30, "connect deadline must stay short"
    assert settings.ai_connect_timeout < settings.ai_timeout


@pytest.mark.real_ai
def test_per_user_timeout_beats_env_default(client, auth, db, slow_provider, monkeypatch):
    """`ai.timeout` saved in Settings → AI API governs the user's calls: with
    the env default sabotaged short, the user's longer wait still succeeds."""
    import app.services.ai_client as ai_client

    ai_client._breakers.clear()
    try:
        monkeypatch.setattr("app.core.config.settings.ai_timeout", 0.2)
        saved = client.put("/api/settings", json={
            "ai": {"base_url": slow_provider, "model": "z-ai/glm-5.3-free",
                   "rpm": 600, "api_key": VALID_KEY, "timeout": 5},
        }, headers=auth)
        assert saved.status_code == 200, saved.text

        from app.models.models import User

        user = db.query(User).filter(User.email == "owner@example.com").first()
        SlowAIHandler.behavior["delay"] = 1.0
        result = asyncio.run(ai_client.chat_completion(
            "parse", "extract the resume", db=db, user_id=user.id))
        assert result["name"] == "Test Candidate"
    finally:
        ai_client._breakers.clear()


def test_timeout_setting_round_trip_and_validation(client, auth):
    """The knob is writable, readable, and bounded (5–1800s)."""
    saved = client.put("/api/settings", json={"ai": {"timeout": 42}}, headers=auth)
    assert saved.status_code == 200, saved.text
    current = client.get("/api/settings", headers=auth)
    assert current.status_code == 200
    assert current.json()["ai"]["timeout"] == 42

    for bad in (2, 0, 9999, "soon"):
        rejected = client.put("/api/settings", json={"ai": {"timeout": bad}}, headers=auth)
        assert rejected.status_code == 400, f"timeout={bad!r} must be rejected"


# --------------------------------------------------------------------------- #
# 5. Classification guards (unit level — no wire involved)
# --------------------------------------------------------------------------- #
def test_reason_classifier_never_calls_a_wait_unreachable():
    from app.services.ai_client import reason_from_message

    assert reason_from_message("timeout: model 'm' was still generating after 300s") == "timeout"
    assert reason_from_message("http_error: timed out") == "timeout"
    assert reason_from_message("http_error: ReadTimeout") == "timeout"
    # …while genuine connect-phase failures stay unreachable.
    assert reason_from_message("http_error: All connection attempts failed") == "unreachable"
    assert reason_from_message("http_error: connection to https://x timed out after 10s") == "unreachable"
    assert reason_from_message("http_error: [Errno -2] Name or service not known") == "unreachable"


def test_probe_cannot_downgrade_timeout_to_unreachable():
    """A failed short-deadline probe must never overwrite a precise `timeout` —
    a slow model makes the probe fail too, while the key is fine."""
    from app.services.ai_client import AIClientError
    from app.services.ai_guardrails import describe_ai_error

    exc = AIClientError(
        "timeout: model 'm' was still generating after 300s (attempt 1/3) — "
        "the endpoint was reached; 1 attempt(s) spent and each may still have been billed.",
        reason="timeout",
    )
    out = describe_ai_error(exc, workflow="parse",
                            probe={"reason": "unreachable", "detail": "", "base_url": "https://x/v1"})
    assert out.reason == "timeout", f"probe must not override timeout, got {out.reason}"
    assert out.detail and not out.detail.strip().endswith(":")


def test_probe_auth_verdict_still_overrides_unknown():
    """Definitive config verdicts from the probe are still honoured."""
    from app.services.ai_client import AIClientError
    from app.services.ai_guardrails import describe_ai_error

    exc = AIClientError("http_error: something odd", reason="unknown")
    out = describe_ai_error(exc, workflow="parse",
                            probe={"reason": "invalid_api_key", "detail": "bad key"})
    assert out.reason == "invalid_api_key"


def test_blank_details_are_replaced_never_rendered():
    from app.services.ai_client import AIClientError
    from app.services.ai_guardrails import describe_ai_error

    for blank in ("", "http_error:", "http_error: ", "timeout:"):
        exc = AIClientError(blank, reason="unknown")
        out = describe_ai_error(exc, workflow="parse")
        assert out.detail.strip(), f"blank {blank!r} must gain a fallback detail"
        assert not out.detail.strip().endswith(":")


def test_timeout_diagnosis_is_actionable():
    from app.services.ai_guardrails import DIAGNOSIS

    message, fix = DIAGNOSIS["timeout"]
    assert "reached" in message, "must say the endpoint was reached"
    assert "Timeout" in fix or "AI_TIMEOUT" in fix
    assert "bill" in fix, "must warn the attempt may have been billed"
