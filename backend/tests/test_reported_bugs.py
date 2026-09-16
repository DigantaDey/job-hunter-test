"""
Regression tests for the bugs reported by the product owner.

Each test names the report it closes and asserts the *fixed* behaviour, so a
change that reintroduces a silent heuristic path, an unauthenticated download,
a fabricated email body or a keyword-overlap score dressed up as an AI verdict
fails here rather than in the user's browser.

Reports covered:
  1. AI offline must explain itself, never silently guess.
  2. Resume download auth + professional filename + core guardrail.
  3. Stronger AI scoring with hallucination guardrails (and honest provenance).
  4. User personas: several tracks, each growing its own memory.
  5. Email drafts must reference the real job/JD, real name, good subject,
     and the bucket must show which posting they target + whether the address
     is a generic guess.
  6. Funding radar: context/resume must reach the UI, and re-scanning must not
     500 on the queue's unique constraint.
  7. Settings keywords start empty and are populated from the resume.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from app.models.models import Email, Job, Profile, Resume
from app.services.job_queue import enqueue
from app.services.outreach import is_role_mailbox
from tests.conftest import draft_email_now, generate_resume_now


# --------------------------------------------------------------------------- #
# 1. AI offline: an explanation, never a silent inaccurate substitute
# --------------------------------------------------------------------------- #
@pytest.mark.real_ai
def test_upload_without_ai_explains_why_and_stores_nothing(client: TestClient, auth: Dict[str, str], db):
    """No key is configured, so the real gateway runs and must refuse loudly."""
    from tests.conftest import pdf_bytes

    response = client.post(
        "/api/resume/upload",
        files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
        headers=auth,
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "ai_unavailable"
    assert body["reason"] == "no_api_key"
    assert body["fix"]
    assert body["workflow"] == "parse"

    # The old behaviour stored a regex-guessed profile whose name was "Unknown"
    # and then wrote resumes and emails from it. Nothing may be persisted.
    assert db.query(Profile).count() == 0
    assert db.query(Resume).count() == 0


@pytest.mark.real_ai
def test_scoring_without_ai_reports_the_outage_instead_of_a_fake_verdict(client: TestClient, auth: Dict[str, str],
                                                                        uploaded_resume=None, db=None):
    """``score_job`` degrades to a *labelled* preliminary estimate, not "ai"."""
    import asyncio

    from app.services.scoring import score_job

    result = asyncio.run(score_job(
        {"skills": ["Python", "FastAPI"], "name": "Test Candidate", "summary": "Backend engineer.",
         "experience": []},
        "Senior Backend Engineer. Python, FastAPI, PostgreSQL, payments platform at scale.",
    ))
    assert result["score_source"] in {"pending", "insufficient_data"}
    assert result["score_source"] != "ai"


# --------------------------------------------------------------------------- #
# 2. Resume download: authentication, signed URLs, professional filenames
# --------------------------------------------------------------------------- #
def test_bare_download_link_is_rejected_but_signed_url_works(client: TestClient, auth: Dict[str, str],
                                                            uploaded_resume, db):
    resume = db.query(Resume).filter(Resume.type == "master").first()
    assert resume is not None

    # Exactly what the SPA used to do: a plain <a href>, so no Authorization
    # header. That is the request that produced {"detail":"Not authenticated"}.
    bare = client.get(f"/api/resumes/{resume.id}/download?format=docx")
    assert bare.status_code == 401
    assert bare.json()["detail"]["code"] == "not_authenticated"

    signed = client.get(f"/api/resumes/{resume.id}/download-url?format=docx", headers=auth)
    assert signed.status_code == 200, signed.text
    payload = signed.json()
    assert payload["url"].startswith(f"/api/resumes/{resume.id}/download?format=docx&token=")
    assert payload["expires_in"] > 0

    # A browser can follow the signed URL with no token in its own storage.
    followed = client.get(payload["url"])
    assert followed.status_code == 200, followed.text
    assert followed.headers["content-type"].startswith("application/")
    assert "attachment" in followed.headers.get("content-disposition", "")


def test_resume_download_rejects_mismatched_bearer_and_signed_token(
        client: TestClient, auth: Dict[str, str], member_auth: Dict[str, str], uploaded_resume, db):
    """A bearer for one account and a signed token for another must not mix."""
    resume = db.query(Resume).filter(Resume.type == "master").first()
    assert resume is not None

    owner_url = client.get(f"/api/resumes/{resume.id}/download-url?format=docx", headers=auth).json()["url"]
    token = owner_url.split("token=", 1)[1]

    # Owner bearer + owner token: fine.
    assert client.get(f"/api/resumes/{resume.id}/download?format=docx&token={token}",
                      headers=auth).status_code == 200

    # Member bearer + owner token: refused, not silently served the owner's file.
    mismatch = client.get(f"/api/resumes/{resume.id}/download?format=docx&token={token}",
                          headers=member_auth)
    assert mismatch.status_code == 401, mismatch.text
    assert mismatch.json()["detail"]["code"] == "credential_mismatch"


def test_download_filename_is_professional_not_resume_id_hash(client: TestClient, auth: Dict[str, str],
                                                             uploaded_resume, db):
    """``resume_26_a6786c6`` was the reported filename. It must be a real name."""
    from app.services.resume_generator import professional_filename

    profile = db.query(Profile).first()
    job = Job(user_id=profile.user_id, title="Senior Backend Engineer", company="FinCo",
              description="Python, FastAPI.", status="discovered", source="lever", dedupe_key="lever:x1")
    db.add(job)
    db.commit()

    name = professional_filename(profile.data, job)
    assert name == "Test-Candidate-FinCo-Senior-Backend-Engineer.pdf", name
    assert professional_filename(profile.data, job, extension="docx").endswith(".docx")
    assert not name.startswith("resume_")

    # And the endpoint advertises the same professional name to the browser.
    resume = db.query(Resume).filter(Resume.type == "master").first()
    signed = client.get(f"/api/resumes/{resume.id}/download-url?format=pdf", headers=auth)
    filename = signed.json()["filename"]
    assert not filename.startswith("resume_"), filename
    assert filename.endswith(".pdf")


def test_generated_resume_carries_the_guardrail_verdict(client: TestClient, auth: Dict[str, str], db,
                                                        uploaded_resume, seeded_job):
    payload = generate_resume_now(client, auth, seeded_job.id)
    assert payload["status"] == "generated", payload

    guardrail = payload["guardrail"]
    assert guardrail["passed"] is True
    assert guardrail["source"] == "ai"
    # The report asked for a core checker, not a prompt instruction: the checks
    # that ran must be named and reproducible.
    for check in ("schema", "accuracy_and_ats_checks", "fact_ledger"):
        assert check in guardrail["checks"], guardrail["checks"]

    resume = db.query(Resume).filter(Resume.id == payload["resume_id"]).first()
    assert resume is not None
    assert resume.guardrail_report["passed"] is True
    assert resume.display_name and not resume.display_name.startswith("resume_")


def test_hallucinated_resume_is_rejected_and_never_saved(client: TestClient, auth: Dict[str, str], monkeypatch,
                                                         uploaded_resume, seeded_job, db):
    """A model that invents an employer must fail the fact ledger, not the user."""
    import app.services.ai_client as ai_client

    async def lying_model(workflow, prompt, **kwargs):
        if workflow == "resume_gen":
            from tests.conftest import _stub_tailored

            payload = _stub_tailored()
            payload["tailored_profile"]["experience"].append({
                "title": "Staff Engineer", "company": "ImaginaryCorp",
                "duration": "2019 - 2021", "location": "Nowhere",
                "bullets": ["Led a team that does not exist."],
            })
            return payload
        from tests.conftest import stub_ai_response

        return stub_ai_response(workflow, prompt)

    monkeypatch.setattr(ai_client, "chat_completion", lying_model)

    # The queued run finishes as a *rejection* (done, result.status=rejected) —
    # not a failure to retry: the model would invent the employer again.
    body = generate_resume_now(client, auth, seeded_job.id)
    assert body["_row"]["status"] == "done"
    assert body["status"] == "rejected" and body["code"] == "guardrail_failed", body
    codes = {issue["code"] for issue in body["issues"]}
    assert "fabricated_employer" in codes, codes

    # Nothing was persisted: the master upload is the only resume on file.
    assert db.query(Resume).filter(Resume.type == "generated").count() == 0


def test_phone_keeps_its_plus_sign_in_rendered_resumes(db):
    """A leading '+' in a phone number was eaten by the markdown bullet pass."""
    from app.services.ai_guardrails import strip_ai_artifacts

    assert strip_ai_artifacts("+49 151 2345 6789") == "+49 151 2345 6789"
    assert strip_ai_artifacts("+91 90000 00001") == "+91 90000 00001"
    # genuine bullets are still normalised to the resume bullet glyph
    assert strip_ai_artifacts("- led a team of five").startswith("•")
    assert strip_ai_artifacts("+ shipped a feature").startswith("•")


# --------------------------------------------------------------------------- #
# 3. Stronger AI scoring, with honest provenance
# --------------------------------------------------------------------------- #
def _grant_pro(db, email: str = "owner@example.com") -> None:
    """AI (advanced) matching is a Pro entitlement; free stays on the estimate."""
    from datetime import datetime, timezone

    from app.models.models import Subscription, User

    user = db.query(User).filter(User.email == email).first()
    db.add(Subscription(user_id=user.id, plan="pro", status="active", provider="manual",
                        current_period_start=datetime.now(timezone.utc)))
    db.commit()


def test_ai_score_uses_the_rubric_and_records_its_source(client: TestClient, auth: Dict[str, str], db,
                                                         uploaded_resume, seeded_job):
    _grant_pro(db)
    intel = client.get(f"/api/jobs/{seeded_job.id}/intelligence", params={"refresh": True}, headers=auth)
    assert intel.status_code == 200, intel.text
    payload = intel.json()
    assert payload["score_source"] == "ai", payload
    assert payload["score"] == 88, payload

    # The request ran in its own session; expire the identity map before
    # re-reading so this asserts what is actually persisted.
    db.expire_all()
    row = db.query(Job).filter(Job.id == seeded_job.id).first()
    assert row.score_source == "ai", row.score_source
    detail = row.score_detail or {}
    assert detail.get("rubric"), detail
    # The rubric must be the weighted one, not an ad-hoc keyword overlap.
    assert abs(sum(detail["rubric"].values()) - 1.0) < 1e-6, detail["rubric"]
    assert detail.get("breakdown"), detail
    # Hallucination guardrail: every strength it claims exists in the profile.
    assert detail.get("strong_matches") == ["python", "fastapi"], detail.get("strong_matches")
    assert detail.get("evidence"), "the AI verdict must cite the phrases it relied on"


def test_free_tier_scores_are_labelled_preliminary_not_ai(client: TestClient, auth: Dict[str, str],
                                                          uploaded_resume, seeded_job):
    """Without the Pro entitlement the estimate must still admit what it is."""
    intel = client.get(f"/api/jobs/{seeded_job.id}/intelligence", params={"refresh": True}, headers=auth)
    assert intel.status_code == 200, intel.text
    payload = intel.json()
    assert payload["score_source"] == "preliminary"
    assert payload.get("upgrade_hint")


def test_preliminary_scores_are_labelled_as_preliminary(client: TestClient, auth: Dict[str, str], db,
                                                        uploaded_resume):
    """Discovery-time keyword estimates must never masquerade as AI verdicts."""
    from app.services.scoring import preliminary_score_detailed

    profile = db.query(Profile).first()
    job = Job(user_id=profile.user_id, title="Data Engineer", company="AcmeData",
              description="Python, SQL, Airflow pipelines.", status="discovered", source="lever",
              dedupe_key="lever:prelim-1", score=0)
    db.add(job)
    db.commit()

    detail = preliminary_score_detailed(profile.data, job.description)
    assert detail["source"] == "preliminary"
    assert detail.get("ai") is not True


# --------------------------------------------------------------------------- #
# 4. Personas: multiple tracks, each with its own growing memory
# --------------------------------------------------------------------------- #
def test_multiple_personas_can_coexist_and_one_is_active(client: TestClient, auth: Dict[str, str],
                                                         uploaded_resume, db):
    analyst = client.post("/api/personas", json={"name": "Data Analyst", "target_role": "Senior Data Analyst",
                                                 "keywords": ["sql", "tableau", "product analytics"]}, headers=auth)
    assert analyst.status_code == 201, analyst.text
    scientist = client.post("/api/personas", json={"name": "Data Scientist", "target_role": "Data Scientist",
                                                   "keywords": ["python", "ml", "pytorch"]}, headers=auth)
    assert scientist.status_code == 201, scientist.text

    listing = client.get("/api/personas", headers=auth).json()
    names = {p["name"] for p in listing["personas"]}
    assert {"Data Analyst", "Data Scientist"} <= names
    assert listing["active_id"] == scientist.json()["id"]

    # Switching tracks changes the discovery context the rest of the product reads.
    client.post(f"/api/personas/{analyst.json()['id']}/activate", headers=auth)
    assert client.get("/api/personas", headers=auth).json()["active_id"] == analyst.json()["id"]

    analyst_context = client.get(f"/api/personas/{analyst.json()['id']}/context", headers=auth).json()
    scientist_context = client.get(f"/api/personas/{scientist.json()['id']}/context", headers=auth).json()
    assert "sql" in analyst_context["keywords"]
    assert "pytorch" in scientist_context["keywords"]
    assert analyst_context["keywords"] != scientist_context["keywords"]


def test_creating_a_duplicate_track_is_refused_not_a_server_error(client: TestClient, auth: Dict[str, str],
                                                                  uploaded_resume):
    """A repeat name used to hit UNIQUE(user, name) and answer 500."""
    first = client.post("/api/personas", json={"name": "Data Analyst", "target_role": "Data Analyst"}, headers=auth)
    assert first.status_code == 201, first.text

    again = client.post("/api/personas", json={"name": "data analyst", "target_role": "Senior Data Analyst"},
                        headers=auth)
    assert again.status_code == 409, again.text
    assert "Data Analyst" in again.json()["detail"]


def test_persona_grows_from_recorded_signals_and_rebuilds_its_portrait(client: TestClient, auth: Dict[str, str],
                                                                       uploaded_resume, db):
    from app.services import persona as persona_service

    created = client.post("/api/personas", json={"name": "Backend Engineer", "target_role": "Senior Backend Engineer"},
                          headers=auth).json()
    persona_id = created["id"]

    from app.models.models import User

    user = db.query(User).filter(User.email == "owner@example.com").first()
    for kind in ("job_scored", "applied", "reply_received", "interview"):
        persona_service.record_signal(db, user.id, persona_id, kind, {"note": kind})

    row = client.get(f"/api/personas/{persona_id}", headers=auth).json()
    memory = row["memory"]
    assert memory["signals"] >= 4
    assert memory["engagement"]["applied"] >= 1
    assert memory["engagement"]["reply_received"] >= 1
    assert memory["maturity"] in {"new", "learning", "established"}

    reflected = client.post(f"/api/personas/{persona_id}/reflect", params={"force": True}, headers=auth)
    assert reflected.status_code == 200, reflected.text
    portrait = reflected.json()["portrait"]
    assert portrait["headline"]
    assert portrait["identity"]
    assert client.get(f"/api/personas/{persona_id}", headers=auth).json()["portrait_at"]


def test_suggestions_offer_more_than_one_track(client: TestClient, auth: Dict[str, str], uploaded_resume):
    suggestions = client.get("/api/personas/suggestions", headers=auth)
    assert suggestions.status_code == 200, suggestions.text
    tracks = suggestions.json()["tracks"]
    assert len(tracks) >= 2
    assert all({"name", "target_role", "keywords", "why"} <= set(t) for t in tracks)


# --------------------------------------------------------------------------- #
# 5. Email drafts: real job context, real name, honest addresses
# --------------------------------------------------------------------------- #
def test_draft_references_the_job_and_the_bucket_shows_the_jd(client: TestClient, auth: Dict[str, str],
                                                              uploaded_resume, seeded_job, db):
    generated = draft_email_now(client, auth, {"company": "FinCo", "job_id": seeded_job.id})
    assert generated["email"], generated

    row = db.query(Email).order_by(Email.id.desc()).first()
    assert row is not None
    assert row.job_id == seeded_job.id
    assert row.job_title == "Senior Backend Engineer"
    assert row.job_url == "https://jobs.lever.co/finco/abc123"
    assert "Python" in (row.jd_excerpt or "")
    assert row.ai_used is True
    assert (row.guardrail_report or {}).get("passed") is True

    # The reported body said "I'm Unknown, a python, sql, rust, ml, ai engineer."
    assert "Unknown" not in row.body
    assert row.to_name != "Unknown"
    assert "Test Candidate" in row.body
    assert "[", "markdown link garbage" if "[" in row.body else ""
    assert "](mailto:" not in row.body
    assert "](http" not in row.body

    # The bucket must answer "which job is this for, and what did the JD say".
    bucket = client.get("/api/emails", headers=auth).json()
    card = next(e for e in bucket if e["id"] == row.id)
    assert card["job"]["title"] == "Senior Backend Engineer"
    assert card["job"]["url"] == seeded_job.url
    assert "Python" in card["job"]["jd_excerpt"]
    assert card["ai_used"] is True
    assert card["guardrail"]["passed"] is True

    # Reported subject: "Embedded Software Engineer IoT & Connectivity (m/w/d) at Wirelane — Unknown"
    assert not card["subject"].endswith("— Unknown")
    assert "Unknown" not in card["subject"]


def test_generic_role_mailbox_is_flagged_not_passed_off_as_real(client: TestClient, auth: Dict[str, str],
                                                                uploaded_resume, db):
    assert is_role_mailbox("engineering@wirelane.com") is True
    assert is_role_mailbox("jobs@wirelane.com") is True
    assert is_role_mailbox("hr@wirelane.com") is True
    assert is_role_mailbox("diganta@wirelane.com") is False

    generated = draft_email_now(client, auth, {"company": "Wirelane", "department": "engineering",
                                               "recipient_email": "engineering@wirelane.com",
                                               "recipient_name": "Engineering Team"})
    assert generated["email"], generated
    row = db.query(Email).order_by(Email.id.desc()).first()
    assert row.verified is False

    card = next(e for e in client.get("/api/emails", headers=auth).json() if e["id"] == row.id)
    assert card["verified"] is False
    assert card["role_mailbox"] is True
    assert card["source"] in {"none", "heuristic", "pattern", "catch_all", "manual"}


def test_email_without_a_job_is_marked_as_unattached(client: TestClient, auth: Dict[str, str], uploaded_resume, db):
    generated = draft_email_now(client, auth, {"company": "FinCo"})
    assert generated["email"], generated
    card = client.get("/api/emails", headers=auth).json()[0]
    assert card["job"] is None


# --------------------------------------------------------------------------- #
# 6. Funding radar: context reaches the UI and re-scanning is safe
# --------------------------------------------------------------------------- #
def test_funding_companies_report_the_uploaded_resume(client: TestClient, auth: Dict[str, str], uploaded_resume):
    payload = client.get("/api/funding/companies", headers=auth).json()
    assert payload["resume"] is not None
    assert payload["resume"]["profile_id"]
    assert payload["resume"]["name"] == "Test Candidate"
    assert payload["context"] and payload["context"]["profile_id"]
    assert payload["context"]["keywords"]


def test_funding_radar_without_a_resume_says_so_explicitly(client: TestClient, auth: Dict[str, str]):
    payload = client.get("/api/funding/companies", headers=auth).json()
    assert payload["resume"] is None
    assert payload["context"] == {} or not payload["context"].get("profile_id")


def test_funding_rescan_can_be_repeated_without_a_server_error(client: TestClient, auth: Dict[str, str]):
    """Reported as "Internal server error": the queue's UNIQUE(user, dedupe_key)."""
    first = client.post("/api/funding/refresh", json={"context": "b2b fintech, seed to series A"}, headers=auth)
    assert first.status_code == 200, first.text
    assert first.json()["queued"] is True

    second = client.post("/api/funding/refresh", json={"context": "b2b fintech, seed to series A"}, headers=auth)
    assert second.status_code == 200, second.text
    assert second.json()["duplicate"] is True

    # A different focus is different work and must be accepted again.
    third = client.post("/api/funding/refresh", json={"context": "climate tech, series B"}, headers=auth)
    assert third.status_code == 200, third.text
    assert third.json()["queued"] is True
    assert third.json()["focus_applied"] == "climate tech, series B"


