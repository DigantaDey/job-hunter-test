"""Funding radar: real providers, honest labelling and anti-fabrication guard.

v2.1.2 (see the marked section at the bottom): the AI relevance pass is a hard
dependency — no key is ``blocked_needs_action``, a provider outage pauses, a
fabricated company name rejects the answer, and an empty radar always says why.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from app.models.models import FundingCompany, User
from app.services import funding_radar, funding_sources
from app.services.funding_sources import (
    FundingEvent,
    FundingProviderError,
    ai_rank_events,
    normalize_company_name,
    normalize_stage,
)


class FakeResponse:
    def __init__(self, payload=None, status_code: int = 200, text: str = "", headers=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text or ""
        self.headers = headers or {"content-type": "application/json"}
        self.request = None

    def json(self):
        return self._payload


def test_stage_normalisation():
    assert normalize_stage("Seed") == "Seed"
    assert normalize_stage("series-b") == "Series B"
    assert normalize_stage("Form D") == "Undisclosed"
    assert normalize_stage("") == "Undisclosed"


def test_provider_status_is_transparent(monkeypatch):
    status = {row["id"]: row for row in funding_sources.provider_status()}
    assert status["sec_edgar"]["configured"] is True
    assert status["crunchbase"]["configured"] is False
    assert status["demo"]["configured"] is False  # synthetic data off by default

    from app.core.config import settings

    monkeypatch.setattr(settings, "allow_synthetic_funding_data", True)
    assert {row["id"]: row for row in funding_sources.provider_status()}["demo"]["configured"] is True


@pytest.mark.asyncio
async def test_sec_edgar_provider_parses_filings(monkeypatch):
    async def fake_request(method, url, **kwargs):
        assert "efts.sec.gov" in url
        assert kwargs["params"]["forms"] == "D"
        return FakeResponse({
            "hits": {"hits": [
                {"_id": "0001234567-24-000123:primary_doc.xml",
                 "_source": {"display_names": ["VectorLoom AI (CIK 0001234567)"],
                             "file_date": (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%d"),
                             "form_type": "D", "ciks": ["0001234567"]}},
            ]}
        })

    monkeypatch.setattr("app.services.http.request", fake_request)
    events, report = await funding_sources.fetch_funding_events({"funding_focus": ["ai"]},
                                                                provider="sec_edgar", limit=5)
    assert report["counts"]["sec_edgar"] == 1
    assert report["scan_status"] == "ok"
    company = events[0]
    assert company.name == "VectorLoom AI"  # CIK suffix stripped
    assert company.source == "sec_edgar"
    assert company.verified is True
    assert company.stage == "Undisclosed"  # Form D does not disclose a stage — we don't guess
    assert "sec.gov/Archives" in company.url


@pytest.mark.asyncio
async def test_demo_provider_requires_explicit_opt_in(monkeypatch):
    from app.core.config import settings

    events, report = await funding_sources.fetch_funding_events({}, provider="demo", limit=5)
    assert events == []
    # Requesting a provider that is switched off is reported, not silently empty.
    assert report["errors"]["demo"] == "not_configured"
    assert report["scan_status"] == "scan_failed"

    monkeypatch.setattr(settings, "allow_synthetic_funding_data", True)
    events, report = await funding_sources.fetch_funding_events({"funding_focus": ["ai/ml"]},
                                                                provider="demo", limit=5)
    assert events
    assert all(event.source == "demo" and event.verified is False for event in events)
    assert "DEMO DATA" in events[0].summary


@pytest.mark.asyncio
async def test_imported_provider_parses_csv(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "funding_import_url", "https://example.com/funding.csv")

    async def fake_request(method, url, **kwargs):
        recent = (datetime.utcnow() - timedelta(days=10)).strftime("%Y-%m-%d")
        return FakeResponse(text=f"name,stage,date,website,industry\nAcme,Series A,{recent},acme.com,fintech\n",
                            headers={"content-type": "text/csv"})

    monkeypatch.setattr("app.services.http.request", fake_request)
    events, report = await funding_sources.fetch_funding_events({}, provider="imported", limit=5)
    assert report["counts"]["imported"] == 1
    assert events[0].name == "Acme"
    assert events[0].stage == "Series A"


@pytest.mark.asyncio
async def test_ai_ranking_rejects_invented_companies(monkeypatch):
    """v2.1.2: a name no provider returned is fabrication — the answer is rejected.

    The old behaviour quietly dropped the invented name and returned the rest,
    which is how a model that hallucinates one company gets away with it.
    """
    from app.services.ai_guardrails import AIUnavailableError

    real = [FundingEvent(name="Acme", stage="Seed", industry="fintech"),
            FundingEvent(name="Beta", stage="Series A", industry="saas")]

    async def fake_completion(workflow, prompt, **kwargs):
        return {"companies": [{"name": "Totally Made Up Corp", "matched": True, "rank": 1,
                               "why": "invented"},
                              {"name": "Beta", "matched": True, "rank": 2, "why": "Series A saas"}]}

    monkeypatch.setattr("app.services.ai_client.chat_completion", fake_completion)
    with pytest.raises(AIUnavailableError) as excinfo:
        await ai_rank_events({"funding_focus": ["fintech"]}, real)
    assert excinfo.value.reason == "guardrail_failed"
    assert excinfo.value.state == "blocked_needs_action"  # dead-letter, never a pause loop
    assert excinfo.value.pausable is False
    assert "Totally Made Up Corp" in excinfo.value.detail


@pytest.mark.asyncio
async def test_scan_respects_stage_filter_and_window(monkeypatch, ai_configured):
    async def fake_request(method, url, **kwargs):
        recent = datetime.utcnow().strftime("%Y-%m-%d")
        return FakeResponse({"hits": {"hits": [
            {"_id": "a:b", "_source": {"display_names": ["Recent Co"], "file_date": recent, "ciks": ["1"]}},
        ]}})

    monkeypatch.setattr("app.services.http.request", fake_request)
    companies, report = await funding_radar.scan_funded_companies({"keywords": ["ai"]}, window_days=30, limit=5,
                                                                  provider="sec_edgar")
    assert companies and companies[0]["name"] == "Recent Co"
    assert report["total"] >= 1
    assert report["scan_status"] == "ok"


def test_sync_and_prune_funding_rows(db, owner):
    user = db.query(User).order_by(User.id).first()
    rows = [
        {"name": "Keep Co", "stage": "Seed", "raised_at": datetime.utcnow(), "source": "sec_edgar",
         "verified": True, "website": "keep.com", "industry": "ai", "summary": "", "keywords_matched": [],
         "url": ""},
        {"name": "Old Co", "stage": "Series A", "raised_at": datetime.utcnow(), "source": "sec_edgar",
         "verified": True, "website": "", "industry": "", "summary": "", "keywords_matched": [], "url": ""},
    ]
    result = funding_radar.sync_funding_db(db, user.id, rows, window_days=30)
    assert result["added"] == 2

    # The prune clock is "last time a provider still reported it" (v2.1.2).
    stale = db.query(FundingCompany).filter(FundingCompany.name == "Old Co").first()
    stale.last_seen_at = datetime.utcnow() - timedelta(days=45)
    db.commit()
    assert funding_radar.prune_funding_db(db, user.id, 30) == 1
    assert db.query(FundingCompany).filter(FundingCompany.user_id == user.id).count() == 1

    # Re-syncing updates instead of duplicating.
    again = funding_radar.sync_funding_db(db, user.id, rows, window_days=30)
    assert again["added"] == 1 and again["updated"] == 1


def test_list_filters_unverified_by_default(db, owner):
    user = db.query(User).order_by(User.id).first()
    db.add(FundingCompany(user_id=user.id, name="Verified Co", source="sec_edgar", verified=True))
    db.add(FundingCompany(user_id=user.id, name="Demo Co", source="demo", verified=False))
    db.commit()
    default = funding_radar.list_funding_companies(db, user.id)
    assert [row.name for row in default] == ["Verified Co"]
    everything = funding_radar.list_funding_companies(db, user.id, include_unverified=True)
    assert len(everything) == 2


def test_funding_api_scopes_to_user(client, auth, member_auth, db):
    user = db.query(User).filter(User.email == "owner@example.com").first()
    db.add(FundingCompany(user_id=user.id, name="Owner Co", source="sec_edgar", verified=True))
    db.commit()
    owner_rows = client.get("/api/funding/companies", headers=auth).json()["companies"]
    member_rows = client.get("/api/funding/companies", headers=member_auth).json()["companies"]
    assert [row["name"] for row in owner_rows] == ["Owner Co"]
    assert member_rows == []
    assert client.post("/api/funding/Owner Co/process", headers=member_auth).status_code == 404


def test_funding_process_creates_job_or_email(client, auth, uploaded_resume, db):
    """Requirement 6: the branch is decided by *provider facts*, not a dead flag.

    A row whose ``meta`` carries real open positions becomes a tracked Job whose
    stored text is factual; a row without them becomes a founder draft that needs
    approval. (The old test set ``has_open_positions=True`` — a column no code
    path ever wrote, gating a branch that fabricated a job description.)
    """
    user = db.query(User).filter(User.email == "owner@example.com").first()
    db.add(FundingCompany(user_id=user.id, name="Hiring Co", source="imported", verified=True,
                          stage="Seed", website="hiring.co", industry="fintech",
                          raised_at=datetime.utcnow() - timedelta(days=4),
                          meta={"open_positions": [{"title": "Backend Engineer",
                                                    "url": "https://hiring.co/jobs/42",
                                                    "location": "Remote",
                                                    "summary": "Python, FastAPI, payments."}],
                                "raised_at_estimated": False}))
    db.add(FundingCompany(user_id=user.id, name="Quiet Co", source="sec_edgar", verified=True,
                          stage="Seed", website="quiet.co", industry="fintech", meta={}))
    db.commit()

    hiring = client.post("/api/funding/Hiring Co/process", headers=auth).json()
    assert hiring["action"] == "apply_flow"
    assert hiring["job_id"]
    assert hiring["positions_source"] == "provider"

    from app.models.models import Job

    job = db.query(Job).filter(Job.id == hiring["job_id"]).first()
    assert job.title == "Backend Engineer"                 # the provider's own posting
    assert job.url == "https://hiring.co/jobs/42"
    assert job.score_source == "funding_context"           # honest: there is no JD to score
    assert "no job description available" in job.score_reason.lower()
    assert "recently raised" not in job.description        # the fabricated sentence is gone
    assert "Open Role at" not in job.title
    assert "Backend Engineer" in job.description and "Seed round reported by imported" in job.description

    quiet = client.post("/api/funding/Quiet Co/process", headers=auth).json()
    assert quiet["action"] == "cold_email_founder"
    assert quiet["email_id"]
    assert quiet["positions_source"] == "none_reported"
    emails = client.get("/api/emails", headers=auth).json()
    assert emails and emails[0]["status"] == "pending_approval"


def test_funding_providers_endpoint(client, auth):
    body = client.get("/api/funding/providers", headers=auth).json()
    assert body["active"] in {"sec_edgar", "crunchbase", "tracxn", "imported", "demo"}
    assert any(row["id"] == "sec_edgar" for row in body["providers"])


# =========================================================================== #
# v2.1.2 — the radar is AI-ranked, and honest when it cannot be
#
# Contract under test (CHANGELOG 2.1.2):
#
# 1. Ranking is a hard dependency: no key → ``blocked_needs_action`` (no
#    companies, no rows), transient outage → pausable 503 / paused queue item,
#    and no code path returns events the model never judged.
# 2. ONE grounded AI pass does relevance + rank + explanation. Zero matches is a
#    successful scan with ``reason="no_matching_events"`` — the old keyword
#    filter was inverted (no matches ⇒ filter skipped ⇒ everything returned).
# 3. Provider failures are data: per-provider timeout + one retry on transport
#    errors, per-provider errors in the report, and ``scan_status="scan_failed"``
#    when nothing could be fetched (persisted as ``funding.last_report``).
# 4. Dedupe is casing-insensitive and first-seen is preserved.
# 5. ``process_company`` never invents a posting.
# =========================================================================== #
@pytest.fixture(autouse=True)
def _clean_ai_state():
    """Breaker + ping-cache state is process-global (see test_ai_pause_resume)."""
    import app.services.ai_client as ai_client

    ai_client._breakers.clear()
    ai_client._PING_CACHE.clear()
    yield
    ai_client._breakers.clear()
    ai_client._PING_CACHE.clear()


@pytest.fixture
def ai_configured(monkeypatch):
    """AI is configured for the radar, answered by the deterministic stand-in.

    The autouse ``ai_stub`` only answers when no key is configured, so a test
    that exercises the ranking path declares a key *and* keeps the stub as the
    wire (patching ``chat_completion`` again, over the stub's own patch).
    """
    import conftest

    monkeypatch.setattr("app.core.config.settings.ai_api_key", "sk-hermetic-funding-test")

    async def fake_completion(workflow, prompt, **kwargs):
        return conftest.stub_ai_response(workflow, prompt)

    monkeypatch.setattr("app.services.ai_client.chat_completion", fake_completion)
    return True


def _script_ai(monkeypatch, verdicts):
    """Configure AI and script exactly what the funding model answers."""
    monkeypatch.setattr("app.core.config.settings.ai_api_key", "sk-hermetic-funding-test")

    async def fake_completion(workflow, prompt, **kwargs):
        assert workflow == "funding_scan"
        fake_completion.prompts.append(prompt)
        return verdicts(prompt) if callable(verdicts) else verdicts

    fake_completion.prompts = []
    monkeypatch.setattr("app.services.ai_client.chat_completion", fake_completion)
    return fake_completion


def _install_providers(monkeypatch, mapping):
    """Replace the provider registry (restored by monkeypatch)."""
    for provider_id, fn in mapping.items():
        monkeypatch.setitem(funding_sources.PROVIDERS, provider_id, fn)


def _static_provider(events, calls=None, error=None, delay=0.0):
    async def provider(context, window_days, limit):
        if calls is not None:
            calls.append({"window_days": window_days, "limit": limit})
        if delay:
            await asyncio.sleep(delay)
        if error is not None:
            raise error
        return list(events)

    return provider


def _events(*names, industry="fintech", stage="Seed"):
    now = datetime.utcnow()
    return [FundingEvent(name=name, stage=stage, industry=industry, raised_at=now - timedelta(days=index + 1),
                         website=f"{name.split()[0].lower()}.com", summary=f"{name} raised a {stage} round.",
                         source="sec_edgar", verified=True, url=f"https://sec.gov/{index}")
            for index, name in enumerate(names)]


# --------------------------------------------------------------------------- #
# 1. Ranking is a hard dependency
# --------------------------------------------------------------------------- #
def test_companies_refresh_without_an_ai_key_is_blocked(client, auth, db, monkeypatch):
    """No key anywhere → the blocked outage shape, no companies, no new rows."""
    calls = []
    _install_providers(monkeypatch, {"sec_edgar": _static_provider(_events("Acme", "Beta"), calls=calls)})

    response = client.get("/api/funding/companies?refresh=true", headers=auth)
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["state"] == "blocked_needs_action", body
    assert body["status"] == "ai_blocked", body
    assert body["reason"] == "no_api_key", body
    assert body["workflow"] == "funding_scan", body
    assert body["pausable"] is False
    assert "Settings" in body["fix"], body["fix"]
    assert body["detail"].strip()
    assert not body.get("companies"), "an outage must never be answered with a company list"

    # The blocked scan wrote nothing and called no provider (fail fast).
    assert db.query(FundingCompany).count() == 0
    assert calls == []

    # … but it did record why the radar is stale.
    user = db.query(User).filter(User.email == "owner@example.com").first()
    report = funding_radar.latest_scan_report(db, user.id)
    assert report["scan_status"] == "blocked"
    assert report["reason"] == "no_api_key"


def test_page_open_with_a_blocked_radar_keeps_the_last_good_rows(client, auth, db):
    """An *implicit* refresh reports the blockage instead of blanking the page."""
    owner_row = db.query(User).order_by(User.id).first()
    stale = datetime.utcnow() - timedelta(days=10)
    db.add(FundingCompany(user_id=owner_row.id, name="Earlier Co", source="sec_edgar", verified=True,
                          stage="Seed", discovered_at=stale, last_seen_at=stale))
    db.commit()

    body = client.get("/api/funding/companies", headers=auth).json()  # stale → implicit refresh
    assert body["scan_status"] == "blocked", body["scan_status"]
    assert body["reason"] == "no_api_key"
    assert body["last_report"]["ai"]["status"] == "ai_blocked"
    assert [row["name"] for row in body["companies"]] == ["Earlier Co"]


@pytest.mark.asyncio
async def test_scan_never_returns_events_the_model_did_not_judge(monkeypatch):
    """The unranked fallback is gone: ``_ai_enabled`` no longer exists at all."""
    import inspect

    source = inspect.getsource(funding_radar)
    assert "_ai_enabled" not in source
    assert "keyword_score" not in source  # the keyword pre-filter is gone too

    _install_providers(monkeypatch, {"sec_edgar": _static_provider(_events("Acme", "Beta"))})
    prompt_holder = _script_ai(monkeypatch, lambda prompt: {"companies": [
        {"name": "Acme", "matched": True, "rank": 1, "why": "Seed fintech round."}]})
    companies, report = await funding_radar.scan_funded_companies({"funding_focus": ["fintech"]},
                                                                 provider="sec_edgar", limit=10)
    assert [row["name"] for row in companies] == ["Acme"]
    assert companies[0]["why"] == "Seed fintech round."
    assert report["ai"]["coverage"] == 0.5          # the model only judged one of two
    assert len(prompt_holder.prompts) == 1          # ONE grounded pass, not two


@pytest.mark.asyncio
async def test_queued_scan_dead_letters_on_a_blocked_radar(client, auth, db, monkeypatch):
    """Blocked (no key) on the queued path → ``fail(retryable=False)``, not a pause."""
    from app.models.models import PipelineJob
    from app.services.job_queue import enqueue
    from app.worker import Worker

    owner_row = db.query(User).order_by(User.id).first()
    calls = []
    _install_providers(monkeypatch, {"sec_edgar": _static_provider(_events("Acme"), calls=calls)})
    item = enqueue(db, user_id=owner_row.id, pipeline="funding",
                   payload={"window_days": 30, "provider": "sec_edgar", "context": {"keywords": ["ai"]}},
                   dedupe_key="funding:blocked-test")
    assert item is not None
    await Worker(pipelines=["funding"])._run_item(item.id, "funding")
    db.refresh(item)
    assert item.status == "dead", item.error
    assert "no_api_key" in (item.error or "")
    assert db.query(FundingCompany).count() == 0
    assert calls == [], "a blocked radar must not burn provider calls"
    assert db.query(PipelineJob).filter(PipelineJob.id == item.id).count() == 1


@pytest.mark.asyncio
async def test_queued_scan_dead_letters_on_a_fabricated_company(client, auth, db, monkeypatch):
    """Guardrail rejection on the queued path is a dead outcome, never a retry loop."""
    from app.worker import Worker

    owner_row = db.query(User).order_by(User.id).first()
    _install_providers(monkeypatch, {"sec_edgar": _static_provider(_events("Acme"))})
    _script_ai(monkeypatch, {"companies": [{"name": "Ghost Holdings", "matched": True, "rank": 1,
                                            "why": "invented"}]})
    from app.services.job_queue import enqueue

    item = enqueue(db, user_id=owner_row.id, pipeline="funding",
                   payload={"window_days": 30, "provider": "sec_edgar", "context": {"keywords": ["ai"]}},
                   dedupe_key="funding:guardrail-test")
    await Worker(pipelines=["funding"])._run_item(item.id, "funding")
    db.refresh(item)
    assert item.status == "dead", item.error
    assert "guardrail" in (item.error or "").lower()
    assert db.query(FundingCompany).count() == 0, "no fabricated row may be persisted"
    report = funding_radar.latest_scan_report(db, owner_row.id)
    assert report["scan_status"] == "blocked"
    assert report["reason"] == "guardrail_failed"


# --------------------------------------------------------------------------- #
# 2. One grounded pass: relevance + rank + why (the inverted filter is gone)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_five_events_two_relevant_returns_exactly_the_two(monkeypatch):
    events = _events("Payments Co", "Lending Co", "Solar Farm", "Gene Therapy Lab", "Payments Rail")
    _install_providers(monkeypatch, {"sec_edgar": _static_provider(events)})
    relevant = {"Payments Co", "Payments Rail"}
    _script_ai(monkeypatch, lambda prompt: {"companies": [
        {"name": name, "matched": name in relevant, "rank": index + 1,
         "why": f"{name}: seed fintech payments round." if name in relevant else "unrelated sector"}
        for index, name in enumerate(["Payments Rail", "Payments Co", "Solar Farm",
                                      "Gene Therapy Lab", "Lending Co"])]})

    companies, report = await funding_radar.scan_funded_companies({"funding_focus": ["payments"]},
                                                                 provider="sec_edgar", limit=10)
    assert [row["name"] for row in companies] == ["Payments Rail", "Payments Co"]  # model order wins
    assert all(row["why"] for row in companies)
    assert report["scan_status"] == "ok"
    assert report["reason"] is None
    assert report["scanned"] == 5 and report["returned"] == 2
    assert report["ai"]["matched"] == 2


@pytest.mark.asyncio
async def test_zero_relevant_is_an_ok_scan_with_a_reason(monkeypatch):
    """Regression for the inverted filter: no matches ⇒ nothing, not everything."""
    _install_providers(monkeypatch, {"sec_edgar": _static_provider(_events("Solar Farm", "Gene Lab", "Shipping Co"))})
    _script_ai(monkeypatch, lambda prompt: {"companies": [
        {"name": name, "matched": False, "rank": index + 1, "why": "outside the candidate's focus"}
        for index, name in enumerate(["Solar Farm", "Gene Lab", "Shipping Co"])]})

    companies, report = await funding_radar.scan_funded_companies({"funding_focus": ["fintech"]},
                                                                 provider="sec_edgar", limit=10)
    assert companies == []
    assert report["scan_status"] == "ok"
    assert report["reason"] == "no_matching_events"
    assert report["scanned"] == 3


def test_companies_endpoint_reports_no_matching_events(client, auth, db, monkeypatch):
    """The UI contract for an honest empty radar."""
    _install_providers(monkeypatch, {"sec_edgar": _static_provider(_events("Solar Farm", "Shipping Co"))})
    _script_ai(monkeypatch, {"companies": [{"name": "Solar Farm", "matched": False, "rank": 1, "why": "no"},
                                          {"name": "Shipping Co", "matched": False, "rank": 2, "why": "no"}]})
    body = client.get("/api/funding/companies?refresh=true", headers=auth).json()
    assert body["companies"] == []
    assert body["scan_status"] == "ok"
    assert body["reason"] == "no_matching_events"
    assert body["scanned"] == 2
    assert body["last_report"]["ai"]["matched"] == 0


@pytest.mark.asyncio
async def test_stage_and_amount_come_from_the_event_not_the_model(monkeypatch):
    events = [FundingEvent(name="Acme", stage="Seed", industry="fintech", raised_usd=2_000_000,
                           raised_at=datetime.utcnow(), source="sec_edgar", verified=True)]
    _install_providers(monkeypatch, {"sec_edgar": _static_provider(events)})
    _script_ai(monkeypatch, {"companies": [{"name": "Acme", "matched": True, "rank": 1,
                                            "why": "Seed fintech round.",
                                            "stage": "Series C", "raised_usd": 999_000_000}]})
    companies, report = await funding_radar.scan_funded_companies({}, provider="sec_edgar")
    assert companies[0]["stage"] == "Seed"                 # the model's "Series C" is ignored
    assert companies[0]["raised_usd"] == 2_000_000
    assert companies[0]["source"] == "sec_edgar" and companies[0]["verified"] is True
    assert report["ai"]["field_conflicts"] == 2            # … and recorded, not silently trusted


@pytest.mark.asyncio
async def test_ranking_survives_a_name_with_a_legal_suffix(monkeypatch):
    """Grounding is strict but not pedantic: "Acme Inc." is "Acme"."""
    _install_providers(monkeypatch, {"sec_edgar": _static_provider(_events("Acme", "Beta"))})
    _script_ai(monkeypatch, {"companies": [{"name": "ACME  INC.", "matched": True, "rank": 1,
                                            "why": "matched."},
                                           {"name": "beta", "matched": True, "rank": 2, "why": "matched."}]})
    companies, _report = await funding_radar.scan_funded_companies({}, provider="sec_edgar")
    assert [row["name"] for row in companies] == ["Acme", "Beta"]   # provider casing preserved


@pytest.mark.asyncio
async def test_unusable_model_answer_is_surfaced_not_swallowed(monkeypatch):
    from app.services.ai_client import AIClientError

    _install_providers(monkeypatch, {"sec_edgar": _static_provider(_events("Acme"))})
    _script_ai(monkeypatch, {"order": ["Acme"]})            # the pre-v2.1.2 shape
    with pytest.raises(AIClientError) as excinfo:
        await funding_radar.scan_funded_companies({}, provider="sec_edgar")
    assert excinfo.value.reason == "invalid_json"

    _script_ai(monkeypatch, {"companies": []})              # answered, but judged nothing
    with pytest.raises(AIClientError) as excinfo:
        await funding_radar.scan_funded_companies({}, provider="sec_edgar")
    assert excinfo.value.reason == "empty_response"


@pytest.mark.asyncio
async def test_no_events_fetched_is_not_a_failure(monkeypatch):
    _install_providers(monkeypatch, {"sec_edgar": _static_provider([])})
    fake = _script_ai(monkeypatch, {"companies": []})
    companies, report = await funding_radar.scan_funded_companies({}, provider="sec_edgar")
    assert companies == []
    assert report["scan_status"] == "ok"
    assert report["reason"] == "no_events_fetched"
    assert fake.prompts == []                               # nothing to rank → no AI call


# --------------------------------------------------------------------------- #
# 3. Provider robustness
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_provider_timeout_and_single_retry(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "funding_provider_timeout_seconds", 0.05)
    monkeypatch.setattr(funding_sources, "PROVIDER_RETRY_DELAY_SECONDS", 0.0)
    calls = []
    _install_providers(monkeypatch, {"sec_edgar": _static_provider([], calls=calls, delay=1.0)})

    events, report = await funding_sources.fetch_funding_events({}, provider="sec_edgar")
    assert events == []
    assert len(calls) == 2, "a transport failure gets exactly one retry"
    assert "timed out" in report["errors"]["sec_edgar"]
    assert report["scan_status"] == "scan_failed"


@pytest.mark.asyncio
async def test_provider_4xx_is_not_retried(monkeypatch):
    calls = []
    _install_providers(monkeypatch, {"sec_edgar": _static_provider(
        [], calls=calls, error=FundingProviderError("EDGAR full-text search returned HTTP 404", status=404))})

    events, report = await funding_sources.fetch_funding_events({}, provider="sec_edgar")
    assert events == []
    assert len(calls) == 1, "a 4xx does not fix itself by being repeated"
    assert "404" in report["errors"]["sec_edgar"]


def test_all_providers_failing_is_scan_failed_not_an_empty_radar(client, auth, db, monkeypatch):
    """Requirement 3: the failure is user-visible, audited, and never "no companies"."""
    calls = []
    _install_providers(monkeypatch, {
        "sec_edgar": _static_provider([], calls=calls, error=RuntimeError("EDGAR is down")),
        "imported": _static_provider([], error=FundingProviderError("import returned HTTP 503", status=503)),
    })
    monkeypatch.setattr("app.core.config.settings.funding_import_url", "https://example.com/f.csv")
    monkeypatch.setattr("app.core.config.settings.funding_provider", "auto")   # every configured provider
    monkeypatch.setattr(funding_sources, "PROVIDER_RETRY_DELAY_SECONDS", 0.0)
    _script_ai(monkeypatch, {"companies": []})

    response = client.get("/api/funding/companies?refresh=true&include_unverified=true", headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scan_status"] == "scan_failed"
    assert body["reason"] == "provider_errors"
    assert body["companies"] == []
    assert set(body["last_report"]["errors"]) == {"sec_edgar", "imported"}
    assert "EDGAR is down" in body["last_report"]["errors"]["sec_edgar"]
    assert body["last_report"]["counts"] == {}

    from app.models.models import AuditLog

    entry = db.query(AuditLog).filter(AuditLog.action == "funding.scan_failed").first()
    assert entry is not None
    assert entry.detail["scan_status"] == "scan_failed"
    assert set(entry.detail["errors"]) == {"sec_edgar", "imported"}
    assert db.query(FundingCompany).count() == 0


def test_partial_provider_failure_stays_ok_with_visible_errors(client, auth, db, monkeypatch):
    events = _events("Acme")
    _install_providers(monkeypatch, {
        "sec_edgar": _static_provider(events),
        "imported": _static_provider([], error=RuntimeError("export unreachable")),
    })
    monkeypatch.setattr("app.core.config.settings.funding_import_url", "https://example.com/f.csv")
    monkeypatch.setattr("app.core.config.settings.funding_provider", "auto")   # every configured provider
    monkeypatch.setattr(funding_sources, "PROVIDER_RETRY_DELAY_SECONDS", 0.0)
    _script_ai(monkeypatch, lambda prompt: {"companies": [
        {"name": "Acme", "matched": True, "rank": 1, "why": "Seed fintech round."}]})

    body = client.get("/api/funding/companies?refresh=true", headers=auth).json()
    assert body["scan_status"] == "ok"
    assert [row["name"] for row in body["companies"]] == ["Acme"]
    assert body["last_report"]["errors"] == {"imported": "RuntimeError: export unreachable"}
    assert body["last_report"]["counts"] == {"sec_edgar": 1}


def test_requested_but_unconfigured_provider_is_reported(client, auth, db, monkeypatch):
    """Choosing crunchbase without a licence is not "nobody raised money"."""
    _script_ai(monkeypatch, {"companies": []})   # AI configured; the provider never gets that far
    monkeypatch.setattr("app.core.config.settings.funding_provider", "crunchbase")
    payload = client.get("/api/funding/companies?refresh=true", headers=auth).json()
    assert payload["scan_status"] == "scan_failed"
    assert payload["last_report"]["errors"] == {"crunchbase": "not_configured"}


def test_no_usable_provider_at_all_is_scan_failed_not_an_empty_radar(monkeypatch):
    """``provider="auto"`` with nothing usable must not report a green scan."""
    monkeypatch.setattr(
        funding_sources, "provider_status",
        lambda: [{"id": pid, "label": pid, "configured": False, "verified": True}
                 for pid in funding_sources.PROVIDERS],
    )
    events, report = asyncio.run(funding_sources.fetch_funding_events(
        {"keywords": ["fintech"]}, window_days=45, limit=10, provider="auto"))
    assert events == []
    assert report["scan_status"] == "scan_failed"
    assert report["errors"] == {"providers": "no_providers_configured"}
    assert report["fetched"] == 0 and report["total"] == 0
    assert report["counts"] == {}, "nothing was fetched, so nothing succeeded"


# --------------------------------------------------------------------------- #
# 4. Dedupe, stability and the persisted report
# --------------------------------------------------------------------------- #
def test_sync_dedupes_case_and_whitespace_variants(db, owner):
    user = db.query(User).order_by(User.id).first()
    first = [{"name": "Stripe", "stage": "Series A", "raised_at": datetime.utcnow(), "source": "crunchbase",
              "verified": True, "website": "stripe.com", "industry": "fintech", "summary": "one",
              "keywords_matched": [], "url": "https://x/1", "meta": {"cik": "123"}}]
    assert funding_radar.sync_funding_db(db, user.id, first, window_days=30)["added"] == 1

    second = [{"name": "  stripe ", "stage": "Series B", "raised_at": datetime.utcnow(), "source": "crunchbase",
               "verified": True, "website": "stripe.com", "industry": "fintech", "summary": "two",
               "keywords_matched": [], "url": "https://x/2", "meta": {"form_type": "D"}}]
    result = funding_radar.sync_funding_db(db, user.id, second, window_days=30)
    assert result == {"added": 0, "updated": 1, "pruned": 0}

    rows = db.query(FundingCompany).filter(FundingCompany.user_id == user.id).all()
    assert len(rows) == 1
    assert rows[0].name == "Stripe"                  # first-seen canonical casing is kept
    assert rows[0].stage == "Series B"               # facts are refreshed
    assert rows[0].meta["cik"] == "123"              # provider meta is merged, not replaced
    assert rows[0].meta["form_type"] == "D"
    assert normalize_company_name("  Stripe\tInc ") == "stripe inc"


def test_first_seen_is_preserved_and_last_seen_drives_prune(db, owner):
    user = db.query(User).order_by(User.id).first()
    rows = [{"name": "Acme", "stage": "Seed", "raised_at": datetime.utcnow(), "source": "sec_edgar",
             "verified": True, "website": "", "industry": "", "summary": "", "keywords_matched": [],
             "url": "", "meta": {}}]
    funding_radar.sync_funding_db(db, user.id, rows, window_days=30)
    row = db.query(FundingCompany).first()
    row.discovered_at = datetime.utcnow() - timedelta(days=20)
    row.last_seen_at = datetime.utcnow() - timedelta(days=20)
    db.commit()
    first_seen = row.discovered_at

    funding_radar.sync_funding_db(db, user.id, rows, window_days=30)
    db.refresh(row)
    assert row.discovered_at == first_seen, "discovered_at is first-seen and must not move"
    assert row.last_seen_at > first_seen, "a re-confirmed row is seen again now"
    assert funding_radar.prune_funding_db(db, user.id, 30) == 0


def test_prune_drops_rows_providers_stopped_reporting(db, owner):
    user = db.query(User).order_by(User.id).first()
    db.add(FundingCompany(user_id=user.id, name="Gone Co", source="sec_edgar", verified=True,
                          discovered_at=datetime.utcnow() - timedelta(days=60),
                          last_seen_at=datetime.utcnow() - timedelta(days=60)))
    db.add(FundingCompany(user_id=user.id, name="Undated Co", source="sec_edgar", verified=True,
                          discovered_at=None, last_seen_at=None))
    db.commit()
    # A row we cannot date is kept — prune must never delete what it cannot judge.
    assert funding_radar.prune_funding_db(db, user.id, 45) == 1
    assert [row.name for row in db.query(FundingCompany).all()] == ["Undated Co"]


def test_needs_refresh_uses_the_refresh_interval_not_the_window(db, owner, monkeypatch):
    """Related bug: ``window_days * 12 hours`` meant ~22.5 days between scans."""
    from app.services.user_settings import set_setting

    user = db.query(User).order_by(User.id).first()
    monkeypatch.setattr("app.core.config.settings.funding_refresh_hours", 12)
    assert funding_radar.funding_needs_refresh(db, user.id) is True       # never scanned

    funding_radar.store_scan_report(db, user.id, {"scan_status": "ok", "counts": {"sec_edgar": 1}})
    assert funding_radar.funding_needs_refresh(db, user.id) is False

    report = funding_radar.latest_scan_report(db, user.id)
    report["attempted_at"] = (datetime.utcnow() - timedelta(hours=13)).isoformat()
    set_setting(db, user.id, "funding", "last_report", report)
    db.commit()
    assert funding_radar.funding_needs_refresh(db, user.id) is True       # 13h > 12h
    # A 45-day freshness window no longer stretches the refresh interval.
    assert funding_radar.funding_needs_refresh(db, user.id, refresh_hours=48) is False


def test_last_report_records_the_successful_scan(client, auth, db, monkeypatch):
    _install_providers(monkeypatch, {"sec_edgar": _static_provider(_events("Acme", "Beta"))})
    _script_ai(monkeypatch, lambda prompt: {"companies": [
        {"name": "Acme", "matched": True, "rank": 1, "why": "Seed fintech round in payments."},
        {"name": "Beta", "matched": False, "rank": 2, "why": "outside the focus"}]})

    body = client.get("/api/funding/companies?refresh=true", headers=auth).json()
    assert body["scan_status"] == "ok"
    assert body["refreshed_at"]
    report = body["last_report"]
    assert report["counts"] == {"sec_edgar": 2}
    assert report["scanned"] == 2 and report["returned"] == 1
    assert report["scanned_at"] and report["attempted_at"]
    assert [row["id"] for row in report["provider_status"]]

    company = body["companies"][0]
    assert company["why"] == "Seed fintech round in payments."
    assert company["rank"] == 1
    assert "has_open_positions" not in company          # the dead flag left the contract
    assert company["open_positions"] == []
    assert company["raised_at_estimated"] is False


def test_include_unverified_setting_applies_on_refresh_too(client, auth, db, monkeypatch):
    """Related bug: the setting was only read on the *non*-refresh branch.

    The same page therefore hid unverified (demo/licensed-but-unverified) rows
    immediately after a scan and showed them on the next open.
    """
    event = FundingEvent(name="Demo Co", stage="Seed", industry="ai/ml", raised_at=datetime.utcnow(),
                         source="demo", verified=False, summary="[DEMO DATA] illustrative round")
    _install_providers(monkeypatch, {"sec_edgar": _static_provider([event])})
    _script_ai(monkeypatch, {"companies": [{"name": "Demo Co", "matched": True, "rank": 1,
                                            "why": "Seed ai/ml round matches the focus."}]})
    saved = client.put("/api/settings", json={"funding": {"include_unverified": True}}, headers=auth)
    assert saved.status_code == 200, saved.text

    refreshed = client.get("/api/funding/companies?refresh=true", headers=auth).json()
    assert [row["name"] for row in refreshed["companies"]] == ["Demo Co"]
    assert refreshed["companies"][0]["verified"] is False
    cached = client.get("/api/funding/companies", headers=auth).json()
    assert [row["name"] for row in cached["companies"]] == ["Demo Co"]


def test_undated_event_is_labelled_not_dressed_as_today(client, auth, db, monkeypatch):
    """Related bug: a Form D with no parseable date was stored as "raised now"."""
    event = FundingEvent(name="Undated Co", stage="Undisclosed", raised_at=None, source="sec_edgar",
                         verified=True, summary="SEC Form D filed recently",
                         meta={"raised_at_estimated": True})
    _install_providers(monkeypatch, {"sec_edgar": _static_provider([event])})
    _script_ai(monkeypatch, {"companies": [{"name": "Undated Co", "matched": True, "rank": 1,
                                            "why": "Form D filing matches the focus."}]})
    body = client.get("/api/funding/companies?refresh=true", headers=auth).json()
    company = body["companies"][0]
    assert company["raised_at_estimated"] is True       # the UI must not claim "raised today"
    assert company["stage"] == "Undisclosed"


# --------------------------------------------------------------------------- #
# 5. process_company: provider facts or a founder draft — never an invented JD
# --------------------------------------------------------------------------- #
def test_process_without_positions_drafts_a_founder_email(client, auth, uploaded_resume, db):
    from app.models.models import Email, Job

    user = db.query(User).filter(User.email == "owner@example.com").first()
    db.add(FundingCompany(user_id=user.id, name="Solo Co", source="sec_edgar", verified=True,
                          stage="Seed", website="solo.co", industry="fintech",
                          raised_at=datetime.utcnow() - timedelta(days=3),
                          meta={"raised_at_estimated": False}))
    db.commit()

    result = client.post("/api/funding/Solo Co/process", headers=auth).json()
    assert result["action"] == "cold_email_founder"
    assert result["positions_source"] == "none_reported"
    assert "approval required" in result["message"]
    email = db.query(Email).filter(Email.id == result["email_id"]).first()
    assert email.status == "pending_approval"           # never auto-sent
    assert db.query(Job).count() == 0                   # no invented posting


def test_process_matches_the_normalised_name_without_wildcards(client, auth, uploaded_resume, db):
    """Related bugs: ``ilike`` treated ``%`` as a wildcard and missed casing."""
    user = db.query(User).filter(User.email == "owner@example.com").first()
    db.add(FundingCompany(user_id=user.id, name="Northwind AI", source="sec_edgar", verified=True,
                          stage="Seed", meta={}))
    db.commit()

    assert client.post("/api/funding/northwind ai/process", headers=auth).status_code == 200
    assert client.post("/api/funding/%25/process", headers=auth).status_code == 404
    assert client.post("/api/funding/_/process", headers=auth).status_code == 404


def test_process_creates_one_job_per_provider_posting(client, auth, uploaded_resume, db):
    """Related bug: funding jobs shared the default ``dedupe_key`` ("")."""
    from app.models.models import Job

    user = db.query(User).filter(User.email == "owner@example.com").first()
    for index, name in enumerate(("Alpha Co", "Beta Co")):
        db.add(FundingCompany(user_id=user.id, name=name, source="imported", verified=True, stage="Seed",
                              website=f"{name.split()[0].lower()}.com",
                              meta={"open_positions": [{"title": f"Engineer {index}",
                                                        "url": f"https://x/{index}"}]}))
    db.commit()

    first = client.post("/api/funding/Alpha Co/process", headers=auth)
    second = client.post("/api/funding/Beta Co/process", headers=auth)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    jobs = db.query(Job).all()
    assert len(jobs) == 2
    assert len({job.dedupe_key for job in jobs}) == 2
    assert all(job.score_source == "funding_context" for job in jobs)

    # Re-processing the same company is idempotent (it is already tracked).
    again = client.post("/api/funding/Alpha Co/process", headers=auth).json()
    assert again["action"] == "apply_flow"
    assert again["job_id"] == first.json()["job_id"]
    assert db.query(Job).count() == 2


def test_process_job_description_is_factual(client, auth, uploaded_resume, db):
    from app.models.models import Job

    user = db.query(User).filter(User.email == "owner@example.com").first()
    db.add(FundingCompany(user_id=user.id, name="Fact Co", source="imported", verified=True,
                          stage="Series A", industry="fintech", website="fact.co",
                          raised_at=datetime.utcnow() - timedelta(days=6),
                          summary="Payments infrastructure for marketplaces.",
                          meta={"url": "https://sec.gov/filing", "raised_usd": 12_000_000,
                                "raised_at_estimated": False,
                                "open_positions": [{"title": "Senior Backend Engineer",
                                                    "url": "https://fact.co/jobs/7",
                                                    "location": "Bengaluru",
                                                    "summary": "Python and FastAPI payments work."}]}))
    db.commit()

    result = client.post("/api/funding/Fact Co/process", headers=auth).json()
    job = db.query(Job).filter(Job.id == result["job_id"]).first()
    text = job.description
    for fact in ("Series A round reported by imported", "Payments infrastructure for marketplaces.",
                 "Industry: fintech.", "Senior Backend Engineer", "Bengaluru",
                 "Python and FastAPI payments work.", "USD 12,000,000", "https://fact.co/jobs/7",
                 "No job description was available"):
        assert fact in text, fact
    for fiction in ("recently raised", "Open Role at", "Engineering role at"):
        assert fiction not in text, fiction
    assert result["url_source"] == "provider_position"


# --------------------------------------------------------------------------- #
# 6. Wire-level outage behaviour (v2.1 pause/resume) — scripted provider only
# --------------------------------------------------------------------------- #
def _funding_job(db, user_id=None):
    from app.models.models import PipelineJob

    query = db.query(PipelineJob).filter(PipelineJob.pipeline == "funding")
    if user_id is not None:
        query = query.filter(PipelineJob.user_id == user_id)
    return query.order_by(PipelineJob.id.desc()).first()


def _behavior():
    import conftest

    return conftest.ScriptedAIHandler.behavior


@pytest.mark.real_ai
def test_sync_scan_outage_is_a_pausable_503(client, auth, db, provider_owner, monkeypatch):
    """Transient outage on the sync path → ``ai_paused`` + ``retry_after_hint``."""
    _install_providers(monkeypatch, {"sec_edgar": _static_provider(_events("Acme", "Beta"))})
    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 1)
    _behavior()["status"] = 503
    _behavior()["fail_after"] = 0

    response = client.get("/api/funding/companies?refresh=true", headers=auth)
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "ai_unavailable", body
    assert body["status"] == "ai_paused", body
    assert body["state"] == "transient_outage", body
    assert body["workflow"] == "funding_scan", body
    assert body["pausable"] is True
    assert body["retry_after_hint"] is not None, body
    assert body["detail"].strip(), "detail must not be blank"
    assert not body.get("companies")
    assert db.query(FundingCompany).count() == 0

    user = db.query(User).filter(User.email == "owner@example.com").first()
    assert funding_radar.latest_scan_report(db, user.id)["scan_status"] == "paused"


@pytest.mark.real_ai
@pytest.mark.asyncio
async def test_queued_scan_pauses_on_outage_then_completes_on_recovery(
        client, auth, db, provider_owner, monkeypatch):
    """Queued path: paused (not failed), then drained and completed with rows."""
    from app.services.job_queue import drain_paused, paused_count
    from app.worker import Worker

    events = _events("Acme", "Beta")
    _install_providers(monkeypatch, {"sec_edgar": _static_provider(events)})
    monkeypatch.setattr("app.core.config.settings.ai_max_retries", 1)

    queued = client.post("/api/funding/refresh", json={"context": "fintech payments"}, headers=auth)
    assert queued.status_code == 200, queued.text
    item = _funding_job(db)
    assert item is not None and item.status == "queued"
    user_id = item.user_id

    _behavior()["status"] = 503
    _behavior()["fail_after"] = 0
    worker = Worker(pipelines=["funding"])
    await worker._run_item(item.id, "funding")
    db.refresh(item)
    assert item.status == "paused", f"an AI outage must pause, not fail: {item.status} ({item.error})"
    assert item.finished_at is None
    assert paused_count(db, user_id=user_id) == 1
    assert db.query(FundingCompany).count() == 0, "nothing is persisted before the AI call succeeds"

    _behavior()["fail_after"] = None                      # the provider is back
    assert drain_paused(db, user_id=user_id, force=True) == 1
    db.refresh(item)
    assert item.status == "queued"

    await worker._run_item(item.id, "funding")
    db.refresh(item)
    assert item.status == "done", item.error
    rows = db.query(FundingCompany).filter(FundingCompany.user_id == user_id).all()
    assert sorted(row.name for row in rows) == ["Acme", "Beta"]
    assert all((row.meta or {}).get("why") for row in rows)
    report = funding_radar.latest_scan_report(db, user_id)
    assert report["scan_status"] == "ok" and report["returned"] == 2
    assert (item.payload or {}).get("result", {}).get("scan_status") == "ok"

    wire = [r for r in _behavior()["requests"]
            if any("funding-scan-v2" in str(m.get("content") or "") for m in r.get("messages") or [])]
    assert len(wire) == 2, f"one attempt before the outage, one after recovery: {len(wire)}"


PRE_FUNDING_HONESTY_REVISION = 'a1b2c3d4e5f6'   # v2.1.1


def test_migration_upgrades_a_real_2_1_1_database(tmp_path):
    """The schema change has to upgrade a *populated* 2.1.1 database.

    ``test_alembic_migrations_apply_to_a_fresh_database`` builds from empty,
    which exercises neither the back-fill, the ``DROP COLUMN`` nor the collapse
    of the case-variant duplicates the old upsert created.
    """
    import os
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    db = tmp_path / "upgrade.db"
    env = {**os.environ, "ALEMBIC_DATABASE_URL": f"sqlite:///{db.as_posix()}",
           "ENVIRONMENT": "test", "SECRET_KEY": "0" * 48}

    def alembic(*args: str) -> None:
        out = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=backend,
                             env=env, capture_output=True, text=True, timeout=300)
        assert out.returncode == 0, (out.stdout + out.stderr)[-2000:]

    alembic("upgrade", PRE_FUNDING_HONESTY_REVISION)      # the v2.1.1 schema

    first_seen, recent = "2026-07-01 00:00:00", "2026-09-10 00:00:00"
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO funding_companies (user_id,name,stage,industry,source,verified,"
        "discovered_at,raised_at,summary,has_open_positions,meta) VALUES (1,'Stripe','Series G',"
        "'fintech','sec_edgar',1,?,?,?,0,'{\"cik\": \"123\"}')", (first_seen, first_seen, "first seen"))
    con.execute(                                              # the duplicate, seen later
        "INSERT INTO funding_companies (user_id,name,stage,industry,source,verified,"
        "discovered_at,raised_at,summary,has_open_positions,meta) VALUES (1,'stripe','Series G',"
        "'fintech','sec_edgar',1,?,?,?,0,NULL)", (recent, recent, "dupe"))
    con.execute(                                              # undated: raised_at is NULL
        "INSERT INTO funding_companies (user_id,name,stage,industry,source,verified,"
        "discovered_at,raised_at,summary,has_open_positions,meta) VALUES (1,'Acme','Seed','ai/ml',"
        "'crunchbase',1,?,NULL,?,1,NULL)", (first_seen, "undated"))
    con.execute(                                              # another user is untouched
        "INSERT INTO funding_companies (user_id,name,stage,industry,source,verified,"
        "discovered_at,raised_at,summary,has_open_positions,meta) VALUES (2,'Stripe','Series G',"
        "'fintech','sec_edgar',1,?,?,?,0,NULL)", (first_seen, first_seen, "other user"))
    con.commit()
    con.close()

    alembic("upgrade", "head")

    con = sqlite3.connect(db)
    columns = [row[1] for row in con.execute("PRAGMA table_info(funding_companies)")]
    assert "has_open_positions" not in columns, "the dead flag must leave the schema"
    assert "last_seen_at" in columns
    indexes = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='funding_companies'")}
    assert "ix_funding_user_seen" in indexes, "prune needs the (user_id, last_seen_at) index"

    rows = {(row[0], row[1]): row for row in con.execute(
        "SELECT name, user_id, discovered_at, last_seen_at, meta FROM funding_companies")}
    assert len(rows) == 3, f"the case-variant duplicate collapses: {sorted(rows)}"
    assert ("stripe", 1) not in rows and ("Stripe", 1) in rows, "the first-seen casing wins"
    stripe, acme = rows[("Stripe", 1)], rows[("Acme", 1)]
    assert stripe[2] == first_seen, "discovered_at keeps its first-seen meaning"
    assert stripe[3] == first_seen, "last_seen_at is back-filled from discovered_at"
    assert stripe[4] == '{"cik": "123"}', "provider meta survives"
    assert acme[3] == first_seen, "a NULL raised_at still gets a usable clock"
    assert ("Stripe", 2) in rows, "another user's rows are untouched"
    con.close()

    alembic("downgrade", "-1")                               # reversible
    con = sqlite3.connect(db)
    assert "has_open_positions" in [r[1] for r in con.execute("PRAGMA table_info(funding_companies)")]
    con.close()
    alembic("upgrade", "head")
