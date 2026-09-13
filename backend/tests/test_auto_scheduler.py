"""v2.2 — auto mode: autonomous scheduled work.

The contract under test is the whole point of the release: the system moves
without a click, and the user can always see *why* it did or did not.

* ``automation.auto_mode`` is writable on every tier but only *enableable* with
  ``can_use_scheduled_workflows`` — a free-tier enable is a 403
  ``upgrade_required``, the same shape the v2.1 token-limit gate uses, and the
  readable settings payload says the same thing the server will do.
* Cadence is one table (pro_plus 2 h / 6 h / 24 h, pro 12 h / 24 h, free none)
  and "due" means *last completed-or-paused auto run older than the cadence*.
  One enqueue per window, ever: a second sweep inside the window adds nothing.
* Nothing is enqueued into a known AI outage — the cycle is recorded as
  ``skipped_outage`` with the provider's reason, and starts when it answers
  again.
* ``automation_runs_per_month`` is charged once per *finished* auto item by the
  worker; at the limit the scheduler stops queueing and the user is told exactly
  once per month.
* Every outcome, including every skip, is readable from ``GET /api/automation``.
* One user's corrupt settings row cannot stop anyone else's sweep, and a failing
  sweep cannot end the loop.

Hermetic: ``ai_availability`` is exercised through the real probe — the
``provider_owner`` fixture points the owner's AI settings at the scripted local
provider, and a member with no key of their own inherits it, which is exactly the
signal the scheduler gates on.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest
from conftest import pdf_bytes  # noqa: F401  (top-level conftest — see the note in test_ai_pause_resume)

from app.services.auto_scheduler import (
    APPLICATION_PREP,
    CADENCE_SECONDS,
    DISCOVERY,
    FINISHED_STATES,
    FUNDING,
    QUOTA_NOTIFICATION_KIND,
    SKIP_STATES,
    AutoScheduler,
    cycle_bucket,
    dedupe_key,
    is_due,
    next_run_at,
    plan_cadence,
)

PRO_PLUS_CADENCE = {DISCOVERY: 7200, FUNDING: 21600, APPLICATION_PREP: 86400}
PRO_CADENCE = {DISCOVERY: 43200, FUNDING: 86400}


# --------------------------------------------------------------------------- #
# Isolation + helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_ai_state():
    import app.services.ai_client as ai_client

    ai_client._breakers.clear()
    ai_client._PING_CACHE.clear()
    yield
    ai_client._breakers.clear()
    ai_client._PING_CACHE.clear()


def _behavior() -> Dict[str, Any]:
    import conftest

    return conftest.ScriptedAIHandler.behavior


def _set_outage(status: int = 503) -> None:
    """The scripted provider goes down from the very next request on."""
    b = _behavior()
    b["status"] = status
    b["fail_after"] = b.get("good", 0)


def _set_recovery() -> None:
    _behavior()["fail_after"] = None


def _sync(db) -> None:
    """End any open read transaction so rows written by the API/worker are seen."""
    db.rollback()
    db.expire_all()


def _metric(snapshot: Dict[str, float], name: str, **labels: str) -> float:
    """Total of one counter across matching label sets (metrics are process-global)."""
    total = 0.0
    for key, value in snapshot.items():
        if key.split("{")[0] != name:
            continue
        if all(f'{label}="{val}"' in key for label, val in labels.items()):
            total += value
    return total


def _auth_for(tokens: Dict[str, Any]) -> Dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _register(client, email: str) -> Dict[str, Any]:
    response = client.post("/api/auth/register",
                           json={"email": email, "password": "auto-mode-password-123",
                                 "name": email.split("@")[0]})
    assert response.status_code == 201, response.text
    return response.json()


def _paid_user(client, db, email: str, plan: str = "pro_plus", *,
               consent: bool = True, auto_mode: bool = True) -> Dict[str, Any]:
    """A non-owner on a paid plan, via the public register endpoint + a live
    ``Subscription`` row (the same recipe ``test_ai_token_budgets`` uses)."""
    from app.models.models import Subscription, User

    tokens = _register(client, email)
    _sync(db)
    user = db.query(User).filter(User.email == email).first()
    assert user is not None
    db.add(Subscription(user_id=user.id, plan=plan, status="active", provider="manual",
                        current_period_start=datetime.utcnow()))
    db.commit()
    auth = _auth_for(tokens)
    if consent:
        accepted = client.post("/api/account/consent",
                               json={"terms": True, "automation": True, "outreach": True,
                                     "data_processing": True}, headers=auth)
        assert accepted.status_code == 200, accepted.text
    if auto_mode:
        saved = client.put("/api/settings", json={"automation": {"auto_mode": True}}, headers=auth)
        assert saved.status_code == 200, saved.text
    return {"id": int(user.id), "auth": auth, "email": email}


def _runs(db, user_id: int, workflow: Optional[str] = None):
    from app.models.models import ScheduledRun

    query = db.query(ScheduledRun).filter(ScheduledRun.user_id == user_id)
    if workflow:
        query = query.filter(ScheduledRun.workflow == workflow)
    return query.order_by(ScheduledRun.id).all()


def _items(db, user_id: int, pipeline: Optional[str] = None):
    from app.models.models import PipelineJob

    query = db.query(PipelineJob).filter(PipelineJob.user_id == user_id)
    if pipeline:
        query = query.filter(PipelineJob.pipeline == pipeline)
    return query.order_by(PipelineJob.id).all()


def _job(db, user_id: int, *, status: str = "discovered", score: float = 80.0, title: str = "Backend Engineer",
         company: str = "FinCo") -> int:
    from app.models.models import Job

    row = Job(user_id=user_id, title=title, company=company, location="Remote",
              description="Python, FastAPI, PostgreSQL.", source="lever",
              dedupe_key=f"lever:{company}:{title}:{user_id}:{score}", status=status, score=score,
              score_reason="test fixture", company_size="small")
    db.add(row)
    db.commit()
    db.refresh(row)
    return int(row.id)


def _snapshot():
    from app.core import metrics

    return metrics.snapshot()


# --------------------------------------------------------------------------- #
# 1. The cadence table and the due clock (pure logic, no I/O)
# --------------------------------------------------------------------------- #
def test_cadence_is_one_documented_table():
    """The schedule the release promises is the schedule the code holds."""
    assert plan_cadence("pro_plus") == PRO_PLUS_CADENCE
    assert plan_cadence("pro") == PRO_CADENCE
    assert plan_cadence("free") == {}
    assert plan_cadence(None) == {}
    assert plan_cadence("no-such-plan") == {}
    # Pro deliberately has no application-prep: a daily prepared-application pass
    # is the Pro+ tier's cost, and nothing else in the product hardcodes that.
    assert APPLICATION_PREP not in plan_cadence("pro")
    assert CADENCE_SECONDS["pro_plus"][DISCOVERY] == 2 * 3600
    assert CADENCE_SECONDS["pro_plus"][FUNDING] == 6 * 3600
    assert CADENCE_SECONDS["pro"][DISCOVERY] == 12 * 3600


def test_cycle_bucket_is_utc_aligned_and_window_stable():
    """The dedupe window is epoch-aligned UTC — never the machine's timezone."""
    moment = datetime(2026, 9, 13, 2, 0, 0)
    bucket = cycle_bucket(moment, 7200)
    assert bucket == int(moment.replace(tzinfo=timezone.utc).timestamp()) // 7200
    assert cycle_bucket(moment + timedelta(minutes=30), 7200) == bucket
    assert cycle_bucket(moment + timedelta(seconds=7199), 7200) == bucket
    assert cycle_bucket(moment + timedelta(seconds=7200), 7200) == bucket + 1
    # A naive datetime must not drift by the host offset: same instant, same bucket.
    assert cycle_bucket(moment.replace(tzinfo=timezone.utc), 7200) == bucket
    assert dedupe_key(DISCOVERY, 7, bucket) == f"auto:{DISCOVERY}:7:{bucket}"
    assert dedupe_key(DISCOVERY, 8, bucket) != dedupe_key(DISCOVERY, 7, bucket)


