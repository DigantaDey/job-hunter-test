"""
Test configuration.

Hermetic by design: a throwaway SQLite database, scratch directories, no AI key
and no network — every external boundary is monkeypatched in the individual
tests. Environment variables are set *before* `app.core.config` is imported.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
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
    ``metadata.drop_all`` unusable. SQLite disables its FK enforcement with a
    PRAGMA; PostgreSQL (no PRAGMAs) gets ``DROP TABLE ... CASCADE``, which
    resolves the cycle the same way.
    """
    is_sqlite = engine.dialect.name == "sqlite"
    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        if is_sqlite:
            cursor.execute("PRAGMA foreign_keys=OFF")
        suffix = "" if is_sqlite else " CASCADE"
        for table in Base.metadata.tables:
            cursor.execute(f'DROP TABLE IF EXISTS "{table}"{suffix}')
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
        if "funding-extract-v1" in (prompt or ""):
            return _stub_funding_extract(prompt)
        return _stub_funding_scan(prompt)
    return {"ok": True}


def _stub_funding_scan(prompt: str) -> Dict[str, Any]:
    """A contract-compliant relevance verdict for the funding radar.

    The stand-in must obey the same rules a real model is given: only company
    names that appear in the prompt (the grounding guardrail rejects anything
    else), and a one-sentence ``why`` that cites the event's own fields. Marking
    everything relevant keeps the deterministic suites on the AI path — the
    funding tests script their own verdicts when they need a specific split.
    """
    import json as _json
    import re as _re

    events: Any = []
    match = _re.search(r"Funding events:\s*(\[.*?\])\s*\nReturn JSON only", prompt or "", _re.DOTALL)
    if match:
        try:
            events = _json.loads(match.group(1))
        except ValueError:
            events = []
    companies: list = []
    for index, event in enumerate(events if isinstance(events, list) else [], start=1):
        if not isinstance(event, dict) or not event.get("name"):
            continue
        stage = str(event.get("stage") or "Undisclosed")
        industry = str(event.get("industry") or "").strip()
        companies.append({
            "name": event["name"],
            "matched": True,
            "rank": index,
            "why": (f"{stage} round{f' in {industry}' if industry else ''} — the event's own "
                    f"fields match the recorded focus."),
        })
    return {"companies": companies}


def _stub_company_name(title: str) -> str:
    """Company name from a headline like ``"VectorLoom AI raises $12M Series A"``.

    Deterministic and deliberately simple: the part of the title before the
    round verb, capped at four words. Every name it produces is a substring of
    the title, so the extraction guardrail (name must be grounded in the cited
    result's text) accepts it — the stand-in obeys the product's own rules.
    """
    import re as _re

    text = " ".join(str(title or "").split())
    cut = _re.split(r"\s+(?:raises|raised|secures|secured|announces|announced|closes|closed)\b",
                    text, maxsplit=1, flags=_re.IGNORECASE)[0]
    return " ".join(cut.split()[:4]).strip(" ,-–—:;.")


def _stub_funding_extract(prompt: str) -> Dict[str, Any]:
    """A contract-compliant extraction from the search results in the prompt.

    Mirrors ``_stub_funding_scan`` for the v2.2.6 search path: one company per
    cited result, ``source_index`` always in range, and the name taken from the
    result's own title so the grounding check accepts it. Stage/amount come
    from the title text when present (the same fields a real model would
    parse); the date is left to the cited row's ``published_at``.
    """
    import json as _json
    import re as _re

    results: Any = []
    match = _re.search(r"Search results:\s*(\[.*\])\s*\nReturn JSON only", prompt or "", _re.DOTALL)
    if match:
        try:
            results = _json.loads(match.group(1))
        except ValueError:
            results = []
    companies: list = []
    for index, row in enumerate(results if isinstance(results, list) else []):
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "")
        name = _stub_company_name(title)
        if not name:
            continue
        stage_match = _re.search(r"\b(Series [ABCD]|Seed)\b", title, _re.IGNORECASE)
        amount_match = _re.search(r"\$([\d.,]+)\s*(B|M|K)?\b", title)
        raised_usd = None
        if amount_match:
            try:
                value = float(amount_match.group(1).replace(",", ""))
                unit = (amount_match.group(2) or "M").upper()
                raised_usd = value * {"B": 1e9, "M": 1e6, "K": 1e3}[unit]
            except (ValueError, KeyError):
                raised_usd = None
        companies.append({
            "name": name,
            "source_index": index,
            "stage": stage_match.group(1).title() if stage_match else "Undisclosed",
            "raised_usd": raised_usd,
            "raised_at": None,
            "industry": "",
        })
    return {"companies": companies}


