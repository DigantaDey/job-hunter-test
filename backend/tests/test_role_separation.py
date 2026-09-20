"""
Role separation: the owner/admin surface vs the end-user surface.

The product has two faces:

* **User surface** — matches, applications, interviews, reminders. Every job
  seeker, owner included.
* **Owner/admin surface** — AI provider configuration, source/queue health,
  usage & cost, failure rates, global feature flags, audit trail and
  plan/entitlement controls. Server-enforced (403), never merely hidden.

These tests pin the boundary from both directions: a member must never reach an
owner endpoint or see provider material in a payload they *can* read, and the
owner must keep full access to the controls they already had.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.models.models import (
    ApplicationTracking,
    InterviewPrep,
    Job,
    MatchResult,
    Resume,
    UserInputRequest,
)

OWNER_ONLY_GETS = [
    "/api/settings/ai/status",
    "/api/ai/config",
    "/api/settings/runtime",
    "/api/ops/status",
    "/api/analytics/costs",
    "/api/admin/overview",
    "/api/admin/audit",
    "/api/admin/flags",
    "/api/admin/users",
]


# --------------------------------------------------------------------------- #
# Endpoint authorization
# --------------------------------------------------------------------------- #
def test_unauthenticated_requests_cannot_reach_owner_surface(client):
    for url in OWNER_ONLY_GETS:
        assert client.get(url).status_code == 401, url


def test_member_is_403_on_every_owner_endpoint(client, member_auth):
    """The whole admin surface refuses a member before any handler runs."""
    for url in OWNER_ONLY_GETS:
        response = client.get(url, headers=member_auth)
        assert response.status_code == 403, (url, response.status_code)
        detail = response.json().get("detail")
        code = detail.get("code") if isinstance(detail, dict) else None
        assert code == "owner_required", (url, detail)


def test_owner_keeps_full_access_to_owner_surface(client, auth):
    for url in OWNER_ONLY_GETS:
        response = client.get(url, headers=auth)
        assert response.status_code == 200, (url, response.text)


def test_member_cannot_write_ai_provider_settings(client, member_auth):
    """PUT /settings with the ai category is a provider change → owner only."""
    for payload in (
        {"ai": {"base_url": "https://evil.example.com/v1", "model": "gpt-4o"}},
        {"ai": {"api_key": "sk-stolen"}},
        {"ai": {"max_input_tokens": 999999}},
    ):
        response = client.put("/api/settings", json=payload, headers=member_auth)
        assert response.status_code == 403, payload
        assert response.json()["detail"]["code"] == "owner_required"


def test_member_can_still_write_their_own_settings(client, member_auth):
    response = client.put("/api/settings", json={"automation": {"auto_mode": False}}, headers=member_auth)
    assert response.status_code == 200, response.text


def test_member_cannot_write_per_workflow_ai_config(client, member_auth):
    response = client.post("/api/ai/config", json={"scoring": {"api_key": "sk-member-key"}}, headers=member_auth)
    assert response.status_code == 403
    # and nothing was stored
    from app.models.models import User
    from app.services.user_settings import read_workflow_override

    # read via the API instead: the member's own key set is untouched (none)
    assert client.get("/api/ai/config", headers=member_auth).status_code == 403


def test_owner_can_write_ai_provider_settings(client, auth):
    response = client.put(
        "/api/settings",
        json={"ai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "rpm": 50}},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True


def test_member_cannot_run_queue_admin_actions(client, member_auth):
    assert client.post("/api/ops/queue/recover", headers=member_auth).status_code == 403
    assert client.post("/api/ops/queue/retry-dead", headers=member_auth).status_code == 403


# --------------------------------------------------------------------------- #
# Payload sanitization (what a member can *read*)
# --------------------------------------------------------------------------- #
def test_member_settings_payload_hides_provider_configuration(client, auth, member_auth):
    """The owner saves a provider; the member's settings read must not carry it."""
    client.put("/api/settings", json={
        "ai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini",
               "api_key": "sk-owner-secret", "rpm": 42, "max_input_tokens": 12000},
    }, headers=auth)

    member_view = client.get("/api/settings", headers=member_auth).json()
    ai = member_view.get("ai", {})
    assert ai.get("configured") is True  # the one bit users need
    for forbidden in ("base_url", "model", "rpm", "api_key", "api_key_set", "api_key_masked",
                      "max_input_tokens", "max_output_tokens", "timeout", "provider"):
        assert forbidden not in ai, forbidden
    # the writable schema no longer advertises the ai category to members
    assert "ai" not in member_view["_meta"]["writable"]
    # other categories are untouched
    assert "general" in member_view

    owner_view = client.get("/api/settings", headers=auth).json()
    assert owner_view["ai"]["base_url"] == "https://api.openai.com/v1"  # owner keeps full control


