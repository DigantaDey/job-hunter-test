"""
v2.2.8 — the page-agnostic live layer.

The bug: "Jobs → Auto-apply: *Status: preparing • resume: master* vanishes on
navigation/F5". The status lived in one component's ``useState``; nothing on
the server could be re-read. The fix has two halves and this file pins the
server half:

* **every AI trigger enqueues** and answers ``{queued, pipeline_job_id}`` —
  apply, discover, resume generate, email draft, funding refresh — including
  on the *duplicate* path (the id of the run already in flight), so the SPA
  can re-attach instead of showing nothing;
* **``GET /api/queues/ai``** is the one read the SPA polls on every route: the
  tenant's in-flight rows, the last half hour of outcomes (with each
  handler's ``result``), the chip counts and ``processing_now``; and
  **``GET /api/pipelines/jobs/{id}``** is how a page re-attaches to the id it
  stored in ``sessionStorage`` before the refresh.
"""
from __future__ import annotations

from datetime import datetime

from app.models.models import Job, PipelineJob, User
from app.services.job_queue import claim_item, complete, enqueue, needs_input
from tests.conftest import queued_result, run_queue_item


def _job(db, user_id: int, **overrides) -> Job:
    defaults = dict(
        user_id=user_id, title="Backend Engineer", company="Acme", location="Remote",
        description="Python, FastAPI, PostgreSQL. Payments.", url="https://jobs.lever.co/acme/9",
        source="lever", dedupe_key=f"live:{datetime.utcnow().timestamp()}", status="discovered", score=80.0,
        extra={"forms": {"portal_type": "lever", "requires_login": False, "detection_source": "html",
                         "fields": [{"name": "email", "label": "Email", "type": "email", "required": True,
                                     "options": [], "profile_key": "email"}]}},
    )
    defaults.update(overrides)
    job = Job(**defaults)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _owner(db) -> User:
    return db.query(User).order_by(User.id).first()


# --------------------------------------------------------------------------- #
# The live read
# --------------------------------------------------------------------------- #
def test_live_view_is_tenant_scoped(client, auth, member_auth, db, owner, member):
    owner_row = _owner(db)
    enqueue(db, user_id=owner_row.id, pipeline="discovery", payload={"keywords": ["x"]}, dedupe_key="live:owner")

    mine = client.get("/api/queues/ai", headers=auth).json()
    assert mine["counts"]["queued"] == 1
    assert [i["pipeline"] for i in mine["items"]] == ["discovery"]
    assert mine["processing_now"] is None

    theirs = client.get("/api/queues/ai", headers=member_auth).json()
    assert theirs["counts"] == {"queued": 0, "processing": 0, "paused": 0, "needs_input": 0}
    assert theirs["items"] == [] and theirs["recent"] == []

    # The single-row read is scoped the same way: another tenant gets a 404, never the row.
    item_id = mine["items"][0]["id"]
    assert client.get(f"/api/pipelines/jobs/{item_id}", headers=auth).status_code == 200
    assert client.get(f"/api/pipelines/jobs/{item_id}", headers=member_auth).status_code == 404


def test_live_view_reports_processing_now_and_recent_outcomes(client, auth, db, owner):
    user = _owner(db)
    working = enqueue(db, user_id=user.id, pipeline="funding", payload={}, dedupe_key="live:proc")
    claim_item(db, working.id)
    finished = enqueue(db, user_id=user.id, pipeline="discovery", payload={}, dedupe_key="live:done")
    claim_item(db, finished.id)
    complete(db, finished, result={"added": 3})

    live = client.get("/api/queues/ai", headers=auth).json()
    assert live["counts"]["processing"] == 1
    assert live["processing_now"]["id"] == working.id and live["processing_now"]["pipeline"] == "funding"
    # Finished rows leave ``items`` but stay in ``recent`` with their result —
    # a page refreshed after the run still sees *what happened*.
    assert [i["id"] for i in live["items"]] == [working.id]
    assert live["recent"][0]["id"] == finished.id
    assert live["recent"][0]["status"] == "done"
    assert live["recent"][0]["result"] == {"added": 3}


def test_pipeline_jobs_accepts_a_comma_status_list(client, auth, db, owner):
    user = _owner(db)
    a = enqueue(db, user_id=user.id, pipeline="ai", payload={"task": "t"}, dedupe_key="live:a")
    b = enqueue(db, user_id=user.id, pipeline="ai", payload={"task": "t"}, dedupe_key="live:b")
    claim_item(db, b.id)
    c = enqueue(db, user_id=user.id, pipeline="ai", payload={"task": "t"}, dedupe_key="live:c")
    claim_item(db, c.id)
    complete(db, c)

    rows = client.get("/api/pipelines/jobs?status=queued,processing", headers=auth).json()
    assert {r["id"] for r in rows} == {a.id, b.id}
    assert all("result" not in r for r in rows), "the list view stays light"
    only_done = client.get("/api/pipelines/jobs?status=done", headers=auth).json()
    assert [r["id"] for r in only_done] == [c.id]


