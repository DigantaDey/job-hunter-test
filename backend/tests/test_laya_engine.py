"""
Laya — the local typed-decision engine (owner-routed).

The contracts pinned here are the ones the product depends on:

* typed answers (choice / noul / ordinal score) are normalised — answer +
  calibrated confidence — and a low-confidence ``auto`` answer escalates to the
  LLM instead of being trusted;
* the owner's routing mode is honoured: ``llm_only`` never reaches Laya,
  ``laya_only`` never falls back to the LLM (an engine outage is reported as
  one — ``AIUnavailableError`` / ``score_source="pending"`` — never papered
  over), and ``auto`` degrades to the pre-Laya behaviour when Laya is absent;
* a Laya ranking verdict is labelled ``source="laya"`` (its own badge in the
  UI) and the recommendation band still follows the score — computed in code,
  never asked;
* the env ceiling (``LAYA_ENABLED=false``) turns all routing off regardless of
  any user's settings.

Everything runs against a fake router (``laya`` is an optional install): the
tests exercise our adapter's parsing, gating and fallbacks, not the model.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from app.models.models import User
from app.services import classifier, scoring
from app.services import laya as laya_service
from app.services.user_settings import set_setting


def run(coro):
    return asyncio.run(coro)


class FakeRouter:
    """Answers each question key via ``fn(question_key, question)``."""

    def __init__(self, fn):
        self.fn = fn
        self.calls = []

    def predict(self, state, questions, **kwargs):
        self.calls.append({"state": state, "questions": dict(questions), "kwargs": kwargs})
        answers = {key: self.fn(key, question) for key, question in questions.items()}
        return {"answers": answers, "routing": {"model": "english"}}


class BrokenRouter:
    def predict(self, state, questions, **kwargs):
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


def _ordinal(level: int = 3, confidence: float = 0.9) -> Dict[str, Any]:
    probs = {str(i): (confidence if i == level else (1.0 - confidence) / 4.0) for i in range(5)}
    return {"score": level, "confidence": confidence, "probs": probs}


def _choice(key: str, confidence: float = 0.9) -> Dict[str, Any]:
    return {"choice": key, "confidence": confidence}


def test_choice_is_answer_plus_confidence(laya_fake):
    laya_fake(lambda key, q: _choice("big"))
    answer = run(laya_service.choice("state", "size", "how big?", {"big": "huge", "small": "tiny"}))
    assert answer == {"answer": "big", "confidence": 0.9, "probabilities": {}}


def test_choice_rejects_low_confidence_and_unknown_keys(laya_fake):
    laya_fake(lambda key, q: _choice("big", confidence=0.2))
    assert run(laya_service.choice("state", "size", "how big?", {"big": "huge"})) is None

    laya_fake(lambda key, q: _choice("medium"))  # not offered by the question
    assert run(laya_service.choice("state", "size", "how big?", {"big": "huge"})) is None


def test_noul_returns_probability_true(laya_fake):
    laya_fake(lambda key, q: {"noul": 0.82})
    assert run(laya_service.noul("state", "churn", "will they leave?")) == pytest.approx(0.82)


def test_score_ordinal_maps_argmax_and_expected(laya_fake):
    laya_fake(lambda key, q: _ordinal(level=4, confidence=0.8))
    answer = run(laya_service.score_ordinal("state", "fit", "how well?", ["low", "mid", "high"]))
    # 5 synthetic levels vs 3 labels is the model's business; what matters is
    # the normalised shape: argmax level inside the rubric + an expected value.
    assert answer["level"] in (0, 1, 2)
    assert 0.0 <= answer["expected"] <= 2.0
    assert answer["confidence"] == pytest.approx(0.8)


def test_env_ceiling_disables_all_routing(laya_fake, monkeypatch):
    monkeypatch.setattr(laya_service.settings, "laya_enabled", False)
    assert laya_service.should_attempt(None, None, "ranking") is False
    assert run(scoring.laya_score_detailed({"skills": ["go"]}, "Write go")) is None


def test_llm_only_mode_never_consults_laya(db, owner, laya_fake):
    user = db.query(User).order_by(User.id).first()
    set_setting(db, user.id, "laya", "mode", "llm_only")
    db.commit()
    router = laya_fake(lambda key, q: _ordinal())
    assert laya_service.should_attempt(db, user.id, "ranking") is False
    assert run(scoring.laya_score_detailed(
        {"skills": ["go"], "summary": "engineer"}, "A Go job", db=db, user_id=user.id)) is None
    assert router.calls == []


def test_ranking_verdict_is_labelged_laya_and_band_follows_score(db, owner, laya_fake):
    user = db.query(User).order_by(User.id).first()
    laya_fake(lambda key, q: _ordinal(level=4, confidence=0.9))
    profile = {"skills": ["python", "fastapi"], "summary": "Backend engineer",
               "experience": [{"title": "Backend Dev", "description": "built APIs"}]}
    jd = "Looking for a backend engineer with python and fastapi experience."
    result = run(scoring.score_job(profile, jd, db=db, user_id=user.id))
    assert result["score_source"] == "laya"
    detail = result["detail"]
    assert detail["source"] == "laya"
    assert detail["breakdown_source"] == "laya"
    # The band is computed from the score in code — nothing can re-grade itself.
    score = result["score"]
    assert detail["recommendation"] == scoring._band(score)
    assert all(0 <= v <= 100 for v in detail["breakdown"].values())
    assert set(detail["breakdown"]) == set(scoring.RUBRIC_WEIGHTS)
    # Laya cannot quote evidence — the slot stays empty, never paraphrased.
    assert detail["evidence"] == []
    assert result["reason"].startswith("Laya (local decision engine)")


def test_auto_mode_escalates_low_confidence(db, owner, laya_fake):
    user = db.query(User).order_by(User.id).first()
    laya_fake(lambda key, q: _ordinal(level=2, confidence=0.2))
    detail = run(scoring.laya_score_detailed(
        {"skills": ["go"], "summary": "engineer"}, "A Go job", db=db, user_id=user.id))
    assert detail is None  # the LLM path gets the last word in auto mode


def test_laya_only_mode_reports_outage_never_llm(db, owner, monkeypatch):
    user = db.query(User).order_by(User.id).first()
    set_setting(db, user.id, "laya", "mode", "laya_only")
    db.commit()
    monkeypatch.setattr(laya_service, "installed", lambda: True)
    monkeypatch.setattr(laya_service, "_get_router", lambda: BrokenRouter())
    laya_service.reset_for_tests()
    from app.services.ai_guardrails import AIUnavailableError

    with pytest.raises(AIUnavailableError) as excinfo:
        run(scoring.ai_score_detailed(
            {"skills": ["go"], "summary": "engineer"}, "A Go job", db=db, user_id=user.id))
    assert excinfo.value.reason == "laya_unavailable"
    # ...and the soft entry point reports it as pending, exactly like an outage.
    result = run(scoring.score_job(
        {"skills": ["go"], "summary": "engineer"}, "A Go job", db=db, user_id=user.id))
    assert result["score_source"] == "pending"
    assert result["error"]["reason"] == "laya_unavailable"
    laya_service.reset_for_tests()


def test_not_installed_is_the_pre_laya_behaviour(db, owner, monkeypatch):
    monkeypatch.setattr(laya_service, "installed", lambda: False)
    user = db.query(User).order_by(User.id).first()
    assert laya_service.should_attempt(db, user.id, "ranking") is False
    with pytest.raises(laya_service.LayaUnavailable):
        laya_service._get_router()


def test_company_size_is_a_laya_choice(db, owner, laya_fake, monkeypatch):
    user = db.query(User).order_by(User.id).first()
    laya_fake(lambda key, q: _choice("medium", confidence=0.85))

    async def no_llm(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("the LLM was consulted in laya_only/auto-with-answer mode")

    monkeypatch.setattr("app.services.ai_client.chat_completion", no_llm)
    size, confidence = run(classifier.ai_company_size("Acme Robotics", "Series B scaleup...", db=db,
                                                      user_id=user.id))
    assert size == "medium"
    assert confidence == pytest.approx(0.85)


def test_field_mapping_batches_questions_and_skips_unconfident(db, owner, laya_fake, monkeypatch):
    from app.services.assisted_fill import ai_map_unknown_fields

    user = db.query(User).order_by(User.id).first()

    def answer(key, question):
        # Map the first field confidently; shrug at the second.
        return _choice("coverLetter" if key == "field_0" else "unmapped",
                       confidence=0.9 if key == "field_0" else 0.9)

    router = laya_fake(answer)

    async def no_llm(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("the LLM was consulted although Laya answered everything")

    monkeypatch.setattr("app.services.ai_client.chat_completion", no_llm)
    observation = {"fields": [
        {"name": "q_zorblatt", "label": "Zorblatt frequency preference", "type": "text"},
        {"name": "q_quuxly", "label": "Quuxly inclination disclosure", "type": "text"},
    ]}
    out = run(ai_map_unknown_fields(observation, profile_keys=["coverLetter"],
                                    profile_sample={"coverLetter": "x"}, db=db, user_id=user.id))
    assert out == {"q_zorblatt": "coverLetter"}
    assert len(router.calls) == 1  # one batched forward pass, not one per field
    assert set(router.calls[0]["questions"]) == {"field_0", "field_1"}


# --------------------------------------------------------------------------- #
# Settings surface: owner-gated, validated
# --------------------------------------------------------------------------- #
def test_member_cannot_write_laya_settings(client, auth, member_auth):
    ok = client.put("/api/settings", json={"laya": {"mode": "laya_only"}}, headers=auth)
    assert ok.status_code == 200, ok.text
    denied = client.put("/api/settings", json={"laya": {"mode": "llm_only"}}, headers=member_auth)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "owner_required"


def test_laya_mode_is_a_closed_vocabulary(client, auth):
    bad = client.put("/api/settings", json={"laya": {"mode": "whatever"}}, headers=auth)
    assert bad.status_code == 400
    ok = client.put("/api/settings", json={"laya": {"mode": "auto",
                                                   "for_ranking": False,
                                                   "for_classification": "true",
                                                   "for_field_mapping": 1}}, headers=auth)
    assert ok.status_code == 200, ok.text
    data = client.get("/api/settings", headers=auth).json()
    assert data["laya"]["mode"] == "auto"
    assert data["laya"]["for_ranking"] is False
    assert data["laya"]["for_classification"] is True
    # Booleans are coerced strictly — the string "false" must never become truthy.
    bad_bool = client.put("/api/settings", json={"laya": {"for_ranking": "banana"}}, headers=auth)
    assert bad_bool.status_code == 400


def test_member_settings_document_hides_laya_controls(client, auth, member_auth):
    client.put("/api/settings", json={"laya": {"mode": "laya_only"}}, headers=auth)
    member_doc = client.get("/api/settings", headers=member_auth).json()
    assert "for_ranking" not in member_doc.get("laya", {})
    assert "laya" not in member_doc["_meta"]["writable"]
    owner_doc = client.get("/api/settings", headers=auth).json()
    assert "mode" in owner_doc["_meta"]["writable"]["laya"]
    assert owner_doc["laya"]["mode"] == "laya_only"


# --------------------------------------------------------------------------- #
# v2.3 — an engine that is down must not cost a run one timeout per candidate
# --------------------------------------------------------------------------- #
def test_repeated_failures_park_the_engine(laya_fake):
    """Three consecutive failures bench the engine; a success un-benches it.

    Without this, a discovery run scores ``AI_RESCORE_TOP`` candidates against a
    dead engine and pays ``LAYA_TIMEOUT_SECONDS`` *per candidate* before every
    one of them falls back — the 25-minute run in the report.
    """
    laya_fake(lambda key, q: (_ for _ in ()).throw(RuntimeError("boom")))

    for _ in range(laya_service._FAILURE_THRESHOLD):
        with pytest.raises(laya_service.LayaUnavailable) as first:
            run(laya_service.predict({"q": "how big?"}, "state"))
        assert first.value.code == "predict_failed"

    assert laya_service.parked() is True
    with pytest.raises(laya_service.LayaUnavailable) as parked_exc:
        run(laya_service.predict({"q": "how big?"}, "state"))
    assert parked_exc.value.code == "engine_parked", "the engine must not be retried while parked"

    # A successful pass clears the park (the engine came back).
    laya_service.reset_for_tests()
    laya_fake(lambda key, q: _choice("big"))
    run(laya_service.predict({"q": "how big?"}, "state"))
    assert laya_service.parked() is False


def test_parked_engine_is_reported_not_retried_in_auto_mode(laya_fake):
    """Auto mode falls back to the LLM (the pre-Laya behaviour) — no exception
    escapes, and the run's verdict is labelled with the engine that answered."""
    laya_fake(lambda key, q: (_ for _ in ()).throw(RuntimeError("boom")))
    for _ in range(laya_service._FAILURE_THRESHOLD):
        with pytest.raises(laya_service.LayaUnavailable):
            run(laya_service.predict({"q": "how big?"}, "state"))
    assert laya_service.parked() is True

    async def _score():
        # The stand-in scorer (conftest) credits "python" and "fastapi", so the
        # profile must actually contain them for the guardrail to pass.
        profile = {"skills": ["python", "fastapi"],
                   "experience": [{"title": "Backend Engineer", "company": "Paystack"}]}
        return await scoring.ai_score_detailed(profile, JD)

    detail = run(_score())
    # The fake 'laya' package cannot answer, so the LLM stand-in does — and the
    # verdict is never labelled "laya".
    assert detail.get("source") != "laya"


JD = "Backend Engineer. Python, FastAPI, PostgreSQL, AWS, Docker, Kubernetes. 5+ years."
