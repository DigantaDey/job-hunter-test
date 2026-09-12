"""
Smoke tests: the smallest possible end-to-end happy path, fully offline.

    cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest tests/ -q
"""
from __future__ import annotations

from tests.conftest import pdf_bytes


def test_health_and_meta(client):
    assert client.get("/api/health").json()["status"] == "ok"
    meta = client.get("/api/meta").json()
    assert meta["name"] and meta["version"]
    assert "ai_keyword_extraction" in meta["features"]
    assert "real_job_sources" in meta["features"]


def test_anonymous_requests_are_rejected(client):
    for path in ("/api/jobs", "/api/resumes", "/api/vault", "/api/settings", "/api/logs"):
        response = client.get(path)
        assert response.status_code == 401, f"{path} leaked to anonymous callers"
        assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_bootstrap_login_me_roundtrip(client, owner):
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {owner['access_token']}"}).json()
    assert me["email"] == "owner@example.com"

    # Wrong password is rejected, and the second bootstrap is not allowed.
    bad = client.post("/api/auth/login", json={"email": "owner@example.com", "password": "wrong-password"})
    assert bad.status_code == 401
    assert client.post("/api/auth/bootstrap", json={"email": "x@example.com", "password": "password-1234"}).status_code == 409


def test_upload_resume_builds_profile(client, auth, uploaded_resume):
    assert uploaded_resume["profile"]["name"] == "Test Candidate"
    assert uploaded_resume["profile"]["email"] == "test.candidate@example.com"
    assert uploaded_resume["search_context"]["keywords"]
    assert client.get("/api/profile/current", headers=auth).status_code == 200


def test_upload_rejects_non_resume(client, auth):
    response = client.post("/api/resume/upload", files={"file": ("photo.png", b"\x89PNG", "image/png")}, headers=auth)
    assert response.status_code == 415


def test_resume_generation_requires_a_resume(client, auth, seeded_job):
    response = client.post(f"/api/resumes/generate?job_id={seeded_job.id}", headers=auth)
    assert response.status_code == 400
    assert "resume" in response.text.lower()


def test_ocr_free_pdf_parsing(tmp_path):
    from app.services.resume_parser import diagnostic_preview_extract, extract_text

    path = tmp_path / "resume.pdf"
    path.write_bytes(pdf_bytes())
    text = extract_text(str(path))
    assert "python" in text.lower()

    profile = diagnostic_preview_extract(text)
    assert profile["email"] == "test.candidate@example.com"
    assert profile["name"]
    assert "python" in [skill.lower() for skill in profile["skills"]]


def test_keyword_extractor_miner():
    from app.services.keyword_extractor import merge_user_keywords, mined_context

    profile = {
        "name": "Test Candidate",
        "skills": ["python", "fastapi", "react", "aws", "kubernetes"],
        "summary": "Backend engineer building fintech payment platforms. Machine learning interest.",
        "experience": [{"title": "Senior Software Engineer", "description": "payments fintech"}],
        "location": "Bangalore, India",
        "raw_text": "python fastapi react aws kubernetes payments fintech microservices postgresql redis",
    }
    context = mined_context(profile, "interested in AI infrastructure startups")
    assert context["keywords"]
    assert "python" in [keyword.lower() for keyword in context["keywords"]]
    assert context["seniority"] in {"junior", "mid", "senior", "senior+", "staff+"}

    merged = merge_user_keywords(context, "rust, golang")
    assert merged["keywords"][0].lower() == "rust"


def test_demo_pool_is_opt_in(client, auth):
    """With INCLUDE_DEMO_POOL=false and live scraping off, discovery finds nothing."""
    body = client.post("/api/jobs/discover", json={"keywords": ["python"]}, headers=auth).json()
    assert body["queued"] is True  # queued for the worker; nothing is fabricated synchronously
    assert client.get("/api/jobs", headers=auth).json() == []


def test_settings_roundtrip(client, auth):
    updated = client.put("/api/settings", json={"general": {"strict_skeleton": True, "generate_min_score": 70}},
                         headers=auth)
    assert updated.status_code == 200, updated.text
    assert updated.json()["updated"]["general"]["generate_min_score"] == 70
    settings = client.get("/api/settings", headers=auth).json()
    assert settings["general"]["strict_skeleton"] is True
    # Unknown categories/keys are rejected with 400, not silently stored.
    assert client.put("/api/settings", json={"nope": {"x": 1}}, headers=auth).status_code == 400
