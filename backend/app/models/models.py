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
from sqlalchemy import (
    text as sa_text,
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


# --------------------------------------------------------------------------- #
# Candidate profile v2 — versioned document + field-level provenance/history
# --------------------------------------------------------------------------- #
# Contract 02-candidate-profile: versioned candidate_profiles document,
# profile_field_provenance (path, value_hash, preview, origin, sensitivity,
# confidence, band, evidence, extractor, ambiguity, review_status,
# review_required, needs_answer_for), profile_field_history append-only,
# persona overlays, state machine draft→review_required→active→superseded→archived.
#
# The legacy ``profiles`` table stays for backward compat; new code writes both
# until the migration is complete. The new tables are the source of truth for
# field-level confidence, evidence and correction audit.


class CandidateProfile(Base):
    """Versioned candidate document (contract 02)."""

    __tablename__ = "candidate_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", "persona_id", "version", name="uq_candidate_user_persona_version"),
        Index("ix_candidate_user_current", "user_id", "persona_id", "is_current"),
        Index("ix_candidate_user_state", "user_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    persona_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("personas.id"), nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)  # draft|review_required|active|superseded|archived
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    document_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    source_document_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("resume_documents.id"), nullable=True)
    source_extraction_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("resume_extractions.id"), nullable=True, index=True)
    # review: {required, resolved, deferred, completed_at}
    review: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    # completeness: {percent, required_filled, required_total, missing, uncertain, breakdown}
    completeness: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    extraction_source: Mapped[str] = mapped_column(String(20), default="ai", nullable=True)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    superseded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)


class ProfileFieldProvenance(Base):
    """Per-field provenance for a candidate profile (contract 02 § provenance)."""

    __tablename__ = "profile_field_provenance"
    __table_args__ = (
        UniqueConstraint("user_id", "profile_id", "path", name="uq_provenance_user_profile_path"),
        Index("ix_provenance_user_profile", "user_id", "profile_id"),
        Index("ix_provenance_review_required", "review_required"),
        Index("ix_provenance_path", "path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    profile_id: Mapped[int] = mapped_column(Integer, ForeignKey("candidate_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    path: Mapped[str] = mapped_column(String(300), nullable=False)  # RFC6901 JSON Pointer
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    value_preview: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # null when sensitivity>=sensitive
    origin: Mapped[str] = mapped_column(String(30), nullable=False)  # user|resume_extraction|ai_inferred|user_corrected|system
    sensitivity: Mapped[str] = mapped_column(String(20), default="internal", nullable=False)  # public|internal|sensitive|restricted
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # null for user origin
    confidence_band: Mapped[str] = mapped_column(String(10), default="none", nullable=False)  # high|medium|low|none
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)  # [{kind, quote, locator}]
    extractor: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)  # {name, version, model, prompt_version}
    source_document_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("resume_documents.id"), nullable=True)
    source_extraction_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("resume_extractions.id"), nullable=True)
    ambiguity: Mapped[str] = mapped_column(String(30), default="none", nullable=False)  # none|multiple_candidates|conflicting_sources|vague_text|missing_context
    review_status: Mapped[str] = mapped_column(String(24), default="needs_review", nullable=False)  # auto_accepted|needs_review|confirmed|corrected|rejected|deferred
    review_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reviewed_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # user id or null for system
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    needs_answer_for: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)


class ProfileFieldHistory(Base):
    """Append-only audit of field changes (contract 02 § history)."""

    __tablename__ = "profile_field_history"
    __table_args__ = (
        Index("ix_field_history_user_provenance", "user_id", "provenance_id"),
        Index("ix_field_history_path", "path"),
        Index("ix_field_history_occurred", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provenance_id: Mapped[int] = mapped_column(Integer, ForeignKey("profile_field_provenance.id", ondelete="CASCADE"), nullable=False, index=True)
    path: Mapped[str] = mapped_column(String(300), nullable=False)
    previous_value_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    previous_value_preview: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    new_value_preview: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    origin_before: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    origin_after: Mapped[str] = mapped_column(String(30), nullable=False)
    review_status_before: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    review_status_after: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(20), default="user", nullable=False)  # user|owner|system|ai
    actor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)  # UUID
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


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
        Index("ix_jobs_user_content_hash", "user_id", "content_hash"),
        Index("ix_jobs_user_last_seen", "user_id", "last_seen_at"),
        Index("ix_jobs_user_expired", "user_id", "expired"),
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
    #: First time this canonical job was seen for this user. Never rewritten
    #: (``discovered_at`` stays the insert-only clock; this is the same instant
    #: on a first insert and is left alone on a later merge).
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=utcnow, nullable=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=utcnow, nullable=True)
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expired: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expired_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    source_kind: Mapped[str] = mapped_column(String(16), default="", nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=True)
    title_normalized: Mapped[str] = mapped_column(String(300), default="", nullable=True)
    #: Compact original source payload. Stored next to the normalised row so
    #: ``extra`` (forms, autofill plan) never has to hold raw ATS JSON.
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)


