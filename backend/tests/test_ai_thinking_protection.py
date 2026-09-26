"""
Think-phase protection — neither the AI provider nor Laya is stopped while
the model is still thinking.

Two halves, one theme:

* **The AI provider (streaming).** A reasoning model can sit silent for its
  whole thinking phase — SSE bytes arrive only once content does. The stream's
  idle-read bound therefore follows the call's wait budget
  (``AI_STREAM_IDLE_TIMEOUT=0`` default): a stream is never aborted
  mid-think before the caller's budget elapses, and a positive setting caps
  silence earlier while staying clamped to the budget. Streaming is also the
  norm for every workflow, including the assisted-fill live field mapping.
* **Laya (no stream — failure accounting instead).** A forward pass is a
  single typed call, so the protection is: a caller whose budget elapsed while
  its pass was queued reports ``engine_busy`` — queue pressure, never an
  engine failure, so it cannot park the engine; a pass abandoned for running
  past the budget credits the engine back when it finishes cleanly in its
  thread (unless something newer failed); crashes and genuine hangs still park.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict

import httpx
import pytest

import app.services.ai_client as ai_client
from app.core import metrics
from app.services import laya as laya_service

VALID_KEY = "sk-think-protection-key-1"
GOOD_ANSWER = json.dumps({"answer": "thought about it", "confidence": 0.9})


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Scripted provider: a "reasoning" stream — silent while thinking, then SSE
# --------------------------------------------------------------------------- #
class ThinkingStreamHandler(BaseHTTPRequestHandler):
    """Streaming chat endpoint that stays silent for `silence` seconds (the
    thinking phase — no bytes on the wire) before the SSE answer arrives."""

    behavior: Dict[str, Any] = {"silence": 0.0}

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
        self.rfile.read(length or 0)
        time.sleep(float(self.behavior.get("silence") or 0.0))
        chunk = {"choices": [{"delta": {"content": GOOD_ANSWER}, "finish_reason": None}]}
        final = {"choices": [{"delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for ch in (chunk, final):
                self.wfile.write(f"data: {json.dumps(ch)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client already gave up — the provider was thinking anyway


@pytest.fixture()
def thinking_provider():
    ThinkingStreamHandler.behavior = {"silence": 0.0}
    server = HTTPServer(("127.0.0.1", 0), ThinkingStreamHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


@pytest.fixture()
def direct_stream_ai(thinking_provider):
    """Point the env-level gateway config at the scripted streaming provider."""
    ai_client._workflow_overrides.clear()
    ai_client._breakers.clear()
    ai_client._PING_CACHE.clear()
    ai_client.set_workflow_overrides(
        {"parse": {"base_url": thinking_provider, "api_key": VALID_KEY,
                   "model": "z-ai/glm-5.3-free"}},
        user_id=0,
    )
    yield ai_client
    ai_client._workflow_overrides.clear()
    ai_client._breakers.clear()
    ai_client._PING_CACHE.clear()


def _capture_stream_timeouts(monkeypatch) -> list:
    """Record every httpx.Timeout the gateway hands to ``client.stream``."""
    captured = []
    original = httpx.AsyncClient.stream

    def _capture(self, method, url, **kwargs):
        timeout = kwargs.get("timeout")
        if isinstance(timeout, httpx.Timeout):
            captured.append(timeout)
        return original(self, method, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "stream", _capture)
    return captured


# --------------------------------------------------------------------------- #
# 1. The stream's idle-read bound follows the budget — thinking is not aborted
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
def test_stream_idle_timeout_follows_the_call_budget(direct_stream_ai, monkeypatch):
    """Default (AI_STREAM_IDLE_TIMEOUT=0): the streamed read timeout is
    the call's whole budget — a reasoning model silent mid-think is never cut
    off by an arbitrary short idle cap. Connect stays short so an unreachable
    endpoint still fails fast."""
    captured = _capture_stream_timeouts(monkeypatch)
    monkeypatch.setattr("app.core.config.settings.ai_stream_idle_timeout", 0.0)

    result = run(ai_client.chat_completion("parse", "think hard, then answer",
                                           stream=True, timeout=300.0))
    assert result.get("answer") == "thought about it"

    assert captured, "the gateway must issue a streaming request"
    timeout = captured[-1]
    assert timeout.read == 300.0, "idle-read must follow the budget (was min(60, budget))"
    assert timeout.connect == 10.0  # ai_connect_timeout, not a hardcoded 10 vs setting mismatch


@pytest.mark.real_ai
def test_stream_idle_override_caps_silence_and_stays_within_the_budget(direct_stream_ai, monkeypatch):
    """A positive AI_STREAM_IDLE_TIMEOUT bounds silence earlier (an
    operator's tighter stall bound) — but never beyond the call budget."""
    captured = _capture_stream_timeouts(monkeypatch)
    monkeypatch.setattr("app.core.config.settings.ai_stream_idle_timeout", 5.0)
    run(ai_client.chat_completion("parse", "hello", stream=True, timeout=300.0))
    assert captured[-1].read == 5.0

    captured.clear()
    monkeypatch.setattr("app.core.config.settings.ai_stream_idle_timeout", 5000.0)
    run(ai_client.chat_completion("parse", "hello", stream=True, timeout=300.0))
    assert captured[-1].read == 300.0, "the override is clamped to the call budget"


@pytest.mark.real_ai
def test_a_silent_thinking_stream_completes(direct_stream_ai, monkeypatch):
    """End to end: the provider thinks silently for a while, then streams the
    answer — within the budget the call must succeed (no idle abort, no retry,
    no `timeout` error)."""
    monkeypatch.setattr("app.core.config.settings.ai_stream_idle_timeout", 0.0)
    ThinkingStreamHandler.behavior = {"silence": 1.0}
    started = time.monotonic()
    result = run(ai_client.chat_completion("parse", "take your time", stream=True, timeout=5.0))
    assert result.get("answer") == "thought about it"
    assert time.monotonic() - started < 4.5, "the answer must arrive right after the think phase"


@pytest.mark.real_ai
def test_idle_override_aborts_a_stream_silent_past_the_cap(direct_stream_ai, monkeypatch):
    """The override is real: silence beyond it aborts the stream with the
    honest `timeout` reason — well before the caller's own budget elapses."""
    monkeypatch.setattr("app.core.config.settings.ai_stream_idle_timeout", 0.3)
    ThinkingStreamHandler.behavior = {"silence": 1.0}
    started = time.monotonic()
    with pytest.raises(ai_client.AIClientError) as excinfo:
        run(ai_client.chat_completion("parse", "hello", stream=True, timeout=5.0))
    assert excinfo.value.reason == "timeout"
    assert time.monotonic() - started < 4.0, "the idle cap — not the budget — ended the wait"


def test_assisted_fill_live_mapping_streams(monkeypatch):
    """Streaming is active for the assisted-fill live field mapping too — the
    last workflow that still opted out (a silent reasoning model there used to
    ride the non-streaming wait, which aborts mid-think)."""
    import app.services.assisted_fill as assisted_fill

    captured: Dict[str, Any] = {}

    async def _fake_chat_completion(workflow, prompt, **kwargs):
        captured.update(kwargs)
        captured["workflow"] = workflow
        return {"mapping": {}}

    monkeypatch.setattr(ai_client, "chat_completion", _fake_chat_completion)
    # Laya is not installed here: the mapping falls through to the LLM.
    monkeypatch.setattr(laya_service, "should_attempt", lambda *a, **k: False)
    run(assisted_fill.ai_map_unknown_fields(
        {"fields": [{"name": "app_portal_ref",
                     "label": "Where did you hear about this role?", "type": "text"}]},
        profile_keys=["firstName"], profile_sample={"firstName": "Test"},
        db=None, user_id=None))

    assert captured.get("workflow") == "form_detect_live"
    assert captured.get("stream") is True, "the live mapping call must stream"


# --------------------------------------------------------------------------- #
# 2. Laya — queue pressure is `engine_busy`, never an engine failure
# --------------------------------------------------------------------------- #
class _SlowRouter:
    """Finishes after `delay` seconds with a well-formed payload."""

    def __init__(self, delay: float):
        self.delay = delay

    def predict(self, state, questions, **kwargs):
        time.sleep(self.delay)
        return {"answers": {}, "routing": {"model": "english"}}


class _HungRouter:
    """A genuinely stuck engine: hangs until `hang` elapses, then crashes."""

    def __init__(self, hang: float):
        self.hang = hang

    def predict(self, state, questions, **kwargs):
        time.sleep(self.hang)
        raise RuntimeError("checkpoint wedged")


@pytest.fixture()
def laya_fake_router(monkeypatch):
    """Route Laya at a caller-supplied router object (installed, importable)."""
    monkeypatch.setattr(laya_service, "installed", lambda: True)
    holder: Dict[str, Any] = {}

    def install(router):
        holder["router"] = router
        monkeypatch.setattr(laya_service, "_get_router", lambda: router)
        laya_service.reset_for_tests()

    yield install
    laya_service.reset_for_tests()


def test_engine_busy_is_not_a_failure_and_never_parks(laya_fake_router):
    """A caller whose budget elapsed while its pass was queued behind a running
    one reports `engine_busy`: no failure recorded, the park threshold does not
    move, and the pass that was thinking completes untouched."""
    laya_fake_router(_SlowRouter(delay=0.6))

    async def scenario():
        task1 = asyncio.ensure_future(laya_service.predict({"q": "first"}, "state", timeout=10))
        deadline = time.monotonic() + 5
        while laya_service.in_flight_passes() < 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert laya_service.in_flight_passes() == 1, "the first pass must be mid-think"

        task2 = asyncio.ensure_future(laya_service.predict({"q": "second"}, "state", timeout=0.3))
        with pytest.raises(laya_service.LayaUnavailable) as excinfo:
            await task2
        assert excinfo.value.code == "engine_busy"

        # Queue pressure is not engine damage: nothing failed, nothing parked.
        assert laya_service.parked() is False
        failures, _last = laya_service._failure_state()
        assert failures == 0

        result = await task1
        assert isinstance(result, dict), "the thinking pass completes and answers"

    run(scenario())


def test_abandoned_pass_finishing_late_credits_the_engine(laya_fake_router):
    """A pass abandoned for running past its caller's budget still reports
    `timeout` — but when it finishes cleanly in its thread, the engine is
    credited back instead of being left counted as broken."""
    laya_fake_router(_SlowRouter(delay=0.4))

    async def scenario():
        task = asyncio.ensure_future(laya_service.predict({"q": "x"}, "state", timeout=0.05))
        with pytest.raises(laya_service.LayaUnavailable) as excinfo:
            await task
        assert excinfo.value.code == "timeout"
        failures, _last = laya_service._failure_state()
        assert failures == 1, "the real timeout is recorded"

        # The abandoned pass keeps thinking in its thread; wait for it to
        # finish, then for the credit to land.
        deadline = time.monotonic() + 5
        while laya_service.in_flight_passes() == 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        failures, _last = laya_service._failure_state()
        assert failures == 0, "the finished think-phase credits the engine back"
        assert _last is None
        assert laya_service.parked() is False

    run(scenario())
    assert metrics.render_prometheus().count("laya_predict_total") >= 1


def test_late_credit_is_skipped_when_something_newer_failed(laya_fake_router):
    """A crash recorded after the abandonment is newer information about the
    engine than the late success — the credit must not erase it."""
    laya_fake_router(_SlowRouter(delay=0.4))

    async def scenario():
        task = asyncio.ensure_future(laya_service.predict({"q": "x"}, "state", timeout=0.05))
        with pytest.raises(laya_service.LayaUnavailable):
            await task
        laya_service._record_failure()  # e.g. another pass crashed in the meantime
        failures_before, _ = laya_service._failure_state()
        assert failures_before == 2

        deadline = time.monotonic() + 5
        while laya_service.in_flight_passes() == 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        failures, _ = laya_service._failure_state()
        assert failures == 2, "the late finish must not reset newer failures"

    run(scenario())


def test_a_genuinely_stuck_engine_still_parks(laya_fake_router):
    """The park stays a real protection: a pass that never finishes (hangs,
    then crashes) counts its timeout every time and benches the engine — a
    slow thinker is not parked, a stuck one still is."""
    laya_fake_router(_HungRouter(hang=0.4))
    for _ in range(laya_service._FAILURE_THRESHOLD):
        with pytest.raises(laya_service.LayaUnavailable) as excinfo:
            run(laya_service.predict({"q": "x"}, "state", timeout=0.05))
        assert excinfo.value.code == "timeout"
    assert laya_service.parked() is True
    with pytest.raises(laya_service.LayaUnavailable) as excinfo:
        run(laya_service.predict({"q": "x"}, "state", timeout=5))
    assert excinfo.value.code == "engine_parked"


def test_status_reports_in_flight_and_parked(laya_fake_router):
    """`in_flight` / `parked` make the think-phase state observable: a non-zero
    `in_flight` means a forward pass is running right now."""
    laya_fake_router(_SlowRouter(delay=0.0))
    run(laya_service.predict({"q": "x"}, "state"))

    status = laya_service.status()
    assert status["in_flight"] == 0
    assert status["parked"] is False


# --------------------------------------------------------------------------- #
# 3. The decision log tells busy apart from timeout / error / parked
# --------------------------------------------------------------------------- #
def test_engine_busy_is_recorded_as_busy_in_the_decision_log(db, owner, laya_fake_router,
                                                             monkeypatch):
    """The owner-only decision log keeps `busy` as its own status — 'the caller
    gave up on a busy engine' is not 'the engine failed', and the health mix
    must not read as an outage."""
    from app.models.models import User
    from app.services import ai_log

    monkeypatch.setattr(ai_log, "enabled", lambda: True)
    laya_fake_router(_SlowRouter(delay=0.6))
    user_id = db.query(User.id).filter(User.email == "owner@example.com").scalar()
    assert user_id is not None, "the owner fixture must have run"

    async def scenario():
        task1 = asyncio.ensure_future(laya_service.predict(
            {"q": "first"}, "state", timeout=10, task="ranking", db=db, user_id=user_id))
        deadline = time.monotonic() + 5
        while laya_service.in_flight_passes() < 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        task2 = asyncio.ensure_future(laya_service.predict(
            {"q": "second"}, "state", timeout=0.3, task="ranking", db=db, user_id=user_id))
        with pytest.raises(laya_service.LayaUnavailable):
            await task2
        await task1

    run(scenario())

    db.rollback()
    db.expire_all()
    rows = ai_log.recent_laya_decisions(db, limit=5)
    statuses = [row["status"] for row in rows]
    assert statuses.count(ai_log.LAYA_BUSY) == 1, statuses
    assert statuses.count(ai_log.LAYA_OK) == 1, statuses
    totals = ai_log.laya_stats(db)["totals"]
    assert totals["busy"] == 1 and totals["ok"] == 1