def test_due_check_includes_the_boundary_and_ignores_open_runs():
    now = datetime(2026, 9, 13, 12, 0, 0)
    assert is_due(None, now, 7200) is True  # never run → due

    from app.models.models import ScheduledRun

    def row(seconds_ago: int, state: str = "done") -> ScheduledRun:
        return ScheduledRun(user_id=1, workflow=DISCOVERY, state=state,
                            triggered_at=now - timedelta(seconds=seconds_ago), cycle_bucket=0)

    assert is_due(row(7199), now, 7200) is False
    assert is_due(row(7200), now, 7200) is True  # exactly one cadence old → due
    assert next_run_at(row(3600), now, 7200) == now - timedelta(seconds=3600) + timedelta(seconds=7200)
    assert next_run_at(None, now, 7200) == now
    # Skips are not the clock; finished states are.
    assert "skipped_outage" in SKIP_STATES and "skipped_outage" not in FINISHED_STATES
    assert set(FINISHED_STATES) == {"done", "paused", "failed", "needs_input"}


# --------------------------------------------------------------------------- #
# 2. The settings surface: gate, round-trip, honest locks
# --------------------------------------------------------------------------- #
def test_free_tier_cannot_enable_auto_mode(client, auth, db, member_auth):
    """Enabling is refused server-side (not just hidden), disabling never is."""
    blocked = client.put("/api/settings", json={"automation": {"auto_mode": True}}, headers=member_auth)
    assert blocked.status_code == 403, blocked.text
    detail = blocked.json()["detail"]
    assert detail["code"] == "upgrade_required", detail
    assert "auto_mode" in detail["fields"], detail
    assert "Pro" in detail["message"], detail

    # Nothing was stored: the readable view still says off, and says why.
    _sync(db)
    free = client.get("/api/settings", headers=member_auth).json()
    automation = free["automation"]
    assert automation["auto_mode"] is False, automation
    assert automation["can_use"] is False, automation
    assert automation["cadence"] == {}, automation
    assert "free tier" in automation["locked_reason"], automation
    assert "auto_mode" in list(free.get("_meta", {}).get("writable", {}).get("automation", []))

    # Turning it *off* is always allowed — a gate that only blocks enabling can
    # never trap a user who wants out.
    off = client.put("/api/settings", json={"automation": {"auto_mode": False}}, headers=member_auth)
    assert off.status_code == 200, off.text


def test_paid_tier_round_trips_and_sees_its_cadence(client, auth, db):
    paid = _paid_user(client, db, "cadence-pro@example.com", "pro", auto_mode=False)
    saved = client.put("/api/settings", json={"automation": {"auto_mode": True}}, headers=paid["auth"])
    assert saved.status_code == 200, saved.text

    shown = client.get("/api/settings", headers=paid["auth"]).json()["automation"]
    assert shown["auto_mode"] is True, shown
    assert shown["can_use"] is True, shown
    assert shown["cadence"] == PRO_CADENCE, shown
    assert "locked_reason" not in shown, shown
    # The same cadence the sweep reads — one table, no copy.
    assert plan_cadence("pro") == shown["cadence"]


