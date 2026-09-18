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
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    """Naive UTC timestamp (the DB stores naive UTC; the API renders ISO-8601)."""
    return datetime.utcnow()


# --------------------------------------------------------------------------- #
# Identity & access
# --------------------------------------------------------------------------- #
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="owner", nullable=True)  # owner | member
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=True)

    # Compliance state (see docs/COMPLIANCE.md). Timestamps prove *when* the
    # user accepted each disclosure; automation is blocked until they are set.
    consents: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    user_agent: Mapped[str] = mapped_column(String(300), default="", nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ApiKey(Base):
    """Machine credentials for the public API (hashed at rest, prefix for lookup)."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), default="default", nullable=True)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    scopes: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- #
# Candidate data
# --------------------------------------------------------------------------- #
class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)  # extracted profile details
    layout: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)  # layout json
    master_resume_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("resumes.id"), nullable=True)
    # Where the extraction came from. "ai" is the only supported source for the
    # master profile — anything else means the AI layer was unavailable and the
    # row must not be trusted as ground truth.
    extraction_source: Mapped[str] = mapped_column(String(20), default="ai", nullable=True)
    #: The ``resume_extractions`` row that produced this profile — the dedupe key
    #: that makes background re-processing idempotent. A profile produced by the
    #: legacy synchronous upload has ``NULL`` here. The extraction handler upserts
    #: on this column, so running the same job twice can never insert a second row.
    source_extraction_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)


class Persona(Base):
    """
    A candidate *track* — e.g. "Data Analyst" and "Data Scientist" for the same
    person. Each persona owns its own search context, preferences, learned memory
    and funnel (discovery → scoring → resume → outreach), so one account can run
    several job searches in parallel without them blurring into each other.

    ``memory`` grows as the user works: every scored job, application, reply and
    edit records a signal, and ``portrait`` is the AI-written reflection of the
    user *under this persona* — rebuilt from observed evidence only.
    """

    __tablename__ = "personas"
    __table_args__ = (
        Index("ix_personas_user_active", "user_id", "is_active"),
        UniqueConstraint("user_id", "name", name="uq_personas_user_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    target_role: Mapped[str] = mapped_column(String(200), default="", nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #: Derived search context for this track (keywords/roles/industries/…)
    search_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    #: Hard preferences the user states (salary, remote, company stage, exclusions)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    #: Learned state: signals, observed strengths/gaps, funnel counters
    memory: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)

    #: AI reflection of the user under this persona, plus the evidence it rests on
    portrait: Mapped[str] = mapped_column(Text, default="", nullable=True)
    portrait_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    portrait_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    source_resume_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("resumes.id"), nullable=True)

    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)



class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    filepath: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, default="master", nullable=True)  # master | generated | uploaded_polished
    profile_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    layout: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    tags: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    jd_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=True)
    parent_resume_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("resumes.id"), nullable=True)
    status: Mapped[str] = mapped_column(String, default="approved", nullable=True)  # approved | pending | rejected | archived
    # Text snapshot of the *rendered* document, used for diff/preview without
    # re-parsing files on every request.
    text_snapshot: Mapped[str] = mapped_column(Text, default="", nullable=True)
    #: Professional download name ("Diganta-Dey-Wirelane-Embedded-Engineer.pdf").
    #: ``filepath`` stays an opaque unique path on disk; this is what the user sees.
    display_name: Mapped[str] = mapped_column(String(300), default="", nullable=True)
    #: Which persona/track this resume was tailored for.
    persona_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("personas.id"), nullable=True)
    #: Guardrail verdict (accuracy + ATS quality checks) — see ai_guardrails.py
    guardrail_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=True)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "dedupe_key", name="uq_jobs_user_dedupe"),
        Index("ix_jobs_user_status", "user_id", "status"),
        Index("ix_jobs_user_company_norm", "user_id", "company_name_normalized"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    company: Mapped[str] = mapped_column(String, nullable=False)
    #: Normalised company identity for SQL-side filtering and funding linkage
    #: (see :func:`app.services.company_normalize.normalize_company_name`):
    #: lowercased, punctuation-stripped, legal-form suffixes dropped. Stored at
    #: create/import time and backfilled by migration ``e5f6a7b8c9d0`` so
    #: ``GET /api/jobs?company=`` filters in SQL (LIMIT/OFFSET applied before
    #: any Python) instead of loading the user's whole board to filter in the
    #: app. ``company`` stays the display name.
    company_name_normalized: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    location: Mapped[str] = mapped_column(String, default="", nullable=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=True)
    url: Mapped[str] = mapped_column(String, default="", nullable=True)
    source: Mapped[str] = mapped_column(String, default="unknown", nullable=True)  # live source id or curated pool id
    external_id: Mapped[str] = mapped_column(String, default="", nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(300), default="", nullable=True)
    #: Board status. Vocabulary: :data:`app.contracts.JOB_STATUSES`
    #: (``docs/contracts/07-application-state-machine.md`` §5) — discovered |
    #: queued | preparing | needs_input | ready_to_apply | applied | rejected |
    #: failed | skipped. ``applying`` and ``emailed`` were documented here but no
    #: code path ever wrote them, while ``preparing``, ``ready_to_apply`` and
    #: ``skipped`` (all written by ``services/apply_flow.py``) were missing — see
    #: CHANGELOG 2.2.18.
    status: Mapped[str] = mapped_column(String, default="discovered", nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=True)
    score_reason: Mapped[str] = mapped_column(Text, default="", nullable=True)
    #: Provenance of ``score``: "ai" (guardrail-verified), "pending" (AI offline,
    #: not yet scored) or "unscored" (no profile/JD to score against).
    score_source: Mapped[str] = mapped_column(String(20), default="pending", nullable=True)
    #: Rubric breakdown + evidence + guardrail report for the score above.
    score_detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    #: Persona/track this job was discovered and scored for.
    persona_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("personas.id"), nullable=True)
    company_size: Mapped[str] = mapped_column(String, default="unknown", nullable=True)  # big, medium, small, startup
    company_info: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=True)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    freshness_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=True)
    applied_with_resume_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("resumes.id"), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)  # forms structure, questions, autofill plan…
    error: Mapped[str] = mapped_column(Text, default="", nullable=True)
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class JobEvent(Base):
    """Append-only state timeline for a job (per-job approval console)."""

    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(60), default="discovered", nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="info", nullable=True)  # info | success | warning | error
    message: Mapped[str] = mapped_column(Text, default="", nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class VaultEntry(Base):
    __tablename__ = "vault_entries"
    __table_args__ = (UniqueConstraint("user_id", "domain", name="uq_vault_user_domain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String, nullable=False)
    username: Mapped[str] = mapped_column(String, nullable=False)
    password_enc: Mapped[str] = mapped_column(String, nullable=False)
    origin: Mapped[str] = mapped_column(String(30), default="auto", nullable=True)  # auto | manual
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Email(Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    to_email: Mapped[str] = mapped_column(String, nullable=False)
    to_name: Mapped[str] = mapped_column(String, default="", nullable=True)
    subject: Mapped[str] = mapped_column(String, default="", nullable=True)
    body: Mapped[str] = mapped_column(Text, default="", nullable=True)
    #: Outreach status. draft | pending_approval | queued | sending | sent |
    #: dry_run | opened | failed | needs_otp | suppressed — every value
    #: ``services/outreach.py`` writes (``dry_run`` and ``opened`` were missing
    #: from this comment; see CHANGELOG 2.2.18).
    status: Mapped[str] = mapped_column(String, default="draft", nullable=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=True)
    company: Mapped[str] = mapped_column(String, default="", nullable=True)
    #: Denormalised job context so the approval bucket can show *which* posting
    #: this draft was written for without a join (and survives job deletion).
    job_title: Mapped[str] = mapped_column(String(300), default="", nullable=True)
    job_url: Mapped[str] = mapped_column(String(500), default="", nullable=True)
    jd_excerpt: Mapped[str] = mapped_column(Text, default="", nullable=True)
    #: Persona/track the outreach was written for.
    persona_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("personas.id"), nullable=True)
    recipient_type: Mapped[str] = mapped_column(String, default="hiring_manager", nullable=True)  # hiring_manager | founder
    source: Mapped[str] = mapped_column(String, default="heuristic", nullable=True)  # ai | hunter | clearbit | apollo | heuristic | manual
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=True)
    #: True only when the address came from a verifying provider (hunter/apollo
    #: or an MX-confirmed personal address). Role mailboxes are always False.
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: True when the message text was written by the model (vs. a template).
    ai_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Guardrail verdict for the drafted subject/body.
    guardrail_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    tracking_token: Mapped[str] = mapped_column(String(64), default="", index=True, nullable=True)
    unsubscribe_token: Mapped[str] = mapped_column(String(64), default="", index=True, nullable=True)
    opens: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    last_opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="", nullable=True)


class EmailEvent(Base):
    """Delivery / engagement / compliance events for an email."""

    __tablename__ = "email_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    email_id: Mapped[int] = mapped_column(Integer, ForeignKey("emails.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), default="info", nullable=True)  # queued|sent|open|bounce|complaint|unsubscribe|blocked|failed
    detail: Mapped[str] = mapped_column(Text, default="", nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class EmailOptOut(Base):
    """Suppression list — honoured before *every* send (CAN-SPAM / GDPR)."""

    __tablename__ = "email_opt_outs"
    __table_args__ = (UniqueConstraint("user_id", "email", name="uq_optout_user_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(200), default="user_request", nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class SettingsModel(Base):
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("user_id", "category", "key", name="uq_settings_user_cat_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String, nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)


class ErrorLog(Base):
    __tablename__ = "error_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, nullable=True)
    pipeline: Mapped[str] = mapped_column(String, default="general", nullable=True)
    level: Mapped[str] = mapped_column(String, default="error", nullable=True)  # debug|info|warning|error|critical
    message: Mapped[str] = mapped_column(Text, default="", nullable=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    request_id: Mapped[str] = mapped_column(String(64), default="", nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)


class UserInputRequest(Base):
    __tablename__ = "user_input_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=True)
    fields: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)  # [{name, label, type, required, value}]
    status: Mapped[str] = mapped_column(String, default="pending", nullable=True)  # pending, completed, cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class PipelineJob(Base):
    """Durable work item for the discovery/application/email/funding pipelines."""

    __tablename__ = "pipeline_jobs"
    __table_args__ = (
        Index("ix_pipeline_jobs_claim", "pipeline", "status", "scheduled_at"),
        UniqueConstraint("user_id", "dedupe_key", name="uq_pipeline_user_dedupe"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    pipeline: Mapped[str] = mapped_column(String, nullable=False)  # discovery|application|email|funding|ai
    job_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    status: Mapped[str] = mapped_column(String, default="queued", nullable=True)  # queued|processing|paused|done|failed|needs_input|dead
    priority: Mapped[int] = mapped_column(Integer, default=5, nullable=True)  # 1 highest
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=True)
    #: How many times this row has been re-queued because its lease expired
    #: (crash/restart). Tracked separately from ``attempts`` (handler failures)
    #: so a crash-looping job can be dead-lettered after N reclaims instead of
    #: bouncing forever. Incremented atomically in :func:`recover_stalled`.
    reclaim_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(300), default="", nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[str] = mapped_column(String(80), default="", nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="", nullable=True)


class ScheduledRun(Base):
    """One auto-mode scheduling decision for one (user, workflow).

    Written by :mod:`app.services.auto_scheduler` — it is *not* the run itself
    (the work is a ``pipeline_jobs`` row, linked here by ``queue_job_id``) but
    the scheduler's record of what it decided. That single table drives three
    things, so the UI, the cadence clock and the quota-notification dedupe can
    never disagree with each other:

    * the ``due`` check — a workflow is due when its newest *completed* run
      (``done|paused|failed|needs_input``) is older than the plan's cadence;
      a run still in ``queued`` state means work is in flight, not that the
      user is due again;
    * the history the Settings card and ``GET /api/automation`` show — every
      state is honest, including the skips (``skipped_outage`` /
      ``skipped_quota`` / ``skipped_no_consent``);
    * the per-window idempotency guard: ``cycle_bucket`` is the cadence window
      this decision belongs to, so two sweeps inside one window cannot enqueue
      the same work twice.

    ``job_id`` is the *Job* the run targets (application-prep only; ``null``
    otherwise), and ``reason`` carries the human-readable why for a skip or a
    failure so the UI never has to guess.
    """

    __tablename__ = "scheduled_runs"
    __table_args__ = (
        Index("ix_scheduled_runs_user_workflow", "user_id", "workflow", "triggered_at"),
        Index("ix_scheduled_runs_user_bucket", "user_id", "workflow", "cycle_bucket"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    #: discovery | funding | application_prep (see auto_scheduler.CADENCE_SECONDS)
    workflow: Mapped[str] = mapped_column(String(40), nullable=False)
    #: The cadence window this decision belongs to (epoch seconds // cadence).
    cycle_bucket: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    triggered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    #: The queue item this decision enqueued (``null`` for a skip).
    queue_job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: The job an application-prep run targets; ``null`` for the other workflows.
    job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: queued|done|paused|failed|needs_input|skipped_outage|skipped_quota|skipped_no_consent
    state: Mapped[str] = mapped_column(String(30), default="queued", nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class FundingCompany(Base):
    """One company on a user's funding radar.

    Identity for dedupe is the *normalised* name (strip + casefold, see
    :func:`funding_radar.normalize_company_name`): providers disagree about
    casing, and "Stripe"/"stripe" used to become two rows. The ``name`` column
    keeps the provider's canonical casing for display.

    ``discovered_at`` is **first seen** and is never rewritten; ``last_seen_at``
    is the most recent scan that still returned the company, and is the clock
    the prune window runs on (see :func:`funding_radar.prune_funding_db`).

    ``meta`` carries the provider's own payload (filing URL, amount, and — when
    a provider really reports them — ``open_positions``).
    ``has_open_positions`` is kept fresh in both directions (v2.2.5): at scan
    persist time and at discovery completion, via the shared matcher.
    """

    __tablename__ = "funding_companies"
    __table_args__ = (
        # Identity is the *normalised* name (v2.2.9): uniqueness used to be on
        # the raw ``name`` while every dedupe/lookup path already keyed on the
        # normalised form, so "Acme Inc." and "acme inc." were two rows the
        # code believed were one.
        UniqueConstraint("user_id", "name_normalized", name="uq_funding_user_name_norm"),
        Index("ix_funding_user_seen", "user_id", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    #: Dedupe/lookup key — :func:`funding_sources.normalize_company_name`
    #: (strip + collapse whitespace + casefold). ``name`` keeps the provider's
    #: display casing.
    name_normalized: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default="", index=True)
    stage: Mapped[str] = mapped_column(String, default="Undisclosed", nullable=True)  # Seed, Series A-D, Undisclosed
    raised_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=True)
    website: Mapped[str] = mapped_column(String, default="", nullable=True)
    industry: Mapped[str] = mapped_column(String, default="", nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=True)
    keywords_matched: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    source: Mapped[str] = mapped_column(String, default="demo", nullable=True)  # sec_edgar | crunchbase | tracxn | imported | demo
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: First scan that surfaced this company (never rewritten).
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=True)
    #: Most recent scan that still returned it — the prune clock.
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    #: v2.2.5 linkage — does this user have ≥1 live job whose company
    #: normalizes to this name (via company_normalize.normalize_company_name).
    has_open_positions: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class FundingScan(Base):
    """One funding scan for one user — the history the radar renders.

    v2.2.5: the radar persisted only the union (FundingCompany rows) and nothing
    per-scan, so the page could show only "what's on the radar now", not "what
    each scan found, and what changed". Every completed scan (ok or
    scan_failed) writes one row here, plus membership rows in
    funding_scan_companies that record exactly what that scan returned, ranked.
    """

    __tablename__ = "funding_scans"
    __table_args__ = (
        Index("ix_funding_scans_user_scanned", "user_id", "scanned_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    scanned_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ok", nullable=False)  # ok | scan_failed
    provider_errors: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    events_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    companies_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)


class FundingScanCompany(Base):
    """Membership: which companies a particular funding scan returned."""

    __tablename__ = "funding_scan_companies"
    __table_args__ = (
        UniqueConstraint("scan_id", "company_id", name="uq_scan_company"),
        Index("ix_funding_scan_companies_scan", "scan_id"),
        Index("ix_funding_scan_companies_company", "company_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    scan_id: Mapped[int] = mapped_column(Integer, ForeignKey("funding_scans.id", ondelete="CASCADE"), nullable=False)
    company_id: Mapped[int] = mapped_column(Integer, ForeignKey("funding_companies.id", ondelete="CASCADE"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    why: Mapped[str] = mapped_column(Text, default="", nullable=True)


class AuditLog(Base):
    """Security-relevant actions (who did what, when, from where)."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target: Mapped[str] = mapped_column(String(200), default="", nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    actor: Mapped[str] = mapped_column(String(320), default="", nullable=True)
    ip: Mapped[str] = mapped_column(String(64), default="", nullable=True)
    request_id: Mapped[str] = mapped_column(String(64), default="", nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)


