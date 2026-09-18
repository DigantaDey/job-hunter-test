"""Hermetic adapter tests: recorded payloads, no network.

Pins the v2.2.21 source contract: every adapter returns the same ``Posting``
schema, duplicates merge by canonical identity, expired listings are not
fresh matches, and a failing source never stops the others.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.models.models import Job, User
from app.services.sources import ADAPTERS, fetch_all, list_sources
from app.services.sources import health as source_health
from app.services.sources.adapters import (
    AdzunaSource,
    ArbeitnowSource,
    AshbySource,
    GreenhouseSource,
    HimalayasSource,
    IndeedSource,
    InstahyreSource,
    JobicySource,
    JoobleSource,
    LeverSource,
    LinkedInSource,
    NaukriSource,
    PersonioSource,
    RecruiteeSource,
    RemoteOKSource,
    RemotiveSource,
    SmartRecruitersSource,
    TheMuseSource,
    USAJobsSource,
    WeWorkRemotelySource,
    WorkableSource,
    WorkdaySource,
)
from app.services.sources.base import Posting, SourceError, compact_raw, merge_postings
from app.services.sources import health as source_health

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sources"

REQUIRED_POSTING_KEYS = {
    "title", "company", "location", "description", "url", "source",
    "external_id", "posted_at", "salary", "remote", "industry", "extra",
    "dedupe_key", "canonical_id", "content_hash", "source_kind",
    "adapter_version", "fetched_at", "expired", "expires_at",
    "title_normalized", "company_name_normalized", "raw",
}


class FakeResponse:
    def __init__(self, payload=None, status_code: int = 200, text: str = "", headers=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text or ""
        self.headers = headers or {"content-type": "application/json"}
        self.request = None

    def json(self):
        return self._payload


def _load_json(name: str):
    return json.loads((FIXTURES / name).read_text())


def _load_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def _async_json(name: str):
    async def inner(url, **kwargs):
        return _load_json(name)
    return inner


def _assert_schema(posting: Posting) -> None:
    payload = posting.to_dict()
    assert REQUIRED_POSTING_KEYS <= set(payload)
    assert posting.title and posting.company and posting.source
    assert posting.canonical_id() == posting.dedupe_key()
    assert ":" in posting.dedupe_key() or posting.dedupe_key()
    assert payload["raw"] is not None
    assert "description" not in (payload.get("extra") or {})


# --------------------------------------------------------------------------- #
# Registry / gated
# --------------------------------------------------------------------------- #
def test_registry_includes_new_ats_and_health():
    sources = {row["id"]: row for row in list_sources()}
    for source_id in ("greenhouse", "lever", "ashby", "workable", "recruitee",
                      "smartrecruiters", "personio", "workday"):
        assert sources[source_id]["available"] is True
        assert sources[source_id]["official_feed"] is True
        assert "health" in sources[source_id]
        assert "capabilities" in sources[source_id]
    for source_id in ("linkedin", "indeed", "naukri", "instahyre"):
        assert sources[source_id]["available"] is False
        assert sources[source_id]["health"]["status"] in {"unknown", "gated"}


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [LinkedInSource, IndeedSource, NaukriSource, InstahyreSource])
async def test_gated_sources_raise_code_gated(cls):
    with pytest.raises(SourceError) as excinfo:
        await cls().fetch(keywords=[], limit=1, since_hours=24, board_tokens=[])
    assert excinfo.value.code == "gated"


# --------------------------------------------------------------------------- #
# Per-adapter fixtures
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_greenhouse_fixture(monkeypatch):
    monkeypatch.setattr("app.services.http.get_json", _async_json("greenhouse.json"))
    postings = await GreenhouseSource().fetch_board("acme", 5)
    assert len(postings) == 1
    _assert_schema(postings[0])
    assert postings[0].company == "Acme"
    assert "Python" in postings[0].description
    assert postings[0].dedupe_key() == "greenhouse:101"
    assert postings[0].raw.get("id") == 101


@pytest.mark.asyncio
async def test_lever_fixture(monkeypatch):
    monkeypatch.setattr("app.services.http.get_json", _async_json("lever.json"))
    postings = await LeverSource().fetch_board("acme", 5)
    _assert_schema(postings[0])
    assert postings[0].title == "Senior Python Engineer"
    assert postings[0].remote is True
    assert postings[0].extra["team"] == "Platform"


@pytest.mark.asyncio
async def test_ashby_skips_unlisted(monkeypatch):
    monkeypatch.setattr("app.services.http.get_json", _async_json("ashby.json"))
    postings = await AshbySource().fetch_board("acme", 5)
    assert [p.external_id for p in postings] == ["ash-1"]
    _assert_schema(postings[0])


@pytest.mark.asyncio
async def test_workable_fixture(monkeypatch):
    monkeypatch.setattr("app.services.http.get_json", _async_json("workable.json"))
    postings = await WorkableSource().fetch_board("acme", 5)
    _assert_schema(postings[0])
    assert postings[0].external_id == "PE1"
    assert postings[0].remote is True


@pytest.mark.asyncio
async def test_smartrecruiters_fixture(monkeypatch):
    monkeypatch.setattr("app.services.http.get_json", _async_json("smartrecruiters.json"))
    postings = await SmartRecruitersSource().fetch_board("Visa", 5)
    _assert_schema(postings[0])
    assert postings[0].company == "Visa"
    assert "Python" in postings[0].description


@pytest.mark.asyncio
async def test_recruitee_marks_closed_expired(monkeypatch):
    monkeypatch.setattr("app.services.http.get_json", _async_json("recruitee.json"))
    postings = await RecruiteeSource().fetch_board("acme", 10)
    assert len(postings) == 2
    live = next(p for p in postings if p.external_id == "77")
    closed = next(p for p in postings if p.external_id == "78")
    _assert_schema(live)
    assert live.expired is False
    assert closed.expired is True
    assert closed.is_expired() is True


@pytest.mark.asyncio
async def test_personio_xml_fixture(monkeypatch):
    async def fake_text(url, **kwargs):
        assert url.endswith("/xml")
        return _load_text("personio.xml")

    monkeypatch.setattr("app.services.http.get_text", fake_text)
    postings = await PersonioSource().fetch_board("acme", 5)
    assert len(postings) == 1
    _assert_schema(postings[0])
    assert postings[0].external_id == "42"
    assert postings[0].location == "Berlin"
    assert "Python" in postings[0].description
    assert postings[0].url.endswith("/job/42")


@pytest.mark.asyncio
async def test_personio_falls_back_to_com(monkeypatch):
    calls = []

    async def fake_text(url, **kwargs):
        calls.append(url)
        if ".personio.de" in url:
            raise RuntimeError("de down")
        return _load_text("personio.xml")

    monkeypatch.setattr("app.services.http.get_text", fake_text)
    postings = await PersonioSource().fetch_board("acme", 5)
    assert any(".personio.de" in url for url in calls)
    assert any(".personio.com" in url for url in calls)
    assert postings[0].external_id == "42"


@pytest.mark.asyncio
async def test_remotive_arbeitnow_jobicy_himalayas_themuse(monkeypatch):
    mapping = {
        "remotive.com": ("remotive.json", RemotiveSource),
        "arbeitnow.com": ("arbeitnow.json", ArbeitnowSource),
        "jobicy.com": ("jobicy.json", JobicySource),
        "himalayas.app": ("himalayas.json", HimalayasSource),
        "themuse.com": ("themuse.json", TheMuseSource),
    }

    async def fake_json(url, **kwargs):
        for host, (name, _) in mapping.items():
            if host in url:
                return _load_json(name)
        raise AssertionError(url)

    monkeypatch.setattr("app.services.http.get_json", fake_json)
    for _host, (_name, cls) in mapping.items():
        postings = await cls().fetch(keywords=["python"], limit=5, since_hours=24 * 365, board_tokens=[])
        assert postings, cls.id
        _assert_schema(postings[0])


@pytest.mark.asyncio
async def test_remoteok_skips_legal(monkeypatch):
    async def fake_request(method, url, **kwargs):
        return FakeResponse(_load_json("remoteok.json"))

    monkeypatch.setattr("app.services.http.request", fake_request)
    postings = await RemoteOKSource().fetch(keywords=["python"], limit=5, since_hours=24 * 365, board_tokens=[])
    assert len(postings) == 1
    _assert_schema(postings[0])


@pytest.mark.asyncio
async def test_weworkremotely_rss(monkeypatch):
    async def fake_text(url, **kwargs):
        return _load_text("weworkremotely.xml")

    monkeypatch.setattr("app.services.http.get_text", fake_text)
    postings = await WeWorkRemotelySource().fetch(keywords=["python"], limit=5, since_hours=24 * 365, board_tokens=[])
    assert postings[0].company == "Acme"
    assert postings[0].title == "Python Engineer"
    _assert_schema(postings[0])


@pytest.mark.asyncio
async def test_workday_structured_token(monkeypatch):
    async def fake_request(method, url, **kwargs):
        return FakeResponse(_load_json("workday.json"))

    monkeypatch.setattr("app.services.http.request", fake_request)
    source = WorkdaySource()
    assert await source.boards(["bad-token"]) == []
    postings = await source.fetch(keywords=["sre"], limit=5, since_hours=24 * 365,
                                  board_tokens=["acme.wd1.myworkdayjobs.com|acme|External"])
    assert postings[0].url.endswith("/job/sre")
    _assert_schema(postings[0])


@pytest.mark.asyncio
async def test_partner_adapters(monkeypatch):
    async def fake_json(url, **kwargs):
        if "adzuna" in url:
            return _load_json("adzuna.json")
        if "usajobs" in url:
            return _load_json("usajobs.json")
        raise AssertionError(url)

    monkeypatch.setattr("app.services.http.get_json", fake_json)
    adzuna = await AdzunaSource().fetch(keywords=["python"], limit=5, since_hours=24 * 365, board_tokens=[])
    _assert_schema(adzuna[0])
    usajobs = await USAJobsSource().fetch(keywords=["python"], limit=5, since_hours=24 * 365, board_tokens=[])
    _assert_schema(usajobs[0])
    assert usajobs[0].company == "GSA"

    async def fake_request(method, url, **kwargs):
        return FakeResponse(_load_json("jooble.json"))

    monkeypatch.setattr("app.services.http.request", fake_request)
    jooble = await JoobleSource().fetch(keywords=["python"], limit=5, since_hours=24 * 365, board_tokens=[])
    _assert_schema(jooble[0])


# --------------------------------------------------------------------------- #
# Fan-out / merge / expiry
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fetch_all_degrades_and_reports_error_codes(monkeypatch):
    source_health.reset()

    async def flaky(source_id, keywords, **kwargs):
        if source_id == "lever":
            raise SourceError("upstream down", code="upstream")
        return [Posting(title="Python Engineer", company="Acme", url="https://x", source=source_id,
                        description="python", posted_at=datetime.utcnow(), external_id="1")]

    monkeypatch.setattr("app.services.sources.fetch_from_source", flaky)
    postings, report = await fetch_all(["python"], limit=10, sources=["lever", "greenhouse"])
    assert report["errors"]["lever"] == "upstream down"
    assert report["error_codes"]["lever"] == "upstream"
    assert report["ok"]["greenhouse"] == 1
    assert len(postings) == 1
    assert report["total"] == 1


@pytest.mark.asyncio
async def test_fetch_all_drops_expired_and_stale(monkeypatch):
    async def mixed(source_id, keywords, **kwargs):
        now = datetime.utcnow()
        return [
            Posting(title="Fresh", company="A", url="https://a", source=source_id,
                    posted_at=now, external_id="fresh"),
            Posting(title="Stale", company="B", url="https://b", source=source_id,
                    posted_at=now - timedelta(days=60), external_id="stale"),
            Posting(title="Closed", company="C", url="https://c", source=source_id,
                    posted_at=now, external_id="closed", expired=True),
        ]

    monkeypatch.setattr("app.services.sources.fetch_from_source", mixed)
    postings, report = await fetch_all(["python"], limit=10, sources=["greenhouse"], since_hours=48)
    assert [p.title for p in postings] == ["Fresh"]
    assert report["expired"] == 1


@pytest.mark.asyncio
async def test_fetch_all_merges_canonical_duplicates(monkeypatch):
    async def dupes(source_id, keywords, **kwargs):
        now = datetime.utcnow()
        first = Posting(title="Backend", company="Acme", url="https://a", source="lever",
                        external_id="42", description="short", posted_at=now)
        second = Posting(title="Backend", company="Acme", url="https://b", source="lever",
                         external_id="42", description="a much longer description of the role",
                         posted_at=now, salary="120k")
        return [first, second]

    monkeypatch.setattr("app.services.sources.fetch_from_source", dupes)
    postings, report = await fetch_all(["python"], limit=10, sources=["lever"])
    assert len(postings) == 1
    assert report["merged"] == 1
    assert "longer description" in postings[0].description
    assert postings[0].salary == "120k"


def test_merge_postings_keeps_first_seen_identity():
    existing = Posting(title="Backend", company="Acme", url="https://a", source="lever",
                       external_id="42", description="a")
    incoming = Posting(title="Backend Engineer", company="Acme Inc", url="https://b",
                       source="greenhouse", external_id="99", description="abcde")
    merge_postings(existing, incoming)
    assert existing.source == "lever"
    assert existing.external_id == "42"
    assert existing.description == "abcde"


def test_compact_raw_strips_html_fields():
    raw = compact_raw({"id": 1, "title": "X", "description": "<p>huge " * 200, "content": "nope"})
    assert raw["id"] == 1
    assert "description" not in raw
    assert "content" not in raw


# --------------------------------------------------------------------------- #
# Persistence: raw off extra, merge re-seen, skip expired as fresh
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_discovery_stores_raw_payload_not_in_extra(db, owner, monkeypatch):
    from app.services.discovery import discover_for_user
    from app.services.sources.base import Posting as P

    user = db.query(User).filter(User.email == "owner@example.com").first()

    async def fake_fetch_all(keywords, **kwargs):
        posting = P(
            title="Backend Engineer", company="Paystack", url="https://jobs.example.com/1",
            source="lever", external_id="ext-1", location="Remote",
            description="Python, FastAPI and PostgreSQL.",
            posted_at=datetime.utcnow(), raw={"id": "ext-1", "board": "paystack"},
        )
        report = {"requested": ["lever"], "ok": {"lever": 1}, "errors": {}, "skipped": {}, "total": 1}
        return [posting], report

    monkeypatch.setattr("app.services.sources.fetch_all", fake_fetch_all)
    result = await discover_for_user(
        db, user, keywords=["python"], freshness_hours=168, limit=10,
        live_enabled=True, source_ids=["lever"],
    )
    assert result["inserted"] == 1
    row = db.query(Job).filter(Job.user_id == user.id).one()
    assert row.dedupe_key == "lever:ext-1"
    assert row.raw_payload.get("id") == "ext-1"
    assert "raw" not in (row.extra or {})
    assert row.first_seen_at is not None
    assert row.last_seen_at is not None
    assert row.expired is False
    first_seen = row.first_seen_at

    # Re-run: same canonical job merges, first_seen stays, last_seen moves, not a fresh insert.
    result2 = await discover_for_user(
        db, user, keywords=["python"], freshness_hours=168, limit=10,
        live_enabled=True, source_ids=["lever"],
    )
    assert result2["inserted"] == 0
    assert result2.get("duplicates_merged") == 1
    db.refresh(row)
    assert db.query(Job).filter(Job.user_id == user.id).count() == 1
    assert row.first_seen_at == first_seen
    assert row.last_seen_at >= first_seen


@pytest.mark.asyncio
async def test_discovery_does_not_insert_expired_as_fresh(db, owner, monkeypatch):
    from app.services.discovery import discover_for_user
    from app.services.sources.base import Posting as P

    user = db.query(User).filter(User.email == "owner@example.com").first()

    async def fake_fetch_all(keywords, **kwargs):
        posting = P(
            title="Closed Role", company="Acme", url="https://jobs.example.com/closed",
            source="recruitee", external_id="gone", location="Remote",
            description="Python", posted_at=datetime.utcnow(), expired=True,
        )
        report = {"requested": ["recruitee"], "ok": {"recruitee": 0}, "errors": {},
                  "skipped": {}, "expired": 1, "total": 1}
        return [posting], report

    monkeypatch.setattr("app.services.sources.fetch_all", fake_fetch_all)
    result = await discover_for_user(
        db, user, keywords=["python"], freshness_hours=168, limit=10,
        live_enabled=True, source_ids=["recruitee"],
    )
    assert result["inserted"] == 0
    assert result.get("expired_dropped") == 1
    assert db.query(Job).filter(Job.user_id == user.id).count() == 0


def test_every_adapter_declares_identity():
    for source_id, adapter in ADAPTERS.items():
        ident = adapter.identity()
        assert ident["id"] == source_id
        assert ident["label"]
        assert ident["kind"] in {"api", "board", "rss", "partner", "gated", "browser"}
        caps = adapter.capabilities()
        assert caps.official_feed or adapter.kind == "gated"
        assert adapter.rate_limit_policy.requests_per_minute >= 1
        assert adapter.retry_policy.max_attempts >= 1
