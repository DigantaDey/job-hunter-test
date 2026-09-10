"""
SQLAlchemy models.

Production notes
----------------
* Every tenant-owned row carries ``user_id`` (indexed). All API queries are
  scoped by the authenticated user — see ``app/api/deps.py``.
* Secret material (vault passwords, SMTP passwords, API keys) is never stored in
  clear text: see ``app/core/security.py``.
* ``JobEvent``/``EmailEvent``/``AuditLog`` give every state transition an
  append-only trail (required for the "delete forever" / cold-outreach flows).
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.db import Base


def utcnow() -> datetime:
    """Naive UTC timestamp (the DB stores naive UTC; the API renders ISO-8601)."""
    return datetime.utcnow()


# --------------------------------------------------------------------------- #
# Identity & access
# --------------------------------------------------------------------------- #
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(320), nullable=False, unique=True, index=True)
    name = Column(String(200), default="")
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="owner")  # owner | member
    is_active = Column(Boolean, default=True, nullable=False)
    timezone = Column(String(64), default="UTC")

    # Compliance state (see docs/COMPLIANCE.md). Timestamps prove *when* the
    # user accepted each disclosure; automation is blocked until they are set.
    consents = Column(JSON, default=dict)
    last_login_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(128), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    user_agent = Column(String(300), default="")
    created_at = Column(DateTime, default=utcnow, nullable=False)


class ApiKey(Base):
    """Machine credentials for the public API (hashed at rest, prefix for lookup)."""

    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(120), default="default")
    prefix = Column(String(16), nullable=False, index=True)
    key_hash = Column(String(128), nullable=False, unique=True, index=True)
    scopes = Column(JSON, default=list)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- #
# Candidate data
# --------------------------------------------------------------------------- #
class Profile(Base):
    __tablename__ = "profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    data = Column(JSON, default=dict)  # extracted profile details
    layout = Column(JSON, default=dict)  # layout json
    master_resume_id = Column(Integer, ForeignKey("resumes.id"), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class Resume(Base):
    __tablename__ = "resumes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    filename = Column(String, nullable=False)
    filepath = Column(String, nullable=False)
    type = Column(String, default="master")  # master | generated | uploaded_polished
    profile_snapshot = Column(JSON, default=dict)
    layout = Column(JSON, default=dict)
    tags = Column(JSON, default=list)
    jd_hash = Column(String, nullable=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    parent_resume_id = Column(Integer, ForeignKey("resumes.id"), nullable=True)
    status = Column(String, default="approved")  # approved | pending | rejected | archived
    # Text snapshot of the *rendered* document, used for diff/preview without
    # re-parsing files on every request.
    text_snapshot = Column(Text, default="")
    approved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "dedupe_key", name="uq_jobs_user_dedupe"),
        Index("ix_jobs_user_status", "user_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String, nullable=False)
    company = Column(String, nullable=False)
    location = Column(String, default="")
    description = Column(Text, default="")
    url = Column(String, default="")
    source = Column(String, default="unknown")  # live source id or curated pool id
    external_id = Column(String, default="")
    dedupe_key = Column(String(300), default="")
    status = Column(String, default="discovered")  # discovered|queued|needs_input|applying|applied|failed|emailed|rejected
    score = Column(Float, default=0.0)
    score_reason = Column(Text, default="")
    company_size = Column(String, default="unknown")  # big, medium, small, startup
    company_info = Column(JSON, default=dict)
    discovered_at = Column(DateTime, default=utcnow)
    posted_at = Column(DateTime, nullable=True)
    freshness_hours = Column(Integer, default=24)
    applied_with_resume_id = Column(Integer, ForeignKey("resumes.id"), nullable=True)
    extra = Column(JSON, default=dict)  # forms structure, questions, autofill plan…
    error = Column(Text, default="")
    applied_at = Column(DateTime, nullable=True)


class JobEvent(Base):
    """Append-only state timeline for a job (per-job approval console)."""

    __tablename__ = "job_events"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    stage = Column(String(60), default="discovered")
    status = Column(String(30), default="info")  # info | success | warning | error
    message = Column(Text, default="")
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class VaultEntry(Base):
    __tablename__ = "vault_entries"
    __table_args__ = (UniqueConstraint("user_id", "domain", name="uq_vault_user_domain"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    domain = Column(String, nullable=False)
    username = Column(String, nullable=False)
    password_enc = Column(String, nullable=False)
    origin = Column(String(30), default="auto")  # auto | manual
    created_at = Column(DateTime, default=utcnow)
    last_used_at = Column(DateTime, nullable=True)


class Email(Base):
    __tablename__ = "emails"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    to_email = Column(String, nullable=False)
    to_name = Column(String, default="")
    subject = Column(String, default="")
    body = Column(Text, default="")
    status = Column(String, default="draft")  # draft|pending_approval|queued|sending|sent|failed|needs_otp|suppressed
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    company = Column(String, default="")
    recipient_type = Column(String, default="hiring_manager")  # hiring_manager | founder
    source = Column(String, default="heuristic")  # ai | hunter | clearbit | apollo | heuristic | manual
    confidence = Column(Float, default=0.0)
    tracking_token = Column(String(64), default="", index=True)
    unsubscribe_token = Column(String(64), default="", index=True)
    opens = Column(Integer, default=0)
    last_opened_at = Column(DateTime, nullable=True)
    dry_run = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    sent_at = Column(DateTime, nullable=True)
    error = Column(Text, default="")


class EmailEvent(Base):
    """Delivery / engagement / compliance events for an email."""

    __tablename__ = "email_events"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    email_id = Column(Integer, ForeignKey("emails.id"), nullable=False, index=True)
    kind = Column(String(40), default="info")  # queued|sent|open|bounce|complaint|unsubscribe|blocked|failed
    detail = Column(Text, default="")
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class EmailOptOut(Base):
    """Suppression list — honoured before *every* send (CAN-SPAM / GDPR)."""

    __tablename__ = "email_opt_outs"
    __table_args__ = (UniqueConstraint("user_id", "email", name="uq_optout_user_email"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    email = Column(String(320), nullable=False, index=True)
    reason = Column(String(200), default="user_request")
    created_at = Column(DateTime, default=utcnow, nullable=False)


class SettingsModel(Base):
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("user_id", "category", "key", name="uq_settings_user_cat_key"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    category = Column(String, nullable=False)
    key = Column(String, nullable=False)
    value = Column(JSON, nullable=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class ErrorLog(Base):
    __tablename__ = "error_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    timestamp = Column(DateTime, default=utcnow, index=True)
    pipeline = Column(String, default="general")
    level = Column(String, default="error")  # debug|info|warning|error|critical
    message = Column(Text, default="")
    job_id = Column(Integer, nullable=True)
    request_id = Column(String(64), default="")
    meta = Column(JSON, default=dict)


class UserInputRequest(Base):
    __tablename__ = "user_input_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"))
    fields = Column(JSON, default=list)  # [{name, label, type, required, value}]
    status = Column(String, default="pending")  # pending, completed, cancelled
    created_at = Column(DateTime, default=utcnow)
    completed_at = Column(DateTime, nullable=True)


class PipelineJob(Base):
    """Durable work item for the discovery/application/email/funding pipelines."""

    __tablename__ = "pipeline_jobs"
    __table_args__ = (
        Index("ix_pipeline_jobs_claim", "pipeline", "status", "scheduled_at"),
        UniqueConstraint("user_id", "dedupe_key", name="uq_pipeline_user_dedupe"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    pipeline = Column(String, nullable=False)  # discovery|application|email|funding|ai
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    payload = Column(JSON, default=dict)
    status = Column(String, default="queued")  # queued|processing|done|failed|needs_input|dead
    priority = Column(Integer, default=5)  # 1 highest
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=3)
    dedupe_key = Column(String(300), default="")
    scheduled_at = Column(DateTime, default=utcnow, index=True)
    lease_expires_at = Column(DateTime, nullable=True)
    locked_by = Column(String(80), default="")
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    finished_at = Column(DateTime, nullable=True)
    error = Column(Text, default="")


class FundingCompany(Base):
    __tablename__ = "funding_companies"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_funding_user_name"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    stage = Column(String, default="Series A")  # Seed, Series A-D
    raised_at = Column(DateTime, default=utcnow)
    website = Column(String, default="")
    industry = Column(String, default="")
    summary = Column(Text, default="")
    keywords_matched = Column(JSON, default=list)
    source = Column(String, default="demo")  # sec-edgar | crunchbase | tracxn | demo
    verified = Column(Boolean, default=False, nullable=False)
    has_open_positions = Column(Boolean, default=False)
    discovered_at = Column(DateTime, default=utcnow)
    meta = Column(JSON, default=dict)


class AuditLog(Base):
    """Security-relevant actions (who did what, when, from where)."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_user_created", "user_id", "created_at"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action = Column(String(80), nullable=False)
    target = Column(String(200), default="")
    detail = Column(JSON, default=dict)
    actor = Column(String(320), default="")
    ip = Column(String(64), default="")
    request_id = Column(String(64), default="")
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)