def test_terminal_queue_items_are_reused_instead_of_colliding(db, owner):
    """Root cause of the funding 500: re-enqueueing a finished item."""
    from app.models.models import PipelineJob, User

    user = db.query(User).filter(User.email == owner["email"]).first()
    first = enqueue(db, user_id=user.id, pipeline="funding", payload={"window_days": 30},
                    dedupe_key="funding:test:30:0:abc")
    assert first is not None

    # Same key while still queued -> de-duplicated, no new row.
    assert enqueue(db, user_id=user.id, pipeline="funding", payload={"window_days": 30},
                   dedupe_key="funding:test:30:0:abc") is None

    # Terminal row -> reused rather than raising IntegrityError.
    row = db.query(PipelineJob).filter(PipelineJob.id == first.id).first()
    row.status = "succeeded"
    db.commit()
    again = enqueue(db, user_id=user.id, pipeline="funding", payload={"window_days": 30},
                    dedupe_key="funding:test:30:0:abc")
    assert again is not None
    assert again.id == first.id
    assert again.status == "queued"


def test_funding_focus_is_persisted_to_settings(client: TestClient, auth: Dict[str, str], db):
    from app.models.models import User
    from app.services.user_settings import get_setting

    response = client.post("/api/funding/refresh",
                           json={"context": "embedded IoT hardware", "industries": "hardware, mobility"},
                           headers=auth)
    assert response.status_code == 200, response.text

    user = db.query(User).filter(User.email == "owner@example.com").first()
    assert get_setting(db, user.id, "funding", "context_notes") == "embedded IoT hardware"
    assert get_setting(db, user.id, "funding", "industries") == "hardware, mobility"


