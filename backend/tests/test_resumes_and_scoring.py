"""Resume generation (fact guard), approval, diff, polish upload and scoring."""
from __future__ import annotations

import os

import pytest

from app.models.models import Job, Resume, User
from app.services.resume_service import diff_text, fact_guard_check, render_profile_text, resume_text
from app.services.scoring import jd_similarity, preliminary_score, tokenize
from tests.conftest import generate_resume_now


def _job(db, description: str = "Python, FastAPI, PostgreSQL, Kubernetes. Payments platform.") -> Job:
    user = db.query(User).order_by(User.id).first()
    job = Job(user_id=user.id, title="Backend Engineer", company="Acme", description=description,
              url="https://jobs.lever.co/acme/1", source="lever", dedupe_key=f"res:{len(description)}",
              score=80.0, status="discovered")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def test_tokenizer_matches_skills_with_punctuation():
    assert "python" in tokenize("Backend role. Python. FastAPI, AWS.")
    assert "c++" in tokenize("Looking for C++ engineers")


def test_preliminary_score_rewards_overlap():
    profile = {"skills": ["python", "fastapi", "postgresql"],
               "summary": "Python backend engineer building FastAPI services on PostgreSQL",
               "experience": [{"title": "Backend Engineer", "description": "python fastapi postgresql"}],
               "projects": []}
    strong, reason = preliminary_score(profile, "Python FastAPI PostgreSQL backend engineer")
    weak, _ = preliminary_score(profile, "Unity game designer with C# and Photoshop experience")
    assert strong > weak
    assert 0 <= weak <= 100 and 0 <= strong <= 100
    assert "skill overlap" in reason


def test_preliminary_score_handles_empty_input():
    score, reason = preliminary_score({}, "")
    assert score == 50.0
    assert "Insufficient" in reason


def test_jd_similarity_bounds():
    assert jd_similarity("", "anything") == 0.0
    assert jd_similarity("python backend", "python backend") > 0.99
    assert jd_similarity("python backend", "gardening tips") < 0.2


# --------------------------------------------------------------------------- #
# Fact guard
# --------------------------------------------------------------------------- #
def test_fact_guard_detects_invented_employers():
    original = {"experience": [{"company": "FinCo", "title": "Engineer", "duration": "2021-2024"}]}
    good = {"experience": [{"company": "FinCo", "title": "Engineer", "duration": "2021-2024"}]}
    bad = {"experience": [{"company": "FinCo", "title": "Engineer"}, {"company": "Google", "title": "Staff"}]}
    assert fact_guard_check(original, good)["passed"] is True
    result = fact_guard_check(original, bad)
    assert result["passed"] is False
    assert {"kind": "companies", "value": "google"} in result["violations"]


def test_render_profile_text_contains_key_facts():
    text = render_profile_text({
        "name": "Test Candidate", "email": "t@example.com", "skills": ["python", "aws"],
        "summary": "Backend engineer", "experience": [{"title": "Engineer", "company": "FinCo",
                                                      "bullets": ["Built payments"]}],
    })
    assert "Test Candidate" in text and "python" in text and "Built payments" in text


def test_diff_text_reports_changes():
    before = "Line one\nLine two\nLine three"
    after = "Line one\nLine two changed\nLine three\nLine four"
    result = diff_text(before, after)
    assert result["changed"] is True
    assert result["added_lines"] >= 1 and result["removed_lines"] >= 1
    assert result["similarity"] < 1.0


# --------------------------------------------------------------------------- #
# API flows
# --------------------------------------------------------------------------- #
def test_upload_creates_profile_master_resume_and_context(client, auth, uploaded_resume):
    assert uploaded_resume["resume"]["id"]
    assert uploaded_resume["profile"]["email"] == "test.candidate@example.com"
    assert uploaded_resume["layout"]
    assert uploaded_resume["search_context"]["keywords"]

    profile = client.get("/api/profile/current", headers=auth).json()
    assert profile["data"]["email"] == "test.candidate@example.com"

    resumes = client.get("/api/resumes", headers=auth).json()
    assert resumes[0]["type"] == "master"
    assert resumes[0]["status"] == "approved"


def test_upload_rejects_wrong_type_and_bad_content(client, auth):
    bad_ext = client.post("/api/resume/upload", files={"file": ("resume.txt", b"hello", "text/plain")}, headers=auth)
    assert bad_ext.status_code == 415
    spoofed = client.post("/api/resume/upload", files={"file": ("resume.pdf", b"not really a pdf", "application/pdf")},
                          headers=auth)
    assert spoofed.status_code == 415


