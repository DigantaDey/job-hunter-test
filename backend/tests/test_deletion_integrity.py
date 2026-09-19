"""
Deletion and export integrity, with foreign keys actually enforced.

The suite used to run with ``PRAGMA foreign_keys`` switched off on a *pooled*
connection (``tests/conftest.py``), which the app never re-enabled — so every
test inherited a database that tolerated dangling references, and every deletion
path that left one behind looked correct. These are the regression pins for that
blind spot: erasing a fully-populated account, deleting a referenced resume, and
the export that has to hand the same data back in bounded memory.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app.api.routers import account as account_api
from app.api.routers import resumes as resumes_api
from app.core.config import settings
from app.db import engine
from app.models.models import (
    AICreditLedger,
    ApiKey,
    ApplicationAction,
    ApplicationPacket,
    ApplicationPacketEvent,
    ApplicationSession,
    ApplicationSubmission,
    AuditLog,
    BillingEvent,
    CandidateProfile,
    CompanyIntel,
    Email,
    EmailEvent,
    EmailOptOut,
    ErrorLog,
    FundingCompany,
    FundingScan,
    FundingScanCompany,
    InterviewPrep,
    Job,
    JobEvent,
    MatchFeedback,
    MatchResult,
    Notification,
    OnboardingEvent,
    OnboardingSession,
    Persona,
    PipelineJob,
    Profile,
    ProfileFieldHistory,
    ProfileFieldProvenance,
    RefreshToken,
    Resume,
    ResumeDocument,
    ResumeExtraction,
    ScheduledRun,
    SearchBudget,
    SearchUsage,
    SettingsModel,
    Subscription,
    UsageCounter,
    User,
    UserInputRequest,
    VaultEntry,
)
from app.services.erasure import break_references, build_plan, count_user_rows, owned_tables

#: One entry per table that carries a ``user_id`` FK — the test's own claim about
#: what "a fully-populated account" means. ``test_seed_covers_every_owned_table``
#: compares it against the schema, so a table added to the models and not to this
#: dict fails here instead of leaving the erasure quietly incomplete.
CHILD_TABLES: dict[str, type] = {
    "refresh_tokens": RefreshToken,
    "api_keys": ApiKey,
    "profiles": Profile,
    "personas": Persona,
    "resumes": Resume,
    "jobs": Job,
    "job_events": JobEvent,
    "vault_entries": VaultEntry,
    "emails": Email,
    "email_events": EmailEvent,
    "email_opt_outs": EmailOptOut,
    "settings": SettingsModel,
    "error_logs": ErrorLog,
    "user_input_requests": UserInputRequest,
    "pipeline_jobs": PipelineJob,
    "scheduled_runs": ScheduledRun,
    "search_budgets": SearchBudget,
    "search_usage": SearchUsage,
    "funding_companies": FundingCompany,
    "funding_scans": FundingScan,
    "audit_logs": AuditLog,
    "subscriptions": Subscription,
    "billing_events": BillingEvent,
    "ai_credit_ledger": AICreditLedger,
    "usage_counters": UsageCounter,
    "notifications": Notification,
    "interview_preps": InterviewPrep,
    "company_intel": CompanyIntel,
    # Resumable onboarding (v2.2.19): the durable wizard state, the preserved
    # upload, its extraction attempts and the append-only timeline.
    "onboarding_sessions": OnboardingSession,
    "onboarding_events": OnboardingEvent,
    "resume_documents": ResumeDocument,
    "resume_extractions": ResumeExtraction,
    # Candidate profile extraction (v2.2.20): versioned document + provenance + history
    "candidate_profiles": CandidateProfile,
    "profile_field_provenance": ProfileFieldProvenance,
    "profile_field_history": ProfileFieldHistory,
    # Multi-stage matching: the versioned explained verdict + user feedback.
    "match_results": MatchResult,
    # Browser-assisted applications (v2.2.21, docs/contracts/12): the session,
    # its human-action queue and the duplicate-submission ledger.
    "application_sessions": ApplicationSession,
    "application_actions": ApplicationAction,
    "application_submissions": ApplicationSubmission,
    "match_feedback": MatchFeedback,
    # Application packet (reviewable, versioned, grounded artifacts)
    "application_packets": ApplicationPacket,
    "application_packet_events": ApplicationPacketEvent,
}


def _owner(db) -> User:
    return db.query(User).order_by(User.id).first()


def _touch(path: str) -> str:
    """A real file on disk, so "the files are gone" is a fact and not a hope."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"%PDF-1.4 test fixture\n")
    return path


