"""v2.3 — the owner-only AI log: the exact request sent, and how Laya is doing.

Contract under test:

* **"What exactly did we send?"** — every provider call stores the outbound body
  it actually put on the wire (one entry per attempt, so an escalation is
  visible as an escalation), the routing parameters, the outcome, tokens/cost
  and a bounded excerpt of the answer. The test drives the *real* gateway
  against a local scripted provider and compares the stored body with what the
  provider received — the log is the wire, not a re-render of it.
* **"How is Laya doing?"** — one row per forward pass with task, status
  (``ok`` / ``low_confidence`` / ``timeout`` / ``error`` / ``parked``), the
  checkpoint, question count, calibrated confidence against the active floor
  and latency.
* **Owner-only.** Every ``/api/admin/ai/*`` and ``/api/admin/laya/*`` route is
  behind the router-level ``require_owner``: anonymous → 401, member → 403
  ``owner_required``, owner → 200. Nothing on the user surface reads either
  table.
* **Bounded twice.** Retention deletes rows past ``AI_LOG_RETENTION_DAYS``; the
  stored bodies are clipped, with the clip marked in the payload itself, and
  secret-shaped values are masked before they are stored.
* **Never in the way.** An observability write that cannot complete is
  swallowed: the AI call it describes is unaffected.

Hermetic: the provider is the conftest scripted server, Laya is a fake router
(``laya`` is an optional install) and no test touches the network.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import conftest
import pytest
from sqlalchemy.orm import Session

from app.models.models import AICallRecord, LayaDecision, User
from app.services import ai_log
from app.services import laya as laya_service

pytestmark = pytest.mark.real_ai


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_log_state(monkeypatch):
    """No process-global gateway state leaks between tests, and no rate limit."""
    import app.services.ai_client as ai_client

    ai_client._breakers.clear()
    ai_client._PING_CACHE.clear()
    ai_log.reset_prune_clock()
    yield
    ai_client._breakers.clear()
    ai_client._PING_CACHE.clear()
    ai_log.reset_prune_clock()


def _owner_id(db: Session) -> int:
    db.rollback()
    value = db.query(User.id).filter(User.email == "owner@example.com").scalar()
    assert value is not None, "the owner fixture must have run"
    return int(value)


@pytest.fixture()
def owner_id(owner, db) -> int:
    """The bootstrap account's id, as the :func:`db` session sees it."""
    return _owner_id(db)


def _sync(db: Session) -> None:
    """End any open read transaction so rows written elsewhere are visible."""
    db.rollback()
    db.expire_all()


def _wire() -> list:
    return conftest.ScriptedAIHandler.behavior["requests"]


# --------------------------------------------------------------------------- #
# Access: the log is for the owner, and only the owner
# --------------------------------------------------------------------------- #
OWNER_GETS = [
    "/api/admin/ai/calls",
    "/api/admin/ai/calls/1",
    "/api/admin/ai/stats",
    "/api/admin/laya/decisions",
    "/api/admin/laya/stats",
]


def test_ai_log_is_owner_only(client, auth, member_auth):
    for url in OWNER_GETS:
        assert client.get(url).status_code == 401, url          # anonymous
        member = client.get(url, headers=member_auth)
        assert member.status_code == 403, (url, member.status_code)
        assert member.json()["detail"]["code"] == "owner_required", url
        # ``/calls/1`` legitimately 404s for the owner (nothing logged yet);
        # everything else answers 200. Member never gets past the gate either way.
        if not url.endswith("/1"):
            assert client.get(url, headers=auth).status_code == 200, url


def test_missing_call_detail_is_404_not_empty(client, auth):
    assert client.get("/api/admin/ai/calls/999999", headers=auth).status_code == 404


def test_user_surface_does_not_expose_the_log(client, auth):
    """There is no user-facing log route — only the self-scoped audit trail."""
    assert client.get("/api/account/ai-log", headers=auth).status_code == 404
    assert client.get("/api/ai/log", headers=auth).status_code == 404