def test_auto_mode_is_coerced_not_guessed(client, auth):
    """``"false"`` is a *string* that is truthy in Python — it must never store an
    enabled scheduler, and non-boolean junk is a 400, not a silent write."""
    off = client.put("/api/settings", json={"automation": {"auto_mode": "false"}}, headers=auth)
    assert off.status_code == 200, off.text
    assert client.get("/api/settings", headers=auth).json()["automation"]["auto_mode"] is False

    on = client.put("/api/settings", json={"automation": {"auto_mode": "on"}}, headers=auth)
    # The owner is on the free tier, so *enabling* is refused — but with the plan
    # reason, and the string was coerced before the gate looked at it.
    assert on.status_code == 403, on.text
    assert on.json()["detail"]["code"] == "upgrade_required", on.text

    junk = client.put("/api/settings", json={"automation": {"auto_mode": "maybe"}}, headers=auth)
    assert junk.status_code == 400, junk.text
    assert junk.json()["detail"]["code"] == "invalid_setting_value", junk.text
    assert client.get("/api/settings", headers=auth).json()["automation"]["auto_mode"] is False


# --------------------------------------------------------------------------- #
# 3. The sweep: one enqueue per window, on the cadence
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_sweep_enqueues_once_per_window_then_waits_for_the_cadence(client, auth, db, provider_owner):
    from app.services import persona as persona_service

    paid = _paid_user(client, db, "pp-sweep@example.com", "pro_plus")
    # What auto discovery searches for is what the user already has on file: their
    # own extra keywords, plus the search context stored on the active persona.
    # Nothing is invented and the model is not called to decide it.
    saved = client.put("/api/settings", json={"scraping": {"keywords": ["fastapi", "postgres"]}},
                       headers=paid["auth"])
    assert saved.status_code == 200, saved.text
    persona_service.ensure_persona(db, paid["id"], name="Platform track",
                                   search_context={"keywords": ["payments", "fastapi"]})
    t0 = datetime.utcnow().replace(microsecond=0)

    counts = await AutoScheduler().sweep(now=t0)
    _sync(db)
    assert counts["users"] >= 1 and counts["enqueued"] >= 1, counts

    discovery_items = _items(db, paid["id"], "discovery")
    funding_items = _items(db, paid["id"], "funding")
    # Both of the paid plan's always-due workflows ran; application-prep found
    # nothing ready, so it recorded a no-op instead of queueing anything.
    assert len(discovery_items) == 1, discovery_items
    assert len(funding_items) == 1, funding_items
    assert _items(db, paid["id"], "application") == []
    assert [row.state for row in _runs(db, paid["id"], APPLICATION_PREP)] == ["done"], _runs(db, paid["id"])

    item = discovery_items[0]
    assert item.payload["trigger"] == "auto", item.payload
    assert item.priority == 6, item.priority  # behind every interactive trigger
    assert item.dedupe_key == dedupe_key(DISCOVERY, paid["id"], cycle_bucket(t0, 7200)), item.dedupe_key
    assert item.payload["keywords"] == ["fastapi", "postgres", "payments"], item.payload
    assert item.payload["limit"] == 40 and item.payload["freshness_hours"] > 0, item.payload
    assert item.payload["live_enabled"] is False and item.payload["persona_id"], item.payload
    assert "context" not in funding_items[0].payload, funding_items[0].payload  # built inside the item

    # A second sweep inside the same window: nothing new, and no second row.
    again = await AutoScheduler().sweep(now=t0 + timedelta(seconds=30))
    _sync(db)
    assert again["enqueued"] == 0, again
    assert len(_items(db, paid["id"], "discovery")) == 1
    assert [r.state for r in _runs(db, paid["id"], DISCOVERY)] == ["queued"]

    # Still not *due* 5 minutes after a run that finished: the cadence is the law.
    _finish_last_run(db, paid["id"], DISCOVERY, t0)
    early = await AutoScheduler().sweep(now=t0 + timedelta(minutes=5))
    _sync(db)
    assert early["enqueued"] == 0, early
    assert len(_items(db, paid["id"], "discovery")) == 1

    # Past 2 h (into the next window) exactly one new item appears.
    late = await AutoScheduler().sweep(now=t0 + timedelta(seconds=7200 + 1))
    _sync(db)
    assert late["enqueued"] >= 1, late
    assert len(_items(db, paid["id"], "discovery")) == 2, _items(db, paid["id"], "discovery")
    assert _metric(_snapshot(), "jobhunter_auto_enqueued_total", workflow=DISCOVERY) >= 1.0


def _finish_last_run(db, user_id: int, workflow: str, moment: datetime) -> None:
    """Close a window's run the way ``reconcile`` would after a worker finished."""
    rows = _runs(db, user_id, workflow)
    rows[-1].state = "done"
    rows[-1].triggered_at = moment
    db.commit()
    db.expire_all()


@pytest.mark.asyncio
async def test_pro_cadence_is_twelve_hours_not_two(client, auth, db, provider_owner):
    paid = _paid_user(client, db, "pro-sweep@example.com", "pro")
    t0 = datetime.utcnow().replace(microsecond=0)
    await AutoScheduler().sweep(now=t0)
    _sync(db)
    assert len(_items(db, paid["id"], "discovery")) == 1
    _finish_last_run(db, paid["id"], DISCOVERY, t0)

    # 2 h on: the pro_plus interval — Pro must not be on it.
    await AutoScheduler().sweep(now=t0 + timedelta(hours=2, minutes=1))
    _sync(db)
    assert len(_items(db, paid["id"], "discovery")) == 1, "Pro discovery ran on the Pro+ cadence"

    await AutoScheduler().sweep(now=t0 + timedelta(seconds=PRO_CADENCE[DISCOVERY] + 1))
    _sync(db)
    assert len(_items(db, paid["id"], "discovery")) == 2

    # And Pro never prepares applications, however long you wait.
    await AutoScheduler().sweep(now=t0 + timedelta(days=7))
    _sync(db)
    assert _runs(db, paid["id"], APPLICATION_PREP) == []
    assert _items(db, paid["id"], "application") == []


