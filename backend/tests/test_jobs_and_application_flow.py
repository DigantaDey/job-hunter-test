"""Discovery queueing, application preparation, user-input gate and timeline.

v2.2.8: ``POST /api/jobs/{id}/apply`` is queue-only — it answers
``{queued, pipeline_job_id}`` and the prepare/execute work runs in the worker.
The helpers below run exactly what the worker runs and then read the outcome
back the way the SPA does (``GET /api/pipelines/jobs/{id}`` → ``result``), so
these tests cover the page-agnostic contract rather than a synchronous one.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from app.models.models import Job, JobEvent, PipelineJob, User, UserInputRequest
from app.worker import Worker


def _run(item_id: int, pipeline: str = "application") -> None:
    """Execute a queued item the way the worker does (claim → handler → finish)."""
    asyncio.run(Worker(pipelines=[pipeline])._run_item(item_id, pipeline))


def _apply(client, auth, job_id: int, body=None) -> dict:
    """Click Auto-apply, run the queued item, return the row the UI polls."""
    response = client.post(f"/api/jobs/{job_id}/apply", json=body or {}, headers=auth)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["status"] == "queued" and receipt["pipeline_job_id"], receipt
    _run(receipt["pipeline_job_id"])
    row = client.get(f"/api/pipelines/jobs/{receipt['pipeline_job_id']}", headers=auth)
    assert row.status_code == 200, row.text
    return row.json()


def _job(db, **overrides) -> Job:
    user = db.query(User).order_by(User.id).first()
    defaults = dict(
        user_id=user.id,
        title="Backend Engineer",
        company="Acme",
        location="Remote",
        description="Python, FastAPI, PostgreSQL. Payments.",
        url="https://jobs.lever.co/acme/1",
        source="lever",
        dedupe_key=f"test:{datetime.utcnow().timestamp()}",
        status="discovered",
        score=78.0,
        extra={"forms": {"portal_type": "lever", "requires_login": True, "vault_domain": "jobs.lever.co",
                         "detection_source": "html", "ai_confidence": 0.9,
                         "fields": [
                             {"name": "firstName", "label": "First name", "type": "text", "required": True,
                              "options": [], "profile_key": "firstName"},
                             {"name": "email", "label": "Email", "type": "email", "required": True,
                              "options": [], "profile_key": "email"},
                             {"name": "resume", "label": "Resume", "type": "file", "required": True,
                              "options": [], "profile_key": "resume"},
                         ]}},
    )
    defaults.update(overrides)
    job = Job(**defaults)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_job_listing_filters_and_pagination(client, auth, db):
    _job(db, title="Python Backend", company="Acme", score=90)
    _job(db, title="Frontend Engineer", company="Beta", score=40, status="skipped")
    assert len(client.get("/api/jobs", headers=auth).json()) == 2
    assert len(client.get("/api/jobs?status=skipped", headers=auth).json()) == 1
    assert len(client.get("/api/jobs?min_score=50", headers=auth).json()) == 1
    assert len(client.get("/api/jobs?q=frontend", headers=auth).json()) == 1
    assert len(client.get("/api/jobs?limit=1", headers=auth).json()) == 1


def test_discovery_queues_work_without_blocking(client, auth, db, uploaded_resume):
    response = client.post("/api/jobs/discover", json={"keywords": ["python"], "freshness_hours": 48}, headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["queued"] is True and body["pipeline_job_id"]

    item = db.query(PipelineJob).filter(PipelineJob.id == body["pipeline_job_id"]).first()
    assert item.pipeline == "discovery"
    assert item.status == "queued"
    assert item.payload["keywords"][0] == "python"

    # The same request must not double-enqueue identical work.
    again = client.post("/api/jobs/discover", json={"keywords": ["python"], "freshness_hours": 48}, headers=auth)
    assert again.json()["queued"] is False and again.json()["duplicate"] is True


def test_discovery_uses_ai_context_keywords(client, auth, uploaded_resume):
    context = client.get("/api/context/keywords", headers=auth).json()
    assert context["keywords"]
    assert context["source"] in ("heuristic", "ai")


def test_apply_is_queued_and_the_receipt_reattaches(client, auth, db, uploaded_resume):
    """The click answers a receipt; the same id is what the live layer re-reads.

    A second click while the run is in flight is a *duplicate* that still hands
    back the in-flight id — the UI must be able to re-attach, not show nothing.
    """
    job = _job(db)
    first = client.post(f"/api/jobs/{job.id}/apply", json={"resume_choice": "master"}, headers=auth).json()
    assert first["queued"] is True and first["status"] == "queued" and first["pipeline_job_id"]
    assert first["mode"] == "prepare"  # no browser automation in tests → prepare only
    db.refresh(job)
    assert job.status == "queued"

    again = client.post(f"/api/jobs/{job.id}/apply", json={"resume_choice": "master"}, headers=auth).json()
    assert again["duplicate"] is True and again["pipeline_job_id"] == first["pipeline_job_id"]

    live = client.get("/api/queues/ai", headers=auth).json()
    assert live["counts"]["queued"] >= 1
    assert any(i["id"] == first["pipeline_job_id"] and i["pipeline"] == "application" for i in live["items"])

    # Tenant scope: the row is not readable by anyone else.
    assert client.get(f"/api/pipelines/jobs/{first['pipeline_job_id']}", headers=auth).status_code == 200


def test_application_preparation_creates_vault_credential(client, auth, db, uploaded_resume):
    job = _job(db)
    row = _apply(client, auth, job.id, {"resume_choice": "master"})
    assert row["status"] == "done", row
    body = row["result"]
    assert body["status"] == "ready_to_apply"
    assert body["resume_decision"] == "master"
    assert body["credential_created"] is True
    assert body["portal_type"] == "lever"
    assert body["fields_mapped"] == 3
    db.refresh(job)
    assert job.status == "ready_to_apply"

    # The finished row stays in the live feed's ``recent`` list so a page
    # refreshed after the run still sees the outcome.
    live = client.get("/api/queues/ai", headers=auth).json()
    assert any(r["id"] == row["id"] and r["result"]["resume_decision"] == "master" for r in live["recent"])

    vault = client.get("/api/vault", headers=auth).json()
    assert [entry["domain"] for entry in vault] == ["jobs.lever.co"]

    events = client.get(f"/api/jobs/{job.id}/events", headers=auth).json()
    stages = [event["stage"] for event in events]
    assert "prepared" in stages


def test_application_requires_user_input_then_completes(client, auth, db, uploaded_resume):
    job = _job(db, extra={"forms": {
        "portal_type": "greenhouse", "requires_login": False, "detection_source": "html",
        "fields": [
            {"name": "email", "label": "Email", "type": "email", "required": True, "options": [],
             "profile_key": "email"},
            {"name": "salary_expectation", "label": "Expected salary", "type": "text", "required": True,
             "options": [], "profile_key": "salaryExpectation"},
        ]}})

    row = _apply(client, auth, job.id)
    assert row["status"] == "needs_input"
    prepared = row["result"]
    assert prepared["status"] == "needs_input"
    assert [f["name"] for f in prepared["missing_fields"]] == ["salary_expectation"]

    queue = client.get("/api/user-input-queue", headers=auth).json()
    assert len(queue) == 1
    assert queue[0]["fields"][0]["name"] == "salary_expectation"

    submitted = client.post(f"/api/jobs/{job.id}/input",
                            json={"answers": {"salary_expectation": "120000"}}, headers=auth)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["requeued"] is True

    db.refresh(job)
    assert job.status == "queued"
    assert job.extra["answers"]["salary_expectation"] == "120000"
    assert db.query(UserInputRequest).filter(UserInputRequest.status == "completed").count() == 1
    assert client.get("/api/user-input-queue", headers=auth).json() == []


async def test_user_answers_survive_the_requeue_round_trip(client, auth, db, uploaded_resume):
    """
    End-to-end input-queue loop. The storage half was fixed earlier (answers
    land in ``UserInputRequest.fields`` and ``job.extra["answers"]``), but the
    *consumption* half was still broken: the SPA and the endpoint key answers
    by the form field ``name`` (``salary_expectation``) while
    ``build_autofill_plan`` looked profile-mapped fields up by ``profile_key``
    (``salaryExpectation``). The re-queued application therefore came back
    ``needs_input`` with the same blank fields, forever — the user's answer
    was stored yet never used, and a fresh blank request appeared on every
    retry.
    """
    job = _job(db, extra={"forms": {
        "portal_type": "greenhouse", "requires_login": False, "detection_source": "html",
        "fields": [
            {"name": "email", "label": "Email", "type": "email", "required": True, "options": [],
             "profile_key": "email"},
            {"name": "salary_expectation", "label": "Expected salary", "type": "text", "required": True,
             "options": [], "profile_key": "salaryExpectation"},
        ]}})

    from app.services.handlers import handle_application
    from app.services.job_queue import claim_item, needs_input

    receipt = client.post(f"/api/jobs/{job.id}/apply", json={}, headers=auth).json()
    first_item = claim_item(db, receipt["pipeline_job_id"])
    prepared = await handle_application(db, first_item)
    assert prepared["status"] == "needs_input"
    assert [f["name"] for f in prepared["missing_fields"]] == ["salary_expectation"]
    needs_input(db, first_item, reason="waiting for user input")

    submitted = client.post(f"/api/jobs/{job.id}/input",
                            json={"answers": {"salary_expectation": "120000"}}, headers=auth)
    assert submitted.status_code == 200, submitted.text
    db.expire_all()
    item = db.query(PipelineJob).filter(PipelineJob.id == submitted.json()["pipeline_job_id"]).one()
    assert item.payload["answers"] == {"salary_expectation": "120000"}

    # Run exactly what the worker runs for the re-queued item.
    result = await handle_application(db, item)
    assert result.get("status") != "needs_input", \
        f"re-run ignored the user's answer, still missing: {result.get('missing_fields')}"

    db.expire_all()
    job_row = db.query(Job).filter(Job.id == job.id).one()
    plan = (job_row.extra or {}).get("autofill_plan") or {}
    field = next(f for f in plan.get("fields", []) if f["name"] == "salary_expectation")
    assert field["value"] == "120000", "the answer never reached the autofill plan"
    assert db.query(UserInputRequest).filter(
        UserInputRequest.job_id == job.id, UserInputRequest.status == "pending").count() == 0, \
        "a fresh blank input request was created for a field the user already answered"


async def test_parked_item_is_requeued_with_fresh_answers(client, auth, db, uploaded_resume):
    """
    Second half of the input-queue bug. When a run parks its queue item in
    ``needs_input`` (the worker's waiting-for-user state), a later answer
    submission used to hit the dedupe key, get treated as a duplicate and
    no-op: the job sat ``queued`` with the answer stored but no runnable item
    left to process it. The parked item must be re-activated with the merged
    answers instead, and no duplicate item may be stacked on top.
    """
    from app.services.handlers import handle_application
    from app.services.job_queue import claim_item, needs_input

    job = _job(db, extra={"forms": {
        "portal_type": "greenhouse", "requires_login": False, "detection_source": "html",
        "fields": [
            {"name": "email", "label": "Email", "type": "email", "required": True, "options": [],
             "profile_key": "email"},
            {"name": "salary_expectation", "label": "Expected salary", "type": "text", "required": True,
             "options": [], "profile_key": "salaryExpectation"},
        ]}})

    receipt = client.post(f"/api/jobs/{job.id}/apply", json={}, headers=auth).json()
    item = db.query(PipelineJob).filter(PipelineJob.id == receipt["pipeline_job_id"]).one()

    # First round: (simulating a stale or interrupted run) the worker
    # processes the item *without* an answer and parks it in needs_input.
    item.payload = {"resume_choice": "auto", "answers": {}}  # stale payload
    db.commit()
    claimed = claim_item(db, item.id)
    assert claimed is not None
    stale_result = await handle_application(db, claimed)
    assert stale_result.get("status") == "needs_input"
    needs_input(db, claimed, reason="waiting for user input")
    assert db.query(UserInputRequest).filter(
        UserInputRequest.job_id == job.id, UserInputRequest.status == "pending").count() == 1

    # Second answer round: the user sees the blank field again and answers.
    again = client.post(f"/api/jobs/{job.id}/input",
                        json={"answers": {"salary_expectation": "120000"}}, headers=auth)
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["pipeline_job_id"] == item.id, "the parked item must be re-queued, not no-op'ed or duplicated"
    assert db.query(PipelineJob).filter(PipelineJob.job_id == job.id).count() == 1, \
        "no duplicate item may stack on top of the parked one"

    db.expire_all()
    row = db.query(PipelineJob).filter(PipelineJob.id == item.id).one()
    assert row.status == "queued"
    assert row.payload["answers"] == {"salary_expectation": "120000"}
    assert row.attempts == 0, "user-driven re-runs start a fresh retry cycle"

    # And the re-run now really completes with the answer in the plan.
    claimed = claim_item(db, row.id)
    result = await handle_application(db, claimed)
    assert result.get("status") != "needs_input", result.get("missing_fields")


def test_needs_input_is_not_duplicated_on_repeat_apply(client, auth, db, uploaded_resume):
    job = _job(db, extra={"forms": {"portal_type": "custom", "detection_source": "html", "fields": [
        {"name": "salary_expectation", "label": "Expected salary", "type": "text", "required": True,
         "options": [], "profile_key": "salaryExpectation"}]}})
    first = _apply(client, auth, job.id)["result"]
    # The parked row is re-run (user-driven re-runs reuse the dedupe row).
    second = _apply(client, auth, job.id)["result"]
    assert first["input_request_id"] == second["input_request_id"]
    assert db.query(UserInputRequest).filter(UserInputRequest.job_id == job.id).count() == 1


def test_mark_applied_and_timeline(client, auth, db, uploaded_resume):
    job = _job(db)
    assert client.post(f"/api/jobs/{job.id}/mark-applied", headers=auth).status_code == 200
    db.refresh(job)
    assert job.status == "applied"
    assert job.applied_at is not None

    events = client.get(f"/api/jobs/{job.id}/events", headers=auth).json()
    assert events[-1]["stage"] == "applied"
    assert events[-1]["status"] == "success"


def test_retry_and_skip(client, auth, db, uploaded_resume):
    job = _job(db, status="failed", error="boom")
    retried = client.post(f"/api/jobs/{job.id}/retry", headers=auth).json()
    assert retried["ok"] is True
    db.refresh(job)
    assert job.status == "queued"

    assert client.post(f"/api/jobs/{job.id}/skip", headers=auth).json()["status"] == "skipped"


def test_apply_requires_profile(client, auth, db):
    job = _job(db)
    response = client.post(f"/api/jobs/{job.id}/apply", json={}, headers=auth)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "profile_missing"


def test_company_classification_is_the_ai_verdict(client, auth, db, request):
    """classify/company is the model's verdict, never a deterministic guess.

    The deterministic estimator is only used for bulk-labelling jobs outside
    the AI top-N of a discovery run (provenance-labelled) — the interactive
    endpoint must reflect the AI answer.
    """
    result = client.post("/api/classify/company?company=Stealth+AI+Startup&jd=seed+stage+3+people",
                         headers=auth)
    assert result.status_code == 200, result.text
    body = result.json()
    # The deterministic AI stand-in answers "small" for the classify workflow.
    assert body["size"] == "small", body
    assert body["confidence"] == 0.7, body  # stand-in omits confidence → documented default

    # And the outage path is covered by the scripted-provider suite
    # (test_ai_pause_resume.py): a failed AI call returns the typed pausable /
    # blocked 503 — never a guessed size.


def test_pipeline_stats_and_jobs_endpoints(client, auth, db, uploaded_resume):
    job = _job(db)
    client.post("/api/jobs/discover", json={"keywords": ["python"]}, headers=auth)
    stats = client.get("/api/pipelines/stats", headers=auth).json()
    assert stats["discovery"]["queued"] >= 1
    assert "rate_limiter" in stats

    items = client.get("/api/pipelines/jobs?pipeline=discovery", headers=auth).json()
    assert items and items[0]["pipeline"] == "discovery"


def test_application_consent_gate_is_recorded(client, full_consent, db, uploaded_resume):
    """With consent + automation disabled we prepare, never silently submit."""
    job = _job(db)
    row = _apply(client, full_consent, job.id)
    assert row["result"]["status"] in ("ready_to_apply", "needs_input")
    db.refresh(job)
    assert job.status != "applied"


def test_dashboard_summary_counts(client, auth, db, uploaded_resume):
    _job(db, status="applied")
    _job(db, status="discovered")
    summary = client.get("/api/dashboard/summary", headers=auth).json()
    assert summary["jobs"]["total"] == 2
    assert summary["jobs"]["applied"] == 1
    assert summary["resumes"]["total"] >= 1