class MatchResult(Base):
    """
    One multi-stage match verdict — this job and this candidate, as they both
    were at a moment in time, computed by a named, versioned scorer.

    See ``docs/MATCHING.md`` and ``docs/contracts/06-match-result.md``. A match
    is a *fact about a pair*, not a column on the job: the inputs (profile
    version, JD text, scorer version) are recorded on the row, which is what
    makes "why 78 on Monday and 61 now?" answerable and re-scoring cheap.

    Append-only apart from ``is_current`` / ``staleness`` / ``superseded_by_id``:
    a re-score inserts a new row and flips the previous current row, it never
    edits a score in place. ``rubric`` carries the deterministic feature
    contributions (stage 2) and ``ai_review`` the evidence review (stage 3) so
    the explanation — why recommended, what evidence, what gaps, what risks —
    is stored with the number, not reconstructed from memory.
    """

    __tablename__ = "match_results"
    __table_args__ = (
        UniqueConstraint("user_id", "job_id", "persona_id", "profile_sha256",
                         "job_description_sha256", "scorer_version",
                         name="uq_match_inputs"),
        Index("ix_match_user_job_current", "user_id", "job_id", "is_current"),
        Index("ix_match_user_persona_score", "user_id", "persona_id", "score"),
        Index("ix_match_user_staleness", "user_id", "staleness"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    persona_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("personas.id"), nullable=True)
    #: The profile *version* scored — ``candidate_profiles.id`` (legacy
    #: ``profiles.id`` when the v2 document does not exist yet).
    profile_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    profile_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    job_description_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    #: deterministic | ai | hybrid — which scorer produced the stored number.
    scorer: Mapped[str] = mapped_column(String(24), default="deterministic", nullable=False)
    #: semver of the scoring code + rubric contract; a bump is what makes old
    #: rows ``scorer_upgraded`` and future recalibration possible.
    scorer_version: Mapped[str] = mapped_column(String(24), default="1.0.0", nullable=False)
    #: provider model id for ``scorer in (ai, hybrid)``.
    model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    prompt_version: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)

    #: 0..100 — the estimated fit. Deliberately *not* an interview probability.
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    band: Mapped[str] = mapped_column(String(12), default="unknown", nullable=False)  # MATCH_BANDS
    #: 0..1 — how much we trust the score itself (``null`` for insufficient_data).
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    #: SCORE_SOURCES — the provenance label the UI must render.
    score_source: Mapped[str] = mapped_column(String(24), default="unscored", nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)

    #: Stage-1 report: hard filters, each with status/penalty/detail.
    hard_filters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    #: Stage-2 rubric: weights + per-feature contributions + evidence.
    rubric: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    #: ``[{name, required, evidence[], explicit|inferred}]``
    matched_skills: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    #: ``[{name, required, severity, remediable}]``
    missing_skills: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    #: Stage-3 AI evidence review (recommendation, strengths, gaps, risks,
    #: resume emphasis, application action) or ``null`` when the model never
    #: ran — a deterministic-only match is honest without it.
    ai_review: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    guardrail_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    #: {plan_gated, ai_unavailable, hard_filtered, duplicate, evidence_gap, …}
    flags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)

    #: Exactly one ``true`` per (user_id, job_id, persona_id).
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    staleness: Mapped[str] = mapped_column(String(20), default="fresh", nullable=False)  # MATCH_STALENESS
    pipeline_job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    superseded_by_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("match_results.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class MatchFeedback(Base):
    """
    One user correction / outcome signal against a recommendation.

    Kinds (``vocabulary.MATCH_FEEDBACK_KINDS``):

    * ``relevant`` / ``not_relevant`` — the user judges the recommendation
      itself. ``not_relevant`` is the "this recommendation is wrong" path:
      the reason is stored and the match read surfaces the override so the
      wrong call is visibly corrected, and the signal feeds recalibration.
    * ``applied`` / ``rejected`` / ``interview`` — post-application outcomes.
      They are the *only* data a future calibrated interview probability could
      ever be built from; until enough exist per scorer version the product
      must keep saying "estimated fit", never "X% chance of an interview"
      (see :func:`app.services.matching.calibration_status`).
    """

    __tablename__ = "match_feedback"
    __table_args__ = (
        Index("ix_match_feedback_user_job", "user_id", "job_id", "created_at"),
        Index("ix_match_feedback_user_kind", "user_id", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    match_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("match_results.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # MATCH_FEEDBACK_KINDS
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Correction detail: e.g. {"wrong_claim": "candidate does not know kafka"}.
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    scorer_version: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


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


class GlobalSetting(Base):
    """
    One platform-wide, owner-controlled setting (currently: feature flags).

    Unlike :class:`SettingsModel` — which is always tenant-scoped — these rows
    have no user: they are the workspace-level switches the owner console edits
    (``/admin``) and every user's request path consults. ``updated_by`` records
    the last owner who changed the row, so the audit trail can answer "who
    turned this off" without guessing.
    """

    __tablename__ = "global_settings"
    __table_args__ = (UniqueConstraint("key", name="uq_global_settings_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
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


class SearchCache(Base):
    """Shared, privacy-minimized query cache and cross-worker single-flight lease."""

    __tablename__ = "search_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    lease_until: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    owner: Mapped[str] = mapped_column(String(36), nullable=False)


class SearchBudget(Base):
    """Atomic fixed-window counters; no query or candidate text."""

    __tablename__ = "search_budgets"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)


class SearchUsage(Base):
    """Provider request accounting, deliberately separate from model tokens.

    Cost is a conservative estimate per attempted request, NOT a provider invoice.
    A worker killed mid-request leaves outcome=started for reconciliation.
    """

    __tablename__ = "search_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requests: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    estimated_cost_microusd: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), default="started", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, nullable=False)


# --------------------------------------------------------------------------- #
# Application packet — reviewable, versioned, grounded artifacts per job
# --------------------------------------------------------------------------- #
# Contract: one packet per (user, job, version). The packet is the *preparation*
# result: tailored resume, cover note, short answers, outreach draft, checklist,
# summary, evidence, emphasized facts, JD version, guardrail and token ledger.
# No auto-submit: status stays pending_approval until the user approves. The
# master profile is snapshotted at generation time and never mutated by
# tailoring. Versions are retained, old current is superseded but never deleted.
# Hallucination is blocked by FactLedger + schema checks before persistence.
# --------------------------------------------------------------------------- #


class ApplicationPacket(Base):
    """Versioned, approval-gated application packet (no auto-submit)."""

    __tablename__ = "application_packets"
    __table_args__ = (
        UniqueConstraint("user_id", "job_id", "version", name="uq_packet_user_job_version"),
        Index("ix_packet_user_job_current", "user_id", "job_id", "is_current"),
        Index("ix_packet_user_status", "user_id", "status"),
        Index("ix_packet_user_job", "user_id", "job_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    persona_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("personas.id"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # approval lifecycle: draft | pending_approval | approved | rejected | superseded | archived
    status: Mapped[str] = mapped_column(String(24), default="pending_approval", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # JD versioning: hash of the normalised JD text + snapshot
    jd_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    jd_text_snapshot: Mapped[str] = mapped_column(Text, default="", nullable=True)
    jd_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Master profile preserved separately (never mutated)
    master_profile_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    master_profile_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Tailored artifacts
    tailored_resume: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    cover_note: Mapped[str] = mapped_column(Text, default="", nullable=True)
    short_answers: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    outreach_draft: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    checklist: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=True)
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    emphasized_facts: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    # Guardrail + accounting
    guardrail_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    token_usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    # Derived display (denormalised for list views)
    job_title: Mapped[str] = mapped_column(String(300), default="", nullable=True)
    company: Mapped[str] = mapped_column(String(200), default="", nullable=True)
    # Audit
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    rejected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    superseded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)


class ApplicationPacketEvent(Base):
    """Append-only audit for a packet (state transitions, edits, approvals)."""

    __tablename__ = "application_packet_events"
    __table_args__ = (
        Index("ix_packet_events_packet_time", "packet_id", "occurred_at"),
        Index("ix_packet_events_user", "user_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    packet_id: Mapped[int] = mapped_column(Integer, ForeignKey("application_packets.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)  # generated | edited | approved | rejected | superseded | stale_detected
    from_status: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    to_status: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    actor_type: Mapped[str] = mapped_column(String(20), default="user", nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- #
# Browser-assisted application sessions (human-in-the-loop)
#
# One row per isolated browser run for one (user, job). The row is the state
# machine, the checkpoint, the (optional, encrypted) session persistence and the
# audit of what the automation did and did not do. What it never holds: a
# password, an MFA code, a CAPTCHA solution, or page content we did not need.
# --------------------------------------------------------------------------- #
class ApplicationSession(Base):
    """A browser-assisted application run with explicit human-in-the-loop pauses.

    Invariants (enforced in ``app.services.browser_session``, pinned by tests):

    * ``user_id`` scopes every read/write; a session is never resumed by, or
      merged into, another user's run.
    * ``awaiting_user`` is always paired with a pending
      :class:`ApplicationAction` — a pause nobody can see is a stall.
    * ``storage_state_enc`` is only ever written when the user opted into
      persistence, is encrypted with the *per-user* key, and is cleared on
      expiry/cancel/erasure.
    * ``checkpoint`` records which fields were filled (with a value
      fingerprint, never the value), so a resume cannot duplicate work and
      cannot silently re-type over the user's own input.
    * ``fill_values`` is a *working set*, not an archive: it holds only values a
      classifier verdict cleared for autofill and is purged when the session
      ends.
    """

    __tablename__ = "application_sessions"
    __table_args__ = (
        Index("ix_app_sessions_user_state", "user_id", "state"),
        Index("ix_app_sessions_user_job", "user_id", "job_id"),
        Index("ix_app_sessions_job_live", "job_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    #: ``APPLICATION_SESSION_STATES``.
    state: Mapped[str] = mapped_column(String(24), default="created", nullable=False)
    #: ``APPLICATION_SESSION_PHASE[state]`` — stored so a board never re-derives it.
    phase: Mapped[str] = mapped_column(String(16), default="new", nullable=False)
    #: Machine-readable why (never free text the UI has to parse).
    state_reason: Mapped[str] = mapped_column(String(60), default="", nullable=True)
    #: Which actor moved the session last (``ACTOR_TYPES``).
    actor_type: Mapped[str] = mapped_column(String(20), default="system_api", nullable=False)
    #: Set only when a resume was refused (``CHECKPOINT_FAILURES``).
    last_checkpoint_failure: Mapped[str] = mapped_column(String(40), default="", nullable=True)

    portal_type: Mapped[str] = mapped_column(String(30), default="custom", nullable=True)
    #: The host automation is bound to. A page on any other host is a mismatch.
    portal_domain: Mapped[str] = mapped_column(String(200), default="", nullable=True)
    #: Per-user isolation slot for the browser profile/context (never a shared dir).
    isolation_key: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    #: Opaque reference to the on-disk profile dir (a name, not a path: the
    #: service joins it under the artifact root, so a DB value can never escape).
    browser_profile_ref: Mapped[str] = mapped_column(String(80), default="", nullable=True)

    # Identity the session is bound to, as fingerprints/values that are not
    # personal data. A resume re-checks all of them (checkpoint validation).
    url_fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    expected_host: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    employer_fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: The posting/application identifier the portal itself gave us (a job's
    #: ``external_id`` when it has one, otherwise the canonical URL digest).
    application_identity: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    #: Safe structured progress: per-field status + timestamps + value
    #: fingerprints. Never a typed value (see ``FIELD_CHECKPOINT_KEYS``).
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    #: Counters the UI renders (detected/filled/skipped/awaiting/…), no content.
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    #: Privacy-minimized last observation (host, markers, field names/types).
    last_observation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    #: The working set of values the *classifier* cleared for autofill — profile
    #: or plan values, plus answers the user typed into the app for a named
    #: field. Never a credential, a one-time code, a CAPTCHA token, a restricted
    #: identifier or an unanswered legal/EEO question (no verdict reaches this
    #: set through those branches), and dropped as soon as the session ends.
    fill_values: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)

    #: Playwright ``storage_state`` — opt-in, per-user encrypted, never logged.
    storage_state_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    storage_state_saved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    storage_state_purged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: Cookie/state retention: a persisted state older than this is refused.
    storage_state_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    #: One-time handoff token (SHA-256 only — the raw token is returned once).
    handoff_token_hash: Mapped[str] = mapped_column(String(64), default="", nullable=True)
    handoff_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    handoff_action_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    #: Permitted screenshots only: [{action, path, sha256, captured_at, retention_days}].
    screenshots: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    #: Why the last pause happened (a ``USER_ACTION_KINDS`` value or "").
    pause_kind: Mapped[str] = mapped_column(String(30), default="", nullable=True)
    pause_reason: Mapped[str] = mapped_column(String(80), default="", nullable=True)

    resumed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    #: Hard TTL. An expired session needs re-authentication — it is never extended
    #: silently to keep an automation alive.
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)


class ApplicationAction(Base):
    """An actionable queue item: something only the human may do.

    ``kind ∈ USER_ACTION_KINDS``. For ``login``/``mfa``/``captcha`` the value is
    entered by the user in the browser — this row carries instructions, a
    one-time handoff reference and an expiry, never a secret. For
    ``unknown_field``/``ambiguous_field``/``sensitive_field``/``legal_question``
    it carries the field metadata (label, type, options, why we stopped) so the
    user can answer in the app.
    """

    __tablename__ = "application_actions"
    __table_args__ = (
        UniqueConstraint("user_id", "dedupe_key", name="uq_application_actions_dedupe"),
        Index("ix_app_actions_user_status", "user_id", "status"),
        Index("ix_app_actions_session", "session_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    session_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("application_sessions.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    #: Machine-readable reason (e.g. ``captcha_detected``, ``unknown_field``).
    reason: Mapped[str] = mapped_column(String(60), default="", nullable=True)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=True)
    #: User-facing instructions. A builder guarantees no secret is ever rendered here.
    instructions: Mapped[str] = mapped_column(Text, default="", nullable=True)
    #: Field metadata for answer-type asks; values are never stored here.
    fields: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    #: {url, host, expires_at, requires, never: [...]} — no credentials, no codes.
    handoff: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    #: Stable key so the same blocker cannot create duplicate queue items.
    dedupe_key: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    #: How many times this blocker has been observed (a pause that repeats is a signal).
    occurrences: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ApplicationSubmission(Base):
    """The at-most-once submission ledger.

    A submission is reserved *before* anything is clicked, keyed by
    ``(user_id, idempotency_key)``, so a retry, a double click, a replayed
    request or a resumed session can never send the same application twice. The
    partial unique index on ``(user_id, job_id)`` for a live
    (``reserved``/``submitted``/``verified``) row is the database-level
    backstop; the service also checks inside the writing transaction, which is
    what carries on SQLite.
    """

    __tablename__ = "application_submissions"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_application_submissions_key"),
        Index("ix_application_submissions_user_job", "user_id", "job_id"),
        Index(
            "uq_application_submissions_live_job",
            "user_id", "job_id",
            unique=True,
            sqlite_where=sa_text("state IN ('reserved', 'submitted', 'verified')"),
            postgresql_where=sa_text("state IN ('reserved', 'submitted', 'verified')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    session_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("application_sessions.id"), nullable=True)
    #: ``SUBMISSION_STATES``.
    state: Mapped[str] = mapped_column(String(16), default="reserved", nullable=False)
    #: ``SUBMISSION_CHANNELS``.
    channel: Mapped[str] = mapped_column(String(20), default="assisted_dry_run", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Why it was refused/abandoned (``SUBMISSION_REFUSALS``), "" when it went out.
    refusal_reason: Mapped[str] = mapped_column(String(40), default="", nullable=True)
    #: The portal's own receipt: {kind, external_id, observed_at, locator}.
    receipt: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)

    # --- automation-policy provenance (contracts/10 §5 rule 3) ------------- #
    #: The ``automation_policies`` row this submission was decided under, plus
    #: the version and the consent snapshot in force *at decision time* — the
    #: three columns that make an automatic submission reconstructible.
    policy_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    policy_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    consent_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)

    reserved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)


# --------------------------------------------------------------------------- #
# Application tracking — the lightweight outcome layer (contracts/07 §11)
#
# One ``application_tracking`` row per ``(user_id, job_id)``: the *current*
# outcome state plus the snapshots that make a later report honest (which source
# the job came from, which match score we believed at the time, which artefact
# version was attached). One ``application_tracking_events`` row per thing that
# happened — append-only, so a status change never overwrites the history that
# explains it, and a manual correction is a *new* event that names the one it
# corrects.
#
# Deliberately not a CRM: no contacts table, no email threads, no per-company
# pipelines, no activity logging. It answers four questions — what happened, who
# says so, what did we send, and what is due next — and stops there.
# --------------------------------------------------------------------------- #
class ApplicationTracking(Base):
    """The outcome record for one ``(user, job)`` application.

    Invariants (enforced in :mod:`app.services.application_tracking`, pinned by
    ``backend/tests/test_application_tracking.py``):

    * ``state`` is a **projection** of the append-only timeline, never the
      source of truth: every write goes through the service, which validates the
      transition and appends the event in the same transaction.
    * ``state_origin`` says who asserted the current state — a system
      observation and the user's memory are different claims, and the report
      keeps them apart (``TRACKING_ORIGINS``).
    * ``is_provisional`` is set only by an ``email_inferred`` suggestion, which
      may never be an interview: the user confirms or dismisses it
      (``TRACKING_TRUSTED_ONLY_STATES``).
    * The snapshot columns (``source``, ``match_*``, ``artifact_*``,
      ``role_family``) are frozen when tracking opens. Refreshing one writes an
      ``application.snapshot_recorded`` / ``application.attribution_updated``
      event, so "which resume version got the interview?" stays answerable after
      the artefact is regenerated or deleted — which is why ``artifact_id`` and
      ``match_id`` are plain integers, **not** FKs (the
      ``automation_policy_revisions.policy_id`` pattern).
    """

    __tablename__ = "application_tracking"
    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="uq_application_tracking_user_job"),
        Index("ix_app_tracking_user_state", "user_id", "state"),
        Index("ix_app_tracking_user_source", "user_id", "source"),
        Index("ix_app_tracking_user_applied", "user_id", "applied_at"),
        Index("ix_app_tracking_user_follow_up", "user_id", "follow_up_at"),
        Index("ix_app_tracking_user_artifact", "user_id", "artifact_kind", "artifact_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    persona_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("personas.id"), nullable=True)

    #: ``APPLICATION_TRACKING_STATES`` — the current outcome state.
    state: Mapped[str] = mapped_column(String(28), default="not_applied", nullable=False)
    #: ``TRACKING_STATE_PHASE[state]`` — stored so a board/report groups without a map lookup.
    phase: Mapped[str] = mapped_column(String(20), default="pre_application", nullable=False)
    #: ``TRACKING_ORIGINS`` — who asserted the current state.
    state_origin: Mapped[str] = mapped_column(String(24), default="user_reported", nullable=False)
    #: ``ACTOR_TYPES`` — the actor behind that assertion (never null, never "unknown").
    state_actor_type: Mapped[str] = mapped_column(String(20), default="user", nullable=False)
    #: True only for an ``email_inferred`` suggestion awaiting the user's verdict.
    is_provisional: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: ``Provenance`` for a provisional state (contracts/01 §6).
    provisional_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)

    # --- attribution & snapshots (frozen; a refresh is an event) ----------- #
    #: Where the job/application came from: the job's own board id, or a
    #: ``TRACKING_MANUAL_SOURCES`` value the user attributed.
    source: Mapped[str] = mapped_column(String(60), default="unknown", nullable=False)
    #: ``SUBMISSION_CHANNELS`` — how the application went out ("" until it did).
    channel: Mapped[str] = mapped_column(String(20), default="", nullable=True)
    #: Normalised role family for reporting, from
    #: :func:`app.services.role_family.role_family` — the same function the
    #: analytics page uses, so "Backend Engineer" means one thing everywhere.
    role_family: Mapped[str] = mapped_column(String(80), default="", nullable=True)
    #: The job title/company as they were when tracking opened (a later rename of
    #: the job row must not rewrite the history the report groups by).
    job_title_snapshot: Mapped[str] = mapped_column(String(300), default="", nullable=True)
    company_snapshot: Mapped[str] = mapped_column(String(200), default="", nullable=True)
    #: Match-score snapshot: the number, its band, the ``match_results`` row it
    #: came from and the scorer version — so a rubric change cannot silently
    #: re-grade last month's conversion rate.
    match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    match_band: Mapped[str] = mapped_column(String(12), default="", nullable=True)
    match_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    score_source: Mapped[str] = mapped_column(String(24), default="", nullable=True)
    scorer_version: Mapped[str] = mapped_column(String(24), default="", nullable=True)
    #: Artefact snapshot: what was attached (``TRACKING_ARTIFACT_KINDS``).
    artifact_kind: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    artifact_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    artifact_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    artifact_label: Mapped[str] = mapped_column(String(200), default="", nullable=True)
    artifact_sha256: Mapped[str] = mapped_column(String(64), default="", nullable=True)

    # --- business timestamps (never inferred from ``updated_at``) ---------- #
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    first_response_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    interview_scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: When the interview happens/happened (the calendar slot the user typed).
    interview_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    interview_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    outcome_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # --- the follow-up promise ------------------------------------------- #
    #: When the user wants to be reminded. ``NULL`` = nothing is due.
    follow_up_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    follow_up_note: Mapped[str] = mapped_column(String(400), default="", nullable=True)
    #: Set when the reminder notification was written — the dedupe marker that
    #: makes a repeated sweep (or a restart) notify once, not once per pass.
    follow_up_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    follow_up_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    #: The user's latest note, denormalised for list views. The prose history is
    #: in the events; this is the one a card renders without a second read.
    note: Mapped[str] = mapped_column(Text, default="", nullable=True)
    #: Timeline bookkeeping: ``sequence`` of the last event, and how many rows
    #: were corrections (a report can show how much of its input was revised).
    last_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    correction_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Idempotency keys already applied to this record, so a replayed request is
    #: a no-op instead of a second event (``{key: event_id}``, bounded).
    applied_idempotency_keys: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)


class ApplicationTrackingEvent(Base):
    """One append-only row of an application's timeline.

    The lightweight sibling of ``application_events`` (contracts/09): the same
    rules — a gapless per-record ``sequence``, a mandatory actor, a typed
    payload, ``occurred_at`` vs ``recorded_at``, an ``event_id`` a retry can be
    deduped on — without the submission-side columns (plans, checkpoints,
    credentials) this layer has no use for.

    **Append-only.** Nothing updates or deletes a row except account erasure. A
    correction is a new row with ``is_correction = true`` and
    ``correction_of_event_id`` pointing at the row it revises; the wrong row
    stays, because "we thought X, the user corrected it to Y" *is* the history.
    """

    __tablename__ = "application_tracking_events"
    __table_args__ = (
        UniqueConstraint("user_id", "tracking_id", "sequence", name="uq_app_tracking_event_sequence"),
        UniqueConstraint("user_id", "event_id", name="uq_app_tracking_event_uuid"),
        # NULLs are distinct in a unique index on both SQLite and PostgreSQL, so
        # only rows that *asked* to be deduped collide — which is the point.
        UniqueConstraint("user_id", "dedupe_key", name="uq_app_tracking_event_dedupe"),
        Index("ix_app_tracking_events_user_type", "user_id", "event_type", "occurred_at"),
        Index("ix_app_tracking_events_job_time", "user_id", "job_id", "occurred_at"),
        Index("ix_app_tracking_events_follow_up", "user_id", "follow_up_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    #: UUIDv4, so a writer in another process can dedupe on it (contracts/09 §4).
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    tracking_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("application_tracking.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    #: 1-based, gapless per ``(user_id, tracking_id)``; ordering never depends on
    #: ``occurred_at``, which has second granularity.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    #: ``EVENT_TYPES`` — ``application.<past_tense_verb>``.
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    state_from: Mapped[Optional[str]] = mapped_column(String(28), nullable=True)
    #: ``NULL`` for a non-transition event (a note, a follow-up, a snapshot).
    state_to: Mapped[Optional[str]] = mapped_column(String(28), nullable=True)
    phase: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    #: ``TRACKING_ORIGINS`` — system-observed vs user-reported vs inferred.
    origin: Mapped[str] = mapped_column(String(24), default="user_reported", nullable=False)
    #: ``ACTOR_TYPES`` — never null, never "unknown" (contracts/09 §7).
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    actor_label: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    #: ``QUEUE_TRIGGERS`` — a click, a schedule, a recovery?
    trigger: Mapped[str] = mapped_column(String(16), default="user", nullable=False)

    #: True when the user revised a previously recorded fact.
    is_correction: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    correction_of_event_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    correction_reason: Mapped[str] = mapped_column(String(400), default="", nullable=True)
    #: An ``email_inferred`` suggestion the user has not confirmed.
    is_provisional: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #: ``NOTIFICATION_SEVERITIES`` — drives the chip colour.
    severity: Mapped[str] = mapped_column(String(20), default="info", nullable=False)
    #: One human sentence, safe to render as-is (no ids the user cannot see).
    message: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    #: The user's own prose. A note is never a field answer and is never fed to
    #: automation (contracts/09 §3 rule 1).
    note: Mapped[str] = mapped_column(Text, default="", nullable=True)
    #: Typed per event type (contracts/09 §3); immutable once written.
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    #: ``[{kind, locator, captured_at}]`` — pointers, never content.
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)

    #: The follow-up date this event promised (mirrored onto the record).
    follow_up_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: Stable key so the same fact cannot be recorded twice (a double click, a
    #: replayed webhook, a re-run sweep). ``NULL`` when the event is unique by
    #: nature (a note).
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    request_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    #: When it happened (the user's typed date for a backfilled interview).
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    #: When we stored it. ``recorded_at > occurred_at`` is meaningful: it means
    #: the fact was reported after the event, which the timeline shows.
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------- #
# Automation policy — the persisted, versioned "what may run for this user"
#
# ``docs/contracts/10-automation-policy.md`` §2. One row per
# ``(user_id, scope, scope_key, workflow)``; every account owns exactly one
# ``global``/``*`` row per workflow (created lazily here, with the plan's
# defaults) and narrower rows override it. Every change writes one append-only
# ``AutomationPolicyRevision`` so "who turned auto-submit on, and when?" is
# answerable without reading audit prose — the revision freezes the limits, the
# submission row freezes the consent snapshot.
# --------------------------------------------------------------------------- #
class AutomationPolicy(Base):
    __tablename__ = "automation_policies"
    __table_args__ = (
        UniqueConstraint("user_id", "scope", "scope_key", "workflow",
                         name="uq_automation_policy_identity"),
        Index("ix_automation_policy_owner_workflow", "user_id", "workflow", "enabled"),
        Index("ix_automation_policy_resolution", "user_id", "workflow", "scope", "scope_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    #: ``AUTOMATION_SCOPES``: global | persona | workflow | company | portal.
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="global")
    #: The persona id, workflow name, ``company_name_normalized`` or portal
    #: domain; ``*`` for the ``global`` row.
    scope_key: Mapped[str] = mapped_column(String(200), nullable=False, default="*")
    #: ``AUTOMATION_WORKFLOWS``.
    workflow: Mapped[str] = mapped_column(String(28), nullable=False)

    #: Master switch for this row.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: ``AUTOMATION_MODES``: off | suggest | prepare | auto_submit.
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="off")
    #: null = inherited plan default, not an explicit choice
    #: (the onboarding gate needs the difference, contracts/10 §2).
    mode_chosen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # --- limits & gates ------------------------------------------------------ #
    min_match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_runs_per_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_runs_per_month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    require_review_before_submit: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    require_resume_approval: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # --- source/company/location constraints --------------------------------- #
    allowed_portals: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    blocked_portals: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    allowed_companies: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    blocked_companies: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    blocked_keywords: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    allowed_locations: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    require_remote: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    require_onsite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    compensation_floor: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)

    # --- field-answer policy ------------------------------------------------- #
    sensitive_field_policy: Mapped[str] = mapped_column(String(20), nullable=False, default="ask_every_time")
    eeo_policy: Mapped[str] = mapped_column(String(20), nullable=False, default="prefer_decline")
    credential_policy: Mapped[str] = mapped_column(String(20), nullable=False, default="create_on_demand")

    # --- schedule & notifications -------------------------------------------- #
    schedule: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    notify: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)

    # --- consent binding ------------------------------------------------------ #
    consents_required: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    consent_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=True)
    disclosure_version: Mapped[str] = mapped_column(String(24), default="", nullable=False)

    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # --- audit --------------------------------------------------------------- #
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(20), default="system_api", nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)


class AutomationPolicyRevision(Base):
    """Append-only history of one policy row's changes (contracts/10 §2.1)."""

    __tablename__ = "automation_policy_revisions"
    __table_args__ = (
        UniqueConstraint("user_id", "policy_id", "version", name="uq_automation_revision_version"),
        Index("ix_automation_revision_owner_policy", "user_id", "policy_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    #: Not an FK on purpose: the revision must outlive a deleted (scoped) row,
    #: because every past submission still resolves against it.
    policy_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The whole row as it stood after the change.
    document: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    changed_keys: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=True)
    actor_type: Mapped[str] = mapped_column(String(20), default="system_api", nullable=False)
    actor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(String(200), default="", nullable=True)
    request_id: Mapped[str] = mapped_column(String(64), default="", nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class AutomationSubmissionCounter(Base):
    """Atomic daily window of auto-submissions (the daily-limit backstop).

    Kept separate from ``application_submissions`` so the daily check is a
    precise, atomic ``SELECT … FOR UPDATE``/insert on a tiny table; the ledger
    itself stays the complete audit record. ``period`` is the UTC day
    (``YYYY-MM-DD``); ``rejected`` counts policy/inspection refusals, which are
    recorded but never charged toward the run limits.
    """

    __tablename__ = "automation_submission_counters"
    __table_args__ = (
        UniqueConstraint("user_id", "period", name="uq_automation_counter_user_period"),
        Index("ix_automation_counter_owner_period", "user_id", "period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    workflow: Mapped[str] = mapped_column(String(28), nullable=False, default="application_submit")
    period: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD (UTC)
    record: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # submissions acted on
    rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # refusals recorded (unmetered)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