# --------------------------------------------------------------------------- #
# Monetization & SaaS — Plans, Subscriptions, Credits, Usage
# --------------------------------------------------------------------------- #
class Subscription(Base):
    """
    Per-user subscription state.

    Provider abstraction: stripe | razorpay | manual.
    Status: trialing | active | past_due | canceled | expired | incomplete
    """

    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("user_id", name="uq_subscriptions_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plan: Mapped[str] = mapped_column(String(20), default="free", nullable=False)  # free | pro | pro_plus
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    provider: Mapped[str] = mapped_column(String(20), default="manual", nullable=True)  # manual | stripe | razorpay
    provider_customer_id: Mapped[str] = mapped_column(String(200), default="", nullable=True)
    provider_subscription_id: Mapped[str] = mapped_column(String(200), default="", nullable=True)
    current_period_start: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=True)
    current_period_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    trial_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    grace_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)


class BillingEvent(Base):
    """Idempotency & audit for webhook events (stripe/razorpay)."""

    __tablename__ = "billing_events"
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_billing_provider_event"),
        Index("ix_billing_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)  # stripe | razorpay | manual
    provider_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(80), default="", nullable=True)  # e.g., invoice.paid, subscription.updated
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class AICreditLedger(Base):
    """
    Every AI operation that costs tokens/money.
    Used for per-user limits, cost ceilings, and billing.
    """

    __tablename__ = "ai_credit_ledger"
    __table_args__ = (Index("ix_ai_ledger_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    workflow: Mapped[str] = mapped_column(String(40), nullable=False)  # parse, scoring, resume_gen, etc.
    model: Mapped[str] = mapped_column(String(120), default="", nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="", nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class UsageCounter(Base):
    """
    Monthly usage counters per user, reset on period start.
    One row per (user, period, capability) or aggregated JSON.
    """

    __tablename__ = "usage_counters"
    __table_args__ = (
        UniqueConstraint("user_id", "period", "capability", name="uq_usage_user_period_cap"),
        Index("ix_usage_user_period", "user_id", "period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    capability: Mapped[str] = mapped_column(String(40), nullable=False)  # jobs_discovered, ai_ops, resumes, etc.
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    limit: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # snapshot of limit at time
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Notification(Base):
    """In-app notifications for job alerts, automation failures, etc."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_read", "user_id", "read", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), default="info", nullable=True)  # high_match, automation_failed, email_reply, etc.
    title: Mapped[str] = mapped_column(String(200), default="", nullable=True)
    body: Mapped[str] = mapped_column(Text, default="", nullable=True)
    link: Mapped[str] = mapped_column(String(500), default="", nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class InterviewPrep(Base):
    """Interview preparation sessions grounded in resume + JD."""

    __tablename__ = "interview_preps"
    __table_args__ = (Index("ix_interview_user_job", "user_id", "job_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=True)
    job_title: Mapped[str] = mapped_column(String(300), default="", nullable=True)
    company: Mapped[str] = mapped_column(String(200), default="", nullable=True)
    questions: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)  # [{q, category, difficulty}]
    answers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)  # user answers
    feedback: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)  # AI feedback per answer
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=True)  # draft | in_progress | completed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)


class CompanyIntel(Base):
    """Cached company intelligence for deeper research."""

    __tablename__ = "company_intel"
    __table_args__ = (UniqueConstraint("user_id", "company", name="uq_intel_user_company"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    company: Mapped[str] = mapped_column(String(200), nullable=False)
    website: Mapped[str] = mapped_column(String(500), default="", nullable=True)
    industry: Mapped[str] = mapped_column(String(200), default="", nullable=True)
    size: Mapped[str] = mapped_column(String(50), default="unknown", nullable=True)
    funding_stage: Mapped[str] = mapped_column(String(50), default="", nullable=True)
    tech_stack: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    culture: Mapped[str] = mapped_column(Text, default="", nullable=True)
    recent_news: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    sources: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)


# --------------------------------------------------------------------------- #
# Onboarding sessions & resume extraction pipeline
#
# The resumable, user-facing onboarding workflow (docs/contracts/03 & 04). Three
# aggregates split what the legacy ``POST /api/resume/upload`` did inside one
# long HTTP request:
#
#   OnboardingSession   the durable wizard state — one row per user, safe to
#                       read after a refresh, a restart or a second device;
#   ResumeDocument      the uploaded file itself (original preserved on disk,
#                       content-hashed so a re-upload of identical bytes is a
#                       no-op);
#   ResumeExtraction    one append-only row per extraction attempt — the
#                       versioned AI artifact, created *before* the model is
#                       called so a crash leaves a failed row instead of
#                       nothing.
#
# Every row is tenant-scoped by ``user_id`` and every query in the service and
# router layers filters on it: cross-tenant reads are 404s by construction.
# --------------------------------------------------------------------------- #
class OnboardingSession(Base):
    """One resumable onboarding journey per user (not per attempt).

    ``state`` is a persisted *projection*: it is reconciled against the stored
    facts (document, extraction attempt, queue row) on every read, so a crash,
    a deploy or a worker restart can never leave a stale state in front of the
    user. States used by the current scope are a subset of
    ``contracts.vocabulary.ONBOARDING_STATES``: ``awaiting_resume`` →
    ``resume_processing`` → (``extraction_blocked`` | ``profile_review_required``).
    """

    __tablename__ = "onboarding_sessions"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_onboarding_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), default="awaiting_resume", nullable=False)
    state_since: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    #: The active master document (``None`` until the first upload).
    resume_document_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("resume_documents.id"), nullable=True)
    #: The extraction attempt the session is attached to.
    extraction_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("resume_extractions.id"), nullable=True)
    #: The in-flight queue item, so the SPA can attach to the live row.
    pipeline_job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: ``{code, message, retryable, retry_after, since}`` — ``{}`` when not blocked.
    #: ``code`` is from the blocked-code vocabulary (contract 04 §7), never free
    #: text; ``message`` is pre-sanitised (see ``onboarding.safe_error_message``).
    blocked: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)


class ResumeDocument(Base):
    """The uploaded file: original bytes preserved, content-hashed, versioned.

    The file at ``filepath`` is the *original* document and is never rewritten
    or removed by the onboarding flow — replacing a resume archives the old
    row (and file) rather than deleting either. ``sha256`` dedupes a
    re-upload: identical bytes under the same role return the existing row.
    """

    __tablename__ = "resume_documents"
    __table_args__ = (
        UniqueConstraint("user_id", "sha256", "role", name="uq_resume_doc_user_hash_role"),
        Index("ix_resume_docs_user_role_state", "user_id", "role", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), default="master", nullable=False)  # RESUME_ROLES
    state: Mapped[str] = mapped_column(String(20), default="stored", nullable=False)  # RESUME_STATES
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    display_name: Mapped[str] = mapped_column(String(300), default="", nullable=True)
    #: Opaque storage path — never exposed in API responses.
    filepath: Mapped[str] = mapped_column(String(600), nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), default="application/pdf", nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    text_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    page_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    layout: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    #: Filled when an extraction succeeds; empty until then.
    profile_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    tags: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    #: Sanitised human sentence — provider secrets are scrubbed before storage.
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)


class ResumeExtraction(Base):
    """One extraction attempt — append-only, one row per try, versioned.

    Created *before* the queue item runs, so a crash leaves a recoverable row
    rather than nothing (that is what replaces the legacy in-process progress
    dict as the durable record). ``attempt`` is 1-based per document.
    """

    __tablename__ = "resume_extractions"
    __table_args__ = (
        UniqueConstraint("user_id", "resume_document_id", "attempt", name="uq_extraction_user_doc_attempt"),
        Index("ix_extractions_user_doc_state", "user_id", "resume_document_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    resume_document_id: Mapped[int] = mapped_column(Integer, ForeignKey("resume_documents.id"), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), default="profile_extraction", nullable=False)
    #: pending → running → succeeded | failed | rejected | superseded
    state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    pipeline_job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    extractor: Mapped[str] = mapped_column(String(60), default="ai_profile_extractor", nullable=True)
    #: A bump is what makes an old extraction ``superseded`` and comparable.
    extractor_version: Mapped[str] = mapped_column(String(24), default="1.0.0", nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    prompt_version: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    profile_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("profiles.id"), nullable=True)
    input_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    output_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    field_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    needs_review_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    guardrail_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    #: ``{prompt, completion, total}`` — mirrors ``ai_credit_ledger``.
    tokens: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    #: Sanitised human sentence (≤2000 chars) — never the provider's raw error.
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Why this attempt exists: ``user`` (upload) | ``retry`` | ``recovery``.
    trigger: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class OnboardingEvent(Base):
    """Append-only onboarding timeline (contract 04 §6).

    Exists because onboarding is the journey support will be asked about —
    ``audit_logs`` records security actions, not progress. ``sequence`` is
    monotonic per session so ordering never depends on timestamp granularity.
    """

    __tablename__ = "onboarding_events"
    __table_args__ = (
        UniqueConstraint("user_id", "session_id", "sequence", name="uq_onboarding_event_sequence"),
        Index("ix_onboarding_events_user_time", "user_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("onboarding_sessions.id"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)  # EVENT_TYPES
    gate: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    state_from: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    state_to: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(20), default="user", nullable=True)  # ACTOR_TYPES
    actor_label: Mapped[str] = mapped_column(String(120), default="", nullable=True)
    pipeline_job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    request_id: Mapped[str] = mapped_column(String(64), default="", nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    severity: Mapped[str] = mapped_column(String(20), default="info", nullable=True)  # NOTIFICATION_SEVERITIES
    message: Mapped[str] = mapped_column(String(400), default="", nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- #
# FundingCompany.name_normalized — kept in sync with ``name`` by the ORM.
#
# The normalised name is the row's identity (UNIQUE(user_id, name_normalized)),
# so it must never depend on a caller remembering to set it. The rules are the
# ones :func:`app.services.funding_sources.normalize_company_name` applies —
# duplicated here (three trivial string operations) rather than imported,
# because the services layer imports the models layer.
# --------------------------------------------------------------------------- #
def _funding_name_key(value) -> str:
    return " ".join(str(value or "").split()).casefold()[:200]


@event.listens_for(FundingCompany, "before_insert")
@event.listens_for(FundingCompany, "before_update")
def _sync_funding_name_normalized(_mapper, _connection, target: "FundingCompany") -> None:
    target.name_normalized = _funding_name_key(target.name)