# --------------------------------------------------------------------------- #
# "What exactly did we send?"
# --------------------------------------------------------------------------- #
def test_successful_call_is_logged_with_the_exact_request(client, auth, db, owner_id, provider_owner):
    """The stored body is the body the provider received — byte for byte."""
    import app.services.ai_client as ai_client

    prompt = "score how well this candidate matches the job: Python, FastAPI, Kubernetes"
    result = run(ai_client.chat_completion("scoring", prompt, db=db, user_id=owner_id))
    assert result.get("score") is not None, result

    _sync(db)
    rows = ai_log.recent_calls(db, limit=5)
    assert len(rows) == 1, rows
    row = rows[0]
    assert (row["workflow"], row["status"], row["user_id"]) == ("scoring", "ok", owner_id)
    assert row["model"] and row["provider"] == "openai_compatible"
    assert row["base_url"].startswith("http://127.0.0.1:")
    assert row["total_tokens"] > 0 and row["latency_ms"] >= 0
    assert prompt[:40] in row["preview"]

    detail = ai_log.get_call(db, row["id"])
    assert detail is not None
    attempts = detail["request"]["attempts"]
    assert len(attempts) == 1, "one healthy attempt must be one stored body"
    stored = attempts[0]["body"]
    assert stored["messages"][-1]["content"].endswith("Kubernetes")
    assert stored["messages"][-1]["content"] == prompt
    # The ground truth on the wire.
    assert stored == _wire()[-1]
    assert detail["response"], "the answer excerpt must be stored"


def test_the_detail_route_returns_bodies_the_list_route_does_not(client, auth, db, owner_id, provider_owner):
    """The list is cheap (metadata); the bodies come from the detail route."""
    import app.services.ai_client as ai_client

    run(ai_client.chat_completion("tagging", "create 3-5 short lowercase hyphenated tags",
                                  db=db, user_id=owner_id))
    _sync(db)
    listed = client.get("/api/admin/ai/calls", headers=auth).json()["items"]
    assert listed and "request" not in listed[0] and "response" not in listed[0]
    assert listed[0]["preview"]

    detail = client.get(f"/api/admin/ai/calls/{listed[0]['id']}", headers=auth).json()
    assert detail["request"]["attempts"][0]["body"]["messages"]
    assert detail["request_url"].startswith("http://127.0.0.1:")


def test_failed_call_is_logged_with_reason(client, auth, db, owner_id, provider_owner):
    """An outage is diagnosable from the log alone: status, reason, HTTP status."""
    import app.services.ai_client as ai_client

    conftest.ScriptedAIHandler.behavior.update({"status": 503, "fail_after": 0})
    with pytest.raises(ai_client.AIClientError):
        run(ai_client.chat_completion("scoring", "score this", db=db, user_id=owner_id))

    _sync(db)
    rows = ai_log.recent_calls(db, limit=5)
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "error"
    assert rows[0]["http_status"] == 503
    assert rows[0]["reason"], "a failure must carry a machine reason"
    assert rows[0]["attempts"] >= 2, "the gateway retried the 503 — the log knows"
    assert ai_log.get_call(db, rows[0]["id"])["request"]["attempts"], "bodies kept on failure too"


def test_preflight_failure_is_logged_too(client, auth, db, owner_id, provider_owner):
    """A refusal raised *before* the payload exists is still a logged call.

    The gateway raises for `circuit_open` / `budget_exhausted` / `limit_exceeded`
    before it builds a request, so those diagnostics are declared up front; this
    test is the regression guard for a failure path that reached for an unbound
    local instead (the log must never turn an honest refusal into a TypeError).
    """
    import app.services.ai_client as ai_client
    from app.core.config import settings

    breaker = ai_client._breaker("scoring")
    for _ in range(max(1, int(settings.ai_breaker_failures))):
        breaker.record_failure("invalid_json: model did not return valid JSON")
    assert breaker.is_open()

    with pytest.raises(ai_client.AIClientError, match="circuit_open"):
        run(ai_client.chat_completion("scoring", "score this", db=db, user_id=owner_id))

    _sync(db)
    rows = ai_log.recent_calls(db, limit=5)
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "error"
    assert rows[0]["reason"] == "circuit_open"
    assert rows[0]["attempt_count_sent"] == 0, "nothing was sent, and the log says so"
    assert rows[0]["http_status"] is None


