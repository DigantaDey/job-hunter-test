"""
Contract-level tests for the automation-policy write surface
(``docs/contracts/10-automation-policy.md`` §8, §9 and §7 P2/P3).

These pin the *write-time* invariants — the ones that decide whether a bad
``PUT /api/automation/policy`` request is refused *and writes nothing*, versus
being silently normalised into a weaker-but-valid row. The evaluation-time
behaviour (gates, downgrades, counters) is covered by the pipeline and
browser-session suites.

Drive everything through the HTTP API so the whole refusal chain is exercised:
the router's typed gate (``402``/``403``/``409``), the service's validation
(``400``), and the audit row the canonical actions promise.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.config import settings
from app.models.models import (
    AuditLog,
    AutomationPolicy,
    AutomationPolicyRevision,
    Subscription,
    User,
)

SUBMIT = "application_submit"
POLICY_URL = "/api/automation/policy"


def _owner(db) -> User:
    return db.query(User).order_by(User.id).first()


def _count_policies(db, user_id: int) -> int:
    return (
        db.query(AutomationPolicy)
        .filter(AutomationPolicy.user_id == user_id)
        .count()
    )


def _count_revisions(db, user_id: int) -> int:
    return (
        db.query(AutomationPolicyRevision)
        .filter(AutomationPolicyRevision.user_id == user_id)
        .count()
    )


def _consent_all(client, auth) -> None:
    response = client.post(
        "/api/account/consent",
        json={"terms": True, "automation": True, "outreach": True, "data_processing": True},
        headers=auth,
    )
    assert response.status_code == 200, response.text


def _enable_server(monkeypatch, *, dry_run: bool = False, allow_submit: bool = True) -> None:
    monkeypatch.setattr(settings, "autofill_enabled", True, raising=False)
    monkeypatch.setattr(settings, "autofill_dry_run", dry_run, raising=False)
    monkeypatch.setattr(settings, "autofill_allow_submit", allow_submit, raising=False)


def _set_allow_auto_submit(client, auth, value: bool) -> None:
    response = client.put(
        "/api/settings", json={"application": {"allow_auto_submit": value}}, headers=auth
    )
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# Invariant 3 — auto_submit is refused (and writes nothing) unless every gate
# is satisfied. The refusal order is plan → server → consent, exactly §3.
# --------------------------------------------------------------------------- #
def test_auto_submit_plan_gate_returns_402_and_writes_no_row(
    client, auth, db, monkeypatch,
):
    """A free-plan user cannot opt into auto_submit: 402 upgrade_required.

    plan is free (no subscription) but can_use_autofill is True on free — the
    *mode ceiling* for free is ``prepare``, so the escalation is plan-locked.
    """
    _consent_all(client, auth)
    _enable_server(monkeypatch, dry_run=False)

    owner = _owner(db)
    assert _count_policies(db, owner.id) == 0

    denied = client.put(POLICY_URL, json={"mode": "auto_submit"}, headers=auth)
    assert denied.status_code == 402
    assert denied.json()["detail"]["code"] == "upgrade_required"
    assert denied.json()["detail"]["capability"] == "auto_submit"

    # Invariant 3: the write was refused — no row, no revision.
    assert _count_policies(db, owner.id) == 0
    assert _count_revisions(db, owner.id) == 0


def test_auto_submit_server_gate_returns_409_and_writes_no_row(
    client, auth, db, monkeypatch, full_consent,
):
    """Even a Pro user cannot raise a ceiling the deployment keeps shut."""
    owner = _owner(db)
    db.add(Subscription(user_id=owner.id, plan="pro", status="active",
                        provider="manual", current_period_start=datetime.now(timezone.utc)))
    db.commit()

    # dry_run server: preparation only, submission is flagged off.
    _enable_server(monkeypatch, dry_run=True)
    _set_allow_auto_submit(client, auth, True)

    denied = client.put(POLICY_URL, json={"mode": "auto_submit"}, headers=auth)
    assert denied.status_code == 409
    assert denied.json()["detail"]["code"] == "server_disabled"
    assert _count_policies(db, owner.id) == 0


def test_auto_submit_consent_gate_returns_403_and_writes_no_row(
    client, auth, db, monkeypatch,
):
    """Pro + server + missing automation disclosure → 403 consent_required."""
    owner = _owner(db)
    db.add(Subscription(user_id=owner.id, plan="pro", status="active",
                        provider="manual", current_period_start=datetime.now(timezone.utc)))
    db.commit()
    _enable_server(monkeypatch, dry_run=False)
    _set_allow_auto_submit(client, auth, True)
    # No consent POSTed at all: automation disclosure is absent.

    denied = client.put(POLICY_URL, json={"mode": "auto_submit"}, headers=auth)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "consent_required"
    assert _count_policies(db, owner.id) == 0


def test_auto_submit_all_gates_present_is_accepted(
    client, auth, db, monkeypatch, full_consent,
):
    """Pro + server + allow_auto_submit consent → the opt-in is allowed."""
    owner = _owner(db)
    db.add(Subscription(user_id=owner.id, plan="pro", status="active",
                        provider="manual", current_period_start=datetime.now(timezone.utc)))
    db.commit()
    _enable_server(monkeypatch, dry_run=False)
    _set_allow_auto_submit(client, auth, True)

    accepted = client.put(POLICY_URL, json={"mode": "auto_submit"}, headers=auth)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["mode_chosen"] == "auto_submit"
    assert accepted.json()["http_allowed"] is True

    row = (
        db.query(AutomationPolicy)
        .filter(AutomationPolicy.user_id == owner.id,
                AutomationPolicy.scope == "global")
        .first()
    )
    assert row is not None
    assert row.mode == "auto_submit"
    assert row.mode_chosen_at is not None
    assert row.version == 2  # creation revision + the explicit change

    # P3: enabling auto-submit writes its own consent-adjacent, canonical audit row.
    enabled = (
        db.query(AuditLog)
        .filter(AuditLog.user_id == owner.id,
                AuditLog.action == "automation.auto_submit_enabled")
        .all()
    )
    assert enabled, "P3 requires the automation.auto_submit_enabled audit row"


def test_auto_submit_rejected_when_user_consent_setting_missing(
    client, auth, db, monkeypatch,
):
    """The shipped ``allow_auto_submit`` setting is one of the four gates."""
    owner = _owner(db)
    db.add(Subscription(user_id=owner.id, plan="pro", status="active",
                        provider="manual", current_period_start=datetime.now(timezone.utc)))
    db.commit()
    _enable_server(monkeypatch, dry_run=False)
    _consent_all(client, auth)
    # allow_auto_submit left at its shipped default (False).

    denied = client.put(POLICY_URL, json={"mode": "auto_submit"}, headers=auth)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "consent_required"
    assert "automation" in denied.json()["detail"]["consent"]
    assert _count_policies(db, owner.id) == 0


# --------------------------------------------------------------------------- #
# Invariant 4 — eeo_policy can never be ``use_saved_answer``.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["use_saved_answer"])
def test_eeo_use_saved_answer_is_rejected_as_400(client, auth, db, bad):
    owner = _owner(db)
    denied = client.put(POLICY_URL, json={"eeo_policy": bad}, headers=auth)
    assert denied.status_code == 400
    assert denied.json()["detail"]["code"] == "invalid_automation_policy"
    assert _count_policies(db, owner.id) == 0
    assert _count_revisions(db, owner.id) == 0


def test_eeo_only_allows_never_answer_and_prefer_decline(client, auth, db):
    owner = _owner(db)
    for good in ("never_answer", "prefer_decline"):
        response = client.put(POLICY_URL, json={"eeo_policy": good}, headers=auth)
        assert response.status_code == 200, response.text
        assert response.json()["policy"]["eeo_policy"] == good
    assert _count_policies(db, owner.id) == 1  # upsert, not a new row each time


# --------------------------------------------------------------------------- #
# Invariant 5 — one revision per change, with the exact changed_keys.
# --------------------------------------------------------------------------- #
def test_every_change_appends_one_revision_with_changed_keys(client, auth, db):
    """Two distinct changes → exactly two revision rows on top of creation."""
    owner = _owner(db)

    first = client.put(POLICY_URL, json={"min_match_score": 70.0}, headers=auth)
    assert first.status_code == 200, first.text
    second = client.put(POLICY_URL, json={"max_runs_per_day": 5}, headers=auth)
    assert second.status_code == 200, second.text

    revisions = (
        db.query(AutomationPolicyRevision)
        .filter(AutomationPolicyRevision.user_id == owner.id)
        .order_by(AutomationPolicyRevision.version)
        .all()
    )
    # version 1 = creation, version 2 = min_match_score, version 3 = max_runs_per_day.
    assert [r.version for r in revisions] == [1, 2, 3]
    assert revisions[1].changed_keys == ["min_match_score"]
    assert revisions[2].changed_keys == ["max_runs_per_day"]
    # Each revision freezes the whole row as it stood after the change.
    assert revisions[2].document["min_match_score"] == 70.0
    assert revisions[2].document["max_runs_per_day"] == 5


# --------------------------------------------------------------------------- #
# Invariant 1 — exactly one global row per (user, workflow), created lazily on
# first write (bootstrap itself must not create one).
# --------------------------------------------------------------------------- #
def test_policy_row_is_a_single_global_upsert(client, auth, db):
    owner = _owner(db)
    assert _count_policies(db, owner.id) == 0  # bootstrap created none

    client.put(POLICY_URL, json={"enabled": False}, headers=auth)
    client.put(POLICY_URL, json={"enabled": True}, headers=auth)

    rows = (
        db.query(AutomationPolicy)
        .filter(AutomationPolicy.user_id == owner.id,
                AutomationPolicy.scope == "global")
        .all()
    )
    assert len(rows) == 1
    assert rows[0].scope_key == "*"
    assert rows[0].enabled is True


# --------------------------------------------------------------------------- #
# Audit action parity — the actions the write surface emits must be in the
# canonical AUDIT_ACTIONS vocabulary and the sensitive one in core.audit.
# --------------------------------------------------------------------------- #
def test_policy_write_audit_actions_are_canonical(client, auth, db, monkeypatch, full_consent):
    from app.contracts.vocabulary import AUDIT_ACTIONS
    from app.core.audit import SENSITIVE_ACTIONS

    owner = _owner(db)
    db.add(Subscription(user_id=owner.id, plan="pro", status="active",
                        provider="manual", current_period_start=datetime.now(timezone.utc)))
    db.commit()
    _enable_server(monkeypatch, dry_run=False)
    _set_allow_auto_submit(client, auth, True)

    response = client.put(POLICY_URL, json={"mode": "auto_submit"}, headers=auth)
    assert response.status_code == 200, response.text

    actions = {row.action for row in db.query(AuditLog).filter(AuditLog.user_id == owner.id)}
    assert "automation.policy_updated" in actions
    assert "automation.auto_submit_enabled" in actions
    assert "automation.policy_updated" in AUDIT_ACTIONS
    assert "automation.auto_submit_enabled" in AUDIT_ACTIONS
    assert "automation.auto_submit_enabled" in SENSITIVE_ACTIONS


# --------------------------------------------------------------------------- #
# No silent normalisation — a non-mode field write must never bend an
# un-authorised escalation into a persisted auto_submit (the pre-fix defect).
# --------------------------------------------------------------------------- #
def test_update_policy_never_normalizes_unconsented_mode(client, auth, db):
    """A mode change that the consent chain cannot authorise is refused by the
    router gate *before* the service runs, so no row appears. This is the
    regression for invariant 3's 'refuse, write no row' rule."""
    owner = _owner(db)
    denied = client.put(POLICY_URL, json={"mode": "auto_submit"}, headers=auth)
    assert denied.status_code in (400, 402, 403)
    assert _count_policies(db, owner.id) == 0
    assert _count_revisions(db, owner.id) == 0
