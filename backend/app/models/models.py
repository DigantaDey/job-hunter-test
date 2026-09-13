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
    # Where the extraction came from. "ai" is the only supported source for the
    # master profile — anything else means the AI layer was unavailable and the
    # row must not be trusted as ground truth.
    extraction_source = Column(String(20), default="ai")
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


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

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    target_role = Column(String(200), default="")
    is_active = Column(Boolean, default=True, nullable=False)
    is_default = Column(Boolean, default=False, nullable=False)

    #: Derived search context for this track (keywords/roles/industries/…)
    search_context = Column(JSON, default=dict)
    #: Hard preferences the user states (salary, remote, company stage, exclusions)
    preferences = Column(JSON, default=dict)
    #: Learned state: signals, observed strengths/gaps, funnel counters
    memory = Column(JSON, default=dict)

    #: AI reflection of the user under this persona, plus the evidence it rests on
    portrait = Column(Text, default="")
    portrait_evidence = Column(JSON, default=dict)
    portrait_at = Column(DateTime, nullable=True)

    stats = Column(JSON, default=dict)
    source_resume_id = Column(Integer, ForeignKey("resumes.id"), nullable=True)

    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
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
    #: Professional download name ("Diganta-Dey-Wirelane-Embedded-Engineer.pdf").
    #: ``filepath`` stays an opaque unique path on disk; this is what the user sees.
    display_name = Column(String(300), default="")
    #: Which persona/track this resume was tailored for.
    persona_id = Column(Integer, ForeignKey("personas.id"), nullable=True)
    #: Guardrail verdict (accuracy + ATS quality checks) — see ai_guardrails.py
    guardrail_report = Column(JSON, default=dict)
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
    #: Provenance of ``score``: "ai" (guardrail-verified), "pending" (AI offline,
    #: not yet scored) or "unscored" (no profile/JD to score against).
    score_source = Column(String(20), default="pending")
    #: Rubric breakdown + evidence + guardrail report for the score above.
    score_detail = Column(JSON, default=dict)
    #: Persona/track this job was discovered and scored for.
    persona_id = Column(Integer, ForeignKey("personas.id"), nullable=True)
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
    #: Denormalised job context so the approval bucket can show *which* posting
    #: this draft was written for without a join (and survives job deletion).
    job_title = Column(String(300), default="")
    job_url = Column(String(500), default="")
    jd_excerpt = Column(Text, default="")
    #: Persona/track the outreach was written for.
    persona_id = Column(Integer, ForeignKey("personas.id"), nullable=True)
    recipient_type = Column(String, default="hiring_manager")  # hiring_manager | founder
    source = Column(String, default="heuristic")  # ai | hunter | clearbit | apollo | heuristic | manual
    confidence = Column(Float, default=0.0)
    #: True only when the address came from a verifying provider (hunter/apollo
    #: or an MX-confirmed personal address). Role mailboxes are always False.
    verified = Column(Boolean, default=False, nullable=False)
    #: True when the message text was written by the model (vs. a template).
    ai_used = Column(Boolean, default=False, nullable=False)
    #: Guardrail verdict for the drafted subject/body.
    guardrail_report = Column(JSON, default=dict)
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
    status = Column(String, default="queued")  # queued|processing|paused|done|failed|needs_input|dead
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

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    #: discovery | funding | application_prep (see auto_scheduler.CADENCE_SECONDS)
    workflow = Column(String(40), nullable=False)
    #: The cadence window this decision belongs to (epoch seconds // cadence).
    cycle_bucket = Column(Integer, nullable=False, default=0)
    triggered_at = Column(DateTime, default=utcnow, nullable=False)
    #: The queue item this decision enqueued (``null`` for a skip).
    queue_job_id = Column(Integer, nullable=True)
    #: The job an application-prep run targets; ``null`` for the other workflows.
    job_id = Column(Integer, nullable=True)
    #: queued|done|paused|failed|needs_input|skipped_outage|skipped_quota|skipped_no_consent
    state = Column(String(30), default="queued", nullable=False)
    reason = Column(Text, default="")
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=utcnow, nullable=False)


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
        UniqueConstraint("user_id", "name", name="uq_funding_user_name"),
        Index("ix_funding_user_seen", "user_id", "last_seen_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    stage = Column(String, default="Undisclosed")  # Seed, Series A-D, Undisclosed
    raised_at = Column(DateTime, default=utcnow)
    website = Column(String, default="")
    industry = Column(String, default="")
    summary = Column(Text, default="")
    keywords_matched = Column(JSON, default=list)
    source = Column(String, default="demo")  # sec_edgar | crunchbase | tracxn | imported | demo
    verified = Column(Boolean, default=False, nullable=False)
    #: First scan that surfaced this company (never rewritten).
    discovered_at = Column(DateTime, default=utcnow)
    #: Most recent scan that still returned it — the prune clock.
    last_seen_at = Column(DateTime, default=utcnow)
    meta = Column(JSON, default=dict)
    #: v2.2.5 linkage — does this user have ≥1 live job whose company
    #: normalizes to this name (via company_normalize.normalize_company_name).
    has_open_positions = Column(Boolean, default=False, nullable=False)


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

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    scanned_at = Column(DateTime, default=utcnow, nullable=False)
    status = Column(String(20), default="ok", nullable=False)  # ok | scan_failed
    provider_errors = Column(JSON, default=dict)
    events_seen = Column(Integer, default=0, nullable=False)
    companies_found = Column(Integer, default=0, nullable=False)
    meta = Column(JSON, default=dict)


class FundingScanCompany(Base):
    """Membership: which companies a particular funding scan returned."""

    __tablename__ = "funding_scan_companies"
    __table_args__ = (
        UniqueConstraint("scan_id", "company_id", name="uq_scan_company"),
        Index("ix_funding_scan_companies_scan", "scan_id"),
        Index("ix_funding_scan_companies_company", "company_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    scan_id = Column(Integer, ForeignKey("funding_scans.id", ondelete="CASCADE"), nullable=False)
    company_id = Column(Integer, ForeignKey("funding_companies.id", ondelete="CASCADE"), nullable=False)
    rank = Column(Integer, nullable=False)
    why = Column(Text, default="")


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

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plan = Column(String(20), default="free", nullable=False)  # free | pro | pro_plus
    status = Column(String(20), default="active", nullable=False)
    provider = Column(String(20), default="manual")  # manual | stripe | razorpay
    provider_customer_id = Column(String(200), default="")
    provider_subscription_id = Column(String(200), default="")
    current_period_start = Column(DateTime, default=utcnow)
    current_period_end = Column(DateTime, nullable=True)
    trial_end = Column(DateTime, nullable=True)
    cancel_at_period_end = Column(Boolean, default=False, nullable=False)
    grace_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class BillingEvent(Base):
    """Idempotency & audit for webhook events (stripe/razorpay)."""

    __tablename__ = "billing_events"
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_billing_provider_event"),
        Index("ix_billing_user_created", "user_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    provider = Column(String(20), nullable=False)  # stripe | razorpay | manual
    provider_event_id = Column(String(200), nullable=False)
    kind = Column(String(80), default="")  # e.g., invoice.paid, subscription.updated
    payload = Column(JSON, default=dict)
    processed = Column(Boolean, default=False, nullable=False)
    error = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow, nullable=False)


class AICreditLedger(Base):
    """
    Every AI operation that costs tokens/money.
    Used for per-user limits, cost ceilings, and billing.
    """

    __tablename__ = "ai_credit_ledger"
    __table_args__ = (Index("ix_ai_ledger_user_created", "user_id", "created_at"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    workflow = Column(String(40), nullable=False)  # parse, scoring, resume_gen, etc.
    model = Column(String(120), default="")
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    estimated_cost_usd = Column(Float, default=0.0)
    success = Column(Boolean, default=True, nullable=False)
    latency_ms = Column(Integer, default=0)
    error = Column(Text, default="")
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=utcnow, nullable=False)


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

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    period = Column(String(7), nullable=False)  # YYYY-MM
    capability = Column(String(40), nullable=False)  # jobs_discovered, ai_ops, resumes, etc.
    count = Column(Integer, default=0, nullable=False)
    limit = Column(Integer, default=0, nullable=False)  # snapshot of limit at time
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class Notification(Base):
    """In-app notifications for job alerts, automation failures, etc."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_read", "user_id", "read", "created_at"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    kind = Column(String(40), default="info")  # high_match, automation_failed, email_reply, etc.
    title = Column(String(200), default="")
    body = Column(Text, default="")
    link = Column(String(500), default="")
    read = Column(Boolean, default=False, nullable=False)
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class InterviewPrep(Base):
    """Interview preparation sessions grounded in resume + JD."""

    __tablename__ = "interview_preps"
    __table_args__ = (Index("ix_interview_user_job", "user_id", "job_id"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    job_title = Column(String(300), default="")
    company = Column(String(200), default="")
    questions = Column(JSON, default=list)  # [{q, category, difficulty}]
    answers = Column(JSON, default=dict)  # user answers
    feedback = Column(JSON, default=dict)  # AI feedback per answer
    status = Column(String(20), default="draft")  # draft | in_progress | completed
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class CompanyIntel(Base):
    """Cached company intelligence for deeper research."""

    __tablename__ = "company_intel"
    __table_args__ = (UniqueConstraint("user_id", "company", name="uq_intel_user_company"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    company = Column(String(200), nullable=False)
    website = Column(String(500), default="")
    industry = Column(String(200), default="")
    size = Column(String(50), default="unknown")
    funding_stage = Column(String(50), default="")
    tech_stack = Column(JSON, default=list)
    culture = Column(Text, default="")
    recent_news = Column(JSON, default=list)
    sources = Column(JSON, default=list)
    summary = Column(Text, default="")
    verified = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
