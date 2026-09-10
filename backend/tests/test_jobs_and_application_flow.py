"""Discovery queueing, application preparation, user-input gate and timeline."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.models.models import Job, JobEvent, PipelineJob, User, UserInputRequest


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


def test_application_preparation_creates_vault_credential(client, auth, db, uploaded_resume):
    job = _job(db)
    response = client.post(f"/api/jobs/{job.id}/apply", json={"resume_choice": "master"}, headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] in ("preparing", "queued")
    assert body["resume_decision"] == "master"
    assert body["credential_created"] is True
    assert body["portal_type"] == "lever"
    assert body["fields_mapped"] == 3

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

    prepared = client.post(f"/api/jobs/{job.id}/apply", json={}, headers=auth).json()
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


def test_needs_input_is_not_duplicated_on_repeat_apply(client, auth, db, uploaded_resume):
    job = _job(db, extra={"forms": {"portal_type": "custom", "detection_source": "html", "fields": [
        {"name": "salary_expectation", "label": "Expected salary", "type": "text", "required": True,
         "options": [], "profile_key": "salaryExpectation"}]}})
    first = client.post(f"/api/jobs/{job.id}/apply", json={}, headers=auth).json()
    second = client.post(f"/api/jobs/{job.id}/apply", json={}, headers=auth).json()
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


def test_company_classification_heuristics(client, auth, db):
    small = client.post("/api/classify/company?company=Stealth+AI+Startup&jd=seed+stage+3+people",
                        headers=auth).json()
    assert small["size"] in ("startup", "small")
    big = client.post("/api/classify/company?company=BigCorp&jd=Fortune+500+enterprise+public+company+10000%2B+employees",
                      headers=auth).json()
    assert big["size"] == "big"


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
    response = client.post(f"/api/jobs/{job.id}/apply", json={}, headers=full_consent)
    assert response.status_code == 200
    assert response.json()["status"] in ("preparing", "needs_input", "queued")
    db.refresh(job)
    assert job.status != "applied"


def test_dashboard_summary_counts(client, auth, db, uploaded_resume):
    _job(db, status="applied")
    _job(db, status="discovered")
    summary = client.get("/api/dashboard/summary", headers=auth).json()
    assert summary["jobs"]["total"] == 2
    assert summary["jobs"]["applied"] == 1
    assert summary["resumes"]["total"] >= 1