# --------------------------------------------------------------------------- #
# 4. The gates that must stop work *before* it is queued
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_missing_consent_skips_and_says_so(client, auth, db, provider_owner):
    """No automation consent → no queue row, and a recorded reason (not silence)."""
    paid = _paid_user(client, db, "noconsent@example.com", "pro_plus", consent=False)
    now = datetime.utcnow().replace(microsecond=0)
    counts = await AutoScheduler().sweep(now=now)
    _sync(db)
    assert counts["enqueued"] == 0, counts
    assert _items(db, paid["id"]) == []
    rows = _runs(db, paid["id"])
    assert rows and all(row.state == "skipped_no_consent" for row in rows), [row.state for row in rows]
    assert all("disclosure" in (row.reason or "") for row in rows), [row.reason for row in rows]
    # A skip does not consume the cadence window's clock: the work is due the
    # moment consent exists.
    _consent_now(client, paid["auth"])
    after = await AutoScheduler().sweep(now=now + timedelta(seconds=1))
    _sync(db)
    assert after["enqueued"] >= 1, after


def _consent_now(client, auth) -> None:
    ok = client.post("/api/account/consent", json={"terms": True, "automation": True}, headers=auth)
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_ai_outage_enqueues_nothing_and_recovers(client, auth, db, provider_owner):
    """The gate is the cached availability probe, not a hope.

    An outage must produce ``skipped_outage`` + the provider's reason and *zero*
    queue rows; when the provider answers again (cache cleared, as the watchdog
    would) the same window still runs.
    """
    paid = _paid_user(client, db, "outage@example.com", "pro_plus")
    now = datetime.utcnow().replace(microsecond=0)

    _set_outage(503)
    import app.services.ai_client as ai_client

    ai_client._PING_CACHE.clear()
    before = len(_items(db, paid["id"]))
    counts = await AutoScheduler().sweep(now=now)
    _sync(db)
    assert counts["enqueued"] == 0, counts
    assert len(_items(db, paid["id"])) == before, "work was queued into an AI outage"
    rows = _runs(db, paid["id"], DISCOVERY)
    assert rows and rows[-1].state == "skipped_outage", [row.state for row in rows]
    assert "transient_outage" in (rows[-1].reason or ""), rows[-1].reason
    assert (rows[-1].meta or {}).get("ai_reason"), rows[-1].meta

    _set_recovery()
    ai_client._PING_CACHE.clear()
    recovered = await AutoScheduler().sweep(now=now + timedelta(seconds=30))
    _sync(db)
    assert recovered["enqueued"] >= 1, recovered
    assert len(_items(db, paid["id"], "discovery")) == before + 1


@pytest.mark.asyncio
async def test_workflow_toggle_off_is_never_scheduled(client, auth, db, provider_owner):
    """Auto mode cannot override a workflows toggle the user switched off."""
    paid = _paid_user(client, db, "notoggle@example.com", "pro_plus")
    saved = client.put("/api/settings", json={"workflows": {"ai_for_discovery": False}}, headers=paid["auth"])
    assert saved.status_code == 200, saved.text

    now = datetime.utcnow().replace(microsecond=0)
    await AutoScheduler().sweep(now=now)
    _sync(db)
    assert _items(db, paid["id"], "discovery") == [], "queued a workflow the user disabled"
    assert _runs(db, paid["id"], DISCOVERY) == []
    # The rest of the plan's schedule is untouched (funding rides the same toggle
    # by design, application-prep rides ai_for_resume → it still runs).
    assert len(_items(db, paid["id"], "application")) == 0
    assert [r.state for r in _runs(db, paid["id"], APPLICATION_PREP)] == ["done"]


@pytest.mark.asyncio
async def test_auto_off_midflight_in_flight_run_finishes_and_nothing_new_starts(client, auth, db, provider_owner):
    """Switching auto mode off stops *scheduling*; it never cancels a run."""
    paid = _paid_user(client, db, "midflight@example.com", "pro_plus")
    now = datetime.utcnow().replace(microsecond=0)
    await AutoScheduler().sweep(now=now)
    _sync(db)
    item = _items(db, paid["id"], "discovery")[0]

    off = client.put("/api/settings", json={"automation": {"auto_mode": False}}, headers=paid["auth"])
    assert off.status_code == 200, off.text

    from app.worker import Worker

    await Worker(pipelines=["discovery"])._run_item(item.id, "discovery")
    _sync(db)
    assert item.status == "done", item.status  # the in-flight run completed

    later = await AutoScheduler().sweep(now=now + timedelta(hours=3))
    _sync(db)
    assert len(_items(db, paid["id"], "discovery")) == 1, "a switched-off user got new work"
    overview_off = client.get("/api/automation", headers=paid["auth"]).json()
    assert overview_off["enabled"] is False and overview_off["active"] is False, overview_off
    # ``next_run`` promises nothing while auto mode is off.
    assert all(value is None for value in overview_off["next_run"].values()), overview_off["next_run"]


