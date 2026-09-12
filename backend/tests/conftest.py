"""
Test configuration.

Hermetic by design: a throwaway SQLite database, scratch directories, no AI key
and no network — every external boundary is monkeypatched in the individual
tests. Environment variables are set *before* `app.core.config` is imported.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from typing import Any, Dict, Iterator

_TMP = tempfile.mkdtemp(prefix="jobhunter-test-")

os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DATABASE_URL": f"sqlite:///{os.path.join(_TMP, 'test.db')}",
        "UPLOAD_DIR": os.path.join(_TMP, "uploads"),
        "GENERATED_DIR": os.path.join(_TMP, "generated"),
        "SCREENSHOT_DIR": os.path.join(_TMP, "screenshots"),
        "SECRET_KEY": "test-secret-key-that-is-long-enough-1234567890",
        "ENCRYPTION_KEY": "test-encryption-key-1234567890-abcdefghij",
        "VAULT_KEY": "test-encryption-key-1234567890-abcdefghij",
        "AUTH_REQUIRED": "true",
        "ALLOW_REGISTRATION": "true",
        "AUTO_MIGRATE": "false",          # create_all is faster; migrations have their own test
        "AI_API_KEY": "",
        "LIVE_SCRAPING_ENABLED": "false",  # never touch the network in tests
        "RUN_WORKER_IN_API": "false",
        "EMAIL_SENDING_ENABLED": "false",
        "EMAIL_DRY_RUN": "true",
        "ALLOW_SYNTHETIC_FUNDING_DATA": "false",
        "LOG_LEVEL": "WARNING",
        "ACCESS_LOG": "false",
        "API_RATE_LIMIT_PER_MINUTE": "10000",
        "PUBLIC_BASE_URL": "http://testserver",
        "INCLUDE_DEMO_POOL": "false",
        # Hermetic tests: an operator's checkout-level .env (created by run.sh
        # from .env.example, which ships METRICS_PORT=9464) must not move the
        # metrics endpoint off /api/metrics for the suite.
        "METRICS_PORT": "0",
    }
)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_database() -> Iterator[None]:
    """
    Every test starts from an empty schema.

    Tables are dropped over a raw connection with foreign keys disabled — the
    schema has an intentional cycle (jobs ↔ resumes) which makes
    ``metadata.drop_all`` unusable.
    """
    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        for table in Base.metadata.tables:
            cursor.execute(f'DROP TABLE IF EXISTS "{table}"')
        connection.commit()
        cursor.close()
    finally:
        connection.close()
    Base.metadata.create_all(bind=engine)
    yield



# --------------------------------------------------------------------------- #
# Deterministic AI stand-in
#
# The product now *requires* the model for profile extraction, resume tailoring,
# scoring and outreach. The suite has no provider, so every test gets a
# deterministic stand-in that answers each workflow with schema-valid JSON.
# Tests marked ``real_ai`` (or running with a resolvable API key) bypass it and
# exercise the real gateway.
# --------------------------------------------------------------------------- #
def _stub_profile() -> Dict[str, Any]:
    return {
        "name": "Test Candidate",
        "email": "test.candidate@example.com",
        "phone": "+91 90000 00001",
        "location": "Bangalore, India",
        "current_title": "Senior Software Engineer",
        "summary": ("Backend engineer with 6 years building Python, FastAPI and PostgreSQL services "
                    "for fintech payments platforms serving 2M users."),
        "skills": ["Python", "FastAPI", "PostgreSQL", "Docker", "Kubernetes", "AWS", "React"],
        "experience": [
            {"title": "Senior Software Engineer", "company": "FinCo", "duration": "2021 - present",
             "location": "Bangalore", "bullets": ["Led the payments platform serving 2M users on AWS.",
                                                 "Cut p95 latency by 40% migrating to FastAPI."]},
            {"title": "Software Engineer", "company": "ShopStack", "duration": "2018 - 2021",
             "location": "Bangalore", "bullets": ["Built the order pipeline on AWS handling 500k orders/day."]},
        ],
        "education": [{"degree": "B.Tech Computer Science", "school": "VTU", "year": "2018", "field": "CS"}],
        "projects": [{"name": "Payments SDK", "description": "Open-source SDK with 1.2k stars.", "tech": ["Python"]}],
        "links": ["https://github.com/testcandidate"],
        "languages": ["English", "Hindi"],
        "certifications": ["AWS Solutions Architect"],
    }


def _stub_tailored() -> Dict[str, Any]:
    profile = _stub_profile()
    return {
        "tailored_profile": {
            **{k: profile[k] for k in ("name", "email", "phone", "location", "links",
                                       "languages", "certifications")},
            "current_title": profile["current_title"],
            "summary": ("Senior backend engineer with 6 years on Python, FastAPI and PostgreSQL, "
                        "specialising in payments platforms at scale."),
            "skills": ["Python", "FastAPI", "PostgreSQL", "Kubernetes", "AWS"],
            "experience": profile["experience"],
            "education": profile["education"],
            "projects": profile["projects"],
        },
        "tags": ["backend-python", "fintech", "aws"],
        "reasoning": "Reordered bullets to lead with payments-platform scale.",
    }


def _stub_score() -> Dict[str, Any]:
    return {
        "score": 88,
        "reason": "Strong Python/FastAPI/PostgreSQL overlap with 6 years of relevant payments work.",
        "breakdown": {"skills": 90, "experience": 88, "seniority": 85, "domain": 90,
                      "location": 85, "education": 80},
        "strengths": ["python", "fastapi"],
        "missing_skills": [],
        "evidence": ["6 years building Python, FastAPI, PostgreSQL", "Payments platform at scale"],
        "recommendation": "HIGH PRIORITY",
        "recommendation_reason": "Matches the core stack and domain.",
    }


def _stub_portrait(prompt: str = "") -> Dict[str, Any]:
    """
    A portrait written from the evidence pack in the prompt.

    A real model can only claim what it was shown, and the persona guardrail
    enforces exactly that — so the stand-in has to do the same or it would fail
    the product's own checker (which is the point of the checker).
    """
    import json as _json
    import re as _re

    evidence: Dict[str, Any] = {}
    match = _re.search(r"Evidence pack:\s*(\{.*\})", prompt or "", _re.DOTALL)
    if match:
        try:
            evidence = _json.loads(match.group(1))
        except ValueError:
            evidence = {}

    facts = evidence.get("resume_facts") or {}
    name = str(facts.get("name") or "The candidate")
    skills = [str(v) for v in (facts.get("skills") or [])][:4]
    companies = [str(v) for v in (facts.get("companies") or [])][:2]
    titles = [str(v) for v in (facts.get("titles") or [])][:2]
    target_role = str(evidence.get("target_role") or evidence.get("persona_name") or "this track")
    context = evidence.get("search_context") or {}
    keywords = [str(v) for v in (context.get("keywords") or [])][:3]

    strength_words = " and ".join(skills[:2]) if skills else target_role
    identity = (
        f"{name} is tracked on the {target_role} track. The evidence on file covers "
        f"{', '.join(titles) if titles else 'the recorded roles'}"
        f"{(' at ' + ' and '.join(companies)) if companies else ''}"
        f"{(', working with ' + ', '.join(skills)) if skills else ''}."
    )
    directives = [f"prioritise {target_role} roles"]
    if keywords:
        directives.append(f"prefer roles emphasising {keywords[0]}")
    return {
        "headline": f"{target_role} track: {strength_words}",
        "identity": identity,
        "strengths": ([f"{strength_words} delivery",
                       f"Recorded work as {titles[0]}" if titles else f"{target_role} experience"]
                      if skills else [f"{target_role} experience", "Recorded activity on this track"]),
        "gaps": ["No recorded ownership of frontend delivery"],
        "positioning": f"Position against {target_role} requirements using the recorded evidence only.",
        "search_directives": directives,
        "outreach_angle": (f"{strength_words} work"
                           + (f" at {companies[0]}" if companies else "") + "."),
        "evidence": (skills + companies)[:6],
    }


def _stub_tracks() -> Dict[str, Any]:
    return {"tracks": [
        {"name": "Backend Engineer", "target_role": "Senior Backend Engineer",
         "keywords": ["python backend", "fastapi", "payments"], "industries": ["fintech"],
         "seniority": "senior", "why": "6 years of Python/FastAPI payments work at FinCo."},
        {"name": "Platform Engineer", "target_role": "Platform Engineer",
         "keywords": ["kubernetes", "aws", "docker"], "industries": ["developer tools"],
         "seniority": "senior", "why": "AWS and Kubernetes ownership across both roles."},
    ]}


def stub_ai_response(workflow: str, prompt: str) -> Dict[str, Any]:
    """Schema-valid answer per workflow — keeps every pipeline on the AI path."""
    lowered = (prompt or "").lower()
    if workflow == "parse":
        return _stub_profile()
    if workflow == "resume_gen":
        if "cover letter" in lowered:
            return {"cover_letter": ("Dear Hiring Manager,\n\n"
                                     "Six years of Python, FastAPI and PostgreSQL work on payments "
                                     "platforms at FinCo aligns directly with this role. At FinCo the "
                                     "payments platform serves 2M users, and a FastAPI migration cut "
                                     "p95 latency by 40%.\n\nI would welcome a short call about how "
                                     "that experience maps to your team.\n\nRegards,\nTest Candidate"),
                    "highlights": ["python", "fastapi"]}
        return _stub_tailored()
    if workflow == "scoring":
        return _stub_score()
    if workflow == "email_gen":
        # Company-agnostic on purpose: the guardrail flags any employer the
        # candidate did not work at, so the stand-in must not name one.
        return {"subject": "Senior Backend Engineer role — Python payments",
                "body": ("Hi team,\n\nI saw the Senior Backend Engineer opening and wanted to reach "
                         "out directly. I have spent 6 years building Python, FastAPI and PostgreSQL "
                         "services, most recently the payments platform at FinCo that serves 2M users.\n\n"
                         "A FastAPI migration there cut p95 latency by 40%, which is the kind of work "
                         "this role describes. Would you be open to a 15-minute call this week or next?")}
    if workflow == "persona":
        return _stub_tracks() if '"tracks"' in prompt else _stub_portrait(prompt)
    if workflow == "tagging":
        return {"tags": ["backend-python", "fintech", "aws"]}
    if workflow == "keyword_extract":
        return {"keywords": ["python backend engineer", "fastapi", "payments"],
                "roles": ["Senior Backend Engineer"], "industries": ["fintech"],
                "tech_stack": ["python", "fastapi", "postgresql"], "locations": ["Bangalore"],
                "seniority": "senior", "funding_focus": ["fintech"]}
    if workflow == "classify":
        return {"size": "small", "reason": "stub"}
    if workflow == "form_detect":
        return {"portal_type": "unknown", "fields": [], "ai_confidence": 0.5}
    if workflow == "interview":
        return {"questions": [{"q": "Describe a payments system you scaled.", "category": "system design",
                               "difficulty": "medium"}]}
    if workflow == "company_intel":
        return {"summary": "stub", "industry": "fintech", "size": "small", "tech_stack": ["python"]}
    if workflow == "funding_scan":
        return {"ranked": []}
    return {"ok": True}


@pytest.fixture(autouse=True)
def ai_stub(monkeypatch, request):
    """Deterministic AI answers unless the test drives a real provider."""
    if request.node.get_closest_marker("real_ai"):
        yield False
        return

    import app.services.ai_client as ai_client

    original = ai_client.chat_completion
    calls: list = []

    async def fake_chat_completion(workflow, prompt, *, json_mode=True, **kwargs):
        calls.append({"workflow": workflow, "prompt": prompt})
        if ai_client.is_configured(workflow, db=kwargs.get("db"), user_id=kwargs.get("user_id")):
            return await original(workflow, prompt, json_mode=json_mode, **kwargs)
        payload = stub_ai_response(workflow, prompt)
        if not json_mode:
            return {"content": str(payload), "raw": {}, "usage": {}}
        return payload

    monkeypatch.setattr(ai_client, "chat_completion", fake_chat_completion)
    request.node.stub_ai_calls = calls
    yield True


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Accounts & auth helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def owner(client: TestClient) -> Dict[str, Any]:
    """Create the owner account and return its tokens."""
    response = client.post(
        "/api/auth/bootstrap",
        json={"email": "owner@example.com", "password": "owner-password-123", "name": "Owner"},
    )
    assert response.status_code == 201, response.text
    tokens = response.json()
    tokens["email"] = "owner@example.com"
    tokens["password"] = "owner-password-123"
    return tokens


@pytest.fixture
def auth(owner: Dict[str, Any]) -> Dict[str, str]:
    return {"Authorization": f"Bearer {owner['access_token']}"}


@pytest.fixture
def member(client: TestClient, owner: Dict[str, Any]) -> Dict[str, Any]:
    """Second (non-owner) account for tenancy tests."""
    response = client.post(
        "/api/auth/register",
        json={"email": "member@example.com", "password": "member-password-123", "name": "Member"},
    )
    assert response.status_code == 201, response.text
    tokens = response.json()
    tokens["email"] = "member@example.com"
    return tokens


@pytest.fixture
def member_auth(member: Dict[str, Any]) -> Dict[str, str]:
    return {"Authorization": f"Bearer {member['access_token']}"}


@pytest.fixture
def full_consent(client: TestClient, auth: Dict[str, str]) -> Dict[str, str]:
    response = client.post(
        "/api/account/consent",
        json={"terms": True, "automation": True, "outreach": True, "data_processing": True},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    return auth


# --------------------------------------------------------------------------- #
# Fixtures for domain data
# --------------------------------------------------------------------------- #
def pdf_bytes() -> bytes:
    """A small, real PDF resume used by upload/parse tests."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=LETTER)
    styles = getSampleStyleSheet()
    doc.build(
        [
            Paragraph("Test Candidate", styles["Title"]),
            Paragraph("test.candidate@example.com | +91 90000 00001 | Bangalore, India", styles["Normal"]),
            Paragraph("Summary", styles["Heading2"]),
            Paragraph(
                "Backend engineer with 6 years building Python, FastAPI, PostgreSQL and Kubernetes "
                "services for fintech payments platforms.",
                styles["Normal"],
            ),
            Paragraph("Skills", styles["Heading2"]),
            Paragraph("Python, FastAPI, PostgreSQL, Docker, Kubernetes, AWS, React", styles["Normal"]),
            Paragraph("Experience", styles["Heading2"]),
            Paragraph("Senior Software Engineer - FinCo (2021 - present): payments platform, 2M users.", styles["Normal"]),
            Paragraph("Software Engineer - ShopStack (2018 - 2021): order pipeline on AWS.", styles["Normal"]),
        ]
    )
    return buffer.getvalue()