# --------------------------------------------------------------------------- #
# Scripted OpenAI-compatible provider (v2.1 pause/resume + token-budget suites)
#
# A local HTTP server that emulates provider wire behaviour: normal JSON
# answers per workflow, latency, and — crucially — a mid-run outage. After
# ``behavior["fail_after"]`` good responses it starts returning
# ``behavior["status"]``, so a test can watch a pipeline get partway through,
# hit an outage, recover, and finish without duplicates. No outbound network.
# --------------------------------------------------------------------------- #
class ScriptedAIHandler(BaseHTTPRequestHandler):
    behavior: Dict[str, Any] = {}

    def log_message(self, *a):  # silence
        pass

    def _send(self, status: int, obj: Any) -> None:
        body = json.dumps(obj).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client already gave up (timeout) — like a real provider

    @classmethod
    def _outage_active(cls) -> bool:
        """Is the scripted provider currently down?

        A real outage takes the *whole* provider down — both the chat
        endpoint and the /models probe — so the availability signal (which
        probes /models) must flip too. ``fail_after=N`` means "serve N good
        responses, then go down"; the counter is shared by GET and POST.
        """
        behavior = cls.behavior
        fail_after = behavior.get("fail_after")
        return fail_after is not None and behavior.get("good", 0) >= fail_after

    def _outage_response(self) -> None:
        self._send(int(self.behavior.get("status") or 503),
                   {"error": {"message": "scripted provider outage"}})

    def do_GET(self):
        # The /models probe — green only while the provider is up.
        if self._outage_active():
            self._outage_response()
            return
        self._send(200, {"data": [{"id": "scripted-model"}]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request_body = json.loads(self.rfile.read(length) or b"{}")
        behavior = self.behavior
        behavior["requests"].append(request_body)
        delay = behavior.get("delay") or 0.0
        if delay:
            time.sleep(delay)
        if self._outage_active():
            # Mid-run outage: the first N responses were fine, the provider
            # is down from here on (until the test flips the behaviour back).
            self._outage_response()
            return
        behavior["good"] = behavior.get("good", 0) + 1
        # One-off raw responses (tests of truncated/abnormal model answers).
        override = behavior.get("override")
        if isinstance(override, list) and override:
            self._send(200, override.pop(0))
            return
        messages = request_body.get("messages") or []
        text = " ".join(str(m.get("content") or "") for m in messages)
        self._send(200, {
            "choices": [{"message": {"content": json.dumps(_scripted_answer(text))},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 200,
                      "total_tokens": 700},
        })


def _scripted_answer(prompt_text: str) -> Any:
    """Schema-valid answer per workflow, detected from the prompt itself.

    Reuses the deterministic stand-in's payloads so a recovery completes the
    pipeline exactly like the stub would — only the wire path is real.
    """
    lowered = (prompt_text or "").lower()
    # Checked first: the funding prompts carry contract markers, and no other
    # workflow's detector may claim them (their event text can contain any word).
    if "funding-extract-v1" in lowered:
        return stub_ai_response("funding_scan", prompt_text)  # extraction shares the workflow
    if "funding-scan-v2" in lowered:
        return stub_ai_response("funding_scan", prompt_text)
    if "extract a complete, structured profile" in lowered:
        return stub_ai_response("parse", prompt_text)
    if "score how well this candidate" in lowered:
        return stub_ai_response("scoring", prompt_text)
    if "classify company size" in lowered:
        return {"size": "small", "confidence": 0.9, "reason": "scripted"}
    if "write a short cold email" in lowered:
        return stub_ai_response("email_gen", prompt_text)
    if "create 3-5 short, lowercase, hyphenated tags" in lowered:
        return stub_ai_response("tagging", prompt_text)
    if "cover letter" in lowered:
        return stub_ai_response("resume_gen", prompt_text)
    if "evidence pack" in lowered or "tracks" in lowered:
        return stub_ai_response("persona", prompt_text)
    if "keywords" in lowered and "respond only with json" in lowered:
        return stub_ai_response("keyword_extract", prompt_text)
    if "interview questions" in lowered:
        return stub_ai_response("interview", prompt_text)
    if "form" in lowered and "json" in lowered:
        return stub_ai_response("form_detect", prompt_text)
    return {"ok": True}


@pytest.fixture()
def ai_provider():
    """Start a local scripted provider; yield its base URL.

    Point a user at it with ``PUT /api/settings`` (``ai.base_url`` +
    ``api_key`` + ``model``). Then the ``ai_stub`` stand-in detects the key
    and hands the call to the REAL gateway → real wire → this server.

    ``ScriptedAIHandler.behavior`` (reset per test):
      * ``status`` (int)    — HTTP status served during the outage
      * ``fail_after`` (int)— after this many good responses, go down
      * ``delay`` (float)   — seconds of latency per request
      * ``requests``        — every wire request body (ground truth)
      * ``good``            — how many good responses were served
    """
    ScriptedAIHandler.behavior = {"requests": [], "good": 0, "status": 503,
                                  "fail_after": None, "delay": 0.0,
                                  "override": []}
    server = HTTPServer(("127.0.0.1", 0), ScriptedAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture()
def provider_owner(client: TestClient, auth: Dict[str, str], ai_provider: str) -> str:
    """The owner's AI settings point at the scripted provider.

    Every AI call for the owner now goes through the real gateway to the
    local scripted server — the hermetic ``real_ai`` convention.
    """
    saved = client.put("/api/settings", json={
        "ai": {"base_url": ai_provider, "model": "scripted-model",
               "api_key": "sk-scripted-test-key", "rpm": 600},
    }, headers=auth)
    assert saved.status_code == 200, saved.text
    return ai_provider


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


# --------------------------------------------------------------------------- #
# v2.2.8 — queued AI triggers
#
# ``POST /api/resumes/generate`` and ``POST /api/emails/generate`` answer
# ``{queued, pipeline_job_id}`` and the work runs in the durable queue. These
# helpers do what the SPA does — click, let the worker run the item, read the
# row back — so tests can keep asserting on the *outcome* in one call.
# --------------------------------------------------------------------------- #
def run_queue_item(item_id: int, pipeline: str) -> None:
    """Execute one queued item exactly the way the worker does."""
    import asyncio

    from app.worker import Worker

    asyncio.run(Worker(pipelines=[pipeline])._run_item(item_id, pipeline))


def queued_result(client: TestClient, headers: Dict[str, str], receipt: Dict[str, Any], pipeline: str) -> Dict[str, Any]:
    """Run the queued item behind ``receipt`` and return the finished row (with ``result``)."""
    assert receipt.get("pipeline_job_id"), receipt
    run_queue_item(int(receipt["pipeline_job_id"]), pipeline)
    row = client.get(f"/api/pipelines/jobs/{receipt['pipeline_job_id']}", headers=headers)
    assert row.status_code == 200, row.text
    return row.json()


def generate_resume_now(client: TestClient, headers: Dict[str, str], job_id: int, **params: Any) -> Dict[str, Any]:
    """Click "Generate tailored resume", run the worker, return the handler's result.

    The result is the old synchronous response (``resume_id``, ``fact_guard``,
    ``guardrail``, ``files`` …) or ``{"status": "rejected", ...}``.
    """
    response = client.post("/api/resumes/generate", params={"job_id": job_id, **params}, headers=headers)
    assert response.status_code == 200, response.text
    row = queued_result(client, headers, response.json(), "ai")
    result = row.get("result") or {}
    result.setdefault("status", row["status"])
    result["_row"] = row
    return result


def draft_email_now(client: TestClient, headers: Dict[str, str], body: Dict[str, Any]) -> Dict[str, Any]:
    """Click "Cold email", run the worker, return the bucket card for the draft.

    Mirrors the old synchronous response: ``{"email": <bucket row>, ...}``;
    a rejected draft returns ``{"email": None, "rejected": <result>}``.
    """
    response = client.post("/api/emails/generate", json=body, headers=headers)
    assert response.status_code == 200, response.text
    row = queued_result(client, headers, response.json(), "email")
    result = row.get("result") or {}
    if not result.get("email_id"):
        return {"email": None, "rejected": result, "_row": row}
    card = client.get(f"/api/emails/{result['email_id']}", headers=headers)
    assert card.status_code == 200, card.text
    return {"email": card.json(), "result": result, "_row": row}