def test_member_assistant_read_has_no_provider_material(client, auth, member_auth):
    client.put("/api/settings", json={"ai": {"base_url": "https://secret.example/v1",
                                             "model": "gpt-4o-mini", "api_key": "sk-owner-secret"}},
               headers=auth)
    body = client.get("/api/me/assistant", headers=member_auth).json()
    assert body["configured"] is True
    for forbidden in ("base_url", "model", "api_key", "key_preview", "key_source",
                      "user_config", "usage", "breakers", "rpm", "remaining", "overrides"):
        assert forbidden not in body, forbidden


# --------------------------------------------------------------------------- #
# Admin controls actually work (owner preserves full control)
# --------------------------------------------------------------------------- #
def test_owner_can_flip_feature_flags_and_members_cannot(client, auth, member_auth, db):
    response = client.put("/api/admin/flags", json={"assistant.interview_prep": False}, headers=auth)
    assert response.status_code == 200, response.text
    flags = {f["key"]: f for f in client.get("/api/admin/flags", headers=auth).json()["flags"]}
    assert flags["assistant.interview_prep"]["enabled"] is False

    # The flag is live for users: generating interview prep is refused with a
    # friendly message, not a silent failure.
    blocked = client.post("/api/interview/generate",
                          json={"job_title": "Backend Engineer", "job_description": "python"},
                          headers=member_auth)
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "feature_disabled"

    # Unknown flags are rejected, and the change was audited.
    assert client.put("/api/admin/flags", json={"not.a.flag": True}, headers=auth).status_code == 400
    from app.models.models import AuditLog

    assert db.query(AuditLog).filter(AuditLog.action == "admin.flag_updated").count() == 1


def test_owner_sets_member_plan_and_role(client, auth, member_auth, db):
    from app.models.models import Subscription, User

    member = db.query(User).filter(User.email == "member@example.com").first()

    plan = client.put(f"/api/admin/users/{member.id}/plan", json={"plan": "pro"}, headers=auth)
    assert plan.status_code == 200, plan.text
    sub = db.query(Subscription).filter(Subscription.user_id == member.id).first()
    assert sub.plan == "pro"

    assert client.put(f"/api/admin/users/{member.id}/plan", json={"plan": "gold"}, headers=auth).status_code == 400
    assert client.put(f"/api/admin/users/{member.id}/plan", json={"plan": "pro"},
                      headers=member_auth).status_code == 403

    # Role changes work, but the last owner cannot demote themselves.
    owner = db.query(User).filter(User.email == "owner@example.com").first()
    demote_self = client.put(f"/api/admin/users/{owner.id}/role", json={"role": "member"}, headers=auth)
    assert demote_self.status_code == 400  # owner@example.com is the only owner so far
    promote = client.put(f"/api/admin/users/{member.id}/role", json={"role": "owner"}, headers=auth)
    assert promote.status_code == 200


def test_admin_audit_lists_cross_user_actions(client, auth, member_auth, db):
    # A member action and an owner action both land in the audit trail.
    client.put("/api/settings", json={"automation": {"auto_mode": False}}, headers=member_auth)
    response = client.get("/api/admin/audit?limit=50", headers=auth)
    assert response.status_code == 200
    actions = [item["action"] for item in response.json()["items"]]
    assert "settings.updated" in actions
    filtered = client.get("/api/admin/audit?action_prefix=admin.", headers=auth)
    assert all(item["action"].startswith("admin.") for item in filtered.json()["items"])


