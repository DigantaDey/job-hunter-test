"""Accounting regressions through the real gateway, with only HTTP mocked."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import (
    _scripted_answer,
    _stub_profile,
    _stub_tailored,
    draft_email_now,
    generate_resume_now,
    pdf_bytes,
)

from app.core.config import settings
from app.models.models import AICreditLedger, Job, Profile, UsageCounter, User
from app.services import ai_client

pytestmark = pytest.mark.real_ai


@pytest.fixture(autouse=True)
def gateway(monkeypatch):
    """No external network, rate-limit sleeps, or leaked process-level state."""
    monkeypatch.setattr(ai_client, "_breakers", {})
    monkeypatch.setattr(ai_client, "_usage", {})
    monkeypatch.setattr(ai_client, "_workflow_overrides", {})
    monkeypatch.setattr(ai_client, "_budget_day", "")
    monkeypatch.setattr(ai_client, "_budget_used", 0)
    monkeypatch.setattr(ai_client, "_semaphore", None)
    monkeypatch.setattr(settings, "ai_api_key", "sk-accounting-test")
    monkeypatch.setattr(settings, "ai_model", "gpt-4o-mini")
    monkeypatch.setattr(settings, "ai_max_retries", 1)
    monkeypatch.setattr(settings, "ai_daily_token_budget", 0)
    monkeypatch.setattr(ai_client.rate_limiter, "wait_and_acquire", AsyncMock())
    monkeypatch.setattr(ai_client, "_ambient_user_id", lambda: None)
    state = {"requests": [], "status": 200, "responses": []}

    async def post(_client, url, **kwargs):
        state["requests"].append(kwargs["json"])
        if state["responses"]:
            body = state["responses"].pop(0)
        else:
            # Handle both OpenAI (messages) and Google (contents) payloads
            req = kwargs.get("json") or {}
            if "messages" in req:
                prompt = " ".join(m.get("content") or "" for m in req.get("messages") or [])
            else:
                parts = []
                for c in req.get("contents") or []:
                    for part in c.get("parts") or []:
                        parts.append(str(part.get("text") or ""))
                sys_inst = req.get("systemInstruction") or {}
                for part in sys_inst.get("parts") or []:
                    parts.append(str(part.get("text") or ""))
                prompt = " ".join(parts)
            body = completion(_scripted_answer(prompt))
        return httpx.Response(state["status"], json=body, request=httpx.Request("POST", url))

    def fake_stream(_client, method, url, **kwargs):
        # Streaming mock — reuse same logic but return a stream-like response (sync func returning async ctx manager)
        req_json = kwargs.get("json") or {}
        state["requests"].append(req_json)
        if state["responses"]:
            body = state["responses"].pop(0)
        else:
            if "messages" in req_json:
                prompt = " ".join(m.get("content") or "" for m in req_json.get("messages") or [])
            else:
                parts = []
                for c in req_json.get("contents") or []:
                    for part in c.get("parts") or []:
                        parts.append(str(part.get("text") or ""))
                sys_inst = req_json.get("systemInstruction") or {}
                for part in sys_inst.get("parts") or []:
                    parts.append(str(part.get("text") or ""))
                prompt = " ".join(parts)
            body = completion(_scripted_answer(prompt))
        import json as _json
        if state["status"] != 200:
            resp = httpx.Response(state["status"], json=body, request=httpx.Request(method, url))
            async def _aread_error():
                return _json.dumps(body).encode()
            resp.aread = _aread_error
            async def _aiter_error():
                if False:
                    yield ""
            resp.aiter_lines = _aiter_error
            resp.headers["content-type"] = "application/json"
            class _CtxErr:
                async def __aenter__(self):
                    return resp
                async def __aexit__(self, *a):
                    return False
            return _CtxErr()
        resp = httpx.Response(200, json=body, request=httpx.Request(method, url))
        resp.headers["content-type"] = "application/json"
        async def _aread():
            return _json.dumps(body).encode()
        resp.aread = _aread
        async def _aiter():
            if False:
                yield ""
        resp.aiter_lines = _aiter
        class _Ctx:
            async def __aenter__(self):
                return resp
            async def __aexit__(self, *a):
                return False
        return _Ctx()

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)
    # Also patch the shared http client if already created
    try:
        from app.services import http as http_service
        if http_service._client is not None:
            http_service._client.post = post.__get__(http_service._client, httpx.AsyncClient)
            http_service._client.stream = fake_stream.__get__(http_service._client, httpx.AsyncClient)
    except Exception:
        pass
    return state


def completion(answer, *, raw_content=False):
    return {
        "choices": [{"message": {"content": answer if raw_content else json.dumps(answer)},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 500, "completion_tokens": 200, "total_tokens": 700},
    }


@pytest.fixture
def account(db, member_auth):
    user = db.query(User).filter(User.email == "member@example.com").one()
    profile = Profile(user_id=user.id, data=_stub_profile(), layout={}, extraction_source="ai")
    job = Job(user_id=user.id, title="Senior Backend Engineer", company="FinCo",
              description="Python, FastAPI, PostgreSQL, Kubernetes, AWS. Payments platform at scale.",
              url="https://example.com/jobs/backend", source="manual", dedupe_key="accounting:job",
              score=88, status="discovered")
    db.add_all([profile, job])
    db.commit()
    return user, profile, job, member_auth


def counters(db, user_id):
    db.expire_all()
    return {row.capability: row.count for row in db.query(UsageCounter).filter_by(user_id=user_id)}


def call(db=None, user_id=None, **kwargs):
    return asyncio.run(ai_client.chat_completion("parse", "Extract a profile", db=db, user_id=user_id, **kwargs))


@pytest.mark.parametrize("model,expected", [
    ("gpt-4o-mini", 0.00075),
    ("gpt-4", 0.09),
    ("gpt-4o", 0.02),
    ("GPT-4O-MINI", 0.00075),
    ("gpt-4o-mini-2024-07-18", 0.00075),
    ("gpt-4-0613", 0.09),
    ("unknown-gpt-4-model", 0.003),
])
def test_cost_exact_then_longest_prefix(model, expected):
    assert ai_client.estimate_cost(model, 1000, 1000) == pytest.approx(expected)


@pytest.mark.parametrize("workflow", ["resume_gen", "email_gen", "parse", "scoring"])
@pytest.mark.parametrize("success", [True, False])
def test_gateway_accounting_never_charges_business_quotas(db, account, workflow, success):
    user, *_ = account
    ai_client.track_ai_usage(db=db, user_id=user.id, workflow=workflow, model="gpt-4o-mini",
                             prompt_tokens=500, completion_tokens=200, success=success)
    assert counters(db, user.id) == ({"ai_operations_per_month": 1, "ai_credits_per_month": 700}
                                     if success else {})
    row = db.query(AICreditLedger).one()
    assert row.success is success
    assert row.total_tokens == 700
    assert row.estimated_cost_usd == pytest.approx(0.000195)
    assert ai_client.budget_snapshot()["used"] == 700


def test_queued_resume_charges_one_business_unit_and_one_ledger_row(client, db, account, gateway):
    user, _, job, headers = account
    gateway["responses"] = [completion(_stub_tailored())]
    result = generate_resume_now(client, headers, job.id)
    assert result["status"] == "generated", result
    assert len(gateway["requests"]) == 1
    assert counters(db, user.id) == {"tailored_resumes_per_month": 1,
                                     "ai_operations_per_month": 1, "ai_credits_per_month": 700}
    row = db.query(AICreditLedger).one()
    assert row.workflow == "resume_gen" and row.success


def test_queued_email_charges_one_draft(client, db, account, gateway):
    user, _, job, headers = account
    result = draft_email_now(client, headers, {
        "company": job.company, "job_id": job.id,
        "recipient_email": "recruiter@example.com", "recipient_name": "Recruiter",
    })
    assert result["email"], result
    assert len(gateway["requests"]) == 1
    assert counters(db, user.id) == {"outreach_per_month": 1, "contact_discovery_per_month": 1,
                                     "ai_operations_per_month": 1, "ai_credits_per_month": 700}
    row = db.query(AICreditLedger).one()
    assert row.workflow == "email_gen" and row.success


def test_upload_parse_charges_once(client, db, account):
    user, _, _, headers = account
    response = client.post("/api/resume/upload", headers=headers,
                           files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")})
    assert response.status_code == 200, response.text
    assert counters(db, user.id)["resume_parses_per_month"] == 1
    assert db.query(AICreditLedger).filter_by(user_id=user.id, workflow="parse").count() == 1


def test_accepted_scoring_charges_one_analysis(db, account):
    from app.services.scoring import ai_score_detailed

    user, profile, job, _ = account
    result = asyncio.run(ai_score_detailed(profile.data, job.description, db=db, user_id=user.id))
    assert result["source"] == "ai"
    assert counters(db, user.id) == {"job_analysis_per_month": 1,
                                     "ai_operations_per_month": 1, "ai_credits_per_month": 700}
    assert db.query(AICreditLedger).count() == 1


@pytest.mark.parametrize("status", [500, 402])
def test_provider_failure_is_observable_but_consumes_no_quota(db, account, gateway, status):
    user, *_ = account
    gateway["status"] = status
    with pytest.raises(ai_client.AIClientError) as error:
        call(db, user.id)
    assert error.value.status == status
    assert counters(db, user.id) == {}
    row = db.query(AICreditLedger).one()
    assert not row.success and f"status {status}" in row.error
    assert row.total_tokens == 0
    assert ai_client.budget_snapshot()["used"] == 0


@pytest.mark.parametrize("gate", ["circuit_open", "budget_exhausted", "limit_exceeded"])
def test_preflight_failure_has_one_ledger_row_and_no_new_charges(db, account, gateway, monkeypatch, gate):
    user, *_ = account
    if gate == "circuit_open":
        monkeypatch.setattr(settings, "ai_breaker_failures", 1)
        ai_client._breaker("parse").record_failure("provider unavailable")
    elif gate == "budget_exhausted":
        monkeypatch.setattr(settings, "ai_daily_token_budget", 700)
        ai_client._spend_tokens(700)
    else:
        from app.core.entitlements import increment_usage
        increment_usage(db, user.id, "ai_operations_per_month", 50)
    before = counters(db, user.id)
    with pytest.raises(ai_client.AIClientError) as error:
        call(db, user.id)
    assert error.value.reason == gate
    assert not gateway["requests"]
    assert counters(db, user.id) == before
    row = db.query(AICreditLedger).one()
    assert not row.success and row.total_tokens == 0


@pytest.mark.parametrize("attributed", [True, False])
def test_daily_budget_spent_once_per_call(db, account, gateway, monkeypatch, attributed):
    user, *_ = account
    monkeypatch.setattr(settings, "ai_daily_token_budget", 1400)
    args = (db, user.id) if attributed else ()
    call(*args)
    assert ai_client.budget_snapshot()["used"] == 700
    assert not ai_client.budget_exhausted()
    call(*args)
    assert ai_client.budget_snapshot()["used"] == 1400
    assert ai_client.budget_exhausted()
    with pytest.raises(ai_client.AIClientError, match="budget_exhausted"):
        call(*args)
    assert len(gateway["requests"]) == 2
    assert ai_client.budget_snapshot()["used"] == 1400


@pytest.mark.parametrize("recover", [True, False])
def test_retry_tokens_count_once_without_billing_failed_calls(db, account, gateway, monkeypatch, recover):
    user, *_ = account
    monkeypatch.setattr(settings, "ai_max_retries", 2)
    bad = completion("not valid JSON", raw_content=True)
    gateway["responses"] = [bad, completion({"ok": True}) if recover else bad]
    if recover:
        assert call(db, user.id) == {"ok": True}
    else:
        with pytest.raises(ai_client.AIClientError):
            call(db, user.id)
    assert len(gateway["requests"]) == 2
    assert ai_client.budget_snapshot()["used"] == 1400
    row = db.query(AICreditLedger).one()
    assert row.success is recover and row.total_tokens == 1400
    assert counters(db, user.id) == ({"ai_operations_per_month": 1, "ai_credits_per_month": 1400}
                                     if recover else {})


@pytest.mark.parametrize("body", [completion("broken JSON", raw_content=True), {"choices": []}])
def test_unusable_200s_open_breaker(db, account, gateway, monkeypatch, body):
    user, *_ = account
    monkeypatch.setattr(settings, "ai_breaker_failures", 2)
    gateway["responses"] = [body, body]
    for failures in (1, 2):
        with pytest.raises(ai_client.AIClientError):
            call(db, user.id)
        assert ai_client._breaker("parse").failures == failures
    assert ai_client._breaker("parse").is_open()
    with pytest.raises(ai_client.AIClientError, match="circuit_open"):
        call(db, user.id)
    assert len(gateway["requests"]) == 2
    assert counters(db, user.id) == {}
    assert db.query(AICreditLedger).filter_by(success=False).count() == 3


@pytest.mark.parametrize("json_mode", [True, False])
def test_validated_response_resets_breaker(db, account, json_mode):
    user, *_ = account
    breaker = ai_client._breaker("parse")
    breaker.record_failure("previous bad response")
    call(db, user.id, json_mode=json_mode)
    assert breaker.failures == 0 and not breaker.is_open()