def _seed_every_child_table(db, user: User) -> dict:
    """
    One row in every table a user owns, wired together the way the app wires them.

    The references deliberately include the ones the schema cannot order its way
    out of — the profile's master resume, the job's applied-with resume, a
    derived resume's parent and its copy — because those cycles are what an
    erasure has to break by nulling, and what used to abort the whole delete.
    """
    from app.services.vault import save_vault_entry

    resume_path = _touch(os.path.join(settings.upload_dir, f"master-{user.id}.docx"))
    rendered_path = os.path.splitext(resume_path)[0] + ".pdf"
    _touch(rendered_path)
    derived_path = _touch(os.path.join(settings.upload_dir, f"derived-{user.id}.docx"))

    persona = Persona(user_id=user.id, name="Backend Engineer", target_role="Senior Backend Engineer",
                      is_active=True, is_default=True, search_context={"keywords": ["python"]},
                      memory={"signals": []}, stats={"scored": 1})
    resume = Resume(user_id=user.id, filename="master.docx", filepath=resume_path, type="master",
                    status="approved", tags=["master"])
    derived = Resume(user_id=user.id, filename="tailored.docx", filepath=derived_path,
                     type="generated", status="pending", tags=["backend-python"])
    job = Job(user_id=user.id, title="Senior Backend Engineer", company="FinCo",
              company_name_normalized="finco", description="Python, FastAPI.", url="https://x/1",
              source="lever", dedupe_key="lever:1", status="applied", score=88.0)
    profile = Profile(user_id=user.id, data={"name": "Test Candidate"}, layout={}, extraction_source="ai")
    email = Email(user_id=user.id, to_email="hm@finco.example", to_name="Hiring Manager",
                  subject="Backend role", body="Hi", status="sent", company="FinCo", dry_run=False)
    funding = FundingCompany(user_id=user.id, name="FinCo", stage="Series B",
                             website="https://finco.example", industry="fintech", summary="Payments.")
    scan = FundingScan(user_id=user.id, scanned_at=datetime.utcnow(), status="ok", events_seen=4,
                       companies_found=1)
    for row in (persona, resume, derived, job, profile, email, funding, scan):
        db.add(row)
    db.flush()

    # The onboarding aggregates, wired the way the flow wires them: document →
    # extraction → session, with the session's state pointing at the finished
    # attempt (so erasure has to break references, not just delete children).
    document = ResumeDocument(user_id=user.id, role="master", state="approved",
                              filename="master.docx", display_name="master.docx",
                              filepath=resume_path, content_type="application/pdf",
                              size_bytes=2048, sha256=f"seed-sha-{user.id}",
                              profile_snapshot={"name": "Test Candidate"})
    db.add(document)
    db.flush()  # id needed for the plain-FK wiring below (no relationships)
    extraction = ResumeExtraction(user_id=user.id, resume_document_id=document.id, attempt=1,
                                  state="succeeded", extractor_version="1.0.0",
                                  profile_id=profile.id, trigger="user")
    session_row = OnboardingSession(user_id=user.id, state="profile_review_required",
                                    state_since=datetime.utcnow(),
                                    resume_document_id=document.id,
                                    extraction_id=extraction.id,
                                    started_at=datetime.utcnow())
    db.add_all([extraction, session_row])
    db.flush()
    db.add(OnboardingEvent(user_id=user.id, session_id=session_row.id, sequence=1,
                           event_type="onboarding.started", actor_type="system_api",
                           occurred_at=datetime.utcnow(), recorded_at=datetime.utcnow(),
                           message="session created"))
    db.flush()

    # Then the cross-references — each one a leftover waiting to happen.
    resume.persona_id = persona.id
    derived.persona_id = persona.id
    derived.job_id = job.id
    derived.parent_resume_id = resume.id
    persona.source_resume_id = resume.id
    profile.master_resume_id = resume.id
    job.persona_id = persona.id
    job.applied_with_resume_id = resume.id
    email.persona_id = persona.id
    email.job_id = job.id
    db.flush()

    # Candidate profile v2 seed (must come before provenance/history because of FK)
    cand_profile = CandidateProfile(
        user_id=user.id,
        persona_id=persona.id,
        version=1,
        state="active",
        is_current=True,
        document={"identity": {"full_name": "Test Candidate"}},
        document_sha256="a" * 64,
        review={"required": 0, "resolved": 0},
        completeness={"percent": 100},
        created_at=datetime.utcnow(),
    )
    db.add(cand_profile)
    db.flush()
    prov = ProfileFieldProvenance(
        user_id=user.id,
        profile_id=cand_profile.id,
        path="/full_name",
        value_hash="b" * 64,
        value_preview="Test Candidate",
        origin="resume_extraction",
        sensitivity="public",
        confidence=0.9,
        confidence_band="high",
        evidence=[{"kind": "text_span", "quote": "Test Candidate"}],
        extractor={"name": "test"},
        ambiguity="none",
        review_status="confirmed",
        review_required=False,
        created_at=datetime.utcnow(),
    )
    db.add(prov)
    db.flush()
    hist = ProfileFieldHistory(
        user_id=user.id,
        provenance_id=prov.id,
        path="/full_name",
        previous_value_hash="c" * 64,
        previous_value_preview="Old Name",
        new_value_hash="b" * 64,
        new_value_preview="Test Candidate",
        origin_before="heuristic",
        origin_after="resume_extraction",
        review_status_before="needs_review",
        review_status_after="confirmed",
        actor_type="system",
        event_id="00000000-0000-0000-0000-000000000001",
        occurred_at=datetime.utcnow(),
    )
    db.add(hist)
    db.flush()

    # Multi-stage matching seed: a verdict for the job (wired to the v2 profile
    # version it was computed from) and a user feedback row against it.
    match = MatchResult(
        user_id=user.id,
        job_id=job.id,
        persona_id=persona.id,
        profile_id=cand_profile.id,
        profile_sha256="d" * 64,
        job_description_sha256="e" * 64,
        scorer="deterministic",
        scorer_version="1.0.0",
        score=88.0,
        band="strong",
        confidence=0.6,
        score_source="preliminary",
        reason="Estimated fit 88/100 from scored features.",
        hard_filters={"status": "eligible", "checks": [], "total_penalty": 0},
        rubric={"weights": {}, "criteria": [], "overall": 88.0, "band": "strong"},
        matched_skills=[{"name": "python", "required": True, "evidence": []}],
        missing_skills=[],
        flags={},
        is_current=True,
        staleness="fresh",
        computed_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    db.add(match)
    db.flush()
    db.add(MatchFeedback(
        user_id=user.id, job_id=job.id, match_id=match.id, kind="relevant",
        reason="good call", meta={}, scorer_version="1.0.0",
        created_at=datetime.utcnow(),
    ))
    db.flush()

    # Application packet seed (must come after job/persona, before erasure)
    packet = ApplicationPacket(
        user_id=user.id,
        job_id=job.id,
        persona_id=persona.id,
        version=1,
        status="pending_approval",
        is_current=True,
        jd_hash="a" * 64,
        jd_text_snapshot="Python, FastAPI.",
        jd_version=1,
        master_profile_snapshot={"name": "Test Candidate"},
        tailored_resume={"name": "Test Candidate"},
        cover_note="Dear Hiring Manager, cover note.",
        short_answers=[],
        outreach_draft={"subject": "Hello", "body": "Hi"},
        checklist=[],
        summary="Summary tying facts to JD.",
        evidence=[],
        emphasized_facts=[],
        guardrail_report={},
        token_usage={},
        job_title="Senior Backend Engineer",
        company="FinCo",
    )
    db.add(packet)
    db.flush()
    db.add(
        ApplicationPacketEvent(
            user_id=user.id,
            packet_id=packet.id,
            job_id=job.id,
            event_type="generated",
            from_status=None,
            to_status="pending_approval",
            detail="Generated version 1",
            meta={},
            actor_type="system",
        )
    )
    db.flush()

    for row in (
        JobEvent(user_id=user.id, job_id=job.id, stage="applied", status="success", message="submitted"),
        EmailEvent(user_id=user.id, email_id=email.id, kind="sent", detail="250 ok"),
        EmailOptOut(user_id=user.id, email="bounced@example.com", reason="manual"),
        SettingsModel(user_id=user.id, category="email", key="password", value="smtp-secret"),
        ErrorLog(user_id=user.id, pipeline="apply", level="error", message="portal refused"),
        UserInputRequest(user_id=user.id, job_id=job.id,
                         fields=[{"name": "salary", "label": "Salary", "type": "text",
                                  "required": True, "value": ""}], status="pending"),
        PipelineJob(user_id=user.id, pipeline="application", job_id=job.id, status="done",
                    dedupe_key=f"apply:{job.id}"),
        ScheduledRun(user_id=user.id, workflow="discovery", cycle_bucket=7, state="done",
                     triggered_at=datetime.utcnow()),
        FundingScanCompany(scan_id=scan.id, company_id=funding.id, rank=1, why="Series B in fintech."),
        AuditLog(user_id=user.id, action="job.applied", target="FinCo", actor="owner@example.com"),
        Subscription(user_id=user.id, plan="pro", status="active", provider="manual"),
        BillingEvent(user_id=user.id, provider="stripe", provider_event_id=f"evt_{user.id}",
                     kind="invoice.paid"),
        AICreditLedger(user_id=user.id, workflow="scoring", model="gpt-x", prompt_tokens=100,
                       completion_tokens=20, total_tokens=120, estimated_cost_usd=0.01),
        SearchBudget(key=f"user:{user.id}:test", user_id=user.id, used=1,
                     expires_at=datetime.utcnow() + timedelta(days=1)),
        SearchUsage(user_id=user.id, provider="brave", query_hash="a" * 64,
                    estimated_cost_microusd=5000, outcome="success"),
        UsageCounter(user_id=user.id, period=datetime.utcnow().strftime("%Y-%m"), capability="ai_ops",
                     count=1, limit=1000),
        Notification(user_id=user.id, kind="high_match", title="88% match", body="FinCo"),
        InterviewPrep(user_id=user.id, job_id=job.id, job_title="Senior Backend Engineer",
                      company="FinCo", questions=[{"q": "Tell me about payments."}], status="draft"),
        CompanyIntel(user_id=user.id, company="FinCo", website="https://finco.example",
                     industry="fintech", summary="Payments."),
        RefreshToken(user_id=user.id, token_hash=f"rt-{user.id}", expires_at=datetime.utcnow() + timedelta(days=7),
                     user_agent="pytest"),
        ApiKey(user_id=user.id, name="ci", prefix="jh_test", key_hash=f"kh-{user.id}", scopes=["read"]),
    ):
        db.add(row)
    db.flush()

    # Browser-assisted application seed: the session (bound to the job), the
    # action it paused on, and the submission ledger row for the same job.
    app_session = ApplicationSession(
        user_id=user.id,
        job_id=job.id,
        state="awaiting_user",
        phase="waiting",
        state_reason="captcha_detected",
        isolation_key=f"u{user.id}-seed",
        browser_profile_ref=f"u{user.id}/seed",
        url_fingerprint="f" * 64,
        expected_host="jobs.lever.co",
        employer_fingerprint="e" * 64,
        application_identity="ext:seed",
        checkpoint={"fields": {"first_name": {"status": "filled"}}, "steps": []},
        progress={"fields_total": 1, "filled": 1},
        last_observation={"host": "jobs.lever.co", "markers": ["captcha"], "fields": []},
        fill_values={"first_name": "Test"},
        pause_kind="captcha",
        pause_reason="captcha_detected",
        expires_at=datetime.utcnow() + timedelta(minutes=30),
    )
    db.add(app_session)
    db.flush()
    db.add(ApplicationAction(
        user_id=user.id, session_id=app_session.id, job_id=job.id, kind="captcha", status="pending",
        title="Complete the bot check", instructions="Finish the check in the browser, then continue.",
        reason="captcha_detected", fields=[{"name": "g-recaptcha-response", "label": "Verify you are human"}],
        dedupe_key=f"captcha:{app_session.id}", occurrences=1,
    ))
    db.add(ApplicationSubmission(
        user_id=user.id, job_id=job.id, session_id=app_session.id, state="reserved",
        channel="automation", idempotency_key=f"sub:{user.id}:{job.id}:seed", dry_run=False,
        reserved_at=datetime.utcnow(),
    ))
    db.commit()
    vault = save_vault_entry(db, user.id, domain="jobs.lever.co", username="me@example.com",
                             password="correct-horse-battery", origin="manual")

    return {"resume": resume, "derived": derived, "job": job, "profile": profile, "persona": persona,
            "email": email, "scan": scan, "funding": funding, "vault": vault, "resume_path": resume_path,
            "rendered_path": rendered_path, "derived_path": derived_path}


# --------------------------------------------------------------------------- #
# The blind spot itself
# --------------------------------------------------------------------------- #
def test_the_suite_runs_with_foreign_keys_enforced(owner, db):
    """
    A dangling reference has to fail *here*, or every deletion test is theatre.

    ``app/db.py`` turns enforcement on when a SQLite connection is created;
    ``tests/conftest.py`` used to turn it back off on that same pooled
    connection and nothing re-enabled it, so the suite silently stopped checking
    the one constraint every deletion path depends on.
    """
    user = _owner(db)
    if engine.dialect.name == "sqlite":
        with engine.connect() as connection:
            assert int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar()) == 1, (
                "SQLite FK enforcement is off on the app engine — the schema's ON DELETE "
                "behaviour, and every deletion test in this file, would be untested"
            )

    db.add(JobEvent(user_id=user.id, job_id=999_999, stage="discovered", message="orphan"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    assert db.query(JobEvent).filter(JobEvent.job_id == 999_999).count() == 0


def test_seed_covers_every_owned_table(owner, db):
    """The dict above must track the schema, or it proves nothing."""
    from_schema = set(owned_tables())
    assert set(CHILD_TABLES) == from_schema, (
        f"missing from the seed: {sorted(from_schema - set(CHILD_TABLES))}; "
        f"not tenant-owned: {sorted(set(CHILD_TABLES) - from_schema)}"
    )

    user = _owner(db)
    _seed_every_child_table(db, user)
    counts = count_user_rows(db, user.id)
    missing = sorted(name for name in CHILD_TABLES if not counts.get(name))
    assert not missing, f"seeded nothing into: {missing}"
    assert counts["funding_scan_companies.scan_id"] == 1  # the ownerless join table too


#: The tables ``delete_account`` forgot, i.e. everything a fully-populated
#: account had that was *not* in its hand-written list (v2.2.14). Any one of
#: these was enough to abort the erasure with an IntegrityError; the schema has
#: since grown more owner tables again, which is why the plan is derived.
_MISSED_BY_THE_OLD_LIST = {
    "personas", "subscriptions", "billing_events", "ai_credit_ledger", "usage_counters", "notifications",
    "interview_preps", "company_intel", "funding_scans", "scheduled_runs", "funding_scan_companies",
}


# --------------------------------------------------------------------------- #
# Account erasure
# --------------------------------------------------------------------------- #
def test_account_deletion_removes_every_child_row_and_file(client, auth, db):
    """
    A fully-populated account can be deleted, and nothing of it is left behind.

    Before the fix this was a 500 as soon as the account had a persona, a
    subscription, a notification or a usage counter — any table the hand-written
    delete list omitted — and the account stayed, mid-deletion.
    """
    user = _owner(db)
    seeded = _seed_every_child_table(db, user)
    user_id = user.id
    # A second account, to prove the erasure is scoped and not a clever TRUNCATE.
    other = User(email="other@example.com", name="Other", password_hash="x" * 32)
    db.add(other)
    db.commit()
    db.add(Job(user_id=other.id, title="Other's job", company="OtherCo", company_name_normalized="otherco",
               dedupe_key="other:1", description="d"))
    db.commit()
    other_job_id = db.query(Job).filter(Job.user_id == other.id).one().id

    assert os.path.exists(seeded["resume_path"]) and os.path.exists(seeded["rendered_path"])

    response = client.delete("/api/account?confirm=owner@example.com", headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True
    # every child table plus the account row itself
    assert response.json()["tables"] == len(CHILD_TABLES) + 1
    assert response.json()["rows_removed"] >= len(CHILD_TABLES) + 1

    db.expire_all()
    assert count_user_rows(db, user_id) == {}, "rows survived the erasure"
    assert db.query(User).filter(User.id == user_id).count() == 0
    assert db.query(FundingScanCompany).count() == 0
    # The files it owned — master, its rendered PDF, and the derived copy.
    assert not os.path.exists(seeded["resume_path"])
    assert not os.path.exists(seeded["rendered_path"])
    assert not os.path.exists(seeded["derived_path"])
    # Other accounts keep their data and their pointers.
    assert db.query(Job).filter(Job.id == other_job_id).count() == 1
    # The erasure is still auditable, with no owner attached.
    trail = db.query(AuditLog).filter(AuditLog.action == "account.deleted").one()
    assert trail.user_id is None
    assert trail.detail["email"] == "owner@example.com"
    assert trail.detail["rows_removed"] >= len(CHILD_TABLES)


def test_account_deletion_removes_its_own_screenshots_and_nobody_elses(client, auth, db):
    """
    Autofill screenshots go with the account — and only its own.

    ``{screenshot_dir}/job_{job_id}_{utc}.png`` is not a row: the file is the only
    record that the screenshot of a person's filled-in application form exists, so
    a "deleted forever" that leaves it behind has deleted nothing of consequence.
    Matching them by the *job id in the name* is also what keeps the sweep scoped:
    a ``job_*`` wildcard would erase the neighbours.
    """
    user = _owner(db)
    seeded = _seed_every_child_table(db, user)
    other = User(email="neighbour@example.com", name="Neighbour", password_hash="y" * 32)
    db.add(other)
    db.commit()
    neighbour_job = Job(user_id=other.id, title="Theirs", company="NeighbourCo",
                        company_name_normalized="neighbourco", dedupe_key="neighbour:1", description="d")
    db.add(neighbour_job)
    db.commit()

    os.makedirs(settings.screenshot_dir, exist_ok=True)
    mine = _touch(os.path.join(settings.screenshot_dir, f"job_{seeded['job'].id}_20260915000000.png"))
    mine_too = _touch(os.path.join(settings.screenshot_dir, f"job_{seeded['job'].id}_20260915010101.png"))
    theirs = _touch(os.path.join(settings.screenshot_dir, f"job_{neighbour_job.id}_20260915000000.png"))

    assert client.delete("/api/account?confirm=owner@example.com", headers=auth).status_code == 200

    db.expire_all()
    assert not os.path.exists(mine) and not os.path.exists(mine_too), "screenshots survived the erasure"
    assert os.path.exists(theirs), "the erasure deleted another account's screenshot"
    assert db.query(Job).filter(Job.user_id == other.id).count() == 1


def test_account_deletion_deletes_tables_the_endpoint_never_names(client, auth, db):
    """
    Completeness is derived, not remembered: a table the router has never heard
    of is still emptied, because the plan comes from the schema.
    """
    user = _owner(db)
    _seed_every_child_table(db, user)
    user_id = user.id
    plan = build_plan()
    covered = set(plan.owned_tables) | {label.split(".", 1)[0] for label, _ in plan.link_rows.items}
    forgotten = sorted(_MISSED_BY_THE_OLD_LIST - covered)
    assert not forgotten, f"the erasure still cannot reach: {forgotten}"

    assert client.delete("/api/account?confirm=owner@example.com", headers=auth).status_code == 200
    db.expire_all()
    assert count_user_rows(db, user_id) == {}


def test_account_deletion_keeps_files_and_rows_when_the_database_refuses(client, auth, db, monkeypatch):
    """
    A failed erasure must not have deleted anything — including the uploads.

    The file removal used to run *before* the deletes, so any abort left an
    account whose resumes were already gone from disk.
    """
    user = _owner(db)
    seeded = _seed_every_child_table(db, user)

    def refuse(session, user_id, **kwargs):
        raise IntegrityError("DELETE FROM users", {}, Exception("FOREIGN KEY constraint failed"))

    monkeypatch.setattr(account_api, "purge_user_data", refuse)
    response = client.delete("/api/account?confirm=owner@example.com", headers=auth)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "deletion_blocked"

    db.expire_all()
    assert os.path.exists(seeded["resume_path"]), "files were deleted for an account that still exists"
    assert db.query(User).filter(User.id == user.id).count() == 1
    assert db.query(Resume).filter(Resume.user_id == user.id).count() == 2


# --------------------------------------------------------------------------- #
# Resume deletion
# --------------------------------------------------------------------------- #
def test_delete_resume_referenced_by_job_profile_and_copy(client, auth, db):
    """
    Deleting a resume pointed at from four places must not 500 — and must not
    leave the references behind either.

    The history pointers (a job's applied-with resume, a derived copy's parent,
    a persona's source) are detached; the profile's master moves to the
    surviving resume rather than dangling.
    """
    user = _owner(db)
    seeded = _seed_every_child_table(db, user)
    resume, derived, job, profile, persona = (seeded["resume"], seeded["derived"], seeded["job"],
                                             seeded["profile"], seeded["persona"])
    resume_id, derived_id = resume.id, derived.id
    assert job.applied_with_resume_id == resume_id
    assert profile.master_resume_id == resume_id
    assert derived.parent_resume_id == resume_id
    assert persona.source_resume_id == resume_id

    response = client.delete(f"/api/resumes/{resume_id}", headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert sorted(body["unlinked"]) == ["jobs.applied_with_resume_id", "personas.source_resume_id",
                                        "profiles.master_resume_id", "resumes.parent_resume_id"]
    assert body["master_repointed_to"] == derived_id
    assert body["master_detached"] is False

    db.expire_all()
    assert db.query(Resume).filter(Resume.id == resume_id).count() == 0
    assert db.query(Job).filter(Job.id == job.id).one().applied_with_resume_id is None
    assert db.query(Resume).filter(Resume.id == derived_id).one().parent_resume_id is None
    assert db.query(Persona).filter(Persona.id == persona.id).one().source_resume_id is None
    assert db.query(Profile).filter(Profile.id == profile.id).one().master_resume_id == derived_id
    # The deleted resume's files went — and they went only after the commit.
    assert not os.path.exists(seeded["resume_path"])
    assert not os.path.exists(seeded["rendered_path"])
    assert os.path.exists(seeded["derived_path"])
    # The audit trail names what it detached: the only record an unlink leaves.
    entry = db.query(AuditLog).filter(AuditLog.action == "resume.deleted").one()
    assert entry.detail["resume_id"] == resume_id
    assert entry.detail["master_repointed_to"] == derived_id
    assert entry.detail["unlinked"]["jobs.applied_with_resume_id"] == 1


def test_delete_the_only_master_resume_detaches_the_profile(client, auth, db):
    """
    The user's last resume is still deletable.

    Refusing ("upload another master first") would mean the product can be told
    to keep a document its owner asked to remove, so the pointer is dropped
    instead — and the response says so, because the profile is now masterless.
    """
    user = _owner(db)
    seeded = _seed_every_child_table(db, user)
    resume_id, profile_id = seeded["resume"].id, seeded["profile"].id
    # Only the master is left: the derived copy goes first, the ordinary way.
    assert client.delete(f"/api/resumes/{seeded['derived'].id}", headers=auth).status_code == 200
    db.expire_all()
    assert db.query(Profile).filter(Profile.id == profile_id).one().master_resume_id == resume_id

    response = client.delete(f"/api/resumes/{resume_id}", headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["master_repointed_to"] is None
    assert response.json()["master_detached"] is True

    db.expire_all()
    assert db.query(Resume).filter(Resume.user_id == user.id).count() == 0
    assert db.query(Profile).filter(Profile.id == profile_id).one().master_resume_id is None
    assert db.query(Job).filter(Job.id == seeded["job"].id).one().applied_with_resume_id is None
    assert not os.path.exists(seeded["resume_path"])


def test_delete_resume_keeps_files_when_the_database_refuses(client, auth, db, monkeypatch):
    """Same rule as the account erasure: the commit decides, then the disk."""
    user = _owner(db)
    seeded = _seed_every_child_table(db, user)
    resume_id = seeded["resume"].id

    def refuse(session, table_name, row_id, **kwargs):
        raise IntegrityError("DELETE FROM resumes", {}, Exception("FOREIGN KEY constraint failed"))

    monkeypatch.setattr(resumes_api, "break_references", refuse)
    response = client.delete(f"/api/resumes/{resume_id}", headers=auth)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "resume_referenced"
    assert os.path.exists(seeded["resume_path"])
    db.expire_all()
    assert db.query(Resume).filter(Resume.id == resume_id).count() == 1


def test_delete_resume_never_leaks_an_integrity_error(client, auth, db, monkeypatch):
    """
    Even a reference the schema forbids detaching is a 409, not a 500.

    ``break_references`` reports NOT NULL referrers as blockers instead of
    nulling them; the endpoint has to turn that into a refusal that names the
    blocker, because that is the one case where deleting the resume would
    silently orphan somebody else's row.
    """
    user = _owner(db)
    seeded = _seed_every_child_table(db, user)
    resume_id = seeded["resume"].id

    def blocked(session, table_name, row_id, **kwargs):
        return {}, ["pipeline_jobs.job_id"]

    monkeypatch.setattr(resumes_api, "break_references", blocked)
    response = client.delete(f"/api/resumes/{resume_id}", headers=auth)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["blockers"] == ["pipeline_jobs.job_id"]
    db.expire_all()
    assert db.query(Resume).filter(Resume.id == resume_id).count() == 1
    assert os.path.exists(seeded["resume_path"])


def test_delete_persona_detaches_the_tracks_rows(client, full_consent, db):
    """
    Deleting a track must not 500 because jobs/resumes/emails remember it.

    ``personas`` is the second table with the same problem as ``resumes``: three
    nullable ``NO ACTION`` FKs point at it, and ``delete_persona`` deleted the row
    without touching them — an ``IntegrityError`` as soon as the track had ever
    scored anything. The history survives, minus the pointer.
    """
    user = _owner(db)
    seeded = _seed_every_child_table(db, user)
    persona_id = seeded["persona"].id
    job, email, resume = seeded["job"], seeded["email"], seeded["resume"]
    assert job.persona_id == persona_id and email.persona_id == persona_id and resume.persona_id == persona_id

    response = client.delete(f"/api/personas/{persona_id}", headers=full_consent)
    assert response.status_code == 200, response.text

    db.expire_all()
    assert db.query(Persona).filter(Persona.id == persona_id).count() == 0
    for row, label in ((db.get(Job, job.id), "jobs"),
                       (db.get(Email, email.id), "emails"),
                       (db.get(Resume, resume.id), "resumes")):
        assert row is not None, label
        assert row.persona_id is None, label
    # The derived copy keeps its own history; nothing else was taken with the track.
    assert db.query(Resume).filter(Resume.id == seeded["derived"].id).count() == 1


def test_delete_resume_of_another_account_is_still_404(client, member_auth, db):
    """Detaching references is no licence to delete somebody else's file."""
    user = _owner(db)
    seeded = _seed_every_child_table(db, user)
    assert client.delete(f"/api/resumes/{seeded['resume'].id}", headers=member_auth).status_code == 404
    assert os.path.exists(seeded["resume_path"])
    assert db.query(Resume).filter(Resume.id == seeded["resume"].id).count() == 1


def test_break_references_is_derived_from_the_schema(owner, db):
    """
    ``break_references`` finds every nullable pointer, including the ones the
    calling endpoint has never heard of.
    """
    user = _owner(db)
    seeded = _seed_every_child_table(db, user)
    nulled, blockers = break_references(db, "resumes", seeded["resume"].id)
    assert blockers == []  # nothing points at a resume through a NOT NULL FK
    assert sorted(nulled) == ["jobs.applied_with_resume_id", "personas.source_resume_id",
                              "profiles.master_resume_id", "resumes.parent_resume_id"]
    db.rollback()


# --------------------------------------------------------------------------- #
# Export integrity
# --------------------------------------------------------------------------- #
@pytest.fixture()
def sql_log():
    """Every statement the app issues while the test body runs."""
    statements: list = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    yield statements
    event.remove(engine, "before_cursor_execute", _before)


def test_export_pages_every_collection_and_returns_it_all(client, full_consent, db, sql_log):
    """
    The export is complete and bounded: every collection is paged, not loaded.

    ``EXPORT_CHUNK_ROWS`` is pinned to 2 so a handful of rows exercises the
    keyset loop. The claim is the one the old ``.all()`` could not make: a
    collection needs several statements *and* still returns every row, once,
    with no unbounded ``SELECT`` over a table that can hold 100k rows.
    """
    user = _owner(db)
    seeded = _seed_every_child_table(db, user)
    extra_ids = []
    for index in range(4):
        job = Job(user_id=user.id, title=f"Extra {index}", company=f"Co {index}",
                  company_name_normalized=f"co {index}", dedupe_key=f"extra:{index}",
                  description="Python", status="discovered")
        db.add(job)
        db.commit()
        extra_ids.append(job.id)
    expected_job_ids = sorted([seeded["job"].id] + extra_ids)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(account_api, "EXPORT_CHUNK_ROWS", 2)
    sql_log.clear()
    try:
        response = client.get("/api/account/export", headers=full_consent)
    finally:
        monkeypatch.undo()

    assert response.status_code == 200, response.text
    assert "attachment" in response.headers["content-disposition"]
    body = json.loads(response.text)

    # Complete: every collection the account owns is there, with its rows.
    assert body["format_version"] == "2.0"
    assert body["account"]["email"] == "owner@example.com"
    assert sorted(job["id"] for job in body["jobs"]) == expected_job_ids
    assert [job["id"] for job in body["jobs"]] == expected_job_ids  # keyset order, no repeats
    for key in ("profile", "personas", "resumes", "job_events", "vault", "emails", "email_events",
                "suppressions", "applications", "interview_preps", "funding_companies", "funding_scans",
                "company_intel", "settings", "pipeline_jobs", "scheduled_runs", "notifications",
                "subscriptions", "usage_counters", "ai_credit_ledger", "logs", "audit"):
        assert body[key], key
    assert body["ai_credit_ledger"][0]["total_tokens"] == 120
    assert body["subscriptions"][0]["plan"] == "pro"
    assert body["notifications"][0]["title"] == "88% match"
    assert body["applications"][0]["job_id"] == seeded["job"].id
    assert body["resumes"][1]["type"] == "generated"
    # Secrets: the SMTP password is masked, the vault is decrypted for the owner.
    assert all(row["value"] != "smtp-secret" for row in body["settings"])
    assert body["vault"][0]["password"] == "correct-horse-battery"

    # Bounded: 5 jobs at 2 rows a chunk is three reads plus the empty probe.
    job_reads = [s for s in sql_log if re.search(r"FROM jobs\b", s)]
    assert len(job_reads) >= 3, job_reads
    unbounded = [s for s in sql_log
                 if s.lstrip().upper().startswith("SELECT")
                 and re.search(r"FROM (jobs|job_events|emails|ai_credit_ledger|audit_logs)\b", s)
                 and "user_id = ?" in s and "LIMIT" not in s.upper()]
    assert not unbounded, unbounded
    assert any("id > ?" in s or "id >=" in s for s in job_reads), job_reads


def test_export_of_an_empty_account_is_a_well_formed_document(client, full_consent, db):
    """The streaming path has to close its own JSON: no rows, still valid."""
    response = client.get("/api/account/export", headers=full_consent)
    assert response.status_code == 200
    body = json.loads(response.text)
    assert body["jobs"] == [] and body["emails"] == [] and body["ai_credit_ledger"] == []
    assert body["vault"] == [] and body["audit"]
    assert body["exported_at"]
    user = _owner(db)
    assert db.query(AuditLog).filter(AuditLog.action == "account.exported",
                                     AuditLog.user_id == user.id).count() == 1


def test_export_survives_a_row_it_cannot_serialise(client, full_consent, db):
    """A float NaN in a score must not truncate the download halfway."""
    user = _owner(db)
    job = Job(user_id=user.id, title="NaN", company="Edge", company_name_normalized="edge",
              dedupe_key="nan:1", description="d", score=float("nan"))
    db.add(job)
    db.commit()

    response = client.get("/api/account/export", headers=full_consent)
    body = json.loads(response.text)
    assert body["jobs"][0]["score"] is None
    # A bare ``NaN`` is not JSON: the document has to stay parseable, not end
    # half-written on the row the serialiser could not encode.
    assert not re.search(r":\s*NaN\b", response.text), response.text[-200:]
