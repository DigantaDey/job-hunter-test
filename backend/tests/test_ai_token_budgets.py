"""v2.1 — user-controlled token budgets (Settings → AI API).

Contract under test:

* ``ai.max_input_tokens`` / ``ai.max_output_tokens`` are editable per user on
  owner + paid tiers (round-trip, bounded 4xx on nonsense), and read-only for
  the free tier — rejected server-side (403 ``upgrade_required``), not merely
  hidden in the UI.
* The gateway obeys the budgets as the single source of truth: every outgoing
  ``max_tokens`` is clamped to the user's output ceiling (the PR #29
  escalation can never exceed it), and every prompt is clamped to the user's
  input budget — **clamping with a warning** (the over-context decision): an
  oversized document still produces an honest, truncated request and the
  ledger flags ``input_truncated``.
"""
from __future__ import annotations

from io import BytesIO
from typing import Any, Dict

import pytest
from conftest import pdf_bytes  # noqa: F401  (top-level conftest — see the note in test_ai_pause_resume)


# --------------------------------------------------------------------------- #
# Isolation (process-global gateway state).
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_ai_state():
    import app.services.ai_client as ai_client

    ai_client._breakers.clear()
    ai_client._PING_CACHE.clear()
    yield
    ai_client._breakers.clear()
    ai_client._PING_CACHE.clear()


def _wire() -> list:
    import conftest

    return conftest.ScriptedAIHandler.behavior["requests"]


def _set_tokens(client, auth, **kwargs) -> None:
    payload = {"ai": kwargs}
    saved = client.put("/api/settings", json=payload, headers=auth)
    assert saved.status_code == 200, saved.text


# --------------------------------------------------------------------------- #
# 1. Settings round-trip + tier gate
# --------------------------------------------------------------------------- #
def test_token_limit_round_trip_owner(client, auth):
    """Owner can read and write both limits; values round-trip unchanged."""
    _set_tokens(client, auth, max_input_tokens=20000, max_output_tokens=4000)
    current = client.get("/api/settings", headers=auth)
    assert current.status_code == 200
    ai = current.json()["ai"]
    assert ai["max_input_tokens"] == 20000, ai
    assert ai["max_output_tokens"] == 4000, ai
    assert ai["token_limits_editable"] is True

    # Back to the shipped defaults.
    _set_tokens(client, auth, max_input_tokens=12000, max_output_tokens=16000)


def test_token_limit_rejects_out_of_range(client, auth):
    """Bounds are enforced server-side (400), with a readable message."""
    for key, bad in (("max_input_tokens", 10), ("max_input_tokens", 999999999),
                     ("max_output_tokens", 10), ("max_output_tokens", 999999),
                     ("max_input_tokens", "lots"), ("max_output_tokens", None)):
        rejected = client.put("/api/settings", json={"ai": {key: bad}}, headers=auth)
        assert rejected.status_code == 400, f"{key}={bad!r}: {rejected.text}"


def test_paid_tier_can_edit_token_limits(client, auth, db):
    """A non-owner on a paid plan edits the limits through the same API."""
    from datetime import datetime, timezone

    from app.models.models import Subscription, User

    response = client.post("/api/auth/register",
                           json={"email": "member@example.com", "password": "member-password-123",
                                 "name": "Member"})
    assert response.status_code == 201, response.text
    member_auth = {"Authorization": f"Bearer {response.json()['access_token']}"}

    user = db.query(User).filter(User.email == "member@example.com").first()
    db.add(Subscription(user_id=user.id, plan="pro", status="active", provider="manual",
                        current_period_start=datetime.now(timezone.utc)))
    db.commit()

    saved = client.put("/api/settings", json={"ai": {"max_input_tokens": 8000,
                                                     "max_output_tokens": 2000}},
                       headers=member_auth)
    assert saved.status_code == 200, saved.text
    ai = client.get("/api/settings", headers=member_auth).json()["ai"]
    assert ai["max_input_tokens"] == 8000
    assert ai["max_output_tokens"] == 2000
    assert ai["token_limits_editable"] is True


def test_free_tier_cannot_edit_token_limits(client, auth, db, member_auth):
    """Free tier: the token limits are locked — the server rejects the PUT,
    not just the UI hiding the fields. Everything else stays writable."""
    rejected = client.put("/api/settings", json={"ai": {"max_output_tokens": 4000}},
                          headers=member_auth)
    assert rejected.status_code == 403, rejected.text
    body = rejected.json()["detail"]
    assert body.get("code") == "upgrade_required", body
    assert "max_output_tokens" in body.get("fields", []), body

    # And the readable view says the same thing the server would do.
    ai = client.get("/api/settings", headers=member_auth).json()["ai"]
    assert ai["token_limits_editable"] is False

    # Non-token AI settings remain writable for everyone.
    ok = client.put("/api/settings", json={"ai": {"timeout": 60}}, headers=member_auth)
    assert ok.status_code == 200, ok.text


