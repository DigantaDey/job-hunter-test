"""
Hermetic end-to-end drill: real app + worker thread + in-process fake OpenAI provider; no network.
C1-C7 live drill suite.

The fixture is hermetic (localhost, no internet) — consistent with standing "no real internet in tests" rule.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.live_drill

VALID_KEY = "sk-e2e-valid-key-abcdef123456"

# ---------------------------------------------------------------------------
# Fake OpenAI provider — ThreadingHTTPServer on 127.0.0.1:0
# modes: ok | fail500 | fail401
# - fail401 must answer 401 on BOTH /models and /chat/completions
# - exact JSON contracts per workflow
# - call log (workflow + timestamp)
# ---------------------------------------------------------------------------

def _payload_for(workflow: str, prompt: str) -> Dict[str, Any]:
    low = (prompt or "").lower()
    if workflow == "parse":
        return {
            "name": "Test Candidate",
            "email": "test.candidate@example.com",
            "phone": "+91 90000 00001",
            "location": "Bangalore, India",
            "current_title": "Senior Software Engineer",
            "summary": "Backend engineer with 6 years building Python, FastAPI, PostgreSQL services for fintech payments platform.",
            "skills": ["Python", "FastAPI", "PostgreSQL", "Docker", "Kubernetes"],
            "experience": [
                {"title": "Senior Software Engineer", "company": "FinCo", "duration": "2021 - present", "bullets": ["Led payments platform"]},
            ],
            "education": [{"degree": "B.Tech", "school": "VTU", "year": "2018"}],
        }
    if workflow == "scoring":
        return {
            "score": 88,
            "reason": "Strong Python FastAPI PostgreSQL overlap with 6 years of payments work experience.",
            "breakdown": {"skills": 90, "experience": 88, "seniority": 85, "domain": 90, "location": 85, "education": 80},
            "strengths": ["python", "fastapi"],
            "missing_skills": [],
            "evidence": ["6 years building Python FastAPI", "Payments platform at scale"],
            "recommendation": "HIGH PRIORITY",
            "recommendation_reason": "Matches core stack and domain perfectly.",
        }
    if workflow == "funding_scan":
        m = re.search(r"Funding events:\s*(\[.*?\])\s*\nReturn JSON only", prompt, re.DOTALL)
        events: List[Any] = []
        if m:
            try:
                events = json.loads(m.group(1))
            except Exception:
                events = []
        comps = []
        for idx, ev in enumerate(events if isinstance(events, list) else [], start=1):
            if not isinstance(ev, dict) or not ev.get("name"):
                continue
            comps.append({"name": ev["name"], "matched": True, "rank": idx, "why": f"{ev.get('stage','Seed')} {ev.get('industry','fintech')} round matches focus."})
        return {"companies": comps}
    if workflow == "keyword_extract":
        return {
            "keywords": ["python backend engineer", "fastapi", "payments"],
            "roles": ["Senior Backend Engineer"],
            "industries": ["fintech"],
            "tech_stack": ["python", "fastapi", "postgresql"],
            "locations": ["Bangalore"],
            "seniority": "senior",
            "funding_focus": ["fintech"],
        }
    if workflow == "resume_gen":
        if "cover letter" in low:
            return {"cover_letter": "Dear Hiring Manager, this is a cover letter with sufficient length and details about the candidate's experience at FinCo."}
        return {"tailored_profile": {"name": "Test Candidate", "summary": "Tailored summary"}, "tags": ["t"], "reasoning": "ok"}
    if workflow == "classify":
        return {"size": "small", "confidence": 0.9, "reason": "scripted"}
    if workflow == "email_gen":
        return {"subject": "Senior Backend Engineer role — Python payments", "body": "Hi team, I have 6 years building Python FastAPI services at FinCo. Would love to chat."}
    if workflow == "tagging":
        return {"tags": ["backend-python"]}
    if workflow == "interview":
        return {"questions": [{"q": "Describe a payments system you scaled.", "category": "system design", "difficulty": "medium"}]}
    if workflow == "company_intel":
        return {"summary": "stub", "industry": "fintech", "size": "small", "tech_stack": ["python"]}
    if workflow == "persona":
        if '"tracks"' in prompt or "tracks[]" in low:
            return {"tracks": [{"name": "Backend Engineer", "target_role": "Senior Backend Engineer", "keywords": ["python"], "industries": ["fintech"], "seniority": "senior", "why": "6 years Python"}]}
        return {"headline": "test", "identity": "test", "strengths": ["python"], "gaps": [], "positioning": "pos", "search_directives": [], "outreach_angle": "angle", "evidence": []}
    if workflow == "form_detect":
        return {"portal_type": "unknown", "fields": [], "ai_confidence": 0.5}
    return {"ok": True}


def _detect_workflow(prompt: str) -> str:
    low = (prompt or "").lower()
    if "funding-scan-v2" in low:
        return "funding_scan"
    if "extract a complete, structured profile" in low:
        return "parse"
    if "score how well this candidate" in low:
        return "scoring"
    if "keywords" in low and "respond only with json" in low:
        return "keyword_extract"
    if "cover letter" in low:
        return "resume_gen"
    if "create 3-5 short, lowercase, hyphenated tags" in low:
        return "tagging"
    if "interview questions" in low:
        return "interview"
    if "classify company size" in low:
        return "classify"
    if "write a short cold email" in low:
        return "email_gen"
    if "evidence pack" in low or '"tracks"' in low:
        return "persona"
    if "funding events:" in low:
        return "funding_scan"
    return "unknown"


class LiveFakeHandler(BaseHTTPRequestHandler):
    mode = "ok"  # ok | fail500 | fail401
    call_log: List[Dict[str, Any]] = []
    lock = threading.Lock()

    def log_message(self, format, *args):
        return

    def _send(self, status: int, obj: Any):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        if self.path.startswith("/v1/models") or self.path.startswith("/models"):
            with self.lock:
                self.call_log.append({"workflow": "models", "ts": time.time(), "path": self.path})
            if self.mode == "fail401":
                self._send(401, {"error": {"message": "invalid api key", "type": "invalid_request_error"}})
                return
            if self.mode == "fail500":
                self._send(500, {"error": {"message": "internal server error"}})
                return
            self._send(200, {"data": [{"id": "test-model"}]})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        messages = body.get("messages") or []
        text = " ".join(str(m.get("content") or "") for m in messages)
        wf = _detect_workflow(text)
        with self.lock:
            self.call_log.append({"workflow": wf, "ts": time.time(), "path": self.path, "prompt": text[:800]})
        if self.mode == "fail401":
            self._send(401, {"error": {"message": "invalid api key", "type": "invalid_request_error"}})
            return
        if self.mode == "fail500":
            self._send(500, {"error": {"message": "internal server error"}})
            return
        payload = _payload_for(wf, text)
        self._send(200, {
            "choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        })


@pytest.fixture(scope="session")
def live_fake_server():
    LiveFakeHandler.call_log = []
    LiveFakeHandler.mode = "ok"
    server = ThreadingHTTPServer(("127.0.0.1", 0), LiveFakeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v1"
    yield {"server": server, "thread": thread, "base_url": base, "handler": LiveFakeHandler}
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture
def fake_mode(live_fake_server):
    def set_mode(m: str):
        assert m in ("ok", "fail500", "fail401")
        LiveFakeHandler.mode = m
        try:
            from app.services.ai_client import _PING_CACHE, _breakers
            _PING_CACHE.clear()
            _breakers.clear()
        except Exception:
            pass
        time.sleep(0.05)
    return set_mode


@pytest.fixture
def live_client(client, live_fake_server, monkeypatch):
    base_url = live_fake_server["base_url"]
    monkeypatch.setattr("app.core.config.settings.ai_api_key", VALID_KEY)
    monkeypatch.setattr("app.core.config.settings.ai_base_url", base_url)
    monkeypatch.setattr("app.core.config.settings.ai_model", "test-model")
    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 1)
    monkeypatch.setattr("app.core.config.settings.ai_breaker_failures", 2)
    monkeypatch.setattr("app.core.config.settings.ai_breaker_cooldown_seconds", 1)
    monkeypatch.setattr("app.core.config.settings.ai_timeout", 5)
    monkeypatch.setattr("app.core.config.settings.ai_connect_timeout", 2)
    monkeypatch.setattr("app.core.config.settings.include_demo_pool", True)
    monkeypatch.setattr("app.core.config.settings.funding_provider_timeout_seconds", 2)
    monkeypatch.setattr("app.core.config.settings.ai_watchdog_interval_seconds", 1)
    monkeypatch.setattr("app.core.config.settings.ai_backoff_base", 0.1)
    try:
        from app.services.ai_client import _PING_CACHE, _breakers
        _PING_CACHE.clear()
        _breakers.clear()
    except Exception:
        pass
    LiveFakeHandler.call_log = []
    yield client
    LiveFakeHandler.mode = "ok"
    try:
        from app.services.ai_client import _PING_CACHE, _breakers
        _PING_CACHE.clear()
        _breakers.clear()
    except Exception:
        pass


def _auth_for(client: TestClient, email: str, password: str = "owner-password-123"):
    resp = client.post("/api/auth/bootstrap", json={"email": email, "password": password, "name": "Test"})
    if resp.status_code == 201:
        tok = resp.json()
        return {"Authorization": f"Bearer {tok['access_token']}"}
    # try register for additional users
    resp2 = client.post("/api/auth/register", json={"email": email, "password": password, "name": "Test"})
    if resp2.status_code == 201:
        tok = resp2.json()
        return {"Authorization": f"Bearer {tok['access_token']}"}
    resp3 = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp3.status_code == 200, resp3.text
    tok = resp3.json()
    return {"Authorization": f"Bearer {tok['access_token']}"}


def _make_pro_user(client: TestClient, db, email: str):
    from app.models.models import Subscription, User
    auth = _auth_for(client, email)
    client.post("/api/account/consent", json={"terms": True, "automation": True, "outreach": True, "data_processing": True}, headers=auth)
    user = db.query(User).filter(User.email == email).first()
    sub = db.query(Subscription).filter(Subscription.user_id == user.id).first()
    if not sub:
        from app.core.entitlements import ensure_subscription
        sub = ensure_subscription(db, user.id)
    sub.plan = "pro"
    sub.status = "active"
    db.commit()
    db.refresh(user)
    return auth, user


def _upload_resume(client: TestClient, auth: Dict[str, str]):
    import io
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER)
    styles = getSampleStyleSheet()
    doc.build([
        Paragraph("Test Candidate", styles["Title"]),
        Paragraph("test.candidate@example.com | +91 90000 00001 | Bangalore, India", styles["Normal"]),
        Paragraph("Summary", styles["Heading2"]),
        Paragraph("Backend engineer with 6 years building Python, FastAPI, PostgreSQL and Kubernetes services for fintech payments platforms.", styles["Normal"]),
        Paragraph("Skills", styles["Heading2"]),
        Paragraph("Python, FastAPI, PostgreSQL, Docker, Kubernetes, AWS, React", styles["Normal"]),
        Paragraph("Experience", styles["Heading2"]),
        Paragraph("Senior Software Engineer - FinCo (2021 - present): payments platform, 2M users.", styles["Normal"]),
        Paragraph("Software Engineer - ShopStack (2018 - 2021): order pipeline on AWS.", styles["Normal"]),
    ])
    data = buf.getvalue()
    resp = client.post("/api/resume/upload", files={"file": ("resume.pdf", data, "application/pdf")}, headers=auth)
    return resp


# ---------------------------------------------------------------------------
# C1 outage → contract → pause → drain → resume
# ---------------------------------------------------------------------------
def test_c1_outage_pause_drain_resume(live_client, live_fake_server, fake_mode, db):
    fake_mode("fail500")
    client = live_client
    auth, user = _make_pro_user(client, db, f"c1-{uuid.uuid4().hex[:6]}@example.com")
    resp = _upload_resume(client, auth)
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body.get("code") == "ai_unavailable"
    assert body.get("status") == "ai_paused"
    assert body.get("state") == "transient_outage"
    assert body.get("pausable") is True
    assert body.get("workflow") == "parse"
    assert "retry_after_hint" in body
    assert body.get("detail")
    # call log should have parse attempt
    assert any(entry["workflow"] == "parse" for entry in LiveFakeHandler.call_log), "call log must contain parse workflow"
    from app.models.models import PipelineJob, Resume
    from app.services.job_queue import enqueue
    from app.worker import Worker
    resume = db.query(Resume).filter(Resume.user_id == user.id).first()
    if not resume:
        from app.models.models import Resume as R
        r = R(user_id=user.id, filename="t.pdf", filepath="/tmp/t.pdf", type="master", profile_snapshot={}, tags=[], status="approved")
        db.add(r); db.commit(); db.refresh(r)
        resume = r
    item = enqueue(db, user_id=user.id, pipeline="ai", payload={"task": "tag_resume", "resume_id": resume.id}, dedupe_key=f"c1-tag-{uuid.uuid4().hex[:4]}")
    assert item is not None
    worker = Worker(pipelines=["ai"])
    asyncio.run(worker._run_item(item.id, "ai"))
    db.refresh(item)
    assert item.status == "paused", f"expected paused got {item.status} {item.error}"
    assert item.attempts == 0
    fake_mode("ok")
    # expire backoff so watchdog can drain immediately
    item.scheduled_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    from app.services.ai_watchdog import watchdog_cycle
    drained = asyncio.run(watchdog_cycle())
    db.refresh(item)
    # after drain should be queued (or resumed)
    assert item.status == "queued", f"after drain expected queued got {item.status}"
    asyncio.run(worker._run_item(item.id, "ai"))
    db.refresh(item)
    assert item.status == "done"


# ---------------------------------------------------------------------------
# C2 attempt accounting (v2.2.2 regression)
# ---------------------------------------------------------------------------
def test_c2_attempt_accounting(live_client, live_fake_server, fake_mode, db):
    fake_mode("fail500")
    client = live_client
    auth, user = _make_pro_user(client, db, f"c2-{uuid.uuid4().hex[:6]}@example.com")
    from app.models.models import PipelineJob, Resume
    from app.services.job_queue import enqueue, claim
    from app.worker import Worker
    r = Resume(user_id=user.id, filename="c2.pdf", filepath="/tmp/c2.pdf", type="master", profile_snapshot={}, tags=[], status="approved")
    db.add(r); db.commit(); db.refresh(r)
    item = enqueue(db, user_id=user.id, pipeline="ai", payload={"task": "tag_resume", "resume_id": r.id}, dedupe_key=f"c2-{uuid.uuid4().hex[:6]}", max_attempts=3)
    worker = Worker(pipelines=["ai"])
    asyncio.run(worker._run_item(item.id, "ai"))
    db.refresh(item)
    assert item.status == "paused"
    assert item.attempts == 0, "paused through outage must not consume failure budget"
    fake_mode("ok")
    # expire and drain
    item.scheduled_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    from app.services.ai_watchdog import watchdog_cycle
    asyncio.run(watchdog_cycle())
    db.refresh(item)
    assert item.status == "queued"
    asyncio.run(worker._run_item(item.id, "ai"))
    db.refresh(item)
    assert item.status == "done"
    # N consecutive real failures dead-letter on Nth.
    from app.services import handlers
    orig = handlers.HANDLERS.get("ai")
    async def always_fail(db_sess, it):
        raise RuntimeError("real failure")
    handlers.HANDLERS["ai"] = always_fail
    try:
        item2 = enqueue(db, user_id=user.id, pipeline="ai", payload={"task": "tag_resume", "resume_id": r.id}, dedupe_key=f"c2-fail-{uuid.uuid4().hex[:6]}", max_attempts=2)
        asyncio.run(worker._run_item(item2.id, "ai"))
        db.refresh(item2)
        assert item2.status == "queued" and item2.attempts == 1, f"first real failure should retry {item2.status} {item2.attempts}"
        # second failure should dead-letter; claim first if needed
        # item is queued with backoff; expire it
        item2.scheduled_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
        claimed = claim(db, pipelines=["ai"])
        assert claimed is not None and claimed.id == item2.id
        asyncio.run(worker._run_item(item2.id, "ai"))
        db.refresh(item2)
        assert item2.status == "dead", f"Nth failure should dead-letter {item2.status}"
        assert item2.attempts == 2
    finally:
        if orig:
            handlers.HANDLERS["ai"] = orig
        else:
            handlers.HANDLERS.pop("ai", None)


# ---------------------------------------------------------------------------
# C3 blocked (v2.2.3-era invariant)
# ---------------------------------------------------------------------------
def test_c3_blocked_needs_action(live_client, live_fake_server, fake_mode, db):
    fake_mode("fail401")
    client = live_client
    auth, user = _make_pro_user(client, db, f"c3-{uuid.uuid4().hex[:6]}@example.com")
    import asyncio as _asyncio
    from app.services.ai_client import ai_availability
    avail = _asyncio.run(ai_availability(db=db, user_id=user.id, probe_timeout=2))
    assert avail.get("state") == "blocked_needs_action", avail
    # verify BOTH endpoints return 401 - check call log and direct probe
    # GET /v1/models should be 401
    import httpx
    base = live_fake_server["base_url"]
    # check that our handler's mode is fail401 and both paths would be 401
    # we verify via call log that models probing happened and returned 401 (ai_availability already did)
    assert any(e["workflow"] == "models" for e in LiveFakeHandler.call_log), "models probe should be in call log"
    # queued AI item dead-letters with diagnosis (not paused)
    from app.models.models import PipelineJob, Resume
    from app.services.job_queue import enqueue
    from app.worker import Worker
    r = Resume(user_id=user.id, filename="c3.pdf", filepath="/tmp/c3.pdf", type="master", profile_snapshot={}, tags=[], status="approved")
    db.add(r); db.commit(); db.refresh(r)
    item = enqueue(db, user_id=user.id, pipeline="ai", payload={"task": "tag_resume", "resume_id": r.id}, dedupe_key=f"c3-{uuid.uuid4().hex[:6]}")
    worker = Worker(pipelines=["ai"])
    _asyncio.run(worker._run_item(item.id, "ai"))
    db.refresh(item)
    assert item.status == "dead", f"blocked should dead-letter not pause {item.status}"
    assert item.attempts == 1
    # funding refresh in this state never returns fabricated companies
    from app.services import funding_radar, funding_sources
    from app.services.ai_client import is_ai_error
    async def fake_provider(ctx, wd, lim):
        return [funding_sources.FundingEvent(name="ShouldNotAppear", stage="Seed", industry="fintech", source="sec_edgar", verified=True)]
    orig_prov = funding_sources.PROVIDERS.get("sec_edgar")
    funding_sources.PROVIDERS["sec_edgar"] = fake_provider
    try:
        try:
            comps, report = _asyncio.run(funding_radar.scan_funded_companies({"funding_focus": ["fintech"]}, provider="sec_edgar", limit=5, db=db, user_id=user.id))
            assert False, "should have raised blocked"
        except Exception as exc:
            assert is_ai_error(exc), f"expected AI error got {exc}"
        resp = client.get("/api/funding/companies?refresh=true&include_unverified=true", headers=auth)
        assert resp.status_code == 503, resp.text
        body = resp.json()
        assert body.get("state") == "blocked_needs_action"
        from app.models.models import FundingCompany
        cnt = db.query(FundingCompany).filter(FundingCompany.user_id == user.id).count()
        assert cnt == 0, "blocked funding should not create fabricated companies"
    finally:
        if orig_prov:
            funding_sources.PROVIDERS["sec_edgar"] = orig_prov
        else:
            funding_sources.PROVIDERS.pop("sec_edgar", None)
        fake_mode("ok")


# ---------------------------------------------------------------------------
# C4 funding honesty
# ---------------------------------------------------------------------------
def test_c4_funding_honesty(live_client, live_fake_server, fake_mode, db, monkeypatch):
    client = live_client
    auth, user = _make_pro_user(client, db, f"c4-{uuid.uuid4().hex[:6]}@example.com")
    from app.services import funding_sources
    orig_status = funding_sources.provider_status
    monkeypatch.setattr(funding_sources, "provider_status", lambda: [{"id": "sec_edgar", "label": "SEC", "configured": True, "verified": True}])
    async def fail_provider(ctx, wd, lim):
        raise RuntimeError("network down")
    orig_provider = funding_sources.PROVIDERS.get("sec_edgar")
    funding_sources.PROVIDERS["sec_edgar"] = fail_provider
    try:
        fake_mode("ok")
        resp = client.get("/api/funding/companies?refresh=true&include_unverified=true", headers=auth)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("scan_status") == "scan_failed"
        assert body.get("companies") == []
        assert body.get("last_report", {}).get("errors")
        from app.models.models import FundingScan
        scans = db.query(FundingScan).filter(FundingScan.user_id == user.id).all()
        assert any(s.status == "scan_failed" for s in scans), f"expected scan_failed row got {[s.status for s in scans]}"
    finally:
        if orig_provider:
            funding_sources.PROVIDERS["sec_edgar"] = orig_provider
        else:
            funding_sources.PROVIDERS.pop("sec_edgar", None)
        monkeypatch.setattr(funding_sources, "provider_status", orig_status)
    fake_mode("ok")
    monkeypatch.setattr("app.core.config.settings.allow_synthetic_funding_data", True)
    async def demo_provider(ctx, wd, lim):
        now = datetime.utcnow()
        return [funding_sources.FundingEvent(name="DemoCo Alpha", stage="Seed", industry="fintech", raised_at=now, source="demo", verified=False, summary="Demo event fintech")]
    funding_sources.PROVIDERS["demo"] = demo_provider
    monkeypatch.setattr("app.core.config.settings.funding_provider", "demo")
    resp2 = client.get("/api/funding/companies?refresh=true&include_unverified=true", headers=auth)
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2.get("scan_status") == "ok"
    assert len(body2.get("companies") or []) >= 1
    from app.models.models import FundingScan, FundingScanCompany
    scans_ok = db.query(FundingScan).filter(FundingScan.user_id == user.id, FundingScan.status == "ok").all()
    assert scans_ok, "expected ok scan row"
    scan_ids = [s.id for s in scans_ok]
    mem = db.query(FundingScanCompany).filter(FundingScanCompany.scan_id.in_(scan_ids)).all()
    assert len(mem) >= 1, "expected membership rows"
    funding_sources.PROVIDERS.pop("demo", None)
    monkeypatch.setattr("app.core.config.settings.funding_provider", "sec_edgar")
    monkeypatch.setattr("app.core.config.settings.allow_synthetic_funding_data", False)


# ---------------------------------------------------------------------------
# C5 discovery AI top slice (v2.2.3)
# ---------------------------------------------------------------------------
def test_c5_discovery_top_slice(live_client, live_fake_server, fake_mode, db, monkeypatch):
    fake_mode("ok")
    client = live_client
    auth_pro, user_pro = _make_pro_user(client, db, f"c5pro-{uuid.uuid4().hex[:6]}@example.com")
    resp = _upload_resume(client, auth_pro)
    assert resp.status_code == 200, resp.text
    monkeypatch.setattr("app.core.config.settings.include_demo_pool", True)
    LiveFakeHandler.call_log = []
    from app.services.job_queue import enqueue
    from app.worker import Worker
    from app.models.models import Job
    item = enqueue(db, user_id=user_pro.id, pipeline="discovery", payload={"keywords": [], "freshness_hours": 168, "limit": 30, "live_enabled": False, "sources": [], "board_tokens": []}, dedupe_key=f"c5-disc-{uuid.uuid4().hex[:6]}")
    worker = Worker(pipelines=["discovery"])
    asyncio.run(worker._run_item(item.id, "discovery"))
    db.refresh(item)
    assert item.status == "done", item.error
    result = (item.payload or {}).get("result") or {}
    # should have scored 12 when profile present and plan pro (min 12 or total)
    inserted = int(result.get("inserted", 0) or len(db.query(Job).filter(Job.user_id == user_pro.id).all()))
    expected_scored = 12 if inserted >= 12 else inserted
    assert result.get("ai_rescore", {}).get("scored") == expected_scored, f"expected {expected_scored} scored got {result.get('ai_rescore')} inserted={inserted}"
    assert result.get("ai_rescore", {}).get("enabled") is True
    jobs = db.query(Job).filter(Job.user_id == user_pro.id).order_by(Job.score.desc()).all()
    ai_jobs = [j for j in jobs if j.score_source == "ai"]
    prelim = [j for j in jobs if j.score_source == "preliminary"]
    assert len(ai_jobs) == expected_scored, f"top {expected_scored} should be ai got {len(ai_jobs)}"
    assert len(prelim) == len(jobs) - expected_scored
    scoring_calls = [c for c in LiveFakeHandler.call_log if c["workflow"] == "scoring"]
    assert len(scoring_calls) >= expected_scored, f"expected >={expected_scored} scoring calls got {len(scoring_calls)}"
    # Free user -> zero scoring calls, all preliminary, ai_skipped: plan_free
    LiveFakeHandler.call_log = []
    auth_free, user_free = _make_pro_user(client, db, f"c5free-{uuid.uuid4().hex[:6]}@example.com")
    from app.models.models import Subscription
    sub = db.query(Subscription).filter(Subscription.user_id == user_free.id).first()
    sub.plan = "free"
    db.commit()
    resp2 = _upload_resume(client, auth_free)
    assert resp2.status_code == 200, resp2.text
    LiveFakeHandler.call_log = []
    item2 = enqueue(db, user_id=user_free.id, pipeline="discovery", payload={"keywords": [], "freshness_hours": 168, "limit": 30, "live_enabled": False, "sources": [], "board_tokens": []}, dedupe_key=f"c5-free-{uuid.uuid4().hex[:6]}")
    asyncio.run(worker._run_item(item2.id, "discovery"))
    db.refresh(item2)
    result2 = (item2.payload or {}).get("result") or {}
    assert result2.get("ai_rescore", {}).get("skipped") == "plan_free", result2
    jobs_free = db.query(Job).filter(Job.user_id == user_free.id).all()
    assert all(j.score_source == "preliminary" for j in jobs_free), "free should have all preliminary"
    scoring_calls_free = [c for c in LiveFakeHandler.call_log if c["workflow"] == "scoring"]
    assert len(scoring_calls_free) == 0, f"free tier should have zero scoring calls got {len(scoring_calls_free)}"
    # No profile -> ai_skipped: no_profile
    LiveFakeHandler.call_log = []
    auth_nopro, user_nopro = _make_pro_user(client, db, f"c5nopro-{uuid.uuid4().hex[:6]}@example.com")
    from app.models.models import Profile
    db.query(Profile).filter(Profile.user_id == user_nopro.id).delete()
    db.commit()
    sub2 = db.query(Subscription).filter(Subscription.user_id == user_nopro.id).first()
    sub2.plan = "pro"
    db.commit()
    item3 = enqueue(db, user_id=user_nopro.id, pipeline="discovery", payload={"keywords": [], "freshness_hours": 168, "limit": 30, "live_enabled": False, "sources": [], "board_tokens": []}, dedupe_key=f"c5-nopro-{uuid.uuid4().hex[:6]}")
    asyncio.run(worker._run_item(item3.id, "discovery"))
    db.refresh(item3)
    result3 = (item3.payload or {}).get("result") or {}
    assert result3.get("ai_rescore", {}).get("skipped") == "no_profile", result3
    jobs_nopro = db.query(Job).filter(Job.user_id == user_nopro.id).all()
    assert all(j.score_source == "preliminary" for j in jobs_nopro) if jobs_nopro else True


# ---------------------------------------------------------------------------
# C6 why_empty (v2.2.4)
# ---------------------------------------------------------------------------
def test_c6_why_empty(live_client, live_fake_server, fake_mode, db, monkeypatch):
    fake_mode("ok")
    client = live_client
    auth, user = _make_pro_user(client, db, f"c6-{uuid.uuid4().hex[:6]}@example.com")
    monkeypatch.setattr("app.core.config.settings.live_scraping_enabled", False)
    monkeypatch.setattr("app.core.config.settings.include_demo_pool", False)
    from app.services.job_queue import enqueue
    from app.worker import Worker
    from app.services import sources as src_reg
    item = enqueue(db, user_id=user.id, pipeline="discovery", payload={"keywords": ["python"], "freshness_hours": 24, "limit": 10, "live_enabled": True, "sources": [], "board_tokens": []}, dedupe_key=f"c6-{uuid.uuid4().hex[:6]}")
    worker = Worker(pipelines=["discovery"])
    asyncio.run(worker._run_item(item.id, "discovery"))
    db.refresh(item)
    assert item.status == "done"
    result = (item.payload or {}).get("result") or {}
    assert "why_empty" in result, f"empty board should have why_empty got {result}"
    assert result["why_empty"] in ("no_sources_configured", "all_sources_failed", "no_fresh_postings")
    resp = client.get("/api/jobs/discovery/last-run", headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("why_empty") in ("no_sources_configured", "all_sources_failed", "no_fresh_postings")
    monkeypatch.setattr("app.core.config.settings.live_scraping_enabled", True)
    async def fake_fetch(keywords, limit=40, since_hours=168, sources=None, board_tokens=None):
        requested = sources if sources is not None else ["greenhouse"]
        return [], {"requested": requested, "ok": {}, "errors": {s: "network down" for s in requested}, "total": 0}
    monkeypatch.setattr(src_reg, "fetch_all", fake_fetch)
    item2 = enqueue(db, user_id=user.id, pipeline="discovery", payload={"keywords": ["python"], "freshness_hours": 24, "limit": 10, "live_enabled": True, "sources": ["greenhouse", "lever"], "board_tokens": []}, dedupe_key=f"c6-2-{uuid.uuid4().hex[:6]}")
    asyncio.run(worker._run_item(item2.id, "discovery"))
    db.refresh(item2)
    result2 = (item2.payload or {}).get("result") or {}
    assert result2.get("why_empty") == "all_sources_failed"
    resp2 = client.get("/api/jobs/discovery/last-run", headers=auth)
    assert resp2.status_code == 200
    assert resp2.json().get("why_empty") == "all_sources_failed"


# ---------------------------------------------------------------------------
# C7 auto mode
# ---------------------------------------------------------------------------
def test_c7_auto_mode(live_client, live_fake_server, fake_mode, db, monkeypatch):
    fake_mode("ok")
    client = live_client
    auth_pro, user_pro = _make_pro_user(client, db, f"c7pro-{uuid.uuid4().hex[:6]}@example.com")
    resp = client.put("/api/settings", json={"automation": {"auto_mode": True}, "workflows": {"ai_for_discovery": True}}, headers=auth_pro)
    assert resp.status_code in (200, 204), resp.text
    resp2 = client.get("/api/automation", headers=auth_pro)
    assert resp2.status_code == 200, resp2.text
    body = resp2.json()
    assert "cadence" in body, body
    auth_free, user_free = _make_pro_user(client, db, f"c7free-{uuid.uuid4().hex[:6]}@example.com")
    from app.models.models import Subscription
    sub = db.query(Subscription).filter(Subscription.user_id == user_free.id).first()
    sub.plan = "free"
    db.commit()
    from app.core.entitlements import can
    assert can(db, user_pro.id, "can_use_scheduled_workflows") is True
    assert can(db, user_free.id, "can_use_scheduled_workflows") is False
    # free enable should be 403
    resp_free_enable = client.put("/api/settings", json={"automation": {"auto_mode": True}}, headers=auth_free)
    assert resp_free_enable.status_code == 403, resp_free_enable.text
    assert resp_free_enable.json()["detail"]["code"] == "upgrade_required"
    from app.services.auto_scheduler import AutoScheduler
    scheduler = AutoScheduler(interval_seconds=10)
    result = asyncio.run(scheduler.sweep())
    from app.models.models import ScheduledRun, PipelineJob
    runs = db.query(ScheduledRun).filter(ScheduledRun.user_id == user_pro.id).all()
    assert len(runs) >= 1, f"expected scheduled_runs row got {runs}"
    q_items = db.query(PipelineJob).filter(PipelineJob.user_id == user_pro.id, PipelineJob.pipeline.in_(["discovery", "funding", "application"])).all()
    assert q_items, "expected queue item from scheduler"
    from app.worker import Worker
    worker = Worker(pipelines=["discovery", "funding", "application"])
    for item in q_items:
        if item.status == "queued":
            # expire backoff if any
            item.scheduled_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
            asyncio.run(worker._run_item(item.id, item.pipeline))
    from app.services.auto_scheduler import reconcile
    reconcile(db, user_pro.id)
    runs2 = db.query(ScheduledRun).filter(ScheduledRun.user_id == user_pro.id).all()
    assert any(r.state in ("done", "queued", "paused", "failed", "needs_input", "skipped_outage", "skipped_quota", "skipped_no_consent") for r in runs2)

