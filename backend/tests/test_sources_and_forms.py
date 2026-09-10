"""Job source adapters, discovery plumbing, form detection and autofill plans."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.autofill import build_autofill_plan
from app.services.form_detector import detect_form_structure, detect_portal_type, map_field_name
from app.services.sources import ADAPTERS, fetch_all, list_sources
from app.services.sources.adapters import (
    ArbeitnowSource,
    GreenhouseSource,
    LeverSource,
    LinkedInSource,
    RemoteOKSource,
    RemotiveSource,
    WorkdaySource,
)
from app.services.sources.base import Posting, SourceError, parse_datetime, strip_html


class FakeResponse:
    def __init__(self, payload=None, status_code: int = 200, text: str = "", headers=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text or ""
        self.headers = headers or {"content-type": "application/json"}
        self.request = None

    def json(self):
        return self._payload


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_registry_reports_gated_sources_honestly():
    sources = {s["id"]: s for s in list_sources()}
    assert sources["linkedin"]["available"] is False
    assert "partner" in sources["linkedin"]["unavailable_reason"]
    assert sources["indeed"]["available"] is False
    assert sources["naukri"]["available"] is False
    assert sources["instahyre"]["available"] is False
    # real, ToS-compliant sources are available
    for source_id in ("greenhouse", "lever", "remotive", "arbeitnow", "workday", "wemuse".replace("wemuse", "themuse")):
        assert sources[source_id]["available"] is True


@pytest.mark.asyncio
async def test_gated_source_raises_with_reason():
    with pytest.raises(SourceError) as excinfo:
        await LinkedInSource().fetch(keywords=[], limit=1, since_hours=24, board_tokens=[])
    assert "partner" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def test_posting_normalisation_helpers():
    assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"
    assert parse_datetime(1_700_000_000) is not None
    assert parse_datetime("2024-05-01T10:00:00Z").year == 2024
    assert parse_datetime("not a date") is None

    posting = Posting(title="Backend", company="Acme", url="https://x", source="lever", external_id="42")
    assert posting.dedupe_key() == "lever:42"
    plain = Posting(title="Backend", company="Acme", url="https://x", source="lever")
    assert plain.dedupe_key() == "acme:backend"


def test_portal_detection_and_field_mapping():
    assert detect_portal_type("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert detect_portal_type("https://jobs.lever.co/acme/abc") == "lever"
    assert detect_portal_type("https://acme.wd1.myworkdayjobs.com/en-US/External/job/1") == "workday"
    assert detect_portal_type("https://careers.acme.com/apply") == "custom"

    assert map_field_name("First Name", "first_name", "text") == "firstName"
    assert map_field_name("Work Authorization", "q3", "select") == "workAuthorization"
    assert map_field_name("Résumé/CV", "resume", "file") == "resume"
    assert map_field_name("Favorite colour", "q7", "text") is None
    assert map_field_name("", "email", "email") == "email"


# --------------------------------------------------------------------------- #
# Adapter parsing (HTTP is monkeypatched — no network in tests)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_greenhouse_adapter(monkeypatch):
    async def fake_get_json(url, **kwargs):
        assert "boards-api.greenhouse.io" in url
        return {"jobs": [{
            "id": 1, "title": "Backend Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
            "location": {"name": "Remote"}, "content": "&lt;p&gt;Python and FastAPI&lt;/p&gt;",
            "updated_at": "2024-05-01T10:00:00-04:00", "company_name": "Acme", "departments": [{"name": "Eng"}],
        }]}

    monkeypatch.setattr("app.services.http.get_json", fake_get_json)
    postings = await GreenhouseSource().fetch_board("acme", 5)
    assert len(postings) == 1
    assert postings[0].company == "Acme"
    assert "Python" in postings[0].description  # HTML entities decoded
    assert postings[0].posted_at.year == 2024


@pytest.mark.asyncio
async def test_lever_adapter(monkeypatch):
    async def fake_get_json(url, **kwargs):
        return [{
            "id": "abc", "text": "Senior Python Engineer", "hostedUrl": "https://jobs.lever.co/acme/abc",
            "createdAt": 1_700_000_000_000, "descriptionPlain": "Python, Kubernetes",
            "categories": {"location": "Remote", "team": "Platform", "commitment": "Full-time"},
        }]

    monkeypatch.setattr("app.services.http.get_json", fake_get_json)
    postings = await LeverSource().fetch_board("acme", 5)
    assert postings[0].title == "Senior Python Engineer"
    assert postings[0].remote is True
    assert postings[0].extra["team"] == "Platform"


@pytest.mark.asyncio
async def test_remotive_and_arbeitnow_adapters(monkeypatch):
    async def remotive_payload(url, **kwargs):
        return {"jobs": [{"id": 9, "url": "https://remotive.com/jobs/9", "title": "Python Developer",
                          "company_name": "Acme", "candidate_required_location": "Anywhere",
                          "publication_date": "2024-05-01T00:00:00", "description": "<p>python</p>",
                          "category": "Software Development"}]}

    monkeypatch.setattr("app.services.http.get_json", remotive_payload)
    postings = await RemotiveSource().fetch(keywords=["python"], limit=5, since_hours=24 * 365, board_tokens=[])
    assert postings and postings[0].remote is True

    async def arbeitnow_payload(url, **kwargs):
        return {"data": [{"slug": "x", "title": "Backend Engineer", "company_name": "Beta",
                          "location": "Berlin", "url": "https://arbeitnow.com/x", "remote": True,
                          "description": "<b>python</b>", "created_at": 1_700_000_000, "tags": ["python"]}]}

    monkeypatch.setattr("app.services.http.get_json", arbeitnow_payload)
    postings = await ArbeitnowSource().fetch(keywords=["python"], limit=5, since_hours=24 * 365, board_tokens=[])
    assert postings[0].company == "Beta"


@pytest.mark.asyncio
async def test_remoteok_skips_legal_notice(monkeypatch):
    async def fake_request(method, url, **kwargs):
        return FakeResponse([{"legal": "notice"}, {"id": "1", "position": "Python Engineer", "company": "Acme",
                                                    "url": "https://remoteok.com/1", "description": "python",
                                                    "date": "2024-05-01T00:00:00", "tags": ["python"]}])

    monkeypatch.setattr("app.services.http.request", fake_request)
    postings = await RemoteOKSource().fetch(keywords=["python"], limit=5, since_hours=24 * 365, board_tokens=[])
    assert len(postings) == 1
    assert postings[0].company == "Acme"


@pytest.mark.asyncio
async def test_workday_requires_structured_token(monkeypatch):
    async def fake_request(method, url, **kwargs):
        return FakeResponse({"jobPostings": [{"title": "SRE", "externalPath": "/job/sre",
                                              "locationsText": "Remote", "postedOn": "Posted 3 Days Ago",
                                              "bulletFields": ["R-123"]}]})

    monkeypatch.setattr("app.services.http.request", fake_request)
    source = WorkdaySource()
    assert await source.boards(["bad-token"]) == []
    postings = await source.fetch(keywords=["sre"], limit=5, since_hours=24 * 365,
                                  board_tokens=["acme.wd1.myworkdayjobs.com|acme|External"])
    assert postings[0].url.endswith("/job/sre")


@pytest.mark.asyncio
async def test_fetch_all_degrades_gracefully(monkeypatch):
    async def flaky(source_id, keywords, **kwargs):
        if source_id == "lever":
            raise SourceError("upstream down")
        return [Posting(title="Python Engineer", company="Acme", url="https://x", source=source_id,
                        description="python", posted_at=datetime.utcnow())]

    monkeypatch.setattr("app.services.sources.fetch_from_source", flaky)
    postings, report = await fetch_all(["python"], limit=10, sources=["lever", "greenhouse"])
    assert report["errors"]["lever"] == "upstream down"
    assert report["ok"]["greenhouse"] == 1
    assert len(postings) == 1


@pytest.mark.asyncio
async def test_fetch_all_filters_stale_postings(monkeypatch):
    async def stale(source_id, keywords, **kwargs):
        return [
            Posting(title="Fresh", company="A", url="https://a", source=source_id, posted_at=datetime.utcnow()),
            Posting(title="Stale", company="B", url="https://b", source=source_id,
                    posted_at=datetime.utcnow() - timedelta(days=60)),
        ]

    monkeypatch.setattr("app.services.sources.fetch_from_source", stale)
    postings, _ = await fetch_all(["python"], limit=10, sources=["greenhouse"], since_hours=48)
    assert [p.title for p in postings] == ["Fresh"]


# --------------------------------------------------------------------------- #
# Form detection (real HTML parsing)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_form_detection_parses_real_html(monkeypatch):
    html = """
    <html><head><title>Apply - Backend Engineer</title></head><body>
      <form id="application_form">
        <label for="first_name">First Name</label><input id="first_name" name="first_name" required>
        <label for="email">Email Address</label><input id="email" name="email" type="email" required>
        <label for="resume">Resume/CV</label><input id="resume" name="resume" type="file" required>
        <label for="wa">Are you authorized to work?</label>
        <select id="wa" name="work_authorization" required><option>Yes</option><option>No</option></select>
        <label for="color">Favorite colour</label><input id="color" name="fav_color">
        <input type="hidden" name="csrf_token" value="x">
      </form></body></html>
    """

    async def fake_request(method, url, **kwargs):
        return FakeResponse(text=html, headers={"content-type": "text/html; charset=utf-8"})

    monkeypatch.setattr("app.services.http.request", fake_request)
    monkeypatch.setattr("app.core.config.settings.live_scraping_enabled", True)

    schema = await detect_form_structure("https://boards.greenhouse.io/acme/jobs/1", "greenhouse")
    assert schema["detection_source"] == "html"
    assert schema["portal_type"] == "greenhouse"
    names = {f["name"] for f in schema["fields"]}
    assert {"first_name", "email", "resume", "work_authorization"} <= names
    assert "csrf_token" not in names  # volatile fields ignored
    assert schema["has_file_upload"] is True
    assert "first_name" in schema["mapped_fields"]
    assert "fav_color" in schema["unmapped_fields"]
    assert 0.5 < schema["ai_confidence"] <= 0.97


@pytest.mark.asyncio
async def test_form_detection_is_honest_when_fetch_blocked(monkeypatch):
    async def blocked(method, url, **kwargs):
        raise PermissionError("robots.txt disallows fetching")

    monkeypatch.setattr("app.services.http.request", blocked)
    monkeypatch.setattr("app.core.config.settings.live_scraping_enabled", True)
    schema = await detect_form_structure("https://example.com/jobs/1", "custom")
    assert schema["detection_source"] == "unavailable"
    assert "robots" in schema["error"]
    assert schema["ai_confidence"] <= 0.45  # no fake high confidence


@pytest.mark.asyncio
async def test_form_detection_flags_js_rendered_pages(monkeypatch):
    async def fake_request(method, url, **kwargs):
        return FakeResponse(text="<html><body><div id='app'></div>Create an account to apply</body></html>",
                            headers={"content-type": "text/html"})

    monkeypatch.setattr("app.services.http.request", fake_request)
    monkeypatch.setattr("app.core.config.settings.live_scraping_enabled", True)
    schema = await detect_form_structure("https://acme.wd1.myworkdayjobs.com/job/1", "workday")
    assert schema["requires_login"] is True
    assert schema["detection_source"] in ("heuristic", "html")


# --------------------------------------------------------------------------- #
# Autofill plan
# --------------------------------------------------------------------------- #
def test_autofill_plan_maps_profile_and_flags_missing():
    schema = {
        "portal_type": "lever", "requires_login": True, "vault_domain": "jobs.lever.co",
        "fields": [
            {"name": "firstName", "label": "First name", "type": "text", "required": True, "profile_key": "firstName"},
            {"name": "email", "label": "Email", "type": "email", "required": True, "profile_key": "email"},
            {"name": "resume", "label": "Resume", "type": "file", "required": True, "profile_key": "resume"},
            {"name": "linkedin", "label": "LinkedIn", "type": "url", "required": True, "profile_key": "linkedin"},
            {"name": "q_gender", "label": "Gender", "type": "select", "required": False, "profile_key": None},
            {"name": "password", "label": "Password", "type": "password", "required": True, "profile_key": None},
        ],
    }
    profile = {"name": "Test Candidate", "firstName": "Test", "email": "t@example.com", "skills": ["python"]}
    plan = build_autofill_plan(schema=schema, profile=profile, resume_path="/tmp/resume.pdf",
                               credential={"username": "t@example.com", "password": "vault-pass"})
    values = {f["name"]: f for f in plan["fields"]}
    assert values["firstName"]["value"] == "Test"
    assert values["email"]["value"] == "t@example.com"
    assert values["resume"]["value"] == "/tmp/resume.pdf"
    assert values["password"]["value"] == "vault-pass"
    assert values["q_gender"]["value"] is None
    assert [m["name"] for m in plan["missing_required"]] == ["linkedin"]
    assert plan["fillable"] == 4


def test_autofill_plan_uses_user_answers_over_profile():
    schema = {"portal_type": "custom", "fields": [
        {"name": "phone", "label": "Phone", "type": "tel", "required": True, "profile_key": "phone"},
        {"name": "notice", "label": "Notice period", "type": "text", "required": False, "profile_key": None},
    ]}
    plan = build_autofill_plan(schema=schema, profile={"phone": "000"},
                               answers={"phone": "999", "notice": "30 days"})
    values = {f["name"]: f["value"] for f in plan["fields"]}
    assert values["phone"] == "999"
    assert values["notice"] == "30 days"


def test_autofill_availability_is_honest():
    from app.services.autofill import autofill_available

    status = autofill_available()
    assert status["available"] is False
    assert "playwright" in status["reason"]


def test_adapters_registry_contains_all_expected_ids():
    for source_id in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "workday",
                      "remotive", "arbeitnow", "jobicy", "remoteok", "himalayas", "themuse",
                      "weworkremotely", "adzuna", "jooble", "usajobs", "linkedin", "indeed",
                      "naukri", "instahyre"):
        assert source_id in ADAPTERS