# --------------------------------------------------------------------------- #
# 7. Settings keywords: empty by default, then resume-derived
# --------------------------------------------------------------------------- #
def test_keywords_start_empty_for_a_new_account(client: TestClient, auth: Dict[str, str]):
    data = client.get("/api/settings", headers=auth).json()
    assert data["scraping"]["keywords"] == [], data["scraping"]["keywords"]

    context = client.get("/api/context/keywords", headers=auth).json()
    assert context["profile_id"] is None


def test_keywords_come_from_the_uploaded_resume(client: TestClient, auth: Dict[str, str], uploaded_resume):
    context = client.get("/api/context/keywords", headers=auth).json()
    assert context["profile_id"]
    assert context["keywords"], context
    assert context["source"]
    # The extras field stays empty — only the extracted set is populated.
    data = client.get("/api/settings", headers=auth).json()
    assert data["scraping"]["keywords"] == []


def test_user_keywords_are_extra_not_a_replacement(client: TestClient, auth: Dict[str, str], uploaded_resume):
    client.put("/api/settings", json={"scraping": {"keywords": ["embedded c", "rust"]}}, headers=auth)
    data = client.get("/api/settings", headers=auth).json()
    assert data["scraping"]["keywords"] == ["embedded c", "rust"]

    context = client.get("/api/context/keywords", headers=auth).json()
    assert "rust" in context["keywords"]
    # ...and the resume-derived terms are still there.
    assert len(context["keywords"]) > 2