# --------------------------------------------------------------------------- #
# 5. The quota
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_quota_exhaustion_stops_the_scheduler_and_notifies_once(client, auth, db):
    from app.core.entitlements import increment_usage, limit_for

    paid = _paid_user(client, db, "quota@example.com", "pro")
    limit = limit_for(db, paid["id"], "automation_runs_per_month")
    assert limit > 0
    increment_usage(db, paid["id"], "automation_runs_per_month", limit)
    _sync(db)

    from app.models.models import Notification

    now = datetime.utcnow().replace(microsecond=0)
    counts = await AutoScheduler().sweep(now=now)
    _sync(db)
    assert counts["enqueued"] == 0, counts
    assert _items(db, paid["id"]) == [], "queued work over the monthly limit"
    rows = _runs(db, paid["id"])
    assert rows and all(row.state == "skipped_quota" for row in rows), [row.state for row in rows]
    assert any("automation_runs_per_month" in (row.reason or "") for row in rows), [row.reason for row in rows]

    notices = db.query(Notification).filter(Notification.user_id == paid["id"],
                                            Notification.kind == QUOTA_NOTIFICATION_KIND).all()
    assert len(notices) == 1, notices
    assert "/billing" == notices[0].link
    assert "limit reached" in notices[0].title.lower()

    # Later windows in the same month: still skipped, still ONE notice.
    await AutoScheduler().sweep(now=now + timedelta(days=1))
    await AutoScheduler().sweep(now=now + timedelta(days=2))
    _sync(db)
    assert len(db.query(Notification).filter(Notification.user_id == paid["id"],
                                            Notification.kind == QUOTA_NOTIFICATION_KIND).all()) == 1
    assert _items(db, paid["id"]) == []
    # ...and the skip rows are deduped per window, not spammed per sweep.
    assert len(_runs(db, paid["id"], DISCOVERY)) == 3, _runs(db, paid["id"], DISCOVERY)


@pytest.mark.asyncio
async def test_finished_auto_run_charges_the_quota_once(client, auth, db, provider_owner):
    """The worker charges the run budget when an auto item *finishes* — once."""
    from app.core.entitlements import entitlements_snapshot

    paid = _paid_user(client, db, "charge@example.com", "pro_plus")
    _sync(db)
    assert entitlements_snapshot(db, paid["id"])["usage"]["automation_runs_per_month"]["used"] == 0

    now = datetime.utcnow().replace(microsecond=0)
    await AutoScheduler().sweep(now=now)
    _sync(db)
    discovery = _items(db, paid["id"], "discovery")[0]
    funding = _items(db, paid["id"], "funding")[0]

    from app.worker import Worker

    worker = Worker(pipelines=["discovery", "funding"])
    await worker._run_item(discovery.id, "discovery")
    _sync(db)
    assert discovery.status == "done", discovery.status
    assert entitlements_snapshot(db, paid["id"])["usage"]["automation_runs_per_month"]["used"] == 1

    # A *manual* run of the same pipeline must not be charged as automation.
    from app.services.job_queue import enqueue

    manual = enqueue(db, user_id=paid["id"], pipeline="discovery", payload={"trigger": "manual"},
                     dedupe_key="manual:charge")
    _sync(db)
    await worker._run_item(manual.id, "discovery")
    _sync(db)
    assert entitlements_snapshot(db, paid["id"])["usage"]["automation_runs_per_month"]["used"] == 1

    # And the history folds the finished item in on the next sweep (no new hook in
    # the queue): queued → done, which is what advances the cadence clock.
    await AutoScheduler().sweep(now=now + timedelta(seconds=40))
    _sync(db)
    rows = _runs(db, paid["id"], DISCOVERY)
    assert [row.state for row in rows] == ["done"], rows  # reconciled in place, not duplicated
    assert rows[0].queue_job_id == discovery.id, rows[0].queue_job_id
    # the funding item is still open (paused or queued) — it was never run
    assert len(_items(db, paid["id"], "funding")) == 1


@pytest.mark.asyncio
async def test_paused_auto_run_charges_nothing(client, auth, db, provider_owner, monkeypatch):
    """A run that never happened is not billable: pause ⇒ no charge."""
    from app.core.entitlements import entitlements_snapshot
    from app.services import handlers

    async def _down(*args, **kwargs):
        from app.services.ai_guardrails import AIUnavailableError

        raise AIUnavailableError("provider_error", workflow="scoring", state="transient_outage")

    monkeypatch.setattr(handlers, "discover_for_user", _down)
    paid = _paid_user(client, db, "pausecharge@example.com", "pro_plus")
    await AutoScheduler().sweep(now=datetime.utcnow().replace(microsecond=0))
    _sync(db)
    item = _items(db, paid["id"], "discovery")[0]

    from app.worker import Worker

    await Worker(pipelines=["discovery"])._run_item(item.id, "discovery")
    _sync(db)
    assert item.status == "paused", item.status
    assert entitlements_snapshot(db, paid["id"])["usage"]["automation_runs_per_month"]["used"] == 0


# --------------------------------------------------------------------------- #
# 6. Application prep
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_application_prep_queues_one_ready_job_and_marks_it(client, auth, db, provider_owner):
    paid = _paid_user(client, db, "appprep@example.com", "pro_plus")
    ready = _job(db, paid["id"], status="ready_to_apply", score=91.0, title="Staff Engineer", company="B")
    _job(db, paid["id"], status="ready_to_apply", score=77.0, title="Lead Engineer", company="C")
    _job(db, paid["id"], status="discovered", score=99.0, title="Not Ready", company="D")

    now = datetime.utcnow().replace(microsecond=0)
    await AutoScheduler().sweep(now=now)
    _sync(db)
    items = _items(db, paid["id"], "application")
    assert len(items) == 1, items
    assert items[0].job_id == ready, "the highest-scored ready job must be first"
    assert items[0].payload["trigger"] == "auto" and items[0].payload["resume_choice"] == "auto"
    assert items[0].dedupe_key == dedupe_key(APPLICATION_PREP, paid["id"], cycle_bucket(now, 86400))
    from app.models.models import Job

    assert db.query(Job).filter(Job.id == ready).first().status == "queued", "job left ready_to_apply"

    # Next window, the *next* job — and never the one already queued.
    _finish_last_run(db, paid["id"], APPLICATION_PREP, now)
    await AutoScheduler().sweep(now=now + timedelta(seconds=86400 + 1))
    _sync(db)
    items = _items(db, paid["id"], "application")
    assert len(items) == 2, items
    assert items[1].job_id != ready


