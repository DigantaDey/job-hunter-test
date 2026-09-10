"""Funding radar: real providers, honest labelling and anti-fabrication guard."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models.models import FundingCompany, User
from app.services import funding_radar, funding_sources
from app.services.funding_sources import FundingEvent, ai_rank_events, normalize_stage


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
    events = await funding_sources.fetch_funding_events({"funding_focus": ["ai"]}, provider="sec_edgar", limit=5)
    companies, report = events
    assert report["counts"]["sec_edgar"] == 1
    company = companies[0]
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

    monkeypatch.setattr(settings, "allow_synthetic_funding_data", True)
    events, report = await funding_sources.fetch_funding_events({"funding_focus": ["ai/ml"]}, provider="demo", limit=5)
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
async def test_ai_ranking_cannot_invent_companies(monkeypatch):
    real = [FundingEvent(name="Acme", stage="Seed", industry="fintech"),
            FundingEvent(name="Beta", stage="Series A", industry="saas")]

    async def fake_completion(workflow, prompt, **kwargs):
        return {"order": ["Totally Made Up Corp", "Beta", "Acme"]}

    monkeypatch.setattr("app.services.ai_client.chat_completion", fake_completion)
    ranked = await ai_rank_events({"funding_focus": ["fintech"]}, real)
    assert [event.name for event in ranked] == ["Beta", "Acme"]
    assert all(event.name in {"Acme", "Beta"} for event in ranked)


@pytest.mark.asyncio
async def test_scan_respects_stage_filter_and_window(monkeypatch):
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

    stale = db.query(FundingCompany).filter(FundingCompany.name == "Old Co").first()
    stale.discovered_at = datetime.utcnow() - timedelta(days=45)
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
    user = db.query(User).filter(User.email == "owner@example.com").first()
    db.add(FundingCompany(user_id=user.id, name="Hiring Co", source="sec_edgar", verified=True,
                          has_open_positions=True, stage="Seed", website="hiring.co", industry="fintech"))
    db.add(FundingCompany(user_id=user.id, name="Quiet Co", source="sec_edgar", verified=True,
                          has_open_positions=False, stage="Seed", website="quiet.co", industry="fintech"))
    db.commit()

    hiring = client.post("/api/funding/Hiring Co/process", headers=auth).json()
    assert hiring["action"] == "apply_flow"
    assert hiring["job_id"]

    quiet = client.post("/api/funding/Quiet Co/process", headers=auth).json()
    assert quiet["action"] == "cold_email_founder"
    assert quiet["email_id"]
    emails = client.get("/api/emails", headers=auth).json()
    assert emails and emails[0]["status"] == "pending_approval"


def test_funding_providers_endpoint(client, auth):
    body = client.get("/api/funding/providers", headers=auth).json()
    assert body["active"] in {"sec_edgar", "crunchbase", "tracxn", "imported", "demo"}
    assert any(row["id"] == "sec_edgar" for row in body["providers"])