def test_generate_approve_and_download_resume(client, auth, db, uploaded_resume):
    job = _job(db)
    body = generate_resume_now(client, auth, job.id)
    assert body["status"] == "generated" and body["resume_status"] == "pending"
    assert body["fact_guard"]["passed"] is True
    resume_id = body["resume_id"]

    client.post(f"/api/resumes/{resume_id}/approve", headers=auth)
    listing = client.get("/api/resumes", headers=auth).json()
    approved = next(r for r in listing if r["id"] == resume_id)
    assert approved["status"] == "approved"

    docx = client.get(f"/api/resumes/{resume_id}/download?format=docx", headers=auth)
    assert docx.status_code == 200
    assert docx.content[:2] == b"PK"  # docx is a zip container
    pdf = client.get(f"/api/resumes/{resume_id}/download?format=pdf", headers=auth)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    preview = client.get(f"/api/resumes/{resume_id}/preview", headers=auth).json()
    assert "SPECIAL" not in preview["text"] and preview["text"]


def test_generated_resume_is_not_usable_until_approved(client, auth, db, uploaded_resume):
    job = _job(db)
    resume_id = generate_resume_now(client, auth, job.id)["resume_id"]
    # The approval gate must not auto-approve generated resumes.
    row = db.query(Resume).filter(Resume.id == resume_id).first()
    assert row.status == "pending"
    client.post(f"/api/resumes/{resume_id}/reject", headers=auth)
    db.refresh(row)
    assert row.status == "rejected"


def test_resume_diff_and_polish_upload(client, auth, db, uploaded_resume):
    job = _job(db)
    resume_id = generate_resume_now(client, auth, job.id)["resume_id"]

    diff = client.get(f"/api/resumes/{resume_id}/diff", headers=auth).json()
    assert diff["against_id"] != resume_id
    assert "diff" in diff and "fact_guard" in diff

    from tests.conftest import pdf_bytes

    polished = client.post(f"/api/resumes/{resume_id}/polish",
                           files={"file": ("polished.pdf", pdf_bytes(), "application/pdf")}, headers=auth)
    assert polished.status_code == 200, polished.text
    body = polished.json()
    assert body["resume"]["status"] == "approved"  # user-provided version is approved by definition
    assert body["resume"]["parent_resume_id"] == resume_id

    listing = client.get("/api/resumes", headers=auth).json()
    polished_rows = [r for r in listing if r["type"] == "uploaded_polished"]
    assert polished_rows and polished_rows[0]["status"] == "approved"


def test_resume_tags_are_editable(client, auth, db, uploaded_resume):
    resume_id = client.get("/api/resumes", headers=auth).json()[0]["id"]
    updated = client.put(f"/api/resumes/{resume_id}/tags", json=["backend", "python"], headers=auth)
    assert updated.json()["tags"] == ["backend", "python"]


def test_delete_resume_removes_files(client, auth, db, uploaded_resume):
    resume_id = client.get("/api/resumes", headers=auth).json()[0]["id"]
    path = db.query(Resume).filter(Resume.id == resume_id).first().filepath
    assert os.path.exists(path)
    assert client.delete(f"/api/resumes/{resume_id}", headers=auth).status_code == 200
    assert not os.path.exists(path)
    assert client.get(f"/api/resumes/{resume_id}", headers=auth).status_code == 404


def test_profile_update_invalidates_context(client, auth, uploaded_resume):
    profile = client.get("/api/profile/current", headers=auth).json()
    before = client.get("/api/context/keywords", headers=auth).json()
    updated = dict(profile["data"])
    updated["skills"] = ["rust", "wasm"]
    assert client.put(f"/api/profile/{profile['id']}", json=updated, headers=auth).status_code == 200
    after = client.get("/api/context/keywords", headers=auth).json()
    assert after["generated_at"] != before["generated_at"]


@pytest.mark.asyncio
async def test_resume_reuse_decision_prefers_similar_jd(client, auth, db, uploaded_resume):
    from app.models.models import Profile
    from app.services.apply_flow import choose_resume

    profile = db.query(Profile).order_by(Profile.id).first()
    first = _job(db, "Python FastAPI PostgreSQL payments backend engineer kubernetes")
    second = _job(db, "Python FastAPI PostgreSQL payments backend engineer kubernetes docker")

    from app.services.handlers import handle_ai
    from app.services.job_queue import claim_item, complete

    # Inside an async test: run the queued item on this loop rather than via the helper.
    receipt = client.post(f"/api/resumes/generate?job_id={first.id}", headers=auth).json()
    item = claim_item(db, receipt["pipeline_job_id"])
    outcome = await handle_ai(db, item)
    complete(db, item, result=outcome)
    generated = outcome["resume_id"]
    client.post(f"/api/resumes/{generated}/approve", headers=auth)

    chosen_id, decision = await choose_resume(db, profile.user_id, profile, second, "auto")
    assert decision == "reused"
    assert chosen_id == generated