def test_needs_input_row_carries_the_missing_fields(client, auth, db, owner, uploaded_resume):
    """The parked row must say *which* fields are missing, not just "needs input"."""
    user = _owner(db)
    job = _job(db, user.id, extra={"forms": {
        "portal_type": "greenhouse", "requires_login": False, "detection_source": "html",
        "fields": [{"name": "visa", "label": "Visa status", "type": "text", "required": True,
                    "options": [], "profile_key": None}]}})
    receipt = client.post(f"/api/jobs/{job.id}/apply", json={}, headers=auth).json()
    row = queued_result(client, auth, receipt, "application")
    assert row["status"] == "needs_input"
    assert [f["name"] for f in row["result"]["missing_fields"]] == ["visa"]
    assert row["result"]["input_request_id"]
    assert row["job_id"] == job.id

    live = client.get("/api/queues/ai", headers=auth).json()
    assert live["counts"]["needs_input"] == 1
    assert live["items"][0]["result"]["missing_fields"][0]["name"] == "visa"


# --------------------------------------------------------------------------- #
# Every trigger answers {queued, pipeline_job_id} — duplicates included
# --------------------------------------------------------------------------- #
def test_discover_duplicate_still_returns_the_in_flight_id(client, auth, uploaded_resume):
    first = client.post("/api/jobs/discover", json={"keywords": ["python"], "freshness_hours": 48}, headers=auth).json()
    assert first["queued"] is True and first["pipeline_job_id"]
    again = client.post("/api/jobs/discover", json={"keywords": ["python"], "freshness_hours": 48}, headers=auth).json()
    assert again["queued"] is False and again["duplicate"] is True
    assert again["pipeline_job_id"] == first["pipeline_job_id"], "the SPA re-attaches to the run in flight"


def test_funding_refresh_duplicate_still_returns_the_in_flight_id(client, auth, uploaded_resume):
    first = client.post("/api/funding/refresh", json={}, headers=auth).json()
    assert first["queued"] is True and first["pipeline_job_id"]
    again = client.post("/api/funding/refresh", json={}, headers=auth).json()
    assert again["duplicate"] is True and again["pipeline_job_id"] == first["pipeline_job_id"]


def test_generate_resume_is_queued_and_the_row_carries_the_files(client, auth, db, owner, uploaded_resume):
    user = _owner(db)
    job = _job(db, user.id)
    receipt = client.post("/api/resumes/generate", params={"job_id": job.id}, headers=auth).json()
    assert receipt["status"] == "queued" and receipt["queued"] is True and receipt["pipeline_job_id"]

    again = client.post("/api/resumes/generate", params={"job_id": job.id}, headers=auth).json()
    assert again["duplicate"] is True and again["pipeline_job_id"] == receipt["pipeline_job_id"]

    # Cheap preconditions still fail fast at the click.
    bare = _job(db, user.id, description="", dedupe_key="live:nojd")
    assert client.post("/api/resumes/generate", params={"job_id": bare.id}, headers=auth).status_code == 422

    row = queued_result(client, auth, receipt, "ai")
    assert row["status"] == "done" and row["task"] == "generate_resume" and row["job_id"] == job.id
    result = row["result"]
    assert result["status"] == "generated" and result["resume_id"]
    assert result["fact_guard"]["passed"] is True
    assert result["files"]["pdf"].endswith("format=pdf")
    # A rerun after completion reuses the dedupe row (user-driven re-run), never 500s.
    rerun = client.post("/api/resumes/generate", params={"job_id": job.id}, headers=auth).json()
    assert rerun["queued"] is True and rerun["pipeline_job_id"] == receipt["pipeline_job_id"]


def test_email_draft_is_queued_and_the_result_names_the_email(client, auth, db, owner, uploaded_resume):
    user = _owner(db)
    job = _job(db, user.id)
    receipt = client.post("/api/emails/generate", json={"company": "Acme", "job_id": job.id}, headers=auth).json()
    assert receipt["status"] == "queued" and receipt["pipeline_job_id"]
    row = queued_result(client, auth, receipt, "email")
    assert row["status"] == "done"
    assert row["result"]["status"] == "pending_approval"
    assert row["result"]["email_id"] and row["result"]["job_id"] == job.id
    card = client.get(f"/api/emails/{row['result']['email_id']}", headers=auth).json()
    assert card["status"] == "pending_approval" and card["job"]["id"] == job.id


def test_email_draft_without_a_profile_fails_fast(client, auth, db, owner):
    denied = client.post("/api/emails/generate", json={"company": "Acme"}, headers=auth)
    assert denied.status_code == 400
    assert denied.json()["detail"]["code"] == "profile_missing"
    assert db.query(PipelineJob).filter(PipelineJob.pipeline == "email").count() == 0


def test_apply_prepare_mode_never_marks_the_job_failed_without_a_browser(client, auth, db, owner, uploaded_resume):
    """Queueing the whole flow must not turn "no Playwright" into a failed job."""
    user = _owner(db)
    job = _job(db, user.id)
    receipt = client.post(f"/api/jobs/{job.id}/apply", json={"resume_choice": "master"}, headers=auth).json()
    assert receipt["mode"] == "prepare"
    run_queue_item(receipt["pipeline_job_id"], "application")
    db.expire_all()
    fresh = db.query(Job).filter(Job.id == job.id).one()
    assert fresh.status == "preparing", fresh.status
    assert not fresh.error


def test_needs_input_helper_stores_the_result(db, owner):
    user = _owner(db)
    item = enqueue(db, user_id=user.id, pipeline="application", payload={}, dedupe_key="live:ni")
    claim_item(db, item.id)
    needs_input(db, item, reason="waiting", result={"missing_fields": [{"name": "x"}]})
    db.expire_all()
    row = db.query(PipelineJob).filter(PipelineJob.id == item.id).one()
    assert row.status == "needs_input"
    assert row.payload["result"]["missing_fields"] == [{"name": "x"}]
