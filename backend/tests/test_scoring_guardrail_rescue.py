"""v2.5 — a guardrail rejection is retried, not dead-ended.

The report behind this file: *most* match scores read "AI scoring unavailable —
this is not an AI verdict … (guardrail_failed)", with a fix line telling the
user to retry and **no way to retry**, and the dead end was permanent: the
rejected payload is persisted into ``job.extra['intelligence']``, and every
later read served that stored copy.

Three things this file pins:

1. **The automatic retry is the local engine.** When the model's answer is
   rejected by the accuracy guardrail (the answer *and* its repair pass), the
   local decision engine is asked once — under the owner's own Laya usage
   policy, never around it: ``llm_only`` is still a refusal, a per-task opt-out
   is still a refusal, a below-floor answer is still not a verdict, and an
   engine outage still reports an outage. A rescued verdict is labelled
   ``laya`` and carries the rejection that produced it.
2. **The user-facing retry exists.** The read announces
   ``actions.retry`` (``?refresh=1``) whenever it stored a verdict with no
   answer behind it, and the retry *does* re-run scoring instead of serving the
   stored copy again.
3. **The board stops claiming "0 score" for a pair nobody scored**: the list
   read carries ``score_source`` next to the number, and a ``laya`` verdict
   clears the automation policy's confidence floor like any other engine
   verdict.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from app.core import metrics
from app.models.models import Job, Profile, User
from app.services import laya as laya_service
from app.services import scoring
from app.services.ai_guardrails import GuardrailError
from app.services.user_settings import set_setting

REJECTION = [{"code": "unsupported_strength", "severity": "error", "field": "strengths",
              "value": "kubernetes", "message": "'kubernetes' is not present in the candidate's profile."}]

PROFILE = {"skills": ["python", "fastapi"], "summary": "Backend engineer",
           "experience": [{"title": "Backend Dev", "description": "built APIs"}]}
JD = "Looking for a backend engineer with python and fastapi experience."


def run(coro):
    return asyncio.run(coro)


def _ordinal(level: int = 4, confidence: float = 0.9) -> Dict[str, Any]:
    probs = {str(i): (confidence if i == level else (1.0 - confidence) / 4.0) for i in range(5)}
    return {"score": level, "confidence": confidence, "probs": probs}


class FakeRouter:
    def __init__(self, fn):
        self.fn = fn
        self.calls: list = []

    def predict(self, state, questions, **kwargs):
        self.calls.append({"questions": dict(questions), "kwargs": kwargs})
        return {"answers": {key: self.fn(key, q) for key, q in questions.items()},
                "routing": {"model": "english"}}


class BrokenRouter:
    def predict(self, state, questions, **kwargs):  # pragma: no cover - always raises
        raise RuntimeError("checkpoint exploded")


@pytest.fixture()
def laya_fake(monkeypatch):
    """Route Laya at a fake router: installed, importable, answering."""
    monkeypatch.setattr(laya_service, "installed", lambda: True)

    def install(fn):
        router = FakeRouter(fn)
        monkeypatch.setattr(laya_service, "_get_router", lambda: router)
        laya_service.reset_for_tests()
        return router

    yield install
    laya_service.reset_for_tests()


@pytest.fixture()
def rejected(monkeypatch):
    """Every guarded scoring call comes back rejected by the accuracy guardrail."""
    calls: list = []

    async def fake_guarded(workflow, **kwargs):
        calls.append(workflow)
        raise GuardrailError(workflow, list(REJECTION), detail="{'score': 90}")

    monkeypatch.setattr(scoring, "run_guarded_task", fake_guarded)
    return calls


def _owner(db) -> User:
    return db.query(User).order_by(User.id).first()


def _snapshot() -> Dict[str, int]:
    return dict(metrics.snapshot())


def _delta(before: Dict[str, int], key: str) -> int:
    return _snapshot().get(key, 0) - before.get(key, 0)


# --------------------------------------------------------------------------- #
# 1. The automatic retry: the local engine, under the owner's own policy
# --------------------------------------------------------------------------- #
def test_a_rejected_verdict_is_retried_on_the_local_engine(db, owner, laya_fake, rejected):
    """The model's answer is thrown away — the pair does not go down with it.

    ``auto`` mode reached the model because the engine's own answer was below
    its confidence floor; the model's answer then failed the accuracy guardrail.
    Rather than dead-ending at 0/100, the engine's calibrated answer stands —
    marked low-confidence, carrying the rejection that produced it.
    """
    user = _owner(db)
    router = laya_fake(lambda key, q: _ordinal(level=2, confidence=0.2))

    result = run(scoring.score_job(PROFILE, JD, db=db, user_id=user.id))

    assert rejected == ["scoring"], "the model was never asked — this is not a rescue"
    assert result["score_source"] == "laya", result
    detail = result["detail"]
    assert detail["source"] == "laya"
    assert detail["low_confidence"] is True
    # Provenance is complete: the verdict says it was rescued *and* why.
    assert detail["guardrail"]["rescued_from"] == scoring.GUARDRAIL_RESCUE_SOURCE
    assert detail["guardrail"]["rescued_from"] == "ai_guardrail_failed"
    assert detail["guardrail"]["low_confidence"] is True
    assert [i["code"] for i in detail["guardrail"]["rejected_issues"]] == ["unsupported_strength"]
    assert detail["guardrail"]["engine"] == "laya"
    assert result["reason"].startswith("Laya (local decision engine)")
    assert "Below its usual confidence floor" in result["reason"]
    assert result["score"] > 0
    assert len(router.calls) == 2, "one forward pass to escalate, one to rescue"


def test_the_rescue_charges_one_analysis_and_counts_itself_once(db, owner, laya_fake, rejected):
    """A rescued verdict is one accepted analysis, and the metric says which path."""
    user = _owner(db)
    laya_fake(lambda key, q: _ordinal(level=2, confidence=0.2))
    from app.core.entitlements import current_period
    from app.models.models import UsageCounter

    def used() -> int:
        db.expire_all()
        row = (db.query(UsageCounter)
               .filter(UsageCounter.user_id == user.id,
                       UsageCounter.period == current_period(),
                       UsageCounter.capability == "job_analysis_per_month")
               .first())
        return int(row.count) if row else 0

    before_usage = used()
    before = _snapshot()
    run(scoring.score_job(PROFILE, JD, db=db, user_id=user.id))
    assert used() - before_usage == 1
    assert _delta(before, 'jobhunter_scoring_guardrail_rescue_total{result="laya_low_confidence"}') == 1
    assert _delta(before, 'jobhunter_scoring_guardrail_rescue_total{result="rejected"}') == 0


def test_llm_only_keeps_the_rejection_and_never_consults_the_engine(db, owner, laya_fake, rejected):
    """The owner routed ranking away from Laya — the rescue respects that."""
    user = _owner(db)
    set_setting(db, user.id, "laya", "mode", "llm_only")
    db.commit()
    router = laya_fake(lambda key, q: _ordinal())
    before = _snapshot()

    result = run(scoring.score_job(PROFILE, JD, db=db, user_id=user.id))

    assert result["score_source"] == "rejected"
    assert result["error"]["reason"] == "guardrail_failed"
    assert router.calls == [], "the engine answered a pair the owner left to the LLM"
    assert _delta(before, 'jobhunter_scoring_guardrail_rescue_total{result="not_routed"}') == 1


class _Report:
    """The slice of ``GuardrailReport`` the scorer reads on the happy path."""

    issues: list = []

    def to_dict(self) -> Dict[str, Any]:
        return {"passed": True, "issues": []}


def test_the_floor_is_still_the_escalation_rule_while_the_model_works(db, owner, laya_fake, monkeypatch):
    """The rescue is a failure handler — it never pre-empts a working model."""
    user = _owner(db)
    laya_fake(lambda key, q: _ordinal(level=2, confidence=0.2))
    before = _snapshot()

    async def working_model(workflow, **kwargs):
        return ({"score": 71.0, "reason": "Grounded answer.", "breakdown": {"skills": 70},
                 "strengths": [], "missing_skills": [], "evidence": ["python"],
                 "recommendation": "GOOD FIT", "recommendation_reason": "ok"}, _Report())

    monkeypatch.setattr(scoring, "run_guarded_task", working_model)

    result = run(scoring.score_job(PROFILE, JD, db=db, user_id=user.id))

    assert result["score_source"] == "ai", result
    assert _delta(before, 'jobhunter_scoring_guardrail_rescue_total{result="laya_low_confidence"}') == 0
    assert _delta(before, 'jobhunter_scoring_guardrail_rescue_total{result="laya"}') == 0


def test_an_engine_outage_is_reported_as_an_outage_not_a_guess(db, owner, monkeypatch, rejected):
    """``auto`` mode: a dead engine plus a rejected model answer is still no verdict."""
    user = _owner(db)
    monkeypatch.setattr(laya_service, "installed", lambda: True)
    monkeypatch.setattr(laya_service, "_get_router", lambda: BrokenRouter())
    laya_service.reset_for_tests()
    before = _snapshot()

    result = run(scoring.score_job(PROFILE, JD, db=db, user_id=user.id))

    assert result["score_source"] == "rejected"
    assert result["error"]["reason"] == "guardrail_failed"
    assert _delta(before, 'jobhunter_scoring_guardrail_rescue_total{result="engine_down"}') == 1


def test_a_healthy_verdict_still_comes_from_the_model(db, owner, laya_fake):
    """Nothing changed for the ordinary path: the rescue is an error handler."""
    user = _owner(db)
    laya_fake(lambda key, q: _ordinal(level=4, confidence=0.9))
    result = run(scoring.score_job(PROFILE, JD, db=db, user_id=user.id))
    assert result["score_source"] == "laya"  # the primary path, un-rescued
    assert "rescued_from" not in result["detail"]["guardrail"]


# --------------------------------------------------------------------------- #
# 2. The retry the user was told to perform and never offered
# --------------------------------------------------------------------------- #
def _grant_pro(db, email: str = "owner@example.com") -> None:
    from datetime import datetime, timezone

    from app.models.models import Subscription

    user = db.query(User).filter(User.email == email).first()
    if not db.query(Subscription).filter(Subscription.user_id == user.id).first():
        db.add(Subscription(user_id=user.id, plan="pro", status="active", provider="manual",
                            current_period_start=datetime.now(timezone.utc)))
        db.commit()


def test_the_read_announces_a_retry_when_there_is_no_verdict(client, auth, db, uploaded_resume,
                                                             seeded_job, rejected):
    _grant_pro(db)
    response = client.get(f"/api/jobs/{seeded_job.id}/intelligence", headers=auth)
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["score_source"] == "rejected"
    assert payload["ai_error"]["code"] == "guardrail_failed"
    retry = payload["actions"]["retry"]
    assert retry["allowed"] is True
    assert retry["route"] == f"/api/jobs/{seeded_job.id}/intelligence?refresh=1"
    assert retry["costs_quota"] is True
    # The dead end was permanent: the rejected payload is persisted...
    db.expire_all()
    row = db.query(Job).filter(Job.id == seeded_job.id).first()
    assert row.score_source == "rejected"
    assert (row.extra or {})["intelligence"]["ai_error"]["code"] == "guardrail_failed"


def test_the_stored_verdict_is_served_until_asked_to_refresh(client, auth, db, uploaded_resume,
                                                             seeded_job, rejected):
    """A plain re-read is free and stable; only ``?refresh=1`` spends a verdict."""
    _grant_pro(db)
    url = f"/api/jobs/{seeded_job.id}/intelligence"
    client.get(url, headers=auth)
    assert rejected == ["scoring"]

    again = client.get(url, headers=auth)
    assert again.status_code == 200
    assert again.json()["score_source"] == "rejected"
    assert rejected == ["scoring"], "a plain re-read re-ran the model"

    retried = client.get(url, params={"refresh": True}, headers=auth)
    assert retried.status_code == 200
    assert rejected == ["scoring", "scoring"], "the retry served the stored copy instead of re-scoring"


def test_the_retry_recovers_the_verdict_and_persists_it(client, auth, db, uploaded_resume,
                                                        seeded_job, monkeypatch, laya_fake):
    """The whole point: after the retry the job has a real verdict, not a 0."""
    _grant_pro(db)
    user = db.query(User).filter(User.email == "owner@example.com").first()
    set_setting(db, user.id, "laya", "mode", "laya_only")
    db.commit()
    laya_fake(lambda key, q: _ordinal(level=4, confidence=0.9))
    # laya_only never reaches the LLM, so the rejection path is unreachable —
    # the retry is what turns the stored "no verdict" into a verdict.
    url = f"/api/jobs/{seeded_job.id}/intelligence"
    first = client.get(url, params={"refresh": True}, headers=auth).json()
    assert first["score_source"] == "laya", first
    assert first["score"] > 0

    db.expire_all()
    row = db.query(Job).filter(Job.id == seeded_job.id).first()
    assert row.score_source == "laya"
    assert row.score > 0
    assert (row.extra or {})["intelligence"]["score_source"] == "laya"
    assert "actions" not in (row.extra or {})["intelligence"], "a real verdict is not re-run by default"


def test_a_member_without_the_plan_never_reaches_the_model(client, auth, db, uploaded_resume, seeded_job):
    """The retry is not a plan bypass (unchanged contract)."""
    payload = client.get(f"/api/jobs/{seeded_job.id}/intelligence",
                         params={"refresh": True}, headers=auth).json()
    assert payload["score_source"] == "preliminary"
    assert payload.get("upgrade_hint")
    assert "actions" not in payload


# --------------------------------------------------------------------------- #
# 3. Provenance the surfaces agree on
# --------------------------------------------------------------------------- #
def test_the_board_row_carries_the_provenance_next_to_the_number(client, auth, db, uploaded_resume):
    profile = db.query(Profile).first()
    db.add_all([
        Job(user_id=profile.user_id, title="Scored", company="Acme", description="Python.",
            status="discovered", source="lever", dedupe_key="prov:1", score=88.0, score_source="ai"),
        Job(user_id=profile.user_id, title="Refused", company="BetaCo", description="Python.",
            status="discovered", source="lever", dedupe_key="prov:2", score=0.0, score_source="rejected"),
    ])
    db.commit()

    rows = client.get("/api/jobs", headers=auth).json()
    by_title = {row["title"]: row for row in rows}
    assert by_title["Scored"]["score_source"] == "ai"
    assert by_title["Refused"]["score_source"] == "rejected"


def test_a_laya_verdict_clears_the_automation_floor_like_any_engine_verdict():
    """``min_match_score`` measures confidence, not provenance (v2.5)."""
    from app.services.automation_policy import score_provenance_can_clear_floor

    assert score_provenance_can_clear_floor("ai") is True
    assert score_provenance_can_clear_floor("hybrid") is True
    assert score_provenance_can_clear_floor("laya") is True
    assert score_provenance_can_clear_floor("pending") is True  # historical pass-through
    assert score_provenance_can_clear_floor("rejected") is False
    assert score_provenance_can_clear_floor("preliminary") is False
    assert score_provenance_can_clear_floor("unscored") is False
    assert score_provenance_can_clear_floor(None) is False
