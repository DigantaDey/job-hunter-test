"""
v2.2.10 — storage caps by real row count, honest company-intel billing, and
bounded per-user loads in the hot endpoints.

Problem A: ``check_limit``/``usage_for`` were purely ``UsageCounter``-based, and
nothing ever called ``increment_usage(..., "resumes_max")`` /
``"vault_entries_max"`` while ``jobs_max`` was not enforced at all — so the
``enforce()`` calls in the create endpoints never tripped and free-tier users
stored unlimited resumes / vault entries / jobs despite the plan advertising
10/20/500. The caps are now the *actual row count* at creation time.

Problem B: company-intel charged the free cache-hit path and left the path that
actually calls the AI uncharged. Metering now follows the work: build on cache
miss or explicit refresh is what costs a ``company_intel_per_month`` unit.

Problem C: the user-input queue endpoint (``GET /api/user-input-queue``)
loaded the user's *entire* board into a dict on every poll, and
``list_jobs?company=`` fetched every row matching the
other filters (``.all()``) and filtered by company in Python with LIMIT/OFFSET
applied afterwards. Both are SQL now: a join, and an indexed
``company_name_normalized`` column (migration e5f6a7b8c9d0 + backfill).

Problem D: the list payload set ``has_open_positions`` to the ``is_funded``
flag — the Jobs UI "open roles" chip was a funding indicator. The field is gone
from the API and the frontend; the funding chip remains, labelled as funding.
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict

import pytest

from app.core.entitlements import PLANS, current_period
from app.models.models import (
    FundingCompany,
    Job,
    Resume,
    UsageCounter,
    User,
    UserInputRequest,
    VaultEntry,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _owner(db) -> User:
    return db.query(User).order_by(User.id).first()


def _seed_resumes(db, user_id: int, count: int) -> None:
    for i in range(count):
        db.add(Resume(user_id=user_id, filename=f"resume_{i}.pdf",
                      filepath=f"/tmp/does-not-matter_{i}.pdf"))
    db.commit()


def _seed_vault(db, user_id: int, count: int) -> None:
    for i in range(count):
        db.add(VaultEntry(user_id=user_id, domain=f"site{i}.example.com",
                          username=f"user{i}", password_enc="x" * 32))
    db.commit()


def _seed_jobs(db, user_id: int, count: int, *, company: str = "Acme Inc",
               start: int = 0) -> None:
    from app.services.company_normalize import normalize_company_name

    for i in range(start, start + count):
        db.add(Job(
            user_id=user_id,
            title=f"Backend Engineer {i}",
            company=company,
            # The identity column carries the *normalized* name — the same
            # value create/import paths store and the migration backfills.
            company_name_normalized=normalize_company_name(company),
            location="Remote",
            description="Python, FastAPI, PostgreSQL.",
            url=f"https://jobs.example.com/{i}",
            source="lever",
            dedupe_key=f"seed:{user_id}:{i}",
            status="discovered",
            score=float(i % 100),
        ))
    db.commit()


def _counter(db, user_id: int, capability: str) -> int:
    row = (
        db.query(UsageCounter)
        .filter(UsageCounter.user_id == user_id,
                UsageCounter.period == current_period(),
                UsageCounter.capability == capability)
        .first()
    )
    return row.count if row else 0


def _install_sources(monkeypatch, count: int = 20) -> None:
    """Replace the source fan-out with ``count`` fixed postings (hermetic)."""
    from app.services import sources as sources_registry
    from app.services.sources import Posting

    async def fake_fetch_all(keywords, *, limit=40, since_hours=168, sources=None,
                             board_tokens=None, timeout_seconds=45.0):
        postings = [
            Posting(
                title=f"Backend Engineer {i}",
                company=f"ClampCo {i}",
                url=f"https://jobs.example.com/clamp-{i}",
                source="lever",
                external_id=f"clamp-{i}",
                location="Remote",
                description="Python, FastAPI, PostgreSQL. Payments platform.",
                posted_at=datetime.utcnow() - timedelta(hours=i),
            )
            for i in range(count)
        ]
        report = {"requested": ["lever"], "ok": {"lever": len(postings)},
                  "errors": {}, "total": len(postings)}
        return postings, report

    monkeypatch.setattr(sources_registry, "fetch_all", fake_fetch_all)


@pytest.fixture
def sql_log():
    """Every SQL statement the app issues while the test body runs."""
    from sqlalchemy import event

    from app.db import engine

    statements: list = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    yield statements
    event.remove(engine, "before_cursor_execute", _before)


# --------------------------------------------------------------------------- #
# A. Storage caps — real row count at creation time
# --------------------------------------------------------------------------- #
def test_resume_upload_blocked_at_storage_cap(client, auth, db):
    """Free cap is 10 resumes; the 11th upload is 429 with the upgrade hint."""
    user = _owner(db)
    _seed_resumes(db, user.id, PLANS["free"]["limits"]["resumes_max"])

    response = client.post(
        "/api/resume/upload",
        files={"file": ("resume.pdf", b"%PDF-1.4 tiny", "application/pdf")},
        headers=auth,
    )
    assert response.status_code == 429, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "limit_exceeded"
    assert detail["limit"] == "resumes_max"
    assert detail["used"] == PLANS["free"]["limits"]["resumes_max"]
    assert detail["limit_value"] == PLANS["free"]["limits"]["resumes_max"]
    assert detail["upgrade_required"] is True
    assert "delete" in detail["message"].lower() or "upgrade" in detail["message"].lower()
    # Nothing was stored: the cap is enforced before the file is even written.
    db.expire_all()
    assert db.query(Resume).filter(Resume.user_id == user.id).count() == \
        PLANS["free"]["limits"]["resumes_max"]


def test_resume_upload_below_cap_still_works(client, auth, db):
    """One slot left → the upload succeeds and lands exactly at the cap."""
    user = _owner(db)
    _seed_resumes(db, user.id, PLANS["free"]["limits"]["resumes_max"] - 1)

    from conftest import pdf_bytes

    response = client.post(
        "/api/resume/upload",
        files={"file": ("resume.pdf", pdf_bytes(), "application/pdf")},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.query(Resume).filter(Resume.user_id == user.id).count() == \
        PLANS["free"]["limits"]["resumes_max"]


def test_vault_entry_blocked_at_storage_cap(client, auth, db):
    """Free cap is 20 vault entries; the 21st is 429 with the upgrade hint."""
    user = _owner(db)
    _seed_vault(db, user.id, PLANS["free"]["limits"]["vault_entries_max"])

    response = client.post(
        "/api/vault",
        json={"domain": "new.example.com", "username": "u", "password": "long-password-1"},
        headers=auth,
    )
    assert response.status_code == 429, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "limit_exceeded"
    assert detail["limit"] == "vault_entries_max"
    assert detail["used"] == PLANS["free"]["limits"]["vault_entries_max"]
    assert detail["limit_value"] == PLANS["free"]["limits"]["vault_entries_max"]
    assert detail["upgrade_required"] is True
    db.expire_all()
    assert db.query(VaultEntry).filter(VaultEntry.user_id == user.id).count() == \
        PLANS["free"]["limits"]["vault_entries_max"]


def test_vault_entry_below_cap_still_works(client, auth, db):
    user = _owner(db)
    _seed_vault(db, user.id, PLANS["free"]["limits"]["vault_entries_max"] - 1)

    response = client.post(
        "/api/vault",
        json={"domain": "new.example.com", "username": "u", "password": "long-password-1"},
        headers=auth,
    )
    assert response.status_code == 201, response.text
    db.expire_all()
    assert db.query(VaultEntry).filter(VaultEntry.user_id == user.id).count() == \
        PLANS["free"]["limits"]["vault_entries_max"]


def test_discovery_blocked_when_board_at_jobs_max(client, auth, db):
    """Free cap is 500 jobs; a full board answers 429, not a queued run."""
    user = _owner(db)
    _seed_jobs(db, user.id, PLANS["free"]["limits"]["jobs_max"])

    response = client.post(
        "/api/jobs/discover",
        json={"keywords": ["python"], "freshness_hours": 24},
        headers=auth,
    )
    assert response.status_code == 429, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "limit_exceeded"
    assert detail["limit"] == "jobs_max"
    assert detail["used"] == PLANS["free"]["limits"]["jobs_max"]
    assert detail["limit_value"] == PLANS["free"]["limits"]["jobs_max"]
    assert detail["upgrade_required"] is True


def test_funding_tracked_job_blocked_when_board_at_jobs_max(client, auth, db, uploaded_resume):
    """A funding-radar tracked job is a real board row: it counts too."""
    user = _owner(db)
    _seed_jobs(db, user.id, PLANS["free"]["limits"]["jobs_max"], company="Other Co")
    db.add(FundingCompany(user_id=user.id, name="FundCo", source="imported", verified=True,
                          stage="Seed", website="fundco.example.com", industry="fintech",
                          meta={"open_positions": [{"title": "Backend Engineer",
                                                    "url": "https://fundco.example.com/jobs/1"}]}))
    db.commit()

    response = client.post("/api/funding/companies/process", json={"company": "FundCo"}, headers=auth)
    assert response.status_code == 429, response.text
    assert response.json()["detail"]["limit"] == "jobs_max"
    db.expire_all()
    assert db.query(Job).filter(Job.user_id == user.id).count() == \
        PLANS["free"]["limits"]["jobs_max"]


def test_discovery_clamps_to_remaining_space(client, auth, db, monkeypatch):
    """A nearly-full board fills exactly its remaining slots — never overshoots."""
    user = _owner(db)
    free_max = PLANS["free"]["limits"]["jobs_max"]
    _seed_jobs(db, user.id, free_max - 5)  # 5 slots left
    _install_sources(monkeypatch, count=20)

    triggered = client.post(
        "/api/jobs/discover",
        json={"keywords": ["python"], "freshness_hours": 168, "limit": 40,
              "live_enabled": True},
        headers=auth,
    )
    assert triggered.status_code == 200, triggered.text
    item_id = triggered.json()["pipeline_job_id"]
    assert item_id, triggered.json()

    from conftest import run_queue_item

    run_queue_item(item_id, "discovery")

    db.expire_all()
    total = db.query(Job).filter(Job.user_id == user.id).count()
    assert total == free_max, f"the cap is a wall: {total} rows"
    assert total <= free_max
    # The run's report carries the cap's accounting.
    from app.models.models import PipelineJob

    item = db.query(PipelineJob).filter(PipelineJob.id == item_id).first()
    assert item.status == "done", f"{item.status}: {item.error}"
    report = (item.payload or {}).get("result") or {}
    assert report.get("jobs_cap") == {"used": free_max - 5, "limit": free_max}
    assert report.get("inserted") == 5, report.get("inserted")


def test_discovery_run_reports_cap_when_board_full(client, auth, db, monkeypatch):
    """A queued run on a full board is an honest zero-job result, not a crash.

    The HTTP trigger 429s, but the auto-mode scheduler can still enqueue a run
    (e.g. queued moments before the board filled). The worker must not
    over-insert and must report *why* the board gained nothing.
    """
    from app.services.job_queue import enqueue

    user = _owner(db)
    _seed_jobs(db, user.id, PLANS["free"]["limits"]["jobs_max"])
    _install_sources(monkeypatch, count=20)

    item = enqueue(
        db,
        user_id=user.id,
        pipeline="discovery",
        payload={"keywords": ["python"], "freshness_hours": 168, "limit": 40,
                 "live_enabled": True, "sources": ["lever"], "board_tokens": [],
                 "persona_id": None},
        priority=3,
        dedupe_key=f"discovery-capped-{uuid.uuid4().hex[:8]}",
    )
    assert item is not None

    from conftest import run_queue_item

    run_queue_item(item.id, "discovery")

    db.expire_all()
    assert db.query(Job).filter(Job.user_id == user.id).count() == \
        PLANS["free"]["limits"]["jobs_max"], "a full board never grows"
    assert item.status == "done", f"{item.status}: {item.error}"
    report = (item.payload or {}).get("result") or {}
    assert report.get("inserted") == 0
    assert report.get("why_empty") == "jobs_cap_reached"
    assert report.get("jobs_cap") == {"used": PLANS["free"]["limits"]["jobs_max"],
                                      "limit": PLANS["free"]["limits"]["jobs_max"]}


def test_entitlements_snapshot_reports_real_storage_usage(client, auth, db):
    """The usage grid shows the rows the user actually has — not "0 used"."""
    user = _owner(db)
    _seed_resumes(db, user.id, 3)
    _seed_vault(db, user.id, 5)
    _seed_jobs(db, user.id, 40)

    snap = client.get("/api/billing/subscription", headers=auth).json()
    free = PLANS["free"]["limits"]
    assert snap["usage"]["resumes_max"] == {
        "used": 3, "limit": free["resumes_max"],
        "remaining": free["resumes_max"] - 3, "unlimited": False,
    }
    assert snap["usage"]["vault_entries_max"]["used"] == 5
    assert snap["usage"]["jobs_max"] == {
        "used": 40, "limit": free["jobs_max"],
        "remaining": free["jobs_max"] - 40, "unlimited": False,
    }
    # Deleting a row frees a slot the same instant — the counter is the rows.
    db.query(Resume).filter(Resume.user_id == user.id).delete()
    db.commit()
    snap2 = client.get("/api/billing/subscription", headers=auth).json()
    assert snap2["usage"]["resumes_max"]["used"] == 0


# --------------------------------------------------------------------------- #
# B. Company intel — charge only when AI work actually happens
# --------------------------------------------------------------------------- #
def test_company_intel_cache_hit_is_free_refresh_charges_once(client, auth, db):
    """Build (cache miss) charges once; a cache hit charges nothing; an
    explicit refresh is real AI work and charges once more."""
    user = _owner(db)
    cap = "company_intel_per_month"

    # 1 — build: no cache yet → AI runs → 1 unit.
    first = client.get("/api/company/Acme%20AI/intel", headers=auth)
    assert first.status_code == 200, first.text
    assert first.json()["cached"] is False
    db.expire_all()
    assert _counter(db, user.id, cap) == 1

    # 2 — cache hit: free work → the counter must not move.
    second = client.get("/api/company/Acme%20AI/intel", headers=auth)
    assert second.status_code == 200, second.text
    assert second.json()["cached"] is True
    db.expire_all()
    assert _counter(db, user.id, cap) == 1, "a cache hit must not charge"

    # 3 — explicit refresh: the user asked for new research → AI runs → +1.
    third = client.get("/api/company/Acme%20AI/intel", params={"refresh": True}, headers=auth)
    assert third.status_code == 200, third.text
    assert third.json()["cached"] is False
    db.expire_all()
    assert _counter(db, user.id, cap) == 2, "a refresh is one unit of AI work"


def test_company_intel_at_quota_still_serves_fresh_cache(client, auth, db):
    """The quota protects the *AI*. A user at their monthly limit can still
    read a cached result they already paid for — the paywall is on the
    refresh (the paid work), not on the free read."""
    user = _owner(db)
    cap = "company_intel_per_month"
    free_cap = PLANS["free"]["limits"][cap]

    # Build once (1 unit), then top the (already existing) counter to the limit.
    assert client.get("/api/company/Acme%20AI/intel", headers=auth).status_code == 200
    counter = (
        db.query(UsageCounter)
        .filter(UsageCounter.user_id == user.id,
                UsageCounter.period == current_period(),
                UsageCounter.capability == cap)
        .first()
    )
    counter.count = free_cap
    db.commit()
    db.expire_all()

    # Fresh cache read: free, no 429.
    cached = client.get("/api/company/Acme%20AI/intel", headers=auth)
    assert cached.status_code == 200, cached.text
    assert cached.json()["cached"] is True
    db.expire_all()
    assert _counter(db, user.id, cap) == free_cap, "the free read must not spend"

    # The paid work is what the quota now blocks.
    refresh = client.get("/api/company/Acme%20AI/intel", params={"refresh": True}, headers=auth)
    assert refresh.status_code == 429, refresh.text
    assert refresh.json()["detail"]["limit"] == cap
    db.expire_all()
    assert _counter(db, user.id, cap) == free_cap


# --------------------------------------------------------------------------- #
# C. Bounded per-user loads in hot endpoints
# --------------------------------------------------------------------------- #
def test_user_input_queue_joins_not_boards(client, auth, db, sql_log):
    """The queue attaches title/company/url with a SQL join — the user's whole
    board is never loaded into a dict just to look up three fields."""
    user = _owner(db)
    big = 300
    _seed_jobs(db, user.id, big)
    job = db.query(Job).filter(Job.user_id == user.id).first()
    db.add(UserInputRequest(user_id=user.id, job_id=job.id,
                            fields=[{"name": "salary", "label": "Salary", "type": "text",
                                     "required": True, "value": ""}],
                            status="pending"))
    db.commit()

    sql_log.clear()
    response = client.get("/api/user-input-queue", headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    assert body[0]["job"]["title"] == job.title
    assert body[0]["job"]["company"] == job.company
    assert body[0]["job"]["url"] == job.url

    queue_statements = [s for s in sql_log if "USER_INPUT_REQUESTS" in s.upper()]
    assert queue_statements, "the queue must read its rows in SQL"
    for statement in queue_statements:
        # The single queue query joins the job row in SQL — never a separate
        # scan of the user's whole board.
        assert "JOIN" in statement.upper(), f"no join in the queue query: {statement}"
        assert "JOBS" in statement.upper()
    # No statement may load the board on its own (the old dict-of-all-jobs).
    for statement in sql_log:
        upper = statement.upper()
        if "FROM JOBS" in upper and "USER_INPUT_REQUESTS" not in upper:
            raise AssertionError(f"unbounded job load in the input queue: {statement}")


def test_user_input_queue_survives_a_deleted_job(client, auth, db):
    """A pending row whose job row was deleted still shows (empty job fields).

    The dangling version of this state used to be constructible by simply
    deleting the job — which only worked because the suite ran with foreign keys
    disabled. ``user_input_requests.job_id`` is a FK like every other reference
    in this schema, so the row has to be detached first; that is exactly what a
    job-deletion endpoint would have to do, and what
    :func:`app.services.erasure.break_references` does for every table.
    """
    from app.services.erasure import break_references

    user = _owner(db)
    job = Job(user_id=user.id, title="Ghost", company="Gone Co",
              company_name_normalized="gone co", dedupe_key="ghost:1",
              description="", source="lever")
    db.add(job)
    db.commit()
    db.refresh(job)
    request_row = UserInputRequest(user_id=user.id, job_id=job.id,
                                   fields=[{"name": "x", "label": "X", "type": "text",
                                            "required": True, "value": ""}],
                                   status="pending")
    db.add(request_row)
    db.commit()
    assert break_references(db, "jobs", job.id)[0] == {"user_input_requests.job_id": 1}
    db.delete(job)
    db.commit()

    body = client.get("/api/user-input-queue", headers=auth).json()
    assert len(body) == 1
    assert body[0]["job_id"] is None
    assert body[0]["job"] == {"title": "", "company": "", "url": ""}


def test_company_filter_runs_in_sql_with_limit(client, auth, db, sql_log):
    """?company= filters in SQL (indexed column) with LIMIT/OFFSET applied by
    the database — a 20k-row board is never fetched whole to filter in Python."""
    user = _owner(db)
    # Two companies that normalize to "acme" (suffix stripping) + one that does
    # not: the exact-normalized match must pull in both acme spellings only.
    _seed_jobs(db, user.id, 25, company="Acme Inc")
    _seed_jobs(db, user.id, 25, company="acme, inc.", start=1000)
    _seed_jobs(db, user.id, 25, company="Beta LLC", start=2000)

    sql_log.clear()
    response = client.get(
        "/api/jobs", params={"company": "acme inc", "limit": 10}, headers=auth)
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 10, "only the requested page comes back"
    assert {r["company"] for r in rows} <= {"Acme Inc", "acme, inc."}

    jobs_statements = [s for s in sql_log if re.search(r"\bFROM\s+\"?jobs\"?", s, re.IGNORECASE)]
    filtered = [s for s in jobs_statements if "company_name_normalized" in s]
    assert filtered, f"the company filter must run in SQL: {jobs_statements}"
    assert any("LIMIT" in s.upper() for s in filtered), "LIMIT must be in the SQL"


def test_company_filter_pagination_and_exact_normalized_match(client, auth, db):
    """Offset/limit page through the SQL-filtered set; exact match only."""
    user = _owner(db)
    for i in range(25):
        db.add(Job(user_id=user.id, title=f"Role {i}", company="Acme Inc",
                   company_name_normalized="acme", dedupe_key=f"page:{i}",
                   description="", source="lever", status="discovered", score=float(i)))
    db.add(Job(user_id=user.id, title="Other", company="Beta LLC",
               company_name_normalized="beta", dedupe_key="page:other",
               description="", source="lever", status="discovered", score=50.0))
    db.commit()

    page1 = client.get("/api/jobs", params={"company": "acme", "limit": 10, "offset": 0},
                       headers=auth).json()
    page2 = client.get("/api/jobs", params={"company": "acme", "limit": 10, "offset": 20},
                       headers=auth).json()
    page_mid = client.get("/api/jobs", params={"company": "acme", "limit": 10, "offset": 10},
                          headers=auth).json()
    assert len(page1) == 10 and len(page_mid) == 10 and len(page2) == 5
    ids = {r["id"] for r in page1} | {r["id"] for r in page_mid} | {r["id"] for r in page2}
    assert len(ids) == 25, "the three pages are disjoint and cover the set"
    assert all(r["company"] == "Acme Inc" for r in page1 + page_mid + page2)
    # offset/limit must be applied to the *filtered* set: the Beta row
    # (score 50, which sorts into the middle of the unfiltered ordering) must
    # never leak into an acme-filtered page.
    assert not any(r["company"] == "Beta LLC" for r in page1 + page_mid + page2)

    # "acme" is an exact normalized match: it does not contain-match "acmeish".
    db.add(Job(user_id=user.id, title="Role 99", company="Acmeish Co",
               company_name_normalized="acmeish", dedupe_key="page:99",
               description="", source="lever", status="discovered", score=1.0))
    db.commit()
    assert len(client.get("/api/jobs", params={"company": "acme"}, headers=auth).json()) == 25
    # And a legal-suffix spelling of the input matches the stored identity.
    assert len(client.get("/api/jobs", params={"company": "Acme Inc."}, headers=auth).json()) == 25


# --------------------------------------------------------------------------- #
# D. The "has open positions" lie is gone
# --------------------------------------------------------------------------- #
def test_list_jobs_no_longer_reports_open_positions(client, auth, db):
    """A Job row *is* an open position — the field had no honest meaning. The
    funding chip remains (labelled as funding); the fake field does not."""
    user = _owner(db)
    db.add(Job(user_id=user.id, title="Role", company="Acme Inc",
               company_name_normalized="acme", dedupe_key="lie:1", description="",
               source="lever", status="discovered", score=50.0))
    db.add(FundingCompany(user_id=user.id, name="Acme", source="imported",
                          verified=True, stage="Seed", website="acme.example.com",
                          industry="fintech", meta={}))
    db.commit()

    rows = client.get("/api/jobs", headers=auth).json()
    assert len(rows) == 1
    row = rows[0]
    assert "has_open_positions" not in row, "the semantic lie is gone from the API"
    # The real funding signal is still there, still labelled as funding.
    assert row["is_funded"] is True
    assert row["funding_match"] is True


# --------------------------------------------------------------------------- #
# Migration — backfill on a populated database
# --------------------------------------------------------------------------- #
def test_migration_backfills_company_name_normalized(tmp_path):
    """The new column is backfilled with the app's own normalizer on a
    *populated* previous-head database, indexed, and reversible."""
    from app.services.company_normalize import normalize_company_name

    backend = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "backfill.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "ALEMBIC_DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "ENVIRONMENT": "test",
        "SECRET_KEY": "test-secret-key-that-is-long-enough-1234567890",
        "ENCRYPTION_KEY": "test-encryption-key-1234567890-abcdefghij",
    }

    def alembic(*args: str) -> None:
        out = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=backend,
                             env=env, capture_output=True, text=True, timeout=300)
        assert out.returncode == 0, (out.stdout + out.stderr)[-2000:]

    alembic("upgrade", "d4e5f6a7b8c9")  # the schema before this change

    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO users (id, email, password_hash, role, is_active, created_at) "
                "VALUES (1, 'a@b.c', 'h', 'owner', 1, '2026-09-01 00:00:00')")
    con.execute("INSERT INTO jobs (id, user_id, title, company, dedupe_key, source) "
                "VALUES (1, 1, 'Role', 'Acme Inc', 'k1', 'lever')")
    con.execute("INSERT INTO jobs (id, user_id, title, company, dedupe_key, source) "
                "VALUES (2, 1, 'Role', 'acme, inc.', 'k2', 'lever')")
    con.execute("INSERT INTO jobs (id, user_id, title, company, dedupe_key, source) "
                "VALUES (3, 1, 'Role', 'Beta  LLC', 'k3', 'lever')")
    con.commit()
    con.close()

    alembic("upgrade", "head")

    con = sqlite3.connect(db_path)
    rows = {row[0]: row[1] for row in con.execute(
        "SELECT company, company_name_normalized FROM jobs")}
    assert rows == {
        "Acme Inc": "acme",
        "acme, inc.": "acme",
        "Beta  LLC": "beta",
    }, f"backfill must use the shared normalizer: {rows}"
    for company, norm in rows.items():
        assert norm == normalize_company_name(company)[:200]
    indexes = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='jobs'")}
    assert "ix_jobs_user_company_norm" in indexes, "the filter needs the composite index"
    con.close()

    # Reversible.
    alembic("downgrade", "d4e5f6a7b8c9")
    con = sqlite3.connect(db_path)
    columns = [row[1] for row in con.execute("PRAGMA table_info(jobs)")]
    assert "company_name_normalized" not in columns
    con.close()
    alembic("upgrade", "head")
