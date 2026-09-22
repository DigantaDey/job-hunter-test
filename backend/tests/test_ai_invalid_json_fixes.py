"""Regression tests for the "invalid_json: model did not return valid JSON
(preview: '')" outage reported while AI-processing an uploaded resume.

Root causes fixed:
1. A 200 response whose ``choices[0].message.content`` is EMPTY (reasoning
   models burn the whole ``max_tokens`` budget on ``reasoning_content``; free
   tiers emit empty answers intermittently) was raised instantly as
   ``invalid_json`` with an empty preview — no retry, no salvage.
2. ``finish_reason`` was never inspected: a truncated JSON (``length``) is
   recoverable by retrying with a larger ``max_tokens``; a blocked answer
   (``content_filter``) needs an honest reason, not "invalid_json".
3. ``max_tokens`` defaulted to 1200 — smaller than the JSON a full resume
   profile needs, so parses truncated on healthy models.
4. ``content`` arriving as a list of parts was ``json.dumps``ed, producing a
   JSON *array* whose first element (``{"type": "text", ...}``) masqueraded as
   the model's answer.
5. Reasoning models that draft the JSON inside ``reasoning_content`` were
   discarded even when the draft was complete and valid.
6. Modern OpenAI-compat models 400 on ``max_tokens``/``temperature``; the
   gateway only knew the ``response_format`` fix-up.

Every test drives the REAL gateway (``@pytest.mark.real_ai``) against a local
scripted OpenAI-compatible provider — no stubs, no outbound network.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional

import pytest

VALID_KEY = "sk-invalid-json-fix-key-1"

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
# Scripted OpenAI-compatible provider
# --------------------------------------------------------------------------- #
def ok(content: Any, finish: str = "stop") -> Dict[str, Any]:
    return {"status": 200, "body": {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300}}}


def empty(finish: str = "length", reasoning: str = "thinking about the resume…") -> Dict[str, Any]:
    return {"status": 200, "body": {
        "choices": [{"message": {"content": "", "reasoning_content": reasoning},
                     "finish_reason": finish}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 400, "total_tokens": 500}}}


def cut(json_text: str) -> Dict[str, Any]:
    """A JSON answer truncated mid-way by the token limit."""
    return {"status": 200, "body": {
        "choices": [{"message": {"content": json_text[:len(json_text) // 2]},
                     "finish_reason": "length"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 400, "total_tokens": 500}}}


def provider_error(status: int, message: str) -> Dict[str, Any]:
    return {"status": status, "body": {"error": {"message": message}}}


class ScriptedAIHandler(BaseHTTPRequestHandler):
    """Serves a scripted queue of responses; records every request body."""

    behavior: Dict[str, Any] = {"script": [], "requests": []}

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
        script: List[Dict[str, Any]] = self.behavior["script"]
        response = script.pop(0) if script else ok(json.dumps(GOOD_PROFILE))
        body = json.dumps(response["body"]).encode()
        self.send_response(response["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def scripted_provider():
    ScriptedAIHandler.behavior = {"script": [], "requests": []}
    server = HTTPServer(("127.0.0.1", 0), ScriptedAIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


@pytest.fixture()
def direct_ai(scripted_provider):
    """Point the env-level gateway config at the scripted provider."""
    import app.services.ai_client as ai_client

    ai_client._workflow_overrides.clear()
    ai_client._breakers.clear()
    ai_client.set_workflow_overrides(
        {"parse": {"base_url": scripted_provider, "api_key": VALID_KEY, "model": "test-model"}},
        user_id=0,
    )
    yield ai_client
    ai_client._workflow_overrides.clear()
    ai_client._breakers.clear()


def _wire_requests(ai_client) -> List[Dict[str, Any]]:
    return ScriptedAIHandler.behavior["requests"]


def _max_tokens_of(request_body: Dict[str, Any]) -> int:
    return request_body.get("max_tokens") or request_body.get("max_completion_tokens") or 0


# --------------------------------------------------------------------------- #
# 1. The reported bug: empty content (preview: '')
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
def test_empty_content_from_reasoning_model_recovers_with_more_tokens(direct_ai):
    """The exact reported outage: 200 + empty content + finish_reason=length.

    A reasoning model burned the whole budget thinking. The gateway must
    escalate max_tokens and retry instead of failing with invalid_json.
    """
    import asyncio

    ScriptedAIHandler.behavior["script"] = [empty(), ok(json.dumps(GOOD_PROFILE))]
    result = asyncio.run(direct_ai.chat_completion("parse", "extract the resume"))

    assert result["name"] == "Test Candidate"
    requests = _wire_requests(direct_ai)
    assert len(requests) == 2, "the empty answer must be retried, not raised"
    assert _max_tokens_of(requests[0]) == 4000          # the new floor default
    assert _max_tokens_of(requests[1]) > _max_tokens_of(requests[0])


@pytest.mark.real_ai
def test_empty_content_with_finish_stop_drops_response_format_and_retries(direct_ai):
    """Provider accepted json_object but emitted nothing (finish=stop).

    The targeted fix is a retry WITHOUT response_format, then the answer comes.
    """
    import asyncio

    ScriptedAIHandler.behavior["script"] = [empty(finish="stop"), empty(finish="stop"),
                                            ok(json.dumps(GOOD_PROFILE))]
    result = asyncio.run(direct_ai.chat_completion("parse", "extract the resume"))

    assert result["name"] == "Test Candidate"
    requests = _wire_requests(direct_ai)
    assert len(requests) >= 2
    assert "response_format" in requests[0]
    assert "response_format" not in requests[1]


@pytest.mark.real_ai
def test_persistent_empty_answer_reports_empty_response_not_invalid_json(direct_ai):
    """If every attempt comes back empty the reason must be honest."""
    import asyncio

    ScriptedAIHandler.behavior["script"] = [empty(finish="stop")] * 5
    with pytest.raises(direct_ai.AIClientError) as excinfo:
        asyncio.run(direct_ai.chat_completion("parse", "extract the resume"))

    assert excinfo.value.reason == "empty_response"
    assert "invalid_json" not in str(excinfo.value)
    # diagnostics stay machine-readable for the UI banner
    assert excinfo.value.diagnostics()["reason"] == "empty_response"


@pytest.mark.real_ai
def test_persistent_prose_answer_reports_invalid_json_not_unknown(direct_ai):
    """A model that keeps answering in prose (finish=stop, non-empty) must end
    with a precise invalid_json error — never a generic "unknown error"."""
    import asyncio

    ScriptedAIHandler.behavior["script"] = [ok("I am sorry, I cannot parse that document.")] * 5
    with pytest.raises(direct_ai.AIClientError) as excinfo:
        asyncio.run(direct_ai.chat_completion("parse", "extract the resume"))

    assert excinfo.value.reason == "invalid_json"
    assert "preview" in str(excinfo.value)


@pytest.mark.real_ai
def test_recovery_works_on_the_last_attempt(monkeypatch, direct_ai):
    """The retry decision must not fire when no attempt is left (otherwise the
    call fell through to a generic 'unknown error')."""
    import asyncio

    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 2)
    ScriptedAIHandler.behavior["script"] = [ok("first flaky prose answer"),
                                            empty(finish="stop"), empty(finish="stop")]
    with pytest.raises(direct_ai.AIClientError) as excinfo:
        asyncio.run(direct_ai.chat_completion("parse", "extract the resume"))

    assert excinfo.value.reason == "empty_response"
    assert "unknown" not in str(excinfo.value).lower().split("(")[0]


# --------------------------------------------------------------------------- #
# 2. Truncated JSON (finish_reason=length)
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
def test_truncated_json_is_retried_with_a_larger_budget(direct_ai):
    import asyncio

    ScriptedAIHandler.behavior["script"] = [
        cut(json.dumps(GOOD_PROFILE)), ok(json.dumps(GOOD_PROFILE))]
    result = asyncio.run(direct_ai.chat_completion("parse", "extract the resume"))

    assert result["name"] == "Test Candidate"
    requests = _wire_requests(direct_ai)
    assert len(requests) == 2
    assert _max_tokens_of(requests[1]) > _max_tokens_of(requests[0])


@pytest.mark.real_ai
def test_persistent_truncation_reports_truncated_response(direct_ai):
    import asyncio

    ScriptedAIHandler.behavior["script"] = [cut(json.dumps(GOOD_PROFILE))] * 5
    with pytest.raises(direct_ai.AIClientError) as excinfo:
        asyncio.run(direct_ai.chat_completion("parse", "extract the resume"))

    assert excinfo.value.reason == "truncated_response"
    assert "max_tokens" in str(excinfo.value)


@pytest.mark.real_ai
def test_content_filter_is_reported_without_pointless_retries(direct_ai):
    import asyncio

    filtered = {"status": 200, "body": {
        "choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10}}}
    ScriptedAIHandler.behavior["script"] = [filtered, filtered, filtered]
    with pytest.raises(direct_ai.AIClientError) as excinfo:
        asyncio.run(direct_ai.chat_completion("parse", "extract the resume"))

    assert excinfo.value.reason == "content_filter"
    assert len(_wire_requests(direct_ai)) == 1, "the same prompt would be blocked again"


# --------------------------------------------------------------------------- #
# 3. Malformed content shapes
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
def test_content_returned_as_parts_list_is_parsed(direct_ai):
    """Some providers return [{"type": "text", "text": "{...}"}] — the text
    parts must be joined, not json.dumpsed into a bogus array."""
    import asyncio

    parts = [{"type": "text", "text": json.dumps(GOOD_PROFILE)}]
    ScriptedAIHandler.behavior["script"] = [ok(parts)]
    result = asyncio.run(direct_ai.chat_completion("parse", "extract the resume"))

    assert result["name"] == "Test Candidate"
    assert len(_wire_requests(direct_ai)) == 1


@pytest.mark.real_ai
def test_null_content_does_not_crash_the_gateway(direct_ai):
    import asyncio

    null_content = {"status": 200, "body": {
        "choices": [{"message": {"content": None}, "finish_reason": "stop"}], "usage": {}}}
    ScriptedAIHandler.behavior["script"] = [null_content] * 5
    with pytest.raises(direct_ai.AIClientError) as excinfo:
        asyncio.run(direct_ai.chat_completion("parse", "extract the resume"))
    assert excinfo.value.reason == "empty_response"


@pytest.mark.real_ai
def test_json_in_reasoning_content_is_salvaged_without_a_second_call(direct_ai):
    """A reasoning model that drafted the final JSON in its reasoning channel:
    the complete draft must be used instead of failing."""
    import asyncio

    reasoning = ("First I will list the sections. "
                 'Draft: {"name": "Wrong Draft"} '
                 f"Refining… final answer: {json.dumps(GOOD_PROFILE)}")
    ScriptedAIHandler.behavior["script"] = [empty(finish="stop", reasoning=reasoning)]
    result = asyncio.run(direct_ai.chat_completion("parse", "extract the resume"))

    # the LAST complete draft is the model's final answer, not the first
    assert result["name"] == "Test Candidate"
    assert len(_wire_requests(direct_ai)) == 1, "no retry needed — the draft was salvaged"


@pytest.mark.real_ai
def test_fenced_json_with_prose_is_parsed_leniently(direct_ai):
    import asyncio

    answer = ("Sure! Here is the structured profile you asked for:\n\n"
              f"```json\n{json.dumps(GOOD_PROFILE)}\n```\n\nHope this helps.")
    ScriptedAIHandler.behavior["script"] = [ok(answer)]
    result = asyncio.run(direct_ai.chat_completion("parse", "extract the resume"))
    assert result["name"] == "Test Candidate"


# --------------------------------------------------------------------------- #
# 4. Provider parameter fix-ups (400s that name the offending parameter)
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
def test_huggingface_structured_outputs_rejection_retries_without_response_format(direct_ai):
    """Hugging Face router 400s json_object as unsupported structured-outputs.

    The error body does not mention ``response_format``, so the generic
    OpenAI/Google field-name matchers would miss it.
    """
    import asyncio

    hf_body = (
        '{"code":400, "reason":"INVALID_REQUEST_BODY", '
        '"message":"model: inclusionai/ling-3.0-flash-fin does not support '
        'feature: structured-outputs", "metadata":{}}'
    )
    ScriptedAIHandler.behavior["script"] = [
        {"status": 400, "body": json.loads(hf_body)},
        ok(json.dumps(GOOD_PROFILE)),
    ]
    result = asyncio.run(direct_ai.chat_completion("parse", "extract the resume"))

    assert result["name"] == "Test Candidate"
    requests = _wire_requests(direct_ai)
    assert "response_format" in requests[0]
    assert "response_format" not in requests[1]


@pytest.mark.real_ai
def test_provider_requiring_max_completion_tokens_is_renamed(direct_ai):
    import asyncio

    ScriptedAIHandler.behavior["script"] = [
        provider_error(400, "Unsupported parameter: 'max_tokens' is not supported with this "
                            "model. Use 'max_completion_tokens' instead."),
        ok(json.dumps(GOOD_PROFILE)),
    ]
    result = asyncio.run(direct_ai.chat_completion("parse", "extract the resume"))

    assert result["name"] == "Test Candidate"
    requests = _wire_requests(direct_ai)
    assert "max_tokens" in requests[0] and "max_completion_tokens" not in requests[0]
    assert "max_completion_tokens" in requests[1]


@pytest.mark.real_ai
def test_provider_rejecting_temperature_retries_without_it(direct_ai):
    import asyncio

    ScriptedAIHandler.behavior["script"] = [
        provider_error(400, "Unsupported parameter: 'temperature' is not supported with this model."),
        ok(json.dumps(GOOD_PROFILE)),
    ]
    result = asyncio.run(direct_ai.chat_completion("parse", "extract the resume"))

    assert result["name"] == "Test Candidate"
    requests = _wire_requests(direct_ai)
    assert "temperature" in requests[0] and "temperature" not in requests[1]


@pytest.mark.real_ai
def test_escalated_max_tokens_above_the_model_limit_is_clamped(direct_ai):
    """The auto-escalation must not wedge on providers with a smaller cap."""
    import asyncio

    # First answer truncates → escalation → 400 says the limit is lower → clamp.
    ScriptedAIHandler.behavior["script"] = [
        cut(json.dumps(GOOD_PROFILE)),
        provider_error(400, "'max_tokens' is too large: 16000. This model supports at most 8192."),
        ok(json.dumps(GOOD_PROFILE)),
    ]
    result = asyncio.run(direct_ai.chat_completion("parse", "extract the resume"))

    assert result["name"] == "Test Candidate"
    budgets = [_max_tokens_of(r) for r in _wire_requests(direct_ai)]
    assert budgets[1] > budgets[0] and budgets[2] < budgets[1]


# --------------------------------------------------------------------------- #
# 5. Lenient JSON extraction unit tests
# --------------------------------------------------------------------------- #
def test_lenient_json_extraction_variants():
    from app.services.ai_client import _extract_json_lenient

    obj = {"name": "Test Candidate", "skills": ["python"]}
    # clean
    assert _extract_json_lenient(json.dumps(obj)) == obj
    # fenced, with prose around it
    assert _extract_json_lenient(f"Here you go:\n```json\n{json.dumps(obj)}\n```\nThanks!") == obj
    # several fenced blocks → the LAST one is the model's final answer
    older = {"name": "Old Draft"}
    fenced = f"```json\n{json.dumps(older)}\n```\nmid prose\n```json\n{json.dumps(obj)}\n```"
    assert _extract_json_lenient(fenced) == obj
    # prose-wrapped without fences
    assert _extract_json_lenient(f"The answer is {json.dumps(obj)} — enjoy.") == obj
    # BOM and zero-width characters (some providers inject them)
    dirty = "﻿​" + json.dumps(obj)
    assert _extract_json_lenient(dirty) == obj
    # a draft inside reasoning, followed by the final complete draft
    reasoning = f'Let me think… {json.dumps(older)} …better: {json.dumps(obj)} done.'
    assert _extract_json_lenient(reasoning) == obj
    # nothing JSON at all
    assert _extract_json_lenient("I cannot parse this document, sorry!") is None
    assert _extract_json_lenient("") is None
    assert _extract_json_lenient(None) is None  # type: ignore[arg-type]


def test_max_output_tokens_default_is_large_enough_for_a_resume():
    """A large resume parse must fit: 1200 truncated every one of them.

    The shipped default is now ``0`` = **unlimited** (no clamp — the provider's
    own per-model maximum, and the escalation on truncation is free to grow),
    which fits any profile by construction. An operator who sets an explicit
    ceiling instead must still be given the old floor of 4000, and the parse
    task's own starting budget carries it either way.
    """
    from app.core.config import settings
    from app.services.resume_parser import PARSE_OUTPUT_TOKENS

    assert settings.ai_max_output_tokens == 0 or settings.ai_max_output_tokens >= 4000, \
        "an explicit AI_MAX_OUTPUT_TOKENS below 4000 truncates every large resume parse"
    assert PARSE_OUTPUT_TOKENS >= 4000
    # …and an unlimited ceiling must not clamp that starting budget down.
    from app.services.ai_client import escalation_ceiling, resolve_output_ceiling

    assert resolve_output_ceiling(0) == 0
    assert resolve_output_ceiling(0, 16000) == 0, "an explicit 0 is unlimited, not 'unset'"
    assert escalation_ceiling(0) > 16000, \
        "unlimited must let the escalation grow past the old shipped ceiling"


# --------------------------------------------------------------------------- #
# 6. The user's actual flow: upload a resume against a flaky provider
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
def test_resume_upload_survives_a_flaky_reasoning_provider(client, auth, scripted_provider, db):
    """End-to-end: the exact scenario from the bug report. The provider's
    first parse answer is empty (thinking used the budget); the second one is
    the profile. The upload must succeed and persist the AI-extracted profile.
    """
    from tests.conftest import pdf_bytes

    saved = client.put("/api/settings", json={
        "ai": {"base_url": scripted_provider, "model": "test-model",
               "rpm": 600, "api_key": VALID_KEY},
    }, headers=auth)
    assert saved.status_code == 200, saved.text

    ScriptedAIHandler.behavior["script"] = [empty(), ok(json.dumps(GOOD_PROFILE))]

    response = client.post(
        "/api/resume/upload",
        files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
        headers=auth,
    )
    assert response.status_code == 200, response.text

    from app.models.models import Profile, Resume

    profile = db.query(Profile).first()
    assert profile is not None, "the AI-extracted profile must be persisted"
    assert profile.data["name"] == "Test Candidate"
    assert profile.extraction_source == "ai"
    resume = db.query(Resume).filter(Resume.type == "master").first()
    assert resume is not None and resume.status == "approved"

    # The recovery really happened on the wire: first attempt empty, second
    # attempt with a larger budget succeeded.
    def _is_parse_call(request_body: Dict[str, Any]) -> bool:
        contents = [str(m.get("content") or "") for m in request_body.get("messages") or []]
        return any("extract a complete, structured profile" in c.lower() for c in contents)

    parse_calls = [r for r in ScriptedAIHandler.behavior["requests"] if _is_parse_call(r)]
    assert len(parse_calls) == 2
    assert _max_tokens_of(parse_calls[1]) > _max_tokens_of(parse_calls[0])


@pytest.mark.real_ai
def test_resume_upload_reports_honest_reason_when_provider_stays_empty(client, auth, scripted_provider):
    """When the provider never produces an answer the UI must show the real
    reason (empty_response) instead of 'text that is not valid JSON'."""
    from tests.conftest import pdf_bytes

    saved = client.put("/api/settings", json={
        "ai": {"base_url": scripted_provider, "model": "test-model",
               "rpm": 600, "api_key": VALID_KEY},
    }, headers=auth)
    assert saved.status_code == 200, saved.text

    ScriptedAIHandler.behavior["script"] = [empty(finish="stop")] * 8

    response = client.post(
        "/api/resume/upload",
        files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
        headers=auth,
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "ai_unavailable"
    assert body["reason"] == "empty_response"
    assert body["workflow"] == "parse"
    assert body["fix"], "the UI needs an actionable fix"