@pytest.fixture
def uploaded_resume(client: TestClient, auth: Dict[str, str]) -> Dict[str, Any]:
    response = client.post(
        "/api/resume/upload",
        files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def seeded_job(db, owner_tokens=None):
    """A job row created directly in the DB (bypasses discovery/network)."""
    from app.models.models import Job, User

    user = db.query(User).order_by(User.id).first()
    job = Job(
        user_id=user.id,
        title="Senior Backend Engineer",
        company="FinCo",
        location="Remote",
        description="Python, FastAPI, PostgreSQL, Kubernetes, AWS. Payments platform at scale.",
        url="https://jobs.lever.co/finco/abc123",
        source="lever",
        dedupe_key="lever:abc123",
        status="discovered",
        score=80.0,
        score_reason="test fixture",
        company_size="small",
        extra={"forms": {"portal_type": "lever", "requires_login": True, "vault_domain": "jobs.lever.co",
                         "fields": [
                             {"name": "firstName", "label": "First name", "type": "text", "required": True,
                              "options": [], "profile_key": "firstName"},
                             {"name": "email", "label": "Email", "type": "email", "required": True,
                              "options": [], "profile_key": "email"},
                             {"name": "resume", "label": "Resume", "type": "file", "required": True,
                              "options": [], "profile_key": "resume"},
                         ],
                         "ai_confidence": 0.9, "detection_source": "html"}},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job