def test_escalation_is_visible_as_more_attempts(client, auth, db, owner_id, provider_owner):
    """A token escalation stores every body it tried, not just the last one."""
    import app.services.ai_client as ai_client
    from app.services.ai_client import _MAX_LOGGED_ATTEMPTS

    conftest.ScriptedAIHandler.behavior["override"] = [
        # First answer is empty with finish_reason=length → the gateway retries
        # with a bigger budget; the second answer is a usable verdict.
        {"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "usage": {}},
    ]
    run(ai_client.chat_completion("scoring", "score how well this candidate matches",
                                  db=db, user_id=owner_id))

    _sync(db)
    detail = ai_log.get_call(db, ai_log.recent_calls(db, limit=1)[0]["id"])
    bodies = [item["body"] for item in detail["request"]["attempts"]]
    assert len(bodies) >= 2, bodies
    budgets = [body.get("max_tokens") or body.get("max_completion_tokens") for body in bodies]
    assert budgets[-1] > budgets[0], budgets
    assert len(bodies) <= _MAX_LOGGED_ATTEMPTS, "the stored history stays bounded"


# --------------------------------------------------------------------------- #
# "How is Laya doing?"
# --------------------------------------------------------------------------- #
class FakeRouter:
    def __init__(self, fn):
        self.fn = fn

    def predict(self, state, questions, **kwargs):
        return {"answers": {key: self.fn(key, question) for key, question in questions.items()},
                "routing": {"model": "english"}}


class DeadRouter:
    def predict(self, state, questions, **kwargs):
        raise RuntimeError("checkpoint exploded")


@pytest.fixture()
def laya_fake(monkeypatch):
    monkeypatch.setattr(laya_service, "installed", lambda: True)

    def install(fn):
        monkeypatch.setattr(laya_service, "_get_router", lambda: FakeRouter(fn))
        laya_service.reset_for_tests()

    yield install
    laya_service.reset_for_tests()


def _questions(count: int = 3) -> dict:
    return {f"dim_{i}": {"type": "score", "instructions": "how good?", "criteria": ["a", "b"]}
            for i in range(count)}


def _answer(confidence: float, level: int = 3) -> dict:
    probs = {str(i): (confidence if i == level else (1.0 - confidence) / 4.0) for i in range(5)}
    return {"score": level, "confidence": confidence, "probs": probs}


def test_answered_pass_is_recorded_with_task_confidence_and_latency(db, owner_id, laya_fake):
    laya_fake(lambda key, q: _answer(0.9))
    run(laya_service.predict(_questions(), "state", task="ranking", db=db, user_id=owner_id))

    _sync(db)
    rows = ai_log.recent_laya_decisions(db)
    assert len(rows) == 1
    row = rows[0]
    assert (row["task"], row["status"]) == ("ranking", "ok")
    assert row["questions"] == 3
    assert row["confidence"] == pytest.approx(0.9, abs=0.001)
    assert row["floor"] == pytest.approx(0.55, abs=0.001)
    assert row["strict"] is False
    assert row["model"] == "english"
    assert row["answers"]["dim_0"]["score"] == 3


def test_low_confidence_pass_is_recorded_as_such(db, owner_id, laya_fake):
    """Below the floor in ``auto`` mode: the pass the LLM takes over from."""
    laya_fake(lambda key, q: _answer(0.2))
    run(laya_service.predict(_questions(2), "state", task="ranking", db=db, user_id=owner_id))

    _sync(db)
    row = ai_log.recent_laya_decisions(db)[0]
    assert row["status"] == ai_log.LAYA_LOW_CONFIDENCE
    assert row["confidence"] == pytest.approx(0.2, abs=0.001)
    assert ai_log.laya_stats(db)["totals"]["low_confidence"] == 1


def test_engine_failures_are_recorded_by_kind(db, owner_id, laya_fake, monkeypatch):
    """timeout / error / parked are three different rows — that is the point."""
    import time

    # 1. timeout — a checkpoint that takes longer than the budget it was given.
    laya_fake(lambda key, q: time.sleep(0.4) or _answer(0.9))
    with pytest.raises(laya_service.LayaUnavailable) as timeout_exc:
        run(laya_service.predict(_questions(1), "state", task="classification",
                                 timeout=0.05, db=db, user_id=owner_id))
    assert timeout_exc.value.code == "timeout"

    # 2. error — the engine itself raised.
    monkeypatch.setattr(laya_service, "_get_router", lambda: DeadRouter())
    with pytest.raises(laya_service.LayaUnavailable) as error_exc:
        run(laya_service.predict(_questions(1), "state", task="field_mapping", db=db, user_id=owner_id))
    assert error_exc.value.code == "predict_failed"

    # 3. parked — benched after repeated failures: a distinct reason, not an "error".
    laya_fake(lambda key, q: _answer(0.9))
    monkeypatch.setattr(laya_service, "parked", lambda: True)
    with pytest.raises(laya_service.LayaUnavailable) as parked_exc:
        run(laya_service.predict(_questions(1), "state", task="ranking", db=db, user_id=owner_id))
    assert parked_exc.value.code == "engine_parked"

    _sync(db)
    rows = ai_log.recent_laya_decisions(db, limit=10)
    by_task = {row["task"]: row for row in rows}
    assert by_task["classification"]["status"] == ai_log.LAYA_TIMEOUT
    assert by_task["field_mapping"]["status"] == ai_log.LAYA_ERROR
    assert by_task["ranking"]["status"] == ai_log.LAYA_PARKED
    assert by_task["field_mapping"]["error"]
    assert by_task["classification"]["confidence"] is None
    stats = ai_log.laya_stats(db)["totals"]
    assert stats["decisions"] == 3 and stats["error"] == 1 and stats["parked"] == 1


def test_laya_stats_roll_up_per_task(db, owner_id, laya_fake):
    laya_fake(lambda key, q: _answer(0.9))
    run(laya_service.predict(_questions(), "state", task="ranking", db=db, user_id=owner_id))
    run(laya_service.predict(_questions(1), "state", task="classification", db=db, user_id=owner_id))

    _sync(db)
    stats = ai_log.laya_stats(db)
    assert stats["totals"]["decisions"] == 2
    tasks = {bucket["task"]: bucket for bucket in stats["by_task"]}
    assert tasks["ranking"]["statuses"]["ok"] == 1
    assert tasks["classification"]["total"] == 1
    assert stats["retention_days"] == ai_log.retention_days()


# --------------------------------------------------------------------------- #
# Bounded, scrubbed, and never in the way
# --------------------------------------------------------------------------- #
def test_retention_prunes_both_tables(db, owner_id):
    now = datetime.utcnow()
    old = now - timedelta(days=ai_log.retention_days() + 3)
    for created in (old, now):
        db.add(AICallRecord(user_id=owner_id, workflow="scoring", status="ok", created_at=created))
        db.add(LayaDecision(user_id=owner_id, task="ranking", status="ok", created_at=created))
    db.commit()

    removed = ai_log.prune(db)
    _sync(db)
    assert removed["calls"] == 1 and removed["laya"] == 1
    assert db.query(AICallRecord).count() == 1
    assert db.query(LayaDecision).count() == 1


def test_a_write_is_what_triggers_the_sweep(db, owner_id):
    """Retention runs on write, at most once per interval — not on every verdict."""
    db.add(AICallRecord(user_id=owner_id, workflow="parse", status="ok",
                        created_at=datetime.utcnow() - timedelta(days=30)))
    db.commit()
    ai_log.reset_prune_clock()
    assert ai_log.maybe_prune(db) == 1        # first write sweeps
    db.add(AICallRecord(user_id=owner_id, workflow="parse", status="ok",
                        created_at=datetime.utcnow() - timedelta(days=30)))
    db.commit()
    assert ai_log.maybe_prune(db) == 0        # inside the interval: skipped, not deleted
    _sync(db)
    assert db.query(AICallRecord).count() == 1


def test_the_first_sweep_does_not_depend_on_host_uptime(db, owner_id, monkeypatch):
    """`time.monotonic()` counts from *boot*, so "never swept" is not 0.0.

    With a numeric sentinel, a machine that booted less than the prune interval
    ago computes `now - 0.0 < interval` and quietly skips its first sweep —
    which is exactly what a fresh CI runner (or a new pod) does. Retention must
    not depend on how long the host has been up.
    """
    import time as time_module

    db.add(AICallRecord(user_id=owner_id, workflow="parse", status="ok",
                        created_at=datetime.utcnow() - timedelta(days=30)))
    db.commit()
    ai_log.reset_prune_clock()

    # Five seconds of uptime — the interval is 300.
    monkeypatch.setattr(time_module, "monotonic", lambda: 5.0)
    assert ai_log.maybe_prune(db) == 1, "a freshly booted host must still prune"
    # …and the rate limit itself still holds.
    db.add(AICallRecord(user_id=owner_id, workflow="parse", status="ok",
                        created_at=datetime.utcnow() - timedelta(days=30)))
    db.commit()
    assert ai_log.maybe_prune(db) == 0
    _sync(db)
    assert db.query(AICallRecord).count() == 1


def test_long_bodies_are_clipped_and_marked(db, owner_id, monkeypatch):
    monkeypatch.setattr(ai_log, "_prompt_chars", lambda: 1000)
    monkeypatch.setattr(ai_log, "_response_chars", lambda: 200)
    ai_log.record_call(db, user_id=owner_id, workflow="scoring", status="ok",
                       request={"url": "http://x/v1", "params": {},
                                "attempts": [{"attempt": 1, "body": {"prompt": "x" * 5000}}]},
                       response="y" * 5000)
    _sync(db)
    detail = ai_log.get_call(db, ai_log.recent_calls(db, limit=1)[0]["id"])
    body = detail["request"]["attempts"][0]["body"]
    assert body["_truncated"] is True and body["_original_chars"] > 1000
    assert len(body["preview"]) == 1000
    assert len(detail["response"]) == 200


def test_secret_shaped_values_are_masked_before_they_are_stored(db, owner_id):
    ai_log.record_call(db, user_id=owner_id, workflow="scoring", status="ok",
                       request={"url": "http://x/v1", "params": {"api_key": "sk-live-abcdefghijklmnopqrstuvwxyz"},
                                "attempts": [{"attempt": 1, "body": {
                                    "messages": [{"role": "user",
                                                  "content": "use sk-live-abcdefghijklmnopqrstuvwxyz please"}]}}]},
                       response="")
    _sync(db)
    detail = ai_log.get_call(db, ai_log.recent_calls(db, limit=1)[0]["id"])
    dumped = json.dumps(detail)
    assert "sk-live-abcdefghijklmnopqrstuvwxyz" not in dumped
    assert "***" in dumped


def test_observability_never_fails_the_call(db):
    """A broken sink is swallowed — the alternative is losing the AI answer."""
    ai_log.record_call(None, user_id=1, workflow="scoring", status="ok")          # no session
    ai_log.record_laya_decision(None, user_id=1, task="ranking", status="ok")
    assert ai_log.get_call(db, 10 ** 9) is None
    assert ai_log.recent_calls(db, limit=1, user_id=10 ** 9) == []


def test_logging_can_be_switched_off(db, owner_id, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ai_log_enabled", False)
    ai_log.record_call(db, user_id=owner_id, workflow="scoring", status="ok")
    ai_log.record_laya_decision(db, user_id=owner_id, task="ranking", status="ok")
    _sync(db)
    assert db.query(AICallRecord).count() == 0
    assert db.query(LayaDecision).count() == 0
    assert ai_log.call_stats(db)["enabled"] is False


# --------------------------------------------------------------------------- #
# The owner console's single pane
# --------------------------------------------------------------------------- #
def test_overview_carries_both_rollups(client, auth, db, owner_id, laya_fake):
    laya_fake(lambda key, q: _answer(0.9))
    run(laya_service.predict(_questions(4), "state", task="ranking",
                             db=db, user_id=owner_id))
    _sync(db)
    body = client.get("/api/admin/overview", headers=auth).json()
    assert body["laya"]["totals"]["decisions"] == 1
    assert body["laya"]["by_task"][0]["task"] == "ranking"
    assert body["ai_log"]["totals"]["calls"] == 0
    assert body["ai_log"]["retention_days"] == ai_log.retention_days()
