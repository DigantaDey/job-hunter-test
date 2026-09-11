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
