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
from app.services import funding_radar, funding_search, funding_sources
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
    assert client.post("/api/funding/companies/process", json={"company": "Owner Co"}, headers=member_auth).status_code == 404


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

    hiring = client.post("/api/funding/companies/process", json={"company": "Hiring Co"}, headers=auth).json()
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

    quiet = client.post("/api/funding/companies/process", json={"company": "Quiet Co"}, headers=auth).json()
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
    # v2.2.5 re-introduces has_open_positions as a live linkage flag (kept fresh both directions)
    assert "has_open_positions" in company
    assert isinstance(company["has_open_positions"], bool)
    assert company["has_open_positions"] is False  # no job for this company yet
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

    result = client.post("/api/funding/companies/process", json={"company": "Solo Co"}, headers=auth).json()
    assert result["action"] == "cold_email_founder"
    assert result["positions_source"] == "none_reported"
    assert "approval required" in result["message"]
    email = db.query(Email).filter(Email.id == result["email_id"]).first()
    assert email.status == "pending_approval"           # never auto-sent
    assert db.query(Job).count() == 0                   # no invented posting


def test_process_matches_the_normalised_name_without_wildcards(client, auth, uploaded_resume, db):
    """Related bugs: ``ilike`` treated ``%`` as a wildcard and missed casing.

    The name now arrives in the body (v2.2.9), where ``%``/``_`` are not even
    URL-special — and they are still plain characters for the lookup.
    """
    user = db.query(User).filter(User.email == "owner@example.com").first()
    db.add(FundingCompany(user_id=user.id, name="Northwind AI", source="sec_edgar", verified=True,
                          stage="Seed", meta={}))
    db.commit()

    assert client.post("/api/funding/companies/process", json={"company": "northwind ai"}, headers=auth).status_code == 200
    assert client.post("/api/funding/companies/process", json={"company": "%"}, headers=auth).status_code == 404
    assert client.post("/api/funding/companies/process", json={"company": "_"}, headers=auth).status_code == 404


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

    first = client.post("/api/funding/companies/process", json={"company": "Alpha Co"}, headers=auth)
    second = client.post("/api/funding/companies/process", json={"company": "Beta Co"}, headers=auth)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    jobs = db.query(Job).all()
    assert len(jobs) == 2
    assert len({job.dedupe_key for job in jobs}) == 2
    assert all(job.score_source == "funding_context" for job in jobs)

    # Re-processing the same company is idempotent (it is already tracked).
    again = client.post("/api/funding/companies/process", json={"company": "Alpha Co"}, headers=auth).json()
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

    result = client.post("/api/funding/companies/process", json={"company": "Fact Co"}, headers=auth).json()
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
    # v2.2.5 re-adds has_open_positions as a live linkage flag kept fresh both directions
    assert "has_open_positions" in columns, "v2.2.5 re-introduces has_open_positions"
    assert "last_seen_at" in columns
    indexes = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='funding_companies'")}
    assert "ix_funding_user_seen" in indexes, "prune needs the (user_id, last_seen_at) index"
    # v2.2.5 history tables
    assert "funding_scans" in [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")], "funding_scans history table"
    assert "funding_scan_companies" in [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")], "funding_scan_companies membership"

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

    # Reversible — pinned to the *revision*, not to ``-1``. The chain grows every
    # release (v2.2 added ``scheduled_runs`` on top of this one), and relative
    # steps then undo the wrong migration: with ``-1`` this assert was checking
    # that a *different* migration could be rolled back, i.e. it was testing the
    # position of head rather than the reversibility of this revision.
    alembic("downgrade", PRE_FUNDING_HONESTY_REVISION)
    con = sqlite3.connect(db)
    assert "has_open_positions" in [r[1] for r in con.execute("PRAGMA table_info(funding_companies)")]
    con.close()
    alembic("upgrade", "head")


# =========================================================================== #
# 7. Web-search provider path (v2.2.6) — efts.sec.gov is bypassed when a
#    search engine is configured; the direct EDGAR path stays honest when not.
#
#  Contract under test:
#  1. With FUNDING_SEARCH_* configured, a scan that used to hit the blocked
#     EDGAR endpoint runs through the search API instead — zero requests to
#     efts.sec.gov, scan_status=ok, events extracted from snippets with a
#     citation (url/snippet from the cited result row, never from the model).
#  2. Without a configured search provider, the direct EDGAR path keeps its
#     robots check: robots-blocked → scan_failed + provider_errors.sec_edgar
#     (an honest 200 body, never a 500, never a fake empty radar).
#  3. Search-provider failures are data: provider_errors.search on the scan
#     report; transport failures retry once, 4xx never.
#  4. The history contract is unchanged (persist_funding_scan stores
#     provider_errors.search like any other provider error).
# =========================================================================== #
def _search_rows(*names: str, days_ago: int = 5) -> list:
    """Realistic search rows (as tavily would return them) for given names."""
    from datetime import datetime as _dt

    published = (_dt.utcnow() - timedelta(days=days_ago)).replace(microsecond=0).isoformat() + "Z"
    rows = []
    for name in names:
        rows.append({
            "title": f"{name} raises $12M Series A for its platform",
            "url": f"https://example.com/press/{normalize_company_name(name).replace(' ', '-')}",
            "snippet": f"{name} announced a $12 million Series A round to grow its platform and engineering team.",
            "published_at": published,
            "query": "ai startup funding round",
        })
    return rows


def _script_search_ai(monkeypatch, extract=None, rank=None):
    """Configure AI and script the two funding_scan passes separately.

    The search path makes ONE extraction call (contract ``funding-extract-v1``)
    inside the provider and ONE ranking call in the radar; the dispatcher
    splits them by contract marker so a test can script each.
    """
    monkeypatch.setattr("app.core.config.settings.ai_api_key", "sk-hermetic-funding-test")

    async def fake_completion(workflow, prompt, **kwargs):
        assert workflow == "funding_scan"
        if "funding-extract-v1" in prompt:
            fake_completion.extract_prompts.append(prompt)
            return extract(prompt) if callable(extract) else extract
        fake_completion.rank_prompts.append(prompt)
        return rank(prompt) if callable(rank) else rank

    fake_completion.extract_prompts = []
    fake_completion.rank_prompts = []
    monkeypatch.setattr("app.services.ai_client.chat_completion", fake_completion)
    return fake_completion


def _forbid_outbound(monkeypatch):
    """Any outbound request on the search path is a contract violation."""
    seen: list = []

    async def forbidden(method, url, **kwargs):
        seen.append(str(url))
        raise AssertionError(f"no outbound request may be made on this path: {url}")

    monkeypatch.setattr("app.services.http.request", forbidden)
    return seen


def test_funding_search_settings_resolution(monkeypatch):
    """The resolver matrix + secret-free public_settings exposure."""
    from app.core.config import resolve_funding_search_provider

    assert resolve_funding_search_provider("", "") == ""            # off: direct path
    assert resolve_funding_search_provider("", "tvly-x") == "tavily"  # auto via key
    assert resolve_funding_search_provider("auto", "tvly-x") == "tavily"
    assert resolve_funding_search_provider("tavily", "") == ""      # key missing
    assert resolve_funding_search_provider("TAVILY", "tvly-x") == "tavily"
    assert resolve_funding_search_provider("stub", "") == "stub"    # hermetic, keyless
    assert resolve_funding_search_provider("serpapi", "key") == ""  # unknown → off + hint

    from app.core.config import settings

    monkeypatch.setattr(settings, "funding_search_provider", "tavily")
    monkeypatch.setattr(settings, "funding_search_api_key", "tvly-secret-value")
    payload = settings.public_settings()
    assert payload["funding_search_configured"] is True
    assert payload["funding_search_provider"] == "tavily"
    assert "tvly-secret-value" not in repr(payload)


@pytest.mark.asyncio
async def test_search_provider_replaces_edgar_and_never_touches_efts(monkeypatch):
    """Acceptance 1: tavily configured → the scan runs on the search path."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "funding_search_provider", "tavily")
    monkeypatch.setattr(settings, "funding_search_api_key", "tvly-hermetic")
    queries: list = []

    async def fake_search(query, *, window_days, limit):
        queries.append(query)
        assert window_days == 30 and limit <= funding_sources.SEARCH_RESULTS_PER_QUERY
        return _search_rows("VectorLoom AI")

    monkeypatch.setattr(funding_search, "search", fake_search)
    outbound = _forbid_outbound(monkeypatch)
    fake = _script_search_ai(
        monkeypatch,
        extract=lambda prompt: {"companies": [{"name": "VectorLoom AI", "source_index": 0,
                                               "stage": "Series A", "raised_usd": 12000000,
                                               "raised_at": None, "industry": "ai infra"}]},
        rank=lambda prompt: {"companies": [{"name": "VectorLoom AI", "matched": True, "rank": 1,
                                            "why": "Series A ai infra round."}]},
    )

    events, report = await funding_sources.fetch_funding_events(
        {"funding_focus": ["ai infra"]}, window_days=30, limit=5, provider="sec_edgar")

    assert report["providers"] == ["search"], "the sec_edgar slot is replaced by search"
    assert report["counts"] == {"search": 1}
    assert report["scan_status"] == "ok"
    assert outbound == [] and not any("efts.sec.gov" in q for q in queries)
    assert len(fake.extract_prompts) == 1 and len(fake.rank_prompts) == 0  # ranking happens in the radar
    assert any("ai infra" in q for q in queries), "queries are built from the search context"
    assert events[0].name == "VectorLoom AI" and events[0].source == "search"
    assert events[0].verified is True and "example.com/press" in events[0].url


@pytest.mark.asyncio
async def test_search_events_cite_the_result_row_not_the_model(monkeypatch):
    """url, snippet and summary come from the cited row; the model only parses."""
    rows = _search_rows("Nightowl Robotics", days_ago=3)
    _script_search_ai(
        monkeypatch,
        extract=lambda prompt: {"companies": [{"name": "Nightowl Robotics", "source_index": 0,
                                               "stage": "seed", "raised_usd": 6500000,
                                               "raised_at": "2026-09-01", "industry": "robotics"}]},
    )
    events = await funding_sources.ai_extract_events_from_results(
        rows, {"funding_focus": ["robotics"]}, search_provider_id="tavily", window_days=45)

    assert len(events) == 1
    event = events[0]
    assert event.url == rows[0]["url"]                    # from the row…
    assert event.meta["snippet"] == rows[0]["snippet"]    # …not the model
    assert event.summary.startswith(rows[0]["title"]) and rows[0]["snippet"] in event.summary
    assert event.stage == "Seed"                          # model-parsed, normalised
    assert event.raised_usd == 6_500_000                  # model-parsed from the snippet
    assert event.raised_at is not None and event.raised_at.date().isoformat() == "2026-09-01"
    assert event.meta["search_provider"] == "tavily" and event.meta["query"]
    assert event.verified is True
    # An unparsable model date falls back to the row's published_at.
    _script_search_ai(
        monkeypatch,
        extract=lambda prompt: {"companies": [{"name": "Nightowl Robotics", "source_index": 0,
                                               "stage": "Undisclosed", "raised_at": "recently"}]},
    )
    (fallback,) = await funding_sources.ai_extract_events_from_results(
        rows, {}, search_provider_id="tavily", window_days=45)
    assert fallback.raised_at is not None and fallback.meta["raised_at_estimated"] is False


@pytest.mark.asyncio
async def test_search_extraction_rejects_ungrounded_company(monkeypatch):
    """A name no result text contains is fabrication → the answer is rejected."""
    from app.services.ai_guardrails import AIUnavailableError

    _script_search_ai(
        monkeypatch,
        extract={"companies": [{"name": "Totally Made Up Corp", "source_index": 0,
                                "stage": "Seed", "raised_usd": None}]},
    )
    with pytest.raises(AIUnavailableError) as excinfo:
        await funding_sources.ai_extract_events_from_results(
            _search_rows("Nightowl Robotics"), {}, search_provider_id="tavily")
    assert excinfo.value.reason == "guardrail_failed"
    assert excinfo.value.state == "blocked_needs_action"
    assert "Totally Made Up Corp" in excinfo.value.detail


@pytest.mark.asyncio
async def test_search_provider_failure_is_provider_errors_search(monkeypatch):
    """Acceptance contract: search failures surface as provider_errors.search."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "funding_search_provider", "tavily")
    monkeypatch.setattr(settings, "funding_search_api_key", "tvly-hermetic")
    monkeypatch.setattr(funding_sources, "PROVIDER_RETRY_DELAY_SECONDS", 0.0)
    _script_search_ai(monkeypatch)  # configured, never reached

    calls: list = []

    async def failing_search(query, *, window_days, limit):
        calls.append(query)
        raise funding_search.FundingSearchError("tavily returned HTTP 401", status=401)

    monkeypatch.setattr(funding_search, "search", failing_search)
    events, report = await funding_sources.fetch_funding_events(
        {"funding_focus": ["ai"]}, provider="sec_edgar")
    assert events == []
    assert report["scan_status"] == "scan_failed"
    assert "search" in report["errors"] and "401" in report["errors"]["search"]
    assert len(calls) == 1, "a 4xx search failure is not retried"

    transport_calls: list = []

    async def flaky_search(query, *, window_days, limit):
        transport_calls.append(query)
        raise funding_search.FundingSearchError("connection reset")

    monkeypatch.setattr(funding_search, "search", flaky_search)
    events, report = await funding_sources.fetch_funding_events(
        {"funding_focus": ["ai"]}, provider="sec_edgar")
    assert len(transport_calls) == 2, "a transport failure gets exactly one retry"
    assert report["scan_status"] == "scan_failed" and "search" in report["errors"]


@pytest.mark.asyncio
async def test_robots_blocked_edgar_without_search_key_stays_honest(monkeypatch):
    """Acceptance 2: no search key + robots block → scan_failed, never a 500."""
    import app.services.robots as robots

    async def disallowed(url, user_agent=None):
        return False

    monkeypatch.setattr(robots, "can_fetch", disallowed)
    events, report = await funding_sources.fetch_funding_events(
        {"funding_focus": ["ai"]}, provider="sec_edgar")
    assert funding_search.search_configured() is False  # the default: no key
    assert events == []
    assert report["scan_status"] == "scan_failed"
    assert "sec_edgar" in report["errors"]
    assert "robots.txt" in report["errors"]["sec_edgar"]
    assert len(report["errors"]["sec_edgar"]) <= 300


@pytest.mark.asyncio
async def test_tavily_wire_parsing_and_robots_not_consulted(monkeypatch):
    """The tavily call is an authenticated API RPC: no robots check, key in the header.

    The shared client is still the (guard-vetted) gateway — only the robots
    step is skipped, because robots.txt governs crawling published pages, not
    a JSON API the key licenses us to call.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "funding_search_provider", "tavily")
    monkeypatch.setattr(settings, "funding_search_api_key", "tvly-wire-test")
    captured: dict = {}

    async def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return FakeResponse({"results": [
            {"title": "Wire Co raises $5M Seed", "url": "https://example.com/wire",
             "content": "Wire Co closed a $5 million Seed round.",
             "published_date": "2026-09-10"},
        ]})

    monkeypatch.setattr("app.services.http.request", fake_request)
    rows = await funding_search.search("ai startup funding round", window_days=30, limit=5)
    assert captured["method"] == "POST" and captured["url"] == funding_search.TAVILY_ENDPOINT
    assert captured["json_body"]["query"] == "ai startup funding round"
    assert captured["json_body"]["days"] == 30
    assert captured["respect_robots"] is False
    assert captured["headers"]["Authorization"] == "Bearer tvly-wire-test"
    assert rows == [{"title": "Wire Co raises $5M Seed", "url": "https://example.com/wire",
                     "snippet": "Wire Co closed a $5 million Seed round.",
                     "published_at": "2026-09-10"}]

    async def unauthorized(method, url, **kwargs):
        return FakeResponse(None, status_code=401)

    monkeypatch.setattr("app.services.http.request", unauthorized)
    with pytest.raises(funding_search.FundingSearchError) as excinfo:
        await funding_search.search("q", window_days=30, limit=5)
    assert excinfo.value.status == 401


@pytest.mark.asyncio
async def test_ai_outage_during_extraction_propagates_not_scan_failed(monkeypatch):
    """An AI outage inside the extraction pass follows the v2.1 outage contract.

    It must reach the caller (→ pausable 503 / blocked, queue pause) — never be
    swallowed into provider_errors.search, which would present an AI outage as
    "no provider returned data".
    """
    from app.core.config import settings
    from app.services.ai_client import AIClientError

    monkeypatch.setattr(settings, "funding_search_provider", "tavily")
    monkeypatch.setattr(settings, "funding_search_api_key", "tvly-hermetic")

    async def fake_search(query, window_days=45, limit=8):
        return _search_rows("VectorLoom AI")

    monkeypatch.setattr(funding_search, "search", fake_search)

    async def ai_down(workflow, prompt, **kwargs):
        raise AIClientError("scripted transient outage", reason="provider_error", retryable=True)

    monkeypatch.setattr("app.services.ai_client.chat_completion", ai_down)
    with pytest.raises(AIClientError):
        await funding_sources.fetch_funding_events({"funding_focus": ["ai"]}, provider="sec_edgar")


def test_companies_refresh_with_tavily_end_to_end_default_view(client, auth, db, monkeypatch, ai_configured):
    """Acceptance 1 verbatim: robots on + tavily + key → refresh=true → ok.

    Companies reach the *default* view (no include_unverified): a real search
    publication is a checkable citation, so its events are verified=True. The
    recorded wire shows zero requests to efts.sec.gov.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "funding_search_provider", "tavily")
    monkeypatch.setattr(settings, "funding_search_api_key", "tvly-e2e")
    wire: list = []

    async def fake_request(method, url, **kwargs):
        wire.append(str(url))
        return FakeResponse({"results": [
            {"title": "VectorLoom AI raises $12M Series A for its platform",
             "url": "https://example.com/press/vectorloom",
             "content": ("VectorLoom AI announced a $12 million Series A round to grow its "
                         "platform and engineering team."),
             "published_date": (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")},
        ]})

    monkeypatch.setattr("app.services.http.request", fake_request)

    body = client.get("/api/funding/companies?refresh=true", headers=auth).json()
    assert body["scan_status"] == "ok", body
    assert [row["name"] for row in body["companies"]] == ["VectorLoom AI"]
    assert body["companies"][0]["verified"] is True and body["companies"][0]["source"] == "search"
    assert body["last_report"]["counts"] == {"search": 1}
    assert wire and all("efts.sec.gov" not in url for url in wire), wire


def test_companies_refresh_with_stub_search_end_to_end(client, auth, db, monkeypatch, ai_configured):
    """Acceptance 1 end to end: stub search → ok scan, persisted history, no EDGAR.

    The stub provider is hermetic (deterministic corpus, no network); its rows
    are synthetic, so the extracted events are labelled ``verified=False`` and
    need ``include_unverified=true`` — the same honesty as the demo dataset.
    """
    from app.core.config import settings
    from app.models.models import FundingScan, FundingScanCompany

    monkeypatch.setattr(settings, "funding_search_provider", "stub")
    _forbid_outbound(monkeypatch)

    body = client.get("/api/funding/companies?refresh=true&include_unverified=true",
                      headers=auth).json()
    assert body["scan_status"] == "ok", body
    names = {row["name"] for row in body["companies"]}
    assert {"VectorLoom AI", "Nightowl Robotics", "Cobalt Health"} <= names
    assert all(row["source"] == "search" for row in body["companies"])
    assert all(row["verified"] is False for row in body["companies"])   # stub = unverified
    assert all(row["url"].startswith("https://example.com/") for row in body["companies"])
    assert body["last_report"]["counts"] == {"search": 3}
    assert body["last_report"]["providers"] == ["search"]
    assert body["last_report"].get("errors") in ({}, None)

    scans = db.query(FundingScan).order_by(FundingScan.id.desc()).all()
    assert scans and scans[0].status == "ok" and scans[0].provider_errors == {}
    assert scans[0].companies_found == len(body["companies"])
    assert db.query(FundingScanCompany).filter(
        FundingScanCompany.scan_id == scans[0].id).count() == scans[0].companies_found

    # History contract unchanged: the scan is retrievable via /funding/history.
    history = client.get("/api/funding/history?limit=5", headers=auth).json()["scans"]
    assert history and history[0]["status"] == "ok" and history[0]["companies"]


def test_providers_endpoint_exposes_search_status(client, auth, monkeypatch):
    """GET /api/funding/providers carries the search block (configured/hint)."""
    from app.core.config import settings

    body = client.get("/api/funding/providers", headers=auth).json()
    assert body["search"]["configured"] is False
    assert body["search"]["provider"] == ""
    assert "FUNDING_SEARCH" in body["search"]["hint"]
    assert {row["id"] for row in body["providers"]} >= {"sec_edgar", "search"}
    search_row = next(row for row in body["providers"] if row["id"] == "search")
    assert search_row["configured"] is False

    monkeypatch.setattr(settings, "funding_search_provider", "tavily")
    body = client.get("/api/funding/providers", headers=auth).json()
    assert body["search"]["configured"] is False           # key still missing
    assert body["search"]["hint"] == "FUNDING_SEARCH_PROVIDER=tavily needs FUNDING_SEARCH_API_KEY"

    monkeypatch.setattr(settings, "funding_search_api_key", "tvly-hermetic")
    body = client.get("/api/funding/providers", headers=auth).json()
    assert body["search"] == {"provider": "tavily", "configured": True, "hint": ""}


@pytest.mark.asyncio
async def test_provider_id_is_case_insensitive(monkeypatch):
    """Small bug fix: FUNDING_PROVIDER=SEC_EDGAR used to be unknown_provider."""
    from app.core.config import settings

    async def fake_request(method, url, **kwargs):
        return FakeResponse({"hits": {"hits": [
            {"_id": "a:b", "_source": {"display_names": ["Case Co"], "file_date": "2026-09-10",
                                       "ciks": ["7"]}}]}})

    monkeypatch.setattr("app.services.http.request", fake_request)
    monkeypatch.setattr(settings, "funding_provider", "SEC_EDGAR")
    events, report = await funding_sources.fetch_funding_events({"keywords": ["ai"]}, limit=5)
    assert report["scan_status"] == "ok", report["errors"]
    assert report["counts"]["sec_edgar"] == 1 and events[0].name == "Case Co"


# =========================================================================== #
# 8. v2.2.9 — the company name is data, not a URL path; and one funding scan
#    costs exactly one unit, in whichever mode it ran.
#
#  Contract under test:
#  1. Storage identity is the *normalised* name: UNIQUE(user_id,
#     name_normalized), the sync keys on it, and the migration backfills it and
#     collapses an existing duplicate pair keeping the oldest row.
#  2. ``POST /funding/companies/process`` takes the company in the body, so
#     spaces, unicode, quotes and slashes need no encoding and cannot fork the
#     route; ``company_id`` works too.
#  3. One charge point: a successful scan costs exactly 1
#     ``funding_companies_per_month``, sync or queued; a failed queued scan
#     costs nothing (and the trigger no longer spends the quota up front).
# =========================================================================== #
def _usage(db, user_id: int) -> int:
    from app.core.entitlements import current_period
    from app.models.models import UsageCounter

    row = (db.query(UsageCounter)
           .filter(UsageCounter.user_id == int(user_id),
                   UsageCounter.period == current_period(),
                   UsageCounter.capability == "funding_companies_per_month")
           .first())
    return int(row.count) if row else 0


def test_normalized_name_is_the_storage_identity(db, owner):
    """Two casings/spacings of one name are one row — enforced by the DB."""
    import sqlalchemy.exc

    user = db.query(User).order_by(User.id).first()
    db.add(FundingCompany(user_id=user.id, name="Acme Inc.", source="imported", verified=True))
    db.commit()
    row = db.query(FundingCompany).filter(FundingCompany.user_id == user.id).one()
    assert row.name_normalized == "acme inc."

    db.add(FundingCompany(user_id=user.id, name="  acme   inc.  ", source="imported", verified=True))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        db.commit()
    db.rollback()

    # And the sync agrees: the second sighting updates, never inserts.
    stats = funding_radar.sync_funding_db(
        db, user.id, [{"name": "ACME Inc.", "stage": "Seed", "source": "imported", "verified": True}], 30)
    assert stats["added"] == 0 and stats["updated"] == 1
    assert db.query(FundingCompany).filter(FundingCompany.user_id == user.id).count() == 1
    assert db.query(FundingCompany).filter(FundingCompany.user_id == user.id).one().name == "Acme Inc."


def test_process_takes_the_company_in_the_body(client, auth, uploaded_resume, db):
    """Spaces, unicode, quotes and a slash: data in a body, not a path segment."""
    user = db.query(User).filter(User.email == "owner@example.com").first()
    awkward = 'Bjørn & Søn "Grüße" A/S'
    db.add(FundingCompany(user_id=user.id, name=awkward, source="sec_edgar", verified=True,
                          stage="Seed", meta={}))
    db.commit()

    by_name = client.post("/api/funding/companies/process", json={"company": awkward}, headers=auth)
    assert by_name.status_code == 200, by_name.text
    assert by_name.json()["company"] == awkward

    # A different casing/spacing of the same name finds the same row…
    variant = client.post("/api/funding/companies/process",
                          json={"company": '  bjørn & søn "grüße" a/s '}, headers=auth)
    assert variant.status_code == 200, variant.text

    # …and so does the stable id.
    row = db.query(FundingCompany).filter(FundingCompany.user_id == user.id).one()
    by_id = client.post("/api/funding/companies/process", json={"company_id": row.id}, headers=auth)
    assert by_id.status_code == 200, by_id.text

    # Another user's id is not theirs to act on, and an empty body is a 422.
    assert client.post("/api/funding/companies/process", json={}, headers=auth).status_code == 422


def test_successful_sync_scan_charges_exactly_one(client, auth, db, monkeypatch):
    """The interactive radar used to charge ``len(companies)`` per scan."""
    user = db.query(User).order_by(User.id).first()
    _install_providers(monkeypatch, {"sec_edgar": _static_provider(_events("Acme", "Beta", "Gamma"))})
    _script_ai(monkeypatch, lambda prompt: {"companies": [
        {"name": name, "matched": True, "rank": index + 1, "why": "fintech seed round."}
        for index, name in enumerate(("Acme", "Beta", "Gamma"))]})

    assert _usage(db, user.id) == 0
    body = client.get("/api/funding/companies?refresh=true", headers=auth)
    assert body.status_code == 200, body.text
    assert len(body.json()["companies"]) == 3
    assert _usage(db, user.id) == 1, "one scan, one unit — not one per company"


@pytest.mark.asyncio
async def test_queued_scan_charges_once_on_success_and_never_on_failure(client, auth, db, monkeypatch):
    """The queued path bills the same as the sync one — and only when it worked."""
    from app.services.job_queue import enqueue
    from app.worker import Worker

    user = db.query(User).order_by(User.id).first()

    # 1. Trigger only *checks* the quota — it no longer spends it up front.
    receipt = client.post("/api/funding/refresh", json={}, headers=auth)
    assert receipt.status_code == 200, receipt.text
    assert _usage(db, user.id) == 0, "queueing a scan is not doing one"

    # 2. A failed (blocked, no AI key) queued scan consumes nothing.
    _install_providers(monkeypatch, {"sec_edgar": _static_provider(_events("Acme"))})
    failing = enqueue(db, user_id=user.id, pipeline="funding",
                      payload={"window_days": 30, "provider": "sec_edgar", "context": {"keywords": ["ai"]}},
                      dedupe_key="funding:charge-fail")
    await Worker(pipelines=["funding"])._run_item(failing.id, "funding")
    db.refresh(failing)
    assert failing.status == "dead", failing.error
    assert _usage(db, user.id) == 0, "a failed scan must not consume quota"

    # 3. A successful queued scan consumes exactly one.
    _script_ai(monkeypatch, lambda prompt: {"companies": [
        {"name": "Acme", "matched": True, "rank": 1, "why": "fintech seed round."}]})
    ok = enqueue(db, user_id=user.id, pipeline="funding",
                 payload={"window_days": 30, "provider": "sec_edgar", "context": {"keywords": ["ai"]}},
                 dedupe_key="funding:charge-ok")
    await Worker(pipelines=["funding"])._run_item(ok.id, "funding")
    db.refresh(ok)
    assert ok.status == "done", ok.error
    assert ((ok.payload or {}).get("result") or {}).get("charged") == 1
    assert _usage(db, user.id) == 1, "one successful scan, one unit — same as the sync path"


def test_migration_backfills_and_collapses_normalized_duplicates(tmp_path):
    """A populated previous-head database: backfill, dedupe (oldest wins), UNIQUE."""
    import os
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "fundnorm.db"
    env = {**os.environ,
           "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
           "ALEMBIC_DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
           "ENVIRONMENT": "test", "SECRET_KEY": "0" * 48}

    def alembic(*args: str) -> None:
        out = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=backend,
                             env=env, capture_output=True, text=True, timeout=300)
        assert out.returncode == 0, (out.stdout + out.stderr)[-2000:]

    alembic("upgrade", "e5f6a7b8c9d0")            # the pre-normalized-name schema

    old, new = "2026-07-01 00:00:00", "2026-09-01 00:00:00"
    con = sqlite3.connect(db_path)
    for name, seen in (("Acme Inc.", old), ("acme inc.", new), ("Zeta", old)):
        con.execute(
            "INSERT INTO funding_companies (user_id,name,stage,source,verified,discovered_at,"
            "last_seen_at,has_open_positions) VALUES (1,?,'Seed','sec_edgar',1,?,?,0)",
            (name, seen, seen))
    con.execute("INSERT INTO funding_companies (user_id,name,stage,source,verified,discovered_at,"
                "last_seen_at,has_open_positions) VALUES (2,'Acme Inc.','Seed','sec_edgar',1,?,?,0)",
                (old, old))
    con.execute("INSERT INTO funding_scans (user_id,scanned_at,status,events_seen,companies_found) "
                "VALUES (1,?, 'ok',1,1)", (new,))
    con.execute("INSERT INTO funding_scan_companies (scan_id,company_id,rank,why) VALUES (1,2,1,'dupe')")
    con.commit()
    con.close()

    alembic("upgrade", "head")

    con = sqlite3.connect(db_path)
    rows = list(con.execute("SELECT id,user_id,name,name_normalized,discovered_at "
                            "FROM funding_companies ORDER BY id"))
    assert [(r[1], r[2]) for r in rows] == [(1, "Acme Inc."), (1, "Zeta"), (2, "Acme Inc.")], rows
    assert rows[0][3] == "acme inc.", "the key is backfilled with the app's rule"
    assert rows[0][4] == old, "the oldest row of a duplicate pair survives"
    # The history of the collapsed row is re-pointed, never orphaned or dropped.
    assert list(con.execute("SELECT scan_id,company_id FROM funding_scan_companies")) == [(1, 1)]
    try:
        con.execute("INSERT INTO funding_companies (user_id,name,name_normalized,verified,"
                    "has_open_positions) VALUES (1,'ACME  inc.','acme inc.',1,0)")
        raise AssertionError("UNIQUE(user_id, name_normalized) is not enforced")
    except sqlite3.IntegrityError:
        pass
    con.close()

    alembic("downgrade", "e5f6a7b8c9d0")
    con = sqlite3.connect(db_path)
    assert "name_normalized" not in [r[1] for r in con.execute("PRAGMA table_info(funding_companies)")]
    con.close()
    alembic("upgrade", "head")