# --------------------------------------------------------------------------- #
# 2. The gateway obeys the budgets (scripted provider echoes the wire truth)
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
def test_gateway_clamps_output_to_user_ceiling(client, auth, db, provider_owner):
    """A per-task starting budget (parse = 4000) is clamped DOWN to the user's
    configured output ceiling — the wire request is the proof."""
    _set_tokens(client, auth, max_output_tokens=3000)

    response = client.post(
        "/api/resume/upload",
        files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
        headers=auth,
    )
    assert response.status_code == 200, response.text

    parse = [r for r in _wire()
             if any("extract a complete, structured profile" in str(m.get("content") or "").lower()
                    for m in r.get("messages") or [])]
    assert len(parse) == 1
    assert parse[0]["max_tokens"] == 3000, \
        f"parse must be clamped to the user ceiling, wire said {parse[0]['max_tokens']}"

    # The ledger records the ceiling that governed the call.
    from app.models.models import AICreditLedger

    row = (db.query(AICreditLedger).filter(AICreditLedger.workflow == "parse",
                                           AICreditLedger.success.is_(True))
           .order_by(AICreditLedger.id.desc()).first())
    assert row is not None
    assert row.meta.get("output_ceiling") == 3000, row.meta


@pytest.mark.real_ai
def test_escalation_never_exceeds_user_ceiling(client, auth, db, provider_owner):
    """The PR #29 escalation (4000 → ×4 on a length-truncated answer) is capped
    at the user's output ceiling: with a ceiling of 5000 the second attempt
    goes out at 5000, not 16000."""
    import conftest

    _set_tokens(client, auth, max_output_tokens=5000)
    # First parse answer: the model burned the budget thinking (empty content,
    # finish_reason=length) — exactly the condition that triggers escalation.
    conftest.ScriptedAIHandler.behavior["override"] = [{
        "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 500, "completion_tokens": 4000, "total_tokens": 4500},
    }]

    response = client.post(
        "/api/resume/upload",
        files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
        headers=auth,
    )
    assert response.status_code == 200, response.text

    parse = [r for r in _wire()
             if any("extract a complete, structured profile" in str(m.get("content") or "").lower()
                    for m in r.get("messages") or [])]
    assert len(parse) == 2, f"expected a retried parse, saw {len(parse)} wire request(s)"
    assert parse[0]["max_tokens"] == 4000, parse[0].get("max_tokens")
    assert parse[1]["max_tokens"] == 5000, \
        f"escalation must stop at the user ceiling, wire said {parse[1]['max_tokens']}"


@pytest.mark.real_ai
def test_over_context_window_clamps_with_warning(client, auth, db, provider_owner, monkeypatch):
    """Documented over-context decision: an oversized document is CLAMPED to
    the user's input budget (never rejected) — the upload still succeeds, the
    wire prompt fits the budget, and the ledger flags the truncation."""
    from app.models.models import AICreditLedger

    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 1)
    _set_tokens(client, auth, max_input_tokens=256)  # → 1024-char budget (the floor)
    budget_chars = 256 * 4

    long_pdf = _long_pdf_bytes()
    response = client.post(
        "/api/resume/upload",
        files={"file": ("resume.pdf", long_pdf, "application/pdf")},
        headers=auth,
    )
    assert response.status_code == 200, f"over-context must clamp, not reject: {response.text}"

    parse = [r for r in _wire()
             if any("extract a complete, structured profile" in str(m.get("content") or "").lower()
                    for m in r.get("messages") or [])]
    assert len(parse) == 1
    user_prompt = [str(m.get("content") or "") for m in parse[0]["messages"]
                   if m.get("role") == "user"][0]
    assert len(user_prompt) <= budget_chars, \
        f"prompt must fit the user input budget: {len(user_prompt)} > {budget_chars}"

    row = (db.query(AICreditLedger).filter(AICreditLedger.workflow == "parse",
                                           AICreditLedger.success.is_(True))
           .order_by(AICreditLedger.id.desc()).first())
    assert row is not None and row.success
    assert row.meta.get("input_truncated") is True, \
        "the ledger must record that the prompt was clamped"


def _long_pdf_bytes() -> bytes:
    """A real PDF whose text far exceeds the minimum input budget."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=LETTER)
    styles = getSampleStyleSheet()
    elements = [
        Paragraph("Test Candidate", styles["Title"]),
        Paragraph("test.candidate@example.com | +91 90000 00001 | Bangalore, India", styles["Normal"]),
        Paragraph("Summary", styles["Heading2"]),
        Paragraph(
            "Backend engineer with 6 years building Python, FastAPI, PostgreSQL and "
            "Kubernetes services for fintech payments platforms.", styles["Normal"]),
        Paragraph("Experience", styles["Heading2"]),
        Paragraph("Senior Software Engineer - FinCo (2021 - present): payments platform, 2M users.",
                  styles["Normal"]),
    ]
    # Pad with real, harmless prose until the document is unambiguously long —
    # keeping every fact the deterministic stand-in profile claims (FinCo,
    # ShopStack, 2018, 2021) so the grounding guardrail stays satisfied.
    filler = ("This paragraph is additional context for the parse budget test. "
              "It describes generic backend work: APIs, databases, queues, caches, "
              "observability, and releases. ")
    n = 0
    while len(filler) * n < 6000:
        n += 1
        elements.append(Paragraph(filler, styles["Normal"]))
    elements.append(Paragraph(
        "Software Engineer - ShopStack (2018 - 2021): order pipeline on AWS.", styles["Normal"]))
    doc.build(elements)
    return buffer.getvalue()
