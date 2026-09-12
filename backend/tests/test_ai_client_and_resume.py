"""
Unit tests for the AI gateway, source normalisation and the resume decision
logic. Everything here is offline — no API key, no network.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.ai_client import (
    AIClientError,
    breaker_snapshot,
    chat_completion,
    is_configured,
    resolve_config,
    set_workflow_overrides,
    usage_snapshot,
)
from app.services.scoring import jd_similarity, should_generate_new_resume


@pytest.mark.real_ai
def test_ai_client_without_key_raises_before_the_wire():
    import app.services.ai_client as ai

    ai._workflow_overrides.clear()
    assert is_configured() is False or not resolve_config()["api_key"]
    with pytest.raises(AIClientError):
        asyncio.run(chat_completion("scoring", "prompt"))


def test_workflow_override_resolution():
    import app.services.ai_client as ai

    ai._workflow_overrides.clear()
    try:
        set_workflow_overrides(
            {"resume_gen": {"model": "gpt-4o", "api_key": "sk-test", "base_url": "https://example.com/v1"}}
        )
        config = resolve_config("resume_gen")
        assert config["model"] == "gpt-4o"
        assert config["api_key"] == "sk-test"
        assert config["base_url"] == "https://example.com/v1"
        assert resolve_config("scoring")["model"] == resolve_config()["model"]
        assert is_configured("resume_gen") is True
    finally:
        ai._workflow_overrides.clear()


def test_usage_and_breaker_snapshots_have_stable_shape():
    assert isinstance(usage_snapshot(), dict)
    for workflow, row in usage_snapshot().items():
        assert {"calls", "tokens"} <= set(row), workflow
    assert all({"failures", "open"} <= set(row) for row in breaker_snapshot().values())


def test_posting_normalisation_is_defensive():
    from app.services.sources.base import Posting, keyword_score, parse_datetime, strip_html

    posting = Posting(
        title="Backend Engineer", company="Acme", url="https://arbeitnow.com/view/x", source="arbeitnow",
        location="Berlin", description=strip_html("<p>Python and AWS <b>payments</b></p>"),
        external_id="x", industry="fintech",
    )
    assert posting.title == "Backend Engineer"
    assert "<" not in posting.description
    assert posting.to_dict()["location"] == "Berlin"
    assert posting.dedupe_key() == "arbeitnow:x"

    # No external id → company:title, so the same job found twice still dedupes.
    without_id = Posting(title="Backend Engineer", company="Acme", url="https://a/b", source="lever")
    duplicate = Posting(title="Backend Engineer", company="Acme", url="https://c/d", source="lever")
    assert without_id.dedupe_key() == duplicate.dedupe_key()

    assert parse_datetime("2024-05-01T10:00:00Z") is not None
    assert parse_datetime("2024-05-01 10:00:00") is not None
    assert parse_datetime(1714557600) is not None
    assert parse_datetime("not a date") is None
    assert keyword_score("python aws payments", ["python", "kubernetes"]) == 1
    assert keyword_score("anything", []) == 1


def test_resume_decision_logic():
    jd_a = "Senior backend engineer Python FastAPI AWS Kubernetes payments fintech microservices."
    jd_b = "Senior backend engineer Python FastAPI AWS Kubernetes payments fintech microservices PostgreSQL."
    jd_c = "Graphic designer Figma branding illustration print media."
    assert jd_similarity(jd_a, jd_b) > 0.8
    assert jd_similarity(jd_a, jd_c) < 0.4
    assert should_generate_new_resume(80, jd_similarity(jd_a, jd_b)) is False   # reuse
    assert should_generate_new_resume(80, jd_similarity(jd_a, jd_c)) is True    # generate
    assert should_generate_new_resume(40, jd_similarity(jd_a, jd_c)) is False   # weak match → master


def test_rate_limiter_waits_instead_of_exceeding_the_window():
    from app.core.rate_limiter import TokenBucketRateLimiter

    async def scenario() -> None:
        limiter = TokenBucketRateLimiter(rpm=2)
        assert await limiter.acquire() == 0.0
        assert await limiter.acquire() == 0.0
        wait = await limiter.acquire()          # budget exhausted → a real wait
        assert wait > 0
        assert limiter.stats()["throttled"] == 1
        assert limiter.stats()["remaining"] == 0

    asyncio.run(scenario())
