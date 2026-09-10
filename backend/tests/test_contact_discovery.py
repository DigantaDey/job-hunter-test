"""Contact discovery: provider parsing, verification honesty, no fabricated emails."""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.services import contact_discovery as cd


class FakeResponse:
    def __init__(self, payload=None, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def offline_mx(monkeypatch):
    """MX lookups need DNS; tests must not depend on the network."""
    monkeypatch.setattr(settings, "contact_verify_mx", False)
    yield


def test_company_domain_from_website_then_name():
    assert cd.company_domain("Acme", "https://www.acme.io/careers") == "acme.io"
    assert cd.company_domain("Acme Corp", "") == "acmecorp.com"
    assert cd.company_domain("", "") == ""


def test_verify_email_rejects_bad_syntax_and_disposable_domains():
    bad = cd.verify_email("not-an-email")
    assert bad["syntax_ok"] is False and bad["score"] == 0.0

    disposable = cd.verify_email("someone@mailinator.com")
    assert disposable["syntax_ok"] is True
    assert disposable["disposable"] is True
    assert disposable["score"] == 0.0
    assert disposable["reason"] == "disposable domain"


def test_verify_email_scores_and_flags_role_accounts():
    result = cd.verify_email("jane.doe@acme.io")
    assert result["syntax_ok"] is True
    assert result["score"] == pytest.approx(0.6)  # 0.4 base + 0.2 syntax (MX unknown)
    assert result["mx_ok"] is None, "an unknown MX result must never be reported as invalid"
    assert result["freemail"] is False

    role = cd.verify_email("hiring@acme.io")
    assert role["role_account"] is True
    assert role["score"] < result["score"]


def test_heuristic_contacts_are_explicitly_unverified():
    contacts = cd.heuristic_contacts("Acme", "acme.io", "engineering")
    assert contacts
    assert all(c["source"] == "heuristic" and c["verified"] is False for c in contacts)
    assert {c["email"].split("@")[0] for c in contacts} >= {"engineering", "hiring", "jobs"}


@pytest.mark.asyncio
async def test_hunter_requires_a_key(monkeypatch):
    monkeypatch.setattr(settings, "hunter_api_key", "")
    assert await cd._hunter("acme.io", "engineering") == []


@pytest.mark.asyncio
async def test_hunter_results_are_marked_verified(monkeypatch):
    monkeypatch.setattr(settings, "hunter_api_key", "hunter-key")

    async def fake_get_json(url, **kwargs):
        assert "domain-search" in url
        return {"data": {"emails": [
            {"value": "jane@acme.io", "first_name": "Jane", "last_name": "Doe",
             "position": "CTO", "confidence": 95},
        ]}}

    monkeypatch.setattr("app.services.contact_discovery.http_client.get_json", fake_get_json)
    contacts = await cd._hunter("acme.io", "engineering")
    assert contacts[0]["email"] == "jane@acme.io"
    assert contacts[0]["confidence"] == 0.95
    assert contacts[0]["source"] == "hunter" and contacts[0]["verified"] is True


@pytest.mark.asyncio
async def test_discovery_ranks_verified_contacts_first(monkeypatch):
    monkeypatch.setattr(settings, "hunter_api_key", "hunter-key")

    async def fake_get_json(url, **kwargs):
        return {"data": {"emails": [{"value": "jane@acme.io", "first_name": "Jane", "last_name": "Doe",
                                     "position": "CTO", "confidence": 90}]}}

    monkeypatch.setattr("app.services.contact_discovery.http_client.get_json", fake_get_json)
    result = await cd.discover_decision_makers("Acme", domain="acme.io")
    assert result["providers"] == ["hunter"]
    assert result["best"]["email"] == "jane@acme.io"
    assert result["best"]["verified"] is True
    emails =[c["email"] for c in result["contacts"]]
    assert len(emails) == len(set(emails)), "contacts must be de-duplicated"


@pytest.mark.asyncio
async def test_discovery_falls_back_to_role_mailboxes(monkeypatch):
    monkeypatch.setattr(settings, "hunter_api_key", "")
    monkeypatch.setattr(settings, "apollo_api_key", "")
    result = await cd.discover_decision_makers("Acme", domain="acme.io", department="hiring")
    assert result["providers"] == ["heuristic"]
    assert result["best"]["verified"] is False
    assert result["best"]["verification"]["score"] < 1.0


@pytest.mark.asyncio
async def test_find_decision_maker_never_invents_an_address():
    """With no resolvable domain the helper must admit it found nobody."""
    result = await cd.find_decision_maker("", department="hiring")
    assert result["source"] == "none"
    assert result["email"] == ""
    assert result["verification"]["reason"] == "no contact could be resolved"


def test_contacts_endpoint_returns_provider_transparency(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "hunter_api_key", "")
    monkeypatch.setattr(settings, "apollo_api_key", "")
    response = client.get("/api/emails/contacts?company=FinCo", headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["company"] == "FinCo"
    assert "heuristic" in body["providers"]
    assert body["contacts"]
    assert all(contact["verified"] is False for contact in body["contacts"])