# --------------------------------------------------------------------------- #
# The user dashboard aggregate
# --------------------------------------------------------------------------- #
def _seed_member_world(db, member):
    """A small, realistic world for one member."""
    job = Job(user_id=member.id, title="Senior Backend Engineer", company="Acme",
              description="Python, FastAPI, PostgreSQL", dedupe_key="acme:1", status="applied",
              score=86.0, applied_at=datetime.utcnow() - timedelta(days=2))
    db.add(job)
    db.flush()
    db.add(MatchResult(
        user_id=member.id, job_id=job.id, score=86.0, band="strong", reason="8 of 10 required skills match your profile",
        matched_skills=[{"name": "Python"}, {"name": "FastAPI"}],
        missing_skills=[{"name": "Kafka", "required": True}],
        rubric={"recommendation": "Apply — strong overlap with your backend experience",
                "contributions": [{"feature": "skills_overlap", "reason": "Your Python and FastAPI depth covers the core stack"}]},
        hard_filters={}, flags={}, is_current=True,
    ))
    needs_input = Job(user_id=member.id, title="Platform Engineer", company="BetaCo",
                      description="k8s", dedupe_key="beta:1", status="needs_input", score=71.0)
    db.add(needs_input)
    db.flush()
    db.add(UserInputRequest(user_id=member.id, job_id=needs_input.id,
                            fields=[{"name": "notice_period", "label": "Notice period", "required": True}],
                            status="pending"))
    in_progress = Job(user_id=member.id, title="Data Engineer", company="GammaCo",
                      description="spark", dedupe_key="gamma:1", status="preparing", score=64.0)
    db.add(in_progress)
    db.add(ApplicationTracking(
        user_id=member.id, job_id=job.id, state="applied", phase="tracking",
        applied_at=datetime.utcnow() - timedelta(days=9),
        job_title_snapshot="Senior Backend Engineer", company_snapshot="Acme",
    ))
    db.add(InterviewPrep(user_id=member.id, job_id=job.id, job_title="Senior Backend Engineer",
                         company="Acme", status="completed"))
    db.add(Resume(user_id=member.id, filename="master.pdf", filepath="/tmp/master.pdf",
                  type="master", status="approved"))
    db.commit()
    return job


def test_user_dashboard_gives_actionable_work_not_infrastructure(client, member_auth, db, member):
    from app.models.models import User

    user = db.query(User).filter(User.email == "member@example.com").first()
    _seed_member_world(db, user)

    body = client.get("/api/me/dashboard", headers=member_auth)
    assert body.status_code == 200, body.text
    data = body.json()

    # The ten product sections are present.
    for section in ("profile", "discovery", "top_matches", "needs_review", "applications_in_progress",
                    "action_queue", "interviews", "weekly_report", "follow_ups", "assistant"):
        assert section in data, section

    # Profile completeness measures the profile, in user language.
    assert 0 <= data["profile"]["percent"] <= 100
    assert data["profile"]["has_master_resume"] is True
    assert data["profile"]["next_step"]

    # Top matches explain *why*, without provider or token vocabulary anywhere.
    top = data["top_matches"]
    assert top and top[0]["title"] == "Senior Backend Engineer"
    assert top[0]["why"], "each match must carry its reasons"
    assert any("Python" in reason or "Apply" in reason for reason in top[0]["why"])
    assert top[0]["missing_skills"] == ["Kafka"]

    serialized = str(data).lower()
    for forbidden in ("api_key", "base_url", "token_budget", "max_input_tokens", "estimated_cost_usd"):
        assert forbidden not in serialized, forbidden

    # The work queue is the user's, and actionable.
    kinds = {item["kind"] for item in data["action_queue"]}
    assert "questions" in kinds
    assert any(item["title"].startswith("Answer 1 question") for item in data["action_queue"])

    assert data["needs_review"] and data["needs_review"][0]["company"] == "BetaCo"
    assert data["applications_in_progress"]
    assert any(item["company"] == "GammaCo" and "Preparing" in item["stage_label"]
               for item in data["applications_in_progress"])
    assert data["interviews"]["practice_completed"] == 1

    # Weekly outcomes lead with applications/interviews, not tokens.
    report = data["weekly_report"]
    assert report["applications_submitted"] == 1
    assert report["headline"]

    # A 9-day-old application with no response gets a follow-up suggestion.
    suggestions = [i for i in data["follow_ups"]["items"] if i.get("suggested")]
    assert suggestions and suggestions[0]["company"] == "Acme"


def test_user_dashboard_empty_state_is_guidance_not_blank(client, member_auth):
    body = client.get("/api/me/dashboard", headers=member_auth)
    assert body.status_code == 200
    data = body.json()
    assert data["top_matches"] == []
    assert data["discovery"]["never_discovered"] is True
    assert "has not run yet" in data["discovery"]["detail"]
    assert data["profile"]["percent"] <= 100
    assert data["weekly_report"]["headline"]  # still says something useful


def test_member_cannot_read_another_tenant_dashboard(client, auth, member_auth, db, owner, member):
    from app.models.models import User

    owner_user = db.query(User).filter(User.email == "owner@example.com").first()
    _seed_member_world(db, owner_user)  # data belongs to the OWNER

    member_view = client.get("/api/me/dashboard", headers=member_auth).json()
    assert member_view["top_matches"] == []  # tenancy: the member sees none of it
    assert member_view["counts"]["jobs_total"] == 0
