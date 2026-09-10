"""Tests for the unified AI client, live scraper normalization, and the
resume approval / reuse decision logic. All offline (no AI key required)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402

from app.services.ai_client import (  # noqa: E402
    chat_completion,
    AIClientError,
    is_configured,
    resolve_config,
    set_workflow_overrides,
)
from app.services.scoring import jd_similarity, should_generate_new_resume  # noqa: E402
from app.services.job_scraper import _normalize_live, fetch_live_jobs, _guess_industry  # noqa: E402


def test_ai_client_no_key_raises():
    """With no API key configured, chat_completion raises before touching the wire."""
    import app.services.ai_client as ac
    # Ensure no key is configured for this run regardless of environment.
    ac._workflow_overrides.clear()
    assert is_configured() is False or not resolve_config()["api_key"]
    with pytest.raises(AIClientError):
        # run inside an event loop
        import asyncio
        asyncio.run(chat_completion("scoring", "prompt"))


def test_workflow_override_resolution():
    import app.services.ai_client as ac
    ac._workflow_overrides.clear()
    try:
        set_workflow_overrides({"resume_gen": {"model": "gpt-4o", "api_key": "sk-test", "base_url": "https://example.com/v1"}})
        cfg = resolve_config("resume_gen")
        assert cfg["model"] == "gpt-4o"
        assert cfg["api_key"] == "sk-test"
        assert cfg["base_url"] == "https://example.com/v1"
        # unrelated workflow falls back to env config
        assert resolve_config("scoring")["model"] == resolve_config()["model"]
        assert is_configured("resume_gen") is True
    finally:
        ac._workflow_overrides.clear()


def test_live_scraper_normalization():
    arbeitnow = {"title": "Backend Engineer", "company_name": "Acme", "location": "Berlin",
                 "url": "https://arbeitnow.com/view/x", "tags": ["python", "aws", "fintech"]}
    j = _normalize_live(arbeitnow, "arbeitnow")
    assert j["title"] == "Backend Engineer"
    assert j["source"] == "arbeitnow"
    assert j["industry"] == "fintech"
    remotive = {"title": "ML Engineer", "company_name": "Beta", "candidate_required_location": "USA",
                "url": "https://remotive.com/jobs/x", "description": "machine learning pipelines"}
    j2 = _normalize_live(remotive, "remotive")
    assert j2["industry"] == "ai/ml"
    assert _normalize_live({}, "arbeitnow") is None
    assert _guess_industry("blockchain solidity") == "web3"


def test_live_scraper_disabled_returns_empty():
    import app.services.job_scraper as js
    old = js.settings.live_scraping_enabled
    js.settings.live_scraping_enabled = False
    try:
        import asyncio
        assert asyncio.run(fetch_live_jobs(["python"])) == []
    finally:
        js.settings.live_scraping_enabled = old


def test_resume_decision_logic():
    # Very similar JDs → reuse; dissimilar → generate new (if score high enough).
    jd_a = "Senior backend engineer Python FastAPI AWS Kubernetes payments fintech microservices."
    jd_b = "Senior backend engineer Python FastAPI AWS Kubernetes payments fintech microservices PostgreSQL."
    jd_c = "Graphic designer Figma branding illustration print media."
    sim_ab = jd_similarity(jd_a, jd_b)
    sim_ac = jd_similarity(jd_a, jd_c)
    assert sim_ab > 0.8
    assert sim_ac < 0.4
    assert should_generate_new_resume(80, sim_ab) is False      # reuse
    assert should_generate_new_resume(80, sim_ac) is True       # generate
    assert should_generate_new_resume(40, sim_ac) is False      # weak match → master


def test_resume_approve_flow():
    """Generated resumes start 'pending' and become 'approved' via the endpoint."""
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    client.__enter__()

    files = {"file": ("r.pdf", _pdf_bytes(), "application/pdf")}
    assert client.post("/api/resume/upload", files=files).status_code == 200

    # discovery to get a job
    client.post("/api/jobs/discover")
    import time
    time.sleep(2.5)
    jobs = client.get("/api/jobs").json()
    assert jobs

    r = client.post("/api/resumes/generate", params={"job_id": jobs[0]["id"]})
    assert r.status_code == 200, r.text
    rid = r.json()["resume_id"]
    assert r.json()["status"] == "pending"

    # approval flips status
    assert client.post(f"/api/resumes/{rid}/approve").json()["status"] == "approved"
    resumes = client.get("/api/resumes").json()
    assert any(x["id"] == rid and x["status"] == "approved" for x in resumes)


def _pdf_bytes() -> bytes:
    import io
    from reportlab.lib.pagesizes import LETTER
    from reportlab.platypus import SimpleDocTemplate, Paragraph
    from reportlab.lib.styles import getSampleStyleSheet
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER)
    styles = getSampleStyleSheet()
    doc.build([
        Paragraph("Test Candidate", styles["Title"]),
        Paragraph("test.candidate@example.com | +91 90000 00001 | Bangalore, India", styles["Normal"]),
        Paragraph("Summary", styles["Heading2"]),
        Paragraph("Full-stack engineer with 6 years in Python, FastAPI, React, TypeScript, AWS, Kubernetes. Fintech and AI products.", styles["Normal"]),
        Paragraph("Skills", styles["Heading2"]),
        Paragraph("Python, FastAPI, React, TypeScript, AWS, Docker, Kubernetes, PostgreSQL", styles["Normal"]),
    ])
    return buf.getvalue()
