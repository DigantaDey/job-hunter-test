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


from app.services.scoring import SCORING_OUTPUT_TOKENS  # noqa: E402


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


def test_token_budgets_are_owner_level_even_on_paid_tiers(client, auth, db):
    """Token budgets moved to the owner surface (v2.3 role separation).

    A member used to be able to edit their own budgets on a paid plan; the
    product now treats budgets as owner-level infrastructure: even a paying
    member is refused server-side, while the owner keeps full control.
    """
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

    refused = client.put("/api/settings", json={"ai": {"max_input_tokens": 8000,
                                                       "max_output_tokens": 2000}},
                         headers=member_auth)
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["code"] == "owner_required"

    # The owner edits the same budgets through the same API.
    saved = client.put("/api/settings", json={"ai": {"max_input_tokens": 8000,
                                                     "max_output_tokens": 2000}},
                       headers=auth)
    assert saved.status_code == 200, saved.text
    ai = client.get("/api/settings", headers=auth).json()["ai"]
    assert ai["max_input_tokens"] == 8000
    assert ai["max_output_tokens"] == 2000
    assert ai["token_limits_editable"] is True


def test_free_tier_member_cannot_edit_ai_settings(client, auth, db, member_auth):
    """Members (free tier included) never write the ai category — the server
    rejects the PUT with owner_required, not just the UI hiding the fields."""
    rejected = client.put("/api/settings", json={"ai": {"max_output_tokens": 4000}},
                          headers=member_auth)
    assert rejected.status_code == 403, rejected.text
    assert rejected.json()["detail"]["code"] == "owner_required"

    # Non-token AI settings are owner-level too now.
    timeout = client.put("/api/settings", json={"ai": {"timeout": 60}}, headers=member_auth)
    assert timeout.status_code == 403, timeout.text

    # The owner keeps full control of the same field through the same API.
    saved = client.put("/api/settings", json={"ai": {"max_output_tokens": 4000}}, headers=auth)
    assert saved.status_code == 200, saved.text


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