@pytest.mark.asyncio
async def test_application_prep_with_nothing_ready_records_nothing_ready(client, auth, db, provider_owner):
    paid = _paid_user(client, db, "nothingready@example.com", "pro_plus")
    _job(db, paid["id"], status="discovered", score=91.0)
    await AutoScheduler().sweep(now=datetime.utcnow().replace(microsecond=0))
    _sync(db)
    assert _items(db, paid["id"], "application") == []
    rows = _runs(db, paid["id"], APPLICATION_PREP)
    assert len(rows) == 1 and rows[0].state == "done", [row.state for row in rows]
    assert rows[0].reason == "nothing_ready", rows[0].reason
    assert (rows[0].meta or {}).get("skipped") == "nothing_ready", rows[0].meta


# --------------------------------------------------------------------------- #
# 7. Isolation: one bad row, one bad user, one bad sweep
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_corrupt_settings_row_fails_one_workflow_only(client, auth, db, provider_owner):
    """A non-numeric ``freshness_hours`` must burn one workflow, not the sweep."""
    broken = _paid_user(client, db, "broken@example.com", "pro_plus")
    healthy = _paid_user(client, db, "healthy@example.com", "pro_plus")
    from app.models.models import SettingsModel

    db.add(SettingsModel(user_id=broken["id"], category="scraping", key="freshness_hours",
                         value="yesterday"))
    db.commit()
    _sync(db)

    before = _snapshot()
    counts = await AutoScheduler().sweep(now=datetime.utcnow().replace(microsecond=0))
    _sync(db)
    after = _snapshot()

    # The broken user's discovery workflow failed, with the error recorded.
    rows = _runs(db, broken["id"], DISCOVERY)
    assert rows and rows[-1].state == "failed", [row.state for row in rows]
    assert "ValueError" in (rows[-1].reason or ""), rows[-1].reason
    assert _items(db, broken["id"], "discovery") == []
    # ...their other scheduled workflow still ran, and the healthy user is untouched.
    assert len(_items(db, broken["id"], "funding")) == 1
    assert len(_items(db, healthy["id"], "discovery")) == 1
    assert counts["enqueued"] >= 2, counts
    assert _metric(after, "jobhunter_auto_sweep_errors_total", stage="workflow") - _metric(
        before, "jobhunter_auto_sweep_errors_total", stage="workflow") >= 1.0


@pytest.mark.asyncio
async def test_sweep_errors_do_not_end_the_loop(client, monkeypatch):
    """``run()`` outlives any single failing pass — the worker would otherwise
    respawn a task whose only crime was one bad user."""
    calls = {"n": 0}

    async def _boom(self, *, now: Optional[datetime] = None):
        calls["n"] += 1
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(AutoScheduler, "sweep", _boom)
    before = _snapshot()
    scheduler = AutoScheduler(interval_seconds=1.0)
    task = asyncio.create_task(scheduler.run())
    try:
        deadline = asyncio.get_event_loop().time() + 8
        while calls["n"] < 2 and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert calls["n"] >= 2, calls
        assert not task.done(), "a failed sweep ended the loop"
    finally:
        scheduler.stop()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert _metric(_snapshot(), "jobhunter_auto_sweep_errors_total", stage="sweep") - _metric(
        before, "jobhunter_auto_sweep_errors_total", stage="sweep") >= 1.0


