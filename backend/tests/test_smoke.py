"""
Smoke tests — run offline (no AI key needed; heuristic fallbacks are exercised).

    cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest tests/ -q
"""
import os
import sys
import io

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

# Context manager form triggers the lifespan (init_db + worker startup)
client = TestClient(app)
client.__enter__()


def _pdf_bytes() -> bytes:
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
        Paragraph("Experience", styles["Heading2"]),
        Paragraph("Senior Software Engineer - FinCo (2021-present): Payments platform, 2M users, kubernetes on AWS.", styles["Normal"]),
    ])
    return buf.getvalue()


def test_health_and_meta():
    assert client.get("/api/health").json()["status"] == "ok"
    meta = client.get("/api/meta").json()
    assert "ai_keyword_extraction" in meta["features"]
    assert "funding_radar_ai_context" in meta["features"]


def test_keyword_extractor_heuristic():
    from app.services.keyword_extractor import heuristic_context, merge_user_keywords
    profile = {
        "name": "Test Candidate",
        "skills": ["python", "fastapi", "react", "aws", "kubernetes"],
        "summary": "Backend engineer building fintech payment platforms. Machine learning interest.",
        "experience": [{"title": "Senior Software Engineer", "description": "payments fintech"}],
        "location": "Bangalore, India",
        "raw_text": "python fastapi react aws kubernetes payments fintech microservices postgresql redis",
    }
    ctx = heuristic_context(profile, "interested in AI infrastructure startups")
    assert ctx["keywords"]
    assert "python" in [k.lower() for k in ctx["keywords"]]
    assert ctx["seniority"] in {"junior", "mid", "senior", "senior+", "staff+"}
    merged = merge_user_keywords(ctx, "rust, golang")
    assert merged["keywords"][0].lower() == "rust"


def test_scoring_tokenizer_fix():
    """'Python.' must match skill 'python' (trailing punctuation bug)."""
    from app.services.scoring import heuristic_score
    profile = {"skills": ["python"], "summary": "python developer", "experience": [], "projects": []}
    score, reason = heuristic_score(profile, "Backend role. Python. FastAPI. AWS.")
    assert score > 0, f"tokenizer bug regressed: {reason}"


def test_resume_upload_and_context():
    files = {"file": ("resume.pdf", _pdf_bytes(), "application/pdf")}
    r = client.post("/api/resume/upload", files=files)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["resume"]["id"] > 0
    assert data["profile"].get("email") == "test.candidate@example.com"
    ctx = data.get("search_context")
    assert ctx and ctx["keywords"], "upload must return AI/heuristic search context"

    # context endpoint
    ctx2 = client.get("/api/context/keywords").json()
    assert ctx2["keywords"]


def test_discovery_no_duplicates():
    r1 = client.post("/api/jobs/discover")
    assert r1.status_code == 200
    r2 = client.post("/api/jobs/discover")
    assert r2.status_code == 200
    import time
    time.sleep(3.5)  # background discovery
    jobs = client.get("/api/jobs").json()
    keys = [(j["title"], j["company"]) for j in jobs]
    assert len(keys) == len(set(keys)), f"duplicate jobs discovered: {keys}"


def test_funding_radar_fresh_and_populated():
    r = client.get("/api/funding/companies")
    assert r.status_code == 200
    data = r.json()
    companies = data["companies"]
    assert len(companies) >= 12, f"radar returned too few results: {len(companies)}"
    today = __import__("datetime").date.today()
    for c in companies:
        assert c["raised_at"] is not None
        y, m, d = map(int, c["raised_at"].split("-"))
        raised = __import__("datetime").date(y, m, d)
        age_days = (today - raised).days
        assert 0 <= age_days <= data["freshness_days"] + 1, f"stale company {c['name']}: {age_days}d"
        assert isinstance(c["has_open_positions"], bool)
    stages = {c["stage"] for c in companies}
    assert stages.issubset({"Seed", "Series A", "Series B", "Series C", "Series D"})
    # stage filter
    r2 = client.get("/api/funding/companies", params={"stage": "Series A"})
    assert all(c["stage"] == "Series A" for c in r2.json()["companies"])
    # context is included and keyword-matched
    assert data["context"]["keywords"]
    assert any(c["keywords_matched"] for c in companies)


def test_funding_refresh_and_process():
    r = client.post("/api/funding/refresh", json={"context": "AI infrastructure"})
    assert r.status_code == 200 and r.json()["ok"]
    companies = client.get("/api/funding/companies").json()["companies"]
    target = next((c for c in companies if c["has_open_positions"]), companies[0])
    pr = client.post(f"/api/funding/{target['name']}/process")
    assert pr.status_code == 200
    body = pr.json()
    if body["action"] == "apply_flow":
        assert body["job_id"]
    else:
        assert body["email_id"]


def test_resume_generate_and_download():
    jobs = client.get("/api/jobs").json()
    if not jobs:
        client.post("/api/jobs/discover")
        import time; time.sleep(3)
        jobs = client.get("/api/jobs").json()
    assert jobs
    r = client.post("/api/resumes/generate", params={"job_id": jobs[0]["id"]})
    assert r.status_code == 200, r.text
    rid = r.json()["resume_id"]
    pdf = client.get(f"/api/resumes/{rid}/download", params={"format": "pdf"})
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    docx = client.get(f"/api/resumes/{rid}/download", params={"format": "docx"})
    assert docx.status_code == 200 and docx.content[:2] == b"PK"


def test_email_flow():
    r = client.post("/api/emails/generate", params={"company": "Linear"})
    assert r.status_code == 200, r.text
    eid = r.json()["email"]["id"]
    assert client.post(f"/api/emails/{eid}/approve").json()["status"] == "queued"
    sent = client.post(f"/api/emails/{eid}/send")
    assert sent.status_code == 200 and sent.json().get("sent") is True


def test_apply_needs_input_then_requeue_completes():
    jobs = client.get("/api/jobs").json()
    target = next((j for j in jobs if j["company_size"] in ("startup", "small")), jobs[0])
    detail = client.get(f"/api/jobs/{target['id']}").json()
    r = client.post(f"/api/jobs/{target['id']}/apply", params={"resume_choice": "master"})
    assert r.status_code == 200, r.text
    if r.json()["status"] == "needs_input":
        uir_id = r.json()["input_request_id"]
        # no duplicate pending requests on re-apply
        r2 = client.post(f"/api/jobs/{target['id']}/apply", params={"resume_choice": "master"})
        assert r2.json()["input_request_id"] == uir_id
        sub = client.post(f"/api/jobs/{target['id']}/input", json={"workAuthorization": "Yes", "linkedin": "https://linkedin.com/in/test"})
        assert sub.status_code == 200
        import time; time.sleep(2)
        detail_after = client.get(f"/api/jobs/{target['id']}").json()
        assert detail_after["status"] == "applied", f"re-queued job stuck at {detail_after['status']}"


def test_vault_export_shape():
    r = client.get("/api/vault/export/chrome")
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    if len(lines) > 1:
        assert lines[0].strip() == "name,url,username,password"
    a = client.get("/api/vault/export/apple")
    assert a.status_code == 200
    assert a.text.strip().splitlines()[0] == "Title,URL,Username,Password,Notes,OTPAuth"


def test_settings_roundtrip():
    r = client.put("/api/settings", json={"funding": {"context_notes": "AI infra"}})
    assert r.status_code == 200
    s = client.get("/api/settings").json()
    assert s["funding"]["context_notes"] == "AI infra"
    assert "scraping" in s and "workflows" in s