# --------------------------------------------------------------------------- #
# 3. ``0`` = UNLIMITED (the shipped default, and the Pro+ promise)
#
# The reported bug: a Pro+ user (222757/2000000 credits — nowhere near a quota)
# got "Score 57 preliminary + AI pending" on a long job description. The AI
# never answered: every outgoing ``max_tokens`` was clamped to a 16000 ceiling
# that neither the operator nor the user had chosen, the escalation was pinned
# at the same value, and the honest outcome was ``truncated_response``.
# ``0`` on either budget now means "no clamp — the provider's own maximum".
# --------------------------------------------------------------------------- #
def _thinking_only() -> Dict[str, Any]:
    """A wire answer whose whole budget went on reasoning: empty + length."""
    return {"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 4000, "completion_tokens": 1200, "total_tokens": 5200}}


def _wire_max_tokens(request_body: Dict[str, Any]) -> int:
    return int(request_body.get("max_tokens") or request_body.get("max_completion_tokens") or 0)


def _long_jd() -> str:
    """A job description long enough to make a reasoning model think for a while."""
    filler = ("Responsibilities include designing, building and operating distributed "
              "backend services: API design, queueing, idempotency, observability, "
              "capacity planning and incident response for a payments platform. ")
    head = ("Senior Backend Engineer. Python, FastAPI, PostgreSQL, Kubernetes and AWS. "
            "You will own the payments platform serving 2M users. ")
    return head + filler * 320  # ~44k chars — well past any 12000-token budget


def test_zero_is_stored_and_reported_as_unlimited(client, auth, db):
    """``0`` round-trips through the settings API and is flagged as unlimited.

    ``PUT {ai:{max_output_tokens:0}}`` used to be rejected by the range check
    (64–16000) and, when it got through, ``value or default`` read the stored 0
    as "unset" and restored the platform ceiling.
    """
    from app.services.user_settings import get_user_ai_config

    _set_tokens(client, auth, max_input_tokens=8000, max_output_tokens=4000)

    saved = client.put("/api/settings",
                       json={"ai": {"max_input_tokens": 0, "max_output_tokens": 0}},
                       headers=auth)
    assert saved.status_code == 200, saved.text
    assert saved.json()["updated"]["ai"]["max_output_tokens"] == 0, saved.json()

    ai = client.get("/api/settings", headers=auth).json()["ai"]
    assert ai["max_input_tokens"] == 0, ai
    assert ai["max_output_tokens"] == 0, ai
    assert ai["max_output_tokens_unlimited"] is True, ai
    assert ai["max_input_tokens_unlimited"] is True, ai
    # …and the gateway resolves the same unlimited value (not the ceiling that
    # was stored a moment earlier).
    cfg = get_user_ai_config(db, _owner_id(db))
    assert cfg["max_output_tokens"] == 0 and cfg["max_input_tokens"] == 0


def test_meta_reports_the_platform_token_budgets(client, auth):
    """/api/meta (and the runtime snapshot) publish the platform ceilings.

    ``0`` must be visible as ``0`` — the SPA renders it as "Unlimited", so a
    meta payload that hid or defaulted the value would show a cap that is not
    there.
    """
    from app.core.config import settings

    meta = client.get("/api/meta").json()
    assert meta["ai_max_output_tokens"] == settings.ai_max_output_tokens == 0, meta
    assert meta["ai_max_input_tokens"] == settings.ai_max_input_tokens == 0, meta
    assert meta["ai_output_unlimited"] is True
    assert meta["ai_input_unlimited"] is True

    platform = client.get("/api/settings/runtime", headers=auth).json()["platform"]
    assert platform["ai_max_output_tokens"] == 0
    assert platform["ai_output_unlimited"] is True


def test_negative_budget_is_unlimited_not_a_one_token_cap():
    """``AI_MAX_OUTPUT_TOKENS=-1`` must mean "off", never "1 token"."""
    from app.core.config import Settings, ai_token_budget

    assert ai_token_budget(-5) == 0
    assert ai_token_budget(None) == 0
    assert ai_token_budget("0") == 0
    assert ai_token_budget("16000") == 16000
    assert ai_token_budget("lots") == 0
    config = Settings(environment="development", ai_max_output_tokens=-1, ai_max_input_tokens=-1)
    assert config.ai_max_output_tokens == 0 and config.ai_max_input_tokens == 0
    assert config.ai_output_unlimited and config.ai_input_unlimited


def test_explicit_ceiling_still_round_trips(client, auth):
    """A real ceiling is unchanged by the new semantics (no regression)."""
    _set_tokens(client, auth, max_input_tokens=8000, max_output_tokens=4000)
    ai = client.get("/api/settings", headers=auth).json()["ai"]
    assert ai["max_input_tokens"] == 8000 and ai["max_output_tokens"] == 4000
    assert ai["max_output_tokens_unlimited"] is False
    _set_tokens(client, auth, max_input_tokens=0, max_output_tokens=0)


def test_negative_setting_is_stored_as_unlimited(client, auth):
    """A negative PUT is a typo for "off" — stored as 0, not rejected, not 1."""
    saved = client.put("/api/settings", json={"ai": {"max_output_tokens": -10}}, headers=auth)
    assert saved.status_code == 200, saved.text
    ai = client.get("/api/settings", headers=auth).json()["ai"]
    assert ai["max_output_tokens"] == 0
    assert ai["max_output_tokens_unlimited"] is True


def test_out_of_range_positive_budget_still_rejected(client, auth):
    """A *positive* value below the floor is still a 400 — it is not a cap, it
    is a guarantee that every answer truncates."""
    for key, bad in (("max_output_tokens", 10), ("max_output_tokens", 999999),
                     ("max_input_tokens", 10), ("max_input_tokens", 999999999),
                     ("max_output_tokens", "lots"), ("max_output_tokens", None)):
        rejected = client.put("/api/settings", json={"ai": {key: bad}}, headers=auth)
        assert rejected.status_code == 400, f"{key}={bad!r}: {rejected.text}"


def test_plan_ceiling_is_the_tier_boundary(client, auth, db, member_auth):
    """Pro+ is unlimited; free/pro keep their cap whatever env says.

    This is what keeps "free still truncates" true now that the *default* is
    unlimited: the plan, not the env default, is the boundary for tenants. The
    operator's own account is exempt — their value IS the platform default.
    """
    from datetime import datetime, timezone

    from app.models.models import Subscription, User
    from app.services.ai_client import resolve_config_for_user

    owner = db.query(User).filter(User.role == "owner").first()
    assert owner is not None
    assert resolve_config_for_user(db, int(owner.id))["max_output_tokens"] == 0, \
        "the operator is unlimited at the shipped default"

    member = db.query(User).filter(User.email == "member@example.com").first()
    assert member is not None
    free_cfg = resolve_config_for_user(db, int(member.id))
    assert free_cfg["max_output_tokens"] == 16000, free_cfg
    assert free_cfg["plan_max_output_tokens"] == 16000

    db.add(Subscription(user_id=member.id, plan="pro", status="active", provider="manual",
                        current_period_start=datetime.now(timezone.utc)))
    db.commit()
    assert resolve_config_for_user(db, int(member.id))["max_output_tokens"] == 16000

    db.query(Subscription).filter(Subscription.user_id == member.id).delete()
    db.add(Subscription(user_id=member.id, plan="pro_plus", status="active", provider="manual",
                        current_period_start=datetime.now(timezone.utc)))
    db.commit()
    pro_plus = resolve_config_for_user(db, int(member.id))
    assert pro_plus["max_output_tokens"] == 0, pro_plus
    assert pro_plus["plan_max_output_tokens"] == 0

    # (v2.3) Per-user budget *choices* are owner-level now: the owner's chosen
    # ceiling is the platform default members inherit, and a member cannot set
    # their own — the PUT is refused before the tier gate even runs.
    refused = client.put("/api/settings", json={"ai": {"max_output_tokens": 4000}},
                         headers=member_auth)
    assert refused.status_code == 403, refused.text
    assert resolve_config_for_user(db, int(member.id))["max_output_tokens"] == 0
    assert resolve_config_for_user(db, int(member.id))["max_output_tokens"] == 0


@pytest.mark.real_ai
def test_unlimited_ceiling_lets_the_escalation_grow_past_the_old_cap(
        client, auth, db, provider_owner):
    """The escalation is no longer pinned at 16000 when the ceiling is unlimited.

    This is the wire-level proof of the fix: the parse task starts at its own
    4000-token budget, burns it on thinking, and grows 4000 → 16000 → 64000.
    Under the old clamp the second step stopped at 16000 and the third could
    never happen, so the call died ``truncated_response``.
    """
    import conftest

    _set_tokens(client, auth, max_input_tokens=0, max_output_tokens=0)
    conftest.ScriptedAIHandler.behavior["override"] = [
        # The thinking phase ate the whole budget: empty content + length.
        {"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
         "usage": {"prompt_tokens": 500, "completion_tokens": 4000, "total_tokens": 4500}},
        # …and again at 16000, with a half-written JSON answer.
        {"choices": [{"message": {"content": '{"name": "Test Candidate", "skills": ['},
                      "finish_reason": "length"}],
         "usage": {"prompt_tokens": 500, "completion_tokens": 16000, "total_tokens": 16500}},
    ]

    response = client.post("/api/resume/upload",
                           files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
                           headers=auth)
    assert response.status_code == 200, response.text

    parse = [r for r in _wire()
             if any("extract a complete, structured profile" in str(m.get("content") or "").lower()
                    for m in r.get("messages") or [])]
    budgets = [_wire_max_tokens(r) for r in parse]
    # 4000 (the parse task's own budget) → ×4 on the empty answer → ×2 on the
    # half-written JSON. The third step is the regression proof: the old clamp
    # pinned both at 16000 and the call died `truncated_response` there.
    assert budgets == [4000, 16000, 32000], \
        f"an unlimited ceiling must let the escalation pass 16000, wire said {budgets}"

    from app.models.models import AICreditLedger

    row = (db.query(AICreditLedger).filter(AICreditLedger.workflow == "parse",
                                           AICreditLedger.success.is_(True))
           .order_by(AICreditLedger.id.desc()).first())
    assert row is not None
    assert row.meta.get("output_ceiling") == 0, row.meta
    assert row.meta.get("output_unlimited") is True, row.meta
    assert row.meta.get("escalation_ceiling") > 16000, row.meta


@pytest.mark.real_ai
def test_unlimited_input_budget_sends_the_document_whole(client, auth, db, provider_owner):
    """``ai.max_input_tokens=0`` truncates nothing — the long resume reaches the
    model in full and the ledger says no truncation happened.

    The old code collapsed an unlimited budget to its 1000-char floor, which
    would have shredded every prompt on the very path that is meant to be free
    of clamps.
    """
    from app.models.models import AICreditLedger

    _set_tokens(client, auth, max_input_tokens=0, max_output_tokens=0)

    response = client.post("/api/resume/upload",
                           files={"file": ("resume.pdf", _long_pdf_bytes(), "application/pdf")},
                           headers=auth)
    assert response.status_code == 200, response.text

    parse = [r for r in _wire()
             if any("extract a complete, structured profile" in str(m.get("content") or "").lower()
                    for m in r.get("messages") or [])]
    assert len(parse) == 1
    user_prompt = [str(m.get("content") or "") for m in parse[0]["messages"]
                   if m.get("role") == "user"][0]
    assert len(user_prompt) > 5000, f"the document must not be clamped: {len(user_prompt)} chars"
    assert "additional context for the parse budget test" in user_prompt

    row = (db.query(AICreditLedger).filter(AICreditLedger.workflow == "parse",
                                           AICreditLedger.success.is_(True))
           .order_by(AICreditLedger.id.desc()).first())
    assert row is not None
    assert row.meta.get("input_truncated") is False, row.meta
    assert row.meta.get("input_unlimited") is True, row.meta

    from app.services.ai_client import input_budget_chars

    assert input_budget_chars(db=db, user_id=int(_owner_id(db))) == 0


@pytest.mark.real_ai
def test_pro_plus_long_jd_gets_the_ai_verdict(client, auth, db, provider_owner):
    """The reported bug, end to end: Pro+ + a long JD + a model that needs to
    think → ``score_source="ai"``, no ``truncated_response``.

    The provider truncates the first two answers exactly as the report
    describes; only the third (unclamped) attempt can answer. A preliminary
    keyword score beside an "AI pending" badge is a fallback, and this tier
    must never be served one.
    """
    import conftest

    member_id, member_auth = _member(client, db, "proplus@example.com", plan="pro_plus")

    _profile(db, member_id)
    job_id = _job(db, member_id, _long_jd())
    # Four wire attempts: the model burns the budget thinking three times, so
    # only the 4th can answer. The chain starts at the workflow's own budget
    # (``SCORING_OUTPUT_TOKENS``, sized from ``SCORE_SCHEMA`` in v2.3) and
    # escalates ×4 per empty/``length`` answer up to the hard stop — every step
    # past 16000 was impossible under the old clamp, and the call died
    # ``truncated_response``.
    # (v2.3) Retry budgets are owner-level: the owner raises the platform
    # default and the Pro+ member inherits it; their own budgets come from the
    # plan (unlimited), not from a member-editable field.
    _set_tokens(client, auth, max_retries=4)
    conftest.ScriptedAIHandler.behavior["override"] = [_thinking_only() for _ in range(3)]

    response = client.get(f"/api/jobs/{job_id}/intelligence", headers=member_auth)
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["score_source"] == "ai", detail.get("score_source")
    assert detail.get("ai_error") is None, detail.get("ai_error")
    assert "truncated_response" not in str(detail), detail

    scored = [r for r in _wire()
              if any("score how well this candidate" in str(m.get("content") or "").lower()
                     for m in r.get("messages") or [])]
    budgets = [_wire_max_tokens(r) for r in scored]
    # Properties, not a frozen chain: the first attempt is the workflow's own
    # schema-sized budget, every retry asks for strictly more, and the last one
    # is past the old 16000 clamp the bug report was about.
    assert budgets[0] == SCORING_OUTPUT_TOKENS, budgets
    assert budgets == sorted(budgets) and len(set(budgets)) == len(budgets), budgets
    assert budgets[-1] > 16000, f"the escalation stayed clamped, wire said {budgets}"
    # And the whole job description went out — no client-side truncation.
    longest = max(len(str(m.get("content") or "")) for m in scored[-1]["messages"])
    assert longest > 20000, longest


@pytest.mark.real_ai
def test_free_tier_long_jd_still_truncates_to_preliminary(client, auth, db, provider_owner):
    """The other half of the contract: a capped tier is still capped.

    With the same provider and the same long JD, a free account's plan ceiling
    (16000) pins the escalation, so the honest outcome stays
    ``truncated_response`` and the caller falls back to the explicitly labelled
    preliminary score — never a number presented as an AI verdict.
    """
    import conftest

    from app.services.scoring import preliminary_score_detailed, score_job

    member_id, free_auth = _member(client, db, "free@example.com")
    _profile(db, member_id)
    jd = _long_jd()

    conftest.ScriptedAIHandler.behavior["override"] = [
        {"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
         "usage": {"prompt_tokens": 4000, "completion_tokens": 1200, "total_tokens": 5200}},
        {"choices": [{"message": {"content": '{"score": 88, "reason": "strong mat'},
                      "finish_reason": "length"}],
         "usage": {"prompt_tokens": 4000, "completion_tokens": 4800, "total_tokens": 8800}},
        {"choices": [{"message": {"content": '{"score": 88, "reason": "still not finished becau'},
                      "finish_reason": "length"}],
         "usage": {"prompt_tokens": 4000, "completion_tokens": 16000, "total_tokens": 20000}},
    ]

    result = _run_coro(score_job(_profile_data(db, member_id), jd, db=db, user_id=member_id,
                                 allow_preliminary=True))
    assert result["score_source"] == "pending", result
    assert result["error"]["reason"] == "truncated_response", result["error"]
    assert result["preliminary_score"] == preliminary_score_detailed(
        _profile_data(db, member_id), jd)["overall"]

    scored = [r for r in _wire()
              if any("score how well this candidate" in str(m.get("content") or "").lower()
                     for m in r.get("messages") or [])]
    budgets = [_wire_max_tokens(r) for r in scored]
    assert budgets and max(budgets) <= 16000, \
        f"a capped tier must never exceed its plan ceiling, wire said {budgets}"

    # And the product surface for that tier is the labelled estimate, never an
    # AI badge: /intelligence does not even call the model on free.
    job_id = _job(db, member_id, jd)
    detail = client.get(f"/api/jobs/{job_id}/intelligence", headers=free_auth).json()
    assert detail["score_source"] == "preliminary", detail.get("score_source")
    assert detail.get("upgrade_hint"), detail


# --------------------------------------------------------------------------- #
# Helpers for the unlimited-budget acceptance tests
# --------------------------------------------------------------------------- #
def _run_coro(coro):
    import asyncio

    return asyncio.run(coro)


def _owner_id(db) -> int:
    from app.models.models import User

    user = db.query(User).filter(User.role == "owner").first()
    assert user is not None
    return int(user.id)


def _member(client, db, email: str, *, plan: str = "free"):
    """Register a second account (optionally on a paid plan) → ``(user_id, auth)``."""
    from datetime import datetime, timezone

    from app.models.models import Subscription, User

    response = client.post("/api/auth/register",
                           json={"email": email, "password": "member-password-123",
                                 "name": "Member"})
    assert response.status_code == 201, response.text
    user = db.query(User).filter(User.email == email).first()
    assert user is not None
    if plan != "free":
        db.add(Subscription(user_id=int(user.id), plan=plan, status="active", provider="manual",
                            current_period_start=datetime.now(timezone.utc)))
        db.commit()
    return int(user.id), {"Authorization": f"Bearer {response.json()['access_token']}"}


def _profile(db, user_id: int) -> Dict[str, Any]:
    """The user's master profile row — the same shape the upload flow writes."""
    from conftest import stub_ai_response

    from app.models.models import Profile

    data = stub_ai_response("parse", "")
    db.add(Profile(user_id=user_id, data=data, layout={}, extraction_source="ai"))
    db.commit()
    return data


def _profile_data(db, user_id: int) -> Dict[str, Any]:
    from app.models.models import Profile

    row = (db.query(Profile).filter(Profile.user_id == user_id)
           .order_by(Profile.created_at.desc()).first())
    assert row is not None
    return row.data or {}


def _job(db, user_id: int, jd: str) -> int:
    """A job row created directly (no discovery, no network)."""
    from app.models.models import Job

    job = Job(user_id=user_id, title="Senior Backend Engineer", company="FinCo",
              location="Remote", description=jd, url="https://jobs.lever.co/finco/long-jd",
              source="lever", dedupe_key=f"lever:long-jd-{user_id}", status="discovered",
              score=0.0, score_source="pending")
    db.add(job)
    db.commit()
    db.refresh(job)
    return int(job.id)