@pytest.mark.asyncio
async def test_disabled_scheduler_sweeps_never_start(client, monkeypatch):
    """``AUTO_SCHEDULER_INTERVAL_SECONDS=0`` disables auto mode process-wide."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "auto_scheduler_interval_seconds", 0, raising=False)
    scheduler = AutoScheduler()
    assert scheduler.enabled is False
    done: Dict[str, Any] = {}

    async def _never(self, *, now: Optional[datetime] = None):
        done["called"] = True

    monkeypatch.setattr(AutoScheduler, "sweep", _never)
    await asyncio.wait_for(scheduler.run(), timeout=5)
    assert not done.get("called"), "the loop ran with auto mode disabled"


# --------------------------------------------------------------------------- #
# 8. Observability: GET /api/automation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_automation_endpoint_reports_state_history_and_quota(client, auth, db, provider_owner):
    paid = _paid_user(client, db, "observe@example.com", "pro_plus")
    now = datetime.utcnow().replace(microsecond=0)
    await AutoScheduler().sweep(now=now)
    _sync(db)

    response = client.get("/api/automation", headers=paid["auth"])
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["enabled"] is True and body["can_use"] is True and body["active"] is True, body
    assert body["cadence"] == PRO_PLUS_CADENCE, body["cadence"]
    assert body["consent_ok"] is True and body["schedulable"] is True, body
    assert body["toggles"] == {DISCOVERY: True, FUNDING: True, APPLICATION_PREP: True}, body["toggles"]
    assert body["blocking"] == [], body["blocking"]
    assert body["plan"] == "pro_plus", body["plan"]
    assert body["sweep_interval_seconds"] > 0, body["sweep_interval_seconds"]

    assert set(body["last_run"]) == set(PRO_PLUS_CADENCE), body["last_run"]
    discovery_run = body["last_run"][DISCOVERY]
    assert discovery_run["state"] == "queued" and discovery_run["job_id"] is None, discovery_run
    assert discovery_run["queue_id"] is not None and discovery_run["at"], discovery_run
    assert body["last_run"][APPLICATION_PREP]["state"] == "done", body["last_run"][APPLICATION_PREP]
    # The run is in flight → no next run to promise for it, and the *finished*
    # no-op pass has a real next moment.
    assert body["next_run"][DISCOVERY] is None, body["next_run"]
    assert body["next_run"][FUNDING] is None, body["next_run"]
    assert body["next_run"][APPLICATION_PREP] is not None, body["next_run"]

    assert 1 <= len(body["recent"]) <= 5, body["recent"]
    assert {row["state"] for row in body["recent"]} <= {"queued", "done", "paused", "failed",
                                                        "needs_input", "skipped_outage",
                                                        "skipped_quota", "skipped_no_consent"}, body["recent"]

    assert body["quota"] == {"used": 0, "limit": 200, "remaining": 200}, body["quota"]

    # Timestamps carry an explicit offset: naive UTC strings would be read as
    # browser-local time and every "next run" would be hours out.
    stamp = body["next_run"][APPLICATION_PREP]
    assert stamp and (stamp.endswith("+00:00") or "+00:00" in stamp or "Z" in stamp), stamp

    # Owner-only: no token, no state.
    anonymous = client.get("/api/automation")
    assert anonymous.status_code == 401, anonymous.status_code
    # And a second user's payload is their own — not the paid user's history.
    other = _register(client, "peeker@example.com")
    theirs = client.get("/api/automation", headers=_auth_for(other)).json()
    assert theirs["recent"] == [] and theirs["enabled"] is False, theirs
    assert theirs["can_use"] is False and theirs["cadence"] == {}, theirs
    assert "consent_required" in theirs["blocking"] or "plan_locked" in theirs["blocking"], theirs["blocking"]


@pytest.mark.asyncio
async def test_dashboard_automation_block_stays_additive(client, auth, db, provider_owner):
    """The v2.2 dashboard fields are additive: old keys intact, new ones honest."""
    paid = _paid_user(client, db, "dashblock@example.com", "pro_plus")
    baseline = client.get("/api/dashboard/summary", headers=auth).json()["automation"]
    for key in ("running", "completed", "failed", "needs_input", "queues"):
        assert key in baseline, baseline

    now = datetime.utcnow().replace(microsecond=0)
    await AutoScheduler().sweep(now=now)
    _sync(db)

    summary = client.get("/api/dashboard/summary", headers=paid["auth"]).json()["automation"]
    assert summary["auto_mode"] is True, summary
    assert summary["auto_mode_active"] is True, summary
    # One clock, two surfaces: the dashboard's next run IS the automation
    # endpoint's next run for the workflow that is not in flight (application-prep
    # finished as a no-op; discovery and funding are still queued, so they promise
    # nothing).
    overview = client.get("/api/automation", headers=paid["auth"]).json()
    assert summary["next_run"] == overview["next_run"][APPLICATION_PREP], (summary, overview["next_run"])
    assert summary["next_run"] is not None, summary
    assert overview["next_run"][DISCOVERY] is None and overview["next_run"][FUNDING] is None, overview["next_run"]
    off = client.put("/api/settings", json={"automation": {"auto_mode": False}}, headers=paid["auth"])
    assert off.status_code == 200, off.text
    after = client.get("/api/dashboard/summary", headers=paid["auth"]).json()["automation"]
    assert after["auto_mode"] is False and after["next_run"] is None, after


# --------------------------------------------------------------------------- #
# 9. Notifications: only for auto runs, only when there is something to say
# --------------------------------------------------------------------------- #
def test_high_match_notification_uses_the_threshold_and_notifies_once_per_run(client, auth, db):
    from app.models.models import Notification, PipelineJob

    paid = _paid_user(client, db, "notify@example.com", "pro_plus", auto_mode=False)
    saved = client.put("/api/settings", json={"general": {"generate_min_score": 70}}, headers=paid["auth"])
    assert saved.status_code == 200, saved.text
    _sync(db)

    high = _job(db, paid["id"], status="discovered", score=88.0, title="Senior Backend Engineer", company="FinCo")
    mid = _job(db, paid["id"], status="discovered", score=74.0, title="Platform Engineer", company="Relay")
    low = _job(db, paid["id"], status="discovered", score=8.0, title="Support", company="Noise")
    other = _job(db, paid["id"], status="discovered", score=90.0, title="Staff Engineer", company="Zeta")
    item = PipelineJob(user_id=paid["id"], pipeline="discovery", status="processing",
                       payload={"trigger": "auto"}, dedupe_key=f"notify:{paid['id']}")
    db.add(item)
    db.commit()
    db.refresh(item)

    from app.services import handlers

    result = {"scanned": 4, "fresh": 4, "inserted": 4, "sources": {}, "elapsed_seconds": 0.1,
              "jobs": [{"id": id_, "title": t, "company": c, "source": "lever", "score": s,
                        "location": "Remote", "url": "https://x/1"}
                       for id_, t, c, s in ((other, "Staff Engineer", "Zeta", 90.0),
                                           (high, "Senior Backend Engineer", "FinCo", 88.0),
                                           (mid, "Platform Engineer", "Relay", 74.0),
                                           (low, "Support", "Noise", 8.0))]}
    from app.models.models import User

    handlers._notify_auto_discovery(db, item, db.get(User, paid["id"]), result)
    _sync(db)
    notices = db.query(Notification).filter(Notification.user_id == paid["id"]).all()
    assert len(notices) == 1, [n.title for n in notices]
    assert notices[0].kind == "high_match" and notices[0].link == "/jobs", notices[0].title
    assert "3 new high-match jobs" in notices[0].title, notices[0].title  # 90, 88, 74 ≥ 70
    body = notices[0].body
    assert "Zeta — Staff Engineer" in body and "Noise" not in body, body
    # The scores are keyword estimates in a queued run — say so, don't imply an
    # AI verdict the run never computed.
    assert "keyword" in body, body
    assert {int(i) for i in (notices[0].meta or {}).get("job_ids", [])} == {other, high, mid}
    assert low not in (other, high, mid)

    # Same run, called again (a retry) → still one notification.
    handlers._notify_auto_discovery(db, item, db.get(User, paid["id"]), result)
    _sync(db)
    assert db.query(Notification).filter(Notification.user_id == paid["id"]).count() == 1


def test_no_notification_when_nothing_clears_the_threshold(client, auth, db):
    from app.models.models import Notification, PipelineJob, User

    paid = _paid_user(client, db, "quiet@example.com", "pro_plus", auto_mode=False)
    _job(db, paid["id"], status="discovered", score=12.0, title="Support Rep", company="QuietCo")
    item = PipelineJob(user_id=paid["id"], pipeline="discovery", status="processing",
                       payload={"trigger": "auto"}, dedupe_key=f"quiet:{paid['id']}")
    db.add(item)
    db.commit()
    from app.services import handlers

    handlers._notify_auto_discovery(db, item, db.get(User, paid["id"]),
                                    {"jobs": [{"id": 1, "title": "Support Rep", "company": "QuietCo",
                                               "score": 12.0}]})
    _sync(db)
    assert db.query(Notification).filter(Notification.user_id == paid["id"]).count() == 0


def test_funding_notification_counts_only_new_verified_companies_of_this_run(client, auth, db):
    from datetime import datetime as dt

    from app.models.models import FundingCompany, Notification, PipelineJob, User

    paid = _paid_user(client, db, "fundnotify@example.com", "pro_plus", auto_mode=False)
    started = dt.utcnow() - timedelta(minutes=5)
    db.add(FundingCompany(user_id=paid["id"], name="Freshco", verified=True,
                          discovered_at=started + timedelta(minutes=1), raised_at=started))
    db.add(FundingCompany(user_id=paid["id"], name="Staleco", verified=True,
                          discovered_at=started - timedelta(days=30), raised_at=started - timedelta(days=30)))
    db.add(FundingCompany(user_id=paid["id"], name="Unverifiedco", verified=False,
                          discovered_at=started + timedelta(minutes=1), raised_at=started))
    db.commit()
    item = PipelineJob(user_id=paid["id"], pipeline="funding", status="processing",
                       payload={"trigger": "auto"}, dedupe_key=f"fund:{paid['id']}")
    db.add(item)
    db.commit()
    db.refresh(item)

    from app.services import handlers

    handlers._notify_auto_funding(db, item, db.get(User, paid["id"]), started)
    _sync(db)
    notices = db.query(Notification).filter(Notification.user_id == paid["id"]).all()
    assert len(notices) == 1, notices
    assert notices[0].kind == "funding_match" and notices[0].link == "/funding", notices[0].title
    assert "1 new verified company" in notices[0].title, notices[0].title
    assert "Freshco" in notices[0].body and "Staleco" not in notices[0].body, notices[0].body
    assert (notices[0].meta or {}).get("added") == 1, notices[0].meta


# --------------------------------------------------------------------------- #
# 10. The v2.1 contract bug this release fixes on the way through
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_queued_discovery_run_records_its_persona_signal(client, auth, db, monkeypatch):
    """``handle_discovery`` used to read ``result["job_ids"]`` — a key
    ``discover_for_user`` has never returned — so the only kind of discovery run
    there is never fed the persona memory the interactive paths do."""
    from app.models.models import PipelineJob, User
    from app.services import handlers
    from app.services import persona as persona_service

    tokens = _register(client, "persona-signal@example.com")
    _sync(db)
    user = db.query(User).filter(User.email == "persona-signal@example.com").first()
    track = persona_service.ensure_persona(db, int(user.id), name="Backend track")

    seen: list = []
    monkeypatch.setattr(handlers, "discover_for_user", _fake_discovery)
    monkeypatch.setattr(persona_service, "record_signal",
                        lambda *args, **kwargs: seen.append(args))

    item = PipelineJob(user_id=int(user.id), pipeline="discovery", status="processing",
                       payload={"trigger": "manual"}, dedupe_key="persona-signal")
    db.add(item)
    db.commit()
    db.refresh(item)

    await handlers.handle_discovery(db, item)
    _sync(db)
    assert seen, "a discovery run with jobs must record one persona signal"
    assert seen[0][1] == int(user.id) and seen[0][2] == int(track.id)
    assert seen[0][3] == "job_scored", seen[0]

    # And a manual run stays silent: notifications are auto-mode only.
    from app.models.models import Notification

    assert db.query(Notification).filter(Notification.user_id == int(user.id)).count() == 0


async def _fake_discovery(db, user, **kwargs) -> Dict[str, Any]:
    return {"scanned": 1, "fresh": 1, "inserted": 1, "sources": {}, "elapsed_seconds": 0.0,
            "jobs": [{"id": 1, "title": "Senior Backend Engineer", "company": "FinCo",
                      "source": "lever", "score": 82.0, "location": "Remote",
                      "url": "https://x/1"}]}
