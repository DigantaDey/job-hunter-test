"""Auto-apply Execute phase: consent-gated submission, mode preservation, batch-safe discovery.

The flagship flow is Prepare → Review → Confirm → Execute. The first three
phases were covered; this suite covers the last one end-to-end with a stub AI
and a mocked Playwright layer, proving:

* (a) a confirmed application with the per-user ``allow_auto_submit`` consent
  and ``AUTOFILL_ALLOW_SUBMIT=True`` reaches ``execute_autofill`` with
  ``dry_run=False`` and actually submits (job → ``applied``);
* (b) without live consent the same queued execute intent stays a dry run
  (job → ``ready_to_apply``, no submit click) — the consent chain, not the
  queue intent, gates submission;
* (c) an execute-mode job that parks in ``needs_input`` and is re-queued
  after an input answer resumes as *execute* (and submits);
* (d) a discovery batch with one failing insert still persists the good jobs
  and records the failure per row.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import types
from datetime import datetime
from typing import Any, Dict

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.models import Job, PipelineJob, Profile, User, UserInputRequest
from app.worker import Worker


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
@pytest.fixture()
def fake_playwright(monkeypatch):
    """Deterministic stand-in for the Playwright browser layer.

    ``execute_autofill`` imports ``playwright.async_api`` lazily, and
    ``autofill_available`` gates on ``import playwright`` — so a fake module
    pair in ``sys.modules`` is the entire surface to mock. The fixture also
    flips the *operator-side* automation flags (enabled, non-dry-run,
    allow-submit): the per-user consent is what the individual tests vary.
    """
    from app.core.config import settings

    class FakeLocator:
        def __init__(self, page, selector):
            self._page = page
            self._selector = selector
            self.first = self  # ``page.locator(sel).first`` is the locator

        async def count(self):
            return 1

        async def fill(self, value, **kwargs):
            self._page.filled[self._selector] = str(value)

        async def set_input_files(self, value, **kwargs):
            self._page.filled[self._selector] = str(value)

        async def select_option(self, *args, **kwargs):
            self._page.filled[self._selector] = str(args[0])

        async def check(self):
            self._page.filled[self._selector] = "True"

    class FakePage:
        def __init__(self):
            self.goto_urls = []
            self.filled = {}
            self.clicks = []
            self.screenshot_path = None

        def set_default_timeout(self, timeout):
            pass

        async def goto(self, url, **kwargs):
            self.goto_urls.append(url)

        def locator(self, selector):
            return FakeLocator(self, selector)

        async def fill(self, selector, value, **kwargs):
            self.filled[selector] = str(value)

        async def click(self, selector, **kwargs):
            self.clicks.append(selector)

        async def wait_for_load_state(self, *args, **kwargs):
            pass

        async def screenshot(self, path=None, **kwargs):
            self.screenshot_path = path

    page = FakePage()

    class FakeContext:
        async def new_page(self):
            return page

        async def close(self):
            pass

    class FakeBrowser:
        async def new_context(self, **kwargs):
            return FakeContext()

        async def close(self):
            pass

    class FakeChromium:
        async def launch(self, **kwargs):
            return FakeBrowser()

    class FakePlaywrightCM:
        def __init__(self):
            self.chromium = FakeChromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async_api = types.ModuleType("playwright.async_api")
    async_api.async_playwright = FakePlaywrightCM
    pw = types.ModuleType("playwright")
    pw.async_api = async_api
    monkeypatch.setitem(sys.modules, "playwright", pw)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)
    monkeypatch.setattr(settings, "autofill_enabled", True)
    monkeypatch.setattr(settings, "autofill_dry_run", False)
    monkeypatch.setattr(settings, "autofill_allow_submit", True)
    return page


def _run(item_id: int, pipeline: str = "application") -> None:
    """Execute a queued item exactly the way the worker does."""
    asyncio.run(Worker(pipelines=[pipeline])._run_item(item_id, pipeline))


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


def _row(client, auth, item_id) -> Dict[str, Any]:
    response = client.get(f"/api/pipelines/jobs/{item_id}", headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def _set_allow_auto_submit(client, auth, value: bool) -> None:
    response = client.put("/api/settings", json={"application": {"allow_auto_submit": value}}, headers=auth)
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# (a) confirmed + consented + AUTOFILL_ALLOW_SUBMIT → real submission
# --------------------------------------------------------------------------- #
def test_confirmed_application_submits_with_consent(client, full_consent, db, uploaded_resume, fake_playwright):
    """The whole chain green: the browser run is *not* a dry run and submits."""
    _set_allow_auto_submit(client, full_consent, True)
    job = _job(db)

    receipt = client.post(f"/api/jobs/{job.id}/apply", json={"resume_choice": "master"}, headers=full_consent).json()
    assert receipt["queued"] is True
    assert receipt["mode"] == "execute", "consent + AUTOFILL flags must queue an execute run"

    _run(receipt["pipeline_job_id"])
    row = _row(client, full_consent, receipt["pipeline_job_id"])
    assert row["status"] == "done", row

    result = row["result"]
    autofill = result["autofill"]
    assert autofill["status"] == "submitted", autofill
    assert autofill["submitted"] is True
    assert autofill["dry_run"] is False, "the consent chain must have lifted the dry run"
    assert autofill["filled"] == 3
    assert result["status"] == "applied"

    db.expire_all()
    job_row = db.query(Job).filter(Job.id == job.id).one()
    assert job_row.status == "applied"
    assert job_row.applied_at is not None
    assert job_row.error == ""

    # The browser really filled the form and clicked submit.
    assert fake_playwright.goto_urls == ["https://jobs.lever.co/acme/1"]
    assert fake_playwright.filled.get('[name="firstName"]') == "Test"
    assert fake_playwright.filled.get('[name="email"]') == "test.candidate@example.com"
    assert fake_playwright.filled.get('[name="resume"]'), "the resume file must be attached"
    assert "button[type=submit]" in fake_playwright.clicks

    events = client.get(f"/api/jobs/{job.id}/events", headers=full_consent).json()
    assert events[-1]["stage"] == "applied"


# --------------------------------------------------------------------------- #
# (b) no live consent → the queued execute intent stays a dry run
# --------------------------------------------------------------------------- #
def test_execute_intent_without_consent_stays_dry_run(client, full_consent, db, uploaded_resume, fake_playwright):
    _job_no_consent = _job(db)
    # Without the per-user consent the click itself only queues a prepare run.
    receipt = client.post(f"/api/jobs/{_job_no_consent.id}/apply", json={"resume_choice": "master"},
                          headers=full_consent).json()
    assert receipt["mode"] == "prepare"
    _run(receipt["pipeline_job_id"])
    row = _row(client, full_consent, receipt["pipeline_job_id"])
    assert row["result"]["status"] == "preparing"
    db.expire_all()
    assert db.query(Job).filter(Job.id == _job_no_consent.id).one().status == "preparing"
    assert "button[type=submit]" not in fake_playwright.clicks

    # Now the user consents and clicks (mode=execute), then revokes the
    # consent before the worker runs. The queued intent alone must never
    # submit — the consent chain is re-checked at execution time. A
    # login-free form so a click on the fake page can only mean "submitted
    # the application" (a dry run with vault login would also click the
    # *login form's* submit button, which is how the candidate gets in).
    _set_allow_auto_submit(client, full_consent, True)
    job = _job(db, extra={"forms": {
        "portal_type": "greenhouse", "requires_login": False, "detection_source": "html",
        "fields": [
            {"name": "firstName", "label": "First name", "type": "text", "required": True,
             "options": [], "profile_key": "firstName"},
            {"name": "email", "label": "Email", "type": "email", "required": True,
             "options": [], "profile_key": "email"},
            {"name": "phone", "label": "Phone", "type": "tel", "required": True,
             "options": [], "profile_key": "phone"},
        ]}})
    receipt = client.post(f"/api/jobs/{job.id}/apply", json={"resume_choice": "master"},
                          headers=full_consent).json()
    assert receipt["mode"] == "execute"
    _set_allow_auto_submit(client, full_consent, False)

    _run(receipt["pipeline_job_id"])
    row = _row(client, full_consent, receipt["pipeline_job_id"])
    result = row["result"]
    assert result["status"] == "ready_to_apply", result
    assert result["autofill"]["status"] == "dry_run"
    assert result["autofill"]["dry_run"] is True
    assert result["autofill"]["submitted"] is False
    assert result["autofill"]["filled"] == 3, "the form was still filled — only the submit is withheld"

    db.expire_all()
    job_row = db.query(Job).filter(Job.id == job.id).one()
    assert job_row.status == "ready_to_apply"
    assert fake_playwright.clicks == [], "no submit click may happen without live consent"
    assert fake_playwright.goto_urls == [job.url]


# --------------------------------------------------------------------------- #
# (c) an execute job re-queued after an input answer resumes as execute
# --------------------------------------------------------------------------- #
def test_execute_mode_survives_input_requeue_and_submits(client, full_consent, db, uploaded_resume, fake_playwright):
    _set_allow_auto_submit(client, full_consent, True)
    job = _job(db, extra={"forms": {
        "portal_type": "greenhouse", "requires_login": False, "detection_source": "html",
        "fields": [
            {"name": "email", "label": "Email", "type": "email", "required": True,
             "options": [], "profile_key": "email"},
            {"name": "salary_expectation", "label": "Expected salary", "type": "text", "required": True,
             "options": [], "profile_key": "salaryExpectation"},
        ]}})

    receipt = client.post(f"/api/jobs/{job.id}/apply", json={"resume_choice": "master"},
                          headers=full_consent).json()
    assert receipt["mode"] == "execute"

    # First round parks in needs_input (salary is unknown).
    _run(receipt["pipeline_job_id"])
    row = _row(client, full_consent, receipt["pipeline_job_id"])
    assert row["status"] == "needs_input", row
    assert [f["name"] for f in row["result"]["missing_fields"]] == ["salary_expectation"]

    submitted = client.post(f"/api/jobs/{job.id}/input",
                            json={"answers": {"salary_expectation": "120000"}}, headers=full_consent)
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["requeued"] is True
    assert body["pipeline_job_id"] == receipt["pipeline_job_id"], "the parked item must be re-used"

    # The re-queued item must keep its execute intent — this is what used to
    # demote the run to prepare, so the answer was filled into a form that
    # would never be submitted.
    db.expire_all()
    item = db.query(PipelineJob).filter(PipelineJob.id == receipt["pipeline_job_id"]).one()
    assert item.status == "queued"
    assert item.payload["mode"] == "execute", f"re-queue lost the execute intent: {item.payload}"
    assert item.payload["answers"] == {"salary_expectation": "120000"}

    # Second round: the answer is in the plan and — because the mode survived —
    # the consented run really submits.
    _run(receipt["pipeline_job_id"])
    row = _row(client, full_consent, receipt["pipeline_job_id"])
    result = row["result"]
    assert result["status"] == "applied", result
    assert result["autofill"]["status"] == "submitted"
    assert result["autofill"]["dry_run"] is False
    assert result["autofill"]["submitted"] is True
    assert "button[type=submit]" in fake_playwright.clicks

    db.expire_all()
    job_row = db.query(Job).filter(Job.id == job.id).one()
    assert job_row.status == "applied"
    plan = (job_row.extra or {}).get("autofill_plan") or {}
    salary = next(f for f in plan.get("fields", []) if f["name"] == "salary_expectation")
    assert salary["value"] == "120000", "the user's answer must reach the executed plan"
    assert db.query(UserInputRequest).filter(
        UserInputRequest.job_id == job.id, UserInputRequest.status == "pending").count() == 0


# --------------------------------------------------------------------------- #
# (d) one failing discovery insert must not discard the batch
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_discovery_batch_survives_one_failing_insert(client, auth, db, uploaded_resume, monkeypatch):
    """After the AI spend, a bad row is skipped and recorded — the rest lands."""
    from app.services import sources as source_registry
    from app.services.discovery import discover_for_user

    user = db.query(User).order_by(User.id).first()

    def posting(title, ext, company):
        return {
            "title": title, "company": company, "location": "Remote",
            "description": "Python, FastAPI, PostgreSQL. Payments platform at scale.",
            "url": f"https://jobs.lever.co/{company}/{ext}",
            "source": "lever", "external_id": ext,
        }

    postings = [
        posting("Senior Backend Engineer", "good1", "FinCo"),
        posting("Payments Engineer", "bad1", "PayCo"),
        posting("Platform Engineer", "good2", "ShopStack"),
    ]

    class FakePosting:
        def __init__(self, data):
            self._data = data

        def to_dict(self):
            return dict(self._data)

    async def fake_fetch_all(keywords, limit=0, since_hours=0, sources=None, board_tokens=None):
        return [FakePosting(d) for d in postings], {
            "requested": ["lever"], "ok": {"lever": 3}, "errors": {}, "total": 3,
        }

    monkeypatch.setattr(source_registry, "fetch_all", fake_fetch_all)

    # Simulate the unique-constraint race: the insert of the ``bad1`` row
    # loses to a concurrent writer and fails. Only that one row. (The
    # candidate's ``dedupe_key`` is its lower-cased ``external_id``.)
    real_commit = db.commit

    def flaky_commit(*args, **kwargs):
        if any(isinstance(o, Job) and getattr(o, "dedupe_key", "") == "bad1"
               for o in list(db.new)):
            raise IntegrityError("INSERT INTO jobs ...", {},
                                 sqlite3.IntegrityError("UNIQUE constraint failed: jobs.dedupe_key"))
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(db, "commit", flaky_commit)

    # The profile turns on the AI top slice (stubbed) so the batch really
    # spends its AI verdicts *before* the failing insert.
    profile_row = db.query(Profile).filter(Profile.user_id == user.id).first()
    report = await discover_for_user(
        db, user,
        keywords=["python"], freshness_hours=48, limit=10,
        live_enabled=True, source_ids=["lever"], board_tokens=[],
        profile=profile_row.data or None,
    )

    # The two good jobs persisted…
    assert report["inserted"] == 2, report
    assert {j["company"] for j in report["jobs"]} == {"FinCo", "ShopStack"}
    assert "why_empty" not in report
    # …and the bad one was recorded per row, not fatal to the run.
    assert len(report["insert_errors"]) == 1, report.get("insert_errors")
    assert report["insert_errors"][0]["dedupe_key"] == "bad1"
    assert report["insert_errors"][0]["company"] == "PayCo"
    assert "UNIQUE constraint failed" in report["insert_errors"][0]["error"]

    db.expire_all()
    rows = db.query(Job).filter(Job.user_id == user.id).all()
    assert {r.dedupe_key for r in rows} == {"good1", "good2"}

    # The run report is what the empty-board read and the queue row surface —
    # it must not claim the bad job landed.
    assert report["ai_rescore"]["enabled"] is True
