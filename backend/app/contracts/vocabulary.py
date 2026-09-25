"""Canonical vocabulary for the JobHunter domain contracts.

Every string here is a *contract*: it appears in ``docs/contracts/``, in the
database (as a stored status/kind), in API payloads and in the SPA. Nothing in
this module computes, queries or renders — it only names things, plus three
pure helpers whose rules the docs state in prose.

Why this file exists
--------------------
The application state machine drifted once already: ``models.Job.status``
documented ``applying``/``emailed``/``rejected``, ``GET /api/analytics/funnel``
bucketed on those three names, the Jobs page offered ``applying`` as a filter —
and the code that actually writes statuses writes ``preparing``,
``ready_to_apply`` and ``skipped``. Every one of those views was therefore
silently empty or silently incomplete, and nothing could fail: each file was
*self-consistent*. A single importable list makes that class of bug a test
failure instead of a wrong number on a dashboard.

Changing this file
------------------
1. Additive first. A new state/kind is a new tuple entry plus a docs row.
2. Removing or renaming a stored value is a **migration**, not an edit: the
   value is in rows, in ``job_events`` and in the SPA's caches. See
   ``docs/contracts/14-migration-strategy.md`` (expand → migrate → contract).
3. Update ``docs/contracts/`` in the same commit — ``test_contracts_vocabulary``
   fails when a name exists here and not in the docs (or in the TS mirror).
4. Bump :data:`CONTRACT_VERSION` (minor for additions, major for renames).
"""
from __future__ import annotations

from typing import Dict, Tuple

#: Version of the contract set itself. Surfaced in docs and (proposed) in
#: ``GET /api/meta`` so a client can detect a server that speaks a newer
#: vocabulary than it does. Minor = additive, major = a rename/removal.
CONTRACT_VERSION = "1.5.0"

#: Header a client sends to make a state-changing POST safely retryable.
#: See ``docs/contracts/01-conventions.md`` §Idempotency.
IDEMPOTENCY_HEADER = "Idempotency-Key"


# --------------------------------------------------------------------------- #
# Actors — who is allowed to initiate a transition
# --------------------------------------------------------------------------- #
#: ``actor_type`` on every event row. A transition with no actor is a bug: the
#: state-machine tables in ``docs/contracts/`` have an Actor column for exactly
#: this reason.
ACTOR_TYPES: Tuple[str, ...] = (
    "user",            # the tenant, through the SPA or the API
    "owner",           # operator/admin role (cross-tenant ops endpoints only)
    "system_worker",   # a pipeline worker executing a claimed queue item
    "system_scheduler",# the auto-mode scheduler
    "system_watchdog", # the AI availability watchdog (pause/drain)
    "system_api",      # the API process outside a request (startup recovery)
    "external_source", # a job board / adapter
    "external_portal", # an ATS or employer portal
    "external_ai",     # the model provider
    "external_smtp",   # the mail transport
    "external_provider",  # a billing/funding/contact provider or webhook
)


# --------------------------------------------------------------------------- #
# Provenance, confidence, evidence, review
# --------------------------------------------------------------------------- #
#: ``provenance.source`` — where a value came from. Mandatory on every
#: AI-extracted or third-party-supplied field.
PROVENANCE_SOURCES: Tuple[str, ...] = (
    "resume_extraction",   # parsed out of an uploaded document by the model
    "document_text",       # deterministic text/layout extraction (no model)
    "user_provided",       # the user typed it
    "user_confirmed",      # the user reviewed an extracted value and accepted it
    "user_corrected",      # the user reviewed an extracted value and changed it
    "ai_inferred",         # the model derived it (not quoted from a document)
    "portal_html",         # read off a live application form
    "ats_api",             # an ATS's own API
    "provider_api",        # a paid/third-party provider (funding, contacts…)
    "heuristic",           # deterministic guess, always low confidence
    "imported",            # bulk import / migration backfill
    "derived",             # computed by us from other stored values
    "email_inferred",      # inferred from an inbound recruiter message
)

#: ``provenance.evidence[*].kind`` — what the pointer points at. An evidence
#: entry without a resolvable locator is not evidence (see
#: ``docs/contracts/01-conventions.md`` §6 and the quote-resolves test in
#: ``docs/contracts/03-resume-and-extraction.md``).
EVIDENCE_KINDS: Tuple[str, ...] = (
    "text_span",       # a quoted substring of a stored document
    "url",             # a fetched page / posting
    "document",        # a whole stored artefact (resume, cover letter)
    "provider_record", # a row from a funding/contact/ATS provider
    "user_statement",  # the user typed or confirmed it
    "portal_response", # the portal's own HTTP response / confirmation page
    "email_message",   # an inbound or outbound message
)


#: ``provenance.confidence_band`` — the label the UI shows. Numeric thresholds
#: live in :data:`CONFIDENCE_BAND_THRESHOLDS`; a writer must never pick a band
#: by hand.
CONFIDENCE_BANDS: Tuple[str, ...] = ("high", "medium", "low", "none")

#: Lower bound (inclusive) of each band. ``none`` is the absence of a score.
CONFIDENCE_BAND_THRESHOLDS: Dict[str, float] = {"high": 0.85, "medium": 0.60, "low": 0.0}

#: ``review.status`` on a provenance-bearing field.
REVIEW_STATUSES: Tuple[str, ...] = (
    "unreviewed",     # written, nobody has looked at it
    "needs_review",   # blocked: low confidence and/or sensitive — see requires_review()
    "auto_accepted",  # non-sensitive + high confidence, accepted without a human
    "confirmed",      # a human accepted it
    "corrected",      # a human replaced the value (the original stays in history)
    "rejected",       # a human said "this is not mine"
    "deferred",       # the user postponed it; non-blocking gates only
)

#: ``sensitivity`` — how a value may be stored, shown, logged and auto-filled.
#: Ordered least → most restrictive. See ``docs/contracts/01-conventions.md``.
SENSITIVITY_LEVELS: Tuple[str, ...] = ("public", "internal", "sensitive", "restricted")

#: Canonical profile/application field keys that are *sensitive*: real personal
#: data, but usable by automation once the user has confirmed them.
SENSITIVE_FIELD_KEYS: Tuple[str, ...] = (
    "email",
    "phone",
    "location",
    "address",
    "dateOfBirth",
    "salaryExpectation",
    "currentCompensation",
    "noticePeriod",
    "availability",
    "workAuthorization",
    "sponsorshipRequired",
    "relocationWilling",
    "linkedin",
    "github",
)

#: Field keys that automation may **never** infer, auto-fill from a guess, or
#: write to a log/audit detail. Answers come only from an explicit user choice.
RESTRICTED_FIELD_KEYS: Tuple[str, ...] = (
    "governmentId",
    "nationalId",
    "passportNumber",
    "visaNumber",
    "portalPassword",
    "otpCode",
    "dateOfBirth",
    "gender",
    "race",
    "ethnicity",
    "veteranStatus",
    "disabilityStatus",
    "referenceContact",
)

#: Voluntary self-identification (EEO) questions. The only legal default is
#: "decline to answer"; a value is never inferred from a name, a photo or a
#: resume, and the answer is never reused across applications without an
#: explicit per-answer ``reuse_scope``.
EEO_FIELD_KEYS: Tuple[str, ...] = ("gender", "race", "ethnicity", "veteranStatus", "disabilityStatus")


def confidence_band(confidence: float | None) -> str:
    """The band label for a 0..1 confidence (``none`` when there is no score)."""
    if confidence is None:
        return "none"
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return "none"
    if value != value:  # NaN
        return "none"
    if value >= CONFIDENCE_BAND_THRESHOLDS["high"]:
        return "high"
    if value >= CONFIDENCE_BAND_THRESHOLDS["medium"]:
        return "medium"
    if value >= CONFIDENCE_BAND_THRESHOLDS["low"]:
        return "low"
    return "none"


def requires_review(sensitivity: str, band: str) -> bool:
    """Must a human look at this value before automation may use it?

    The rule the docs state: **sensitive or restricted data always needs a
    human, and so does anything the model is not confident about.** Everything
    else may be auto-accepted so onboarding is not a wall of confirmations.
    """
    if sensitivity in ("sensitive", "restricted"):
        return True
    return band in ("low", "none")


# --------------------------------------------------------------------------- #
# Candidate profile (deliverable 1)
# --------------------------------------------------------------------------- #
#: ``candidate_profiles.state``. The profile is versioned: a new version is
#: inserted, never updated in place, and exactly one row per
#: ``(user_id, persona_id)`` is ``is_current``.
#: See ``docs/contracts/02-candidate-profile.md`` §5.
PROFILE_STATES: Tuple[str, ...] = (
    "draft",             # written by extraction, nobody has looked at it
    "review_required",   # ≥1 field needs a human before automation may use it
    "active",            # current, usable
    "superseded",        # a newer version took over; kept to explain the past
    "archived",          # user-deleted or erasure-tombstoned; terminal
)

#: ``candidate_profiles.extraction_source`` — a **frozen legacy** vocabulary:
#: shipped rows hold these values and they are never rewritten. New code writes
#: ``provenance.source`` (see :data:`PROVENANCE_SOURCES`) instead.
LEGACY_PROFILE_EXTRACTION_SOURCES: Tuple[str, ...] = ("ai", "user_edited", "manual", "imported")

#: ``document.contact.links[*].kind``.
PROFILE_LINK_KINDS: Tuple[str, ...] = ("linkedin", "github", "portfolio", "website", "other")

#: The actions a human may take on a field that needs review
#: (``POST /api/profile/review/resolve`` and the per-field answer endpoint).
#: ``defer`` is refused for ``restricted`` sensitivity.
REVIEW_ACTIONS: Tuple[str, ...] = ("confirm", "correct", "reject", "defer")


# --------------------------------------------------------------------------- #
# Onboarding (deliverable 3)
# --------------------------------------------------------------------------- #
#: ``onboarding_sessions.state``. The state is *derived* from the gate
#: checklist server-side (``docs/contracts/04-onboarding-state-machine.md``):
#: it is the first incomplete required gate, so it can always be recomputed
#: after a crash, a restart or a second device — never stored only in a browser.
ONBOARDING_STATES: Tuple[str, ...] = (
    "not_started",
    "account_created",
    "consents_required",
    "awaiting_resume",
    "resume_processing",
    "extraction_blocked",
    "profile_review_required",
    "preferences_required",
    "discovery_setup",
    "automation_policy_optional",
    "ready",
    "abandoned",
)

#: ``ready`` is the success terminal; ``abandoned`` is the explicit-exit
#: terminal (reversible only by a user ``resume`` transition).
ONBOARDING_TERMINAL_STATES: Tuple[str, ...] = ("ready", "abandoned")

#: The gates the state is derived from, in order. ``required=False`` gates can
#: be completed with ``skipped`` by the user and never block ``ready``.
ONBOARDING_GATES: Tuple[Dict[str, object], ...] = (
    {"key": "account", "label": "Account created", "required": True, "blocking": True},
    {"key": "consents", "label": "Terms & data processing accepted", "required": True, "blocking": True},
    {"key": "resume", "label": "Master resume uploaded & extracted", "required": True, "blocking": True},
    {"key": "profile_review", "label": "Extracted profile reviewed", "required": True, "blocking": True},
    {"key": "preferences", "label": "Search & compensation preferences", "required": True, "blocking": True},
    {"key": "discovery_setup", "label": "Job sources chosen", "required": False, "blocking": False},
    {"key": "automation_policy", "label": "Automation mode chosen", "required": False, "blocking": False},
)

#: ``onboarding_sessions.checklist[*].status`` (per gate).
ONBOARDING_GATE_STATES: Tuple[str, ...] = (
    "pending", "in_progress", "blocked", "completed", "skipped",
)


# --------------------------------------------------------------------------- #
# Resume documents & extraction (deliverable 2)
# --------------------------------------------------------------------------- #
#: ``resume_documents.role`` — superset of today's ``resumes.type``.
RESUME_ROLES: Tuple[str, ...] = ("master", "tailored", "polished", "cover_letter", "attachment")

#: ``resume_documents.state``.
RESUME_STATES: Tuple[str, ...] = (
    "uploading",      # bytes accepted, row not committed yet
    "stored",         # file on disk + row committed, no extraction yet
    "extracting",     # a queued extraction is running
    "pending_approval",  # tailored: fact guard passed, awaiting the user
    "approved",
    "rejected",       # guardrail/fact-guard rejection, or the user said no
    "archived",
    "deleted",        # tombstone: the row survives erasure of the file
)

#: ``resume_extractions.state`` — one row per attempt, append-only.
EXTRACTION_STATES: Tuple[str, ...] = (
    "pending", "running", "succeeded", "failed", "rejected", "superseded",
)


# --------------------------------------------------------------------------- #
# Application lifecycle (deliverable 6)
# --------------------------------------------------------------------------- #
#: Phases group states for the UI and for the "may this transition happen?"
#: checks. Phase order is informational, not a constraint.
APPLICATION_PHASES: Tuple[str, ...] = (
    "intake", "preparation", "blocked", "review", "submission", "tracking", "closed",
)

#: ``applications.state`` — the canonical business lifecycle.
APPLICATION_STATES: Tuple[str, ...] = (
    # intake
    "not_started",
    "queued",
    "preparing",
    # blocked (each one names what is missing and who can unblock it)
    "blocked_input",
    "blocked_resume_approval",
    "blocked_consent",
    "blocked_credential",
    "blocked_policy",
    "blocked_quota",
    # review
    "prepared",
    "awaiting_approval",
    # submission
    "submitting",
    "submitted",
    "submitted_unverified",
    # tracking (post-submission outcomes)
    "interview_scheduled",
    "rejected_by_employer",
    "offer_received",
    # closed
    "failed",
    "withdrawn",
    "expired",
    "skipped",
    "cancelled",
)

#: State → phase. Complete by construction; ``test_contracts_vocabulary`` fails
#: if a state is added to :data:`APPLICATION_STATES` without a phase.
APPLICATION_STATE_PHASE: Dict[str, str] = {
    "not_started": "intake",
    "queued": "intake",
    "preparing": "preparation",
    "blocked_input": "blocked",
    "blocked_resume_approval": "blocked",
    "blocked_consent": "blocked",
    "blocked_credential": "blocked",
    "blocked_policy": "blocked",
    "blocked_quota": "blocked",
    "prepared": "review",
    "awaiting_approval": "review",
    "submitting": "submission",
    "submitted": "submission",
    "submitted_unverified": "submission",
    "interview_scheduled": "tracking",
    "rejected_by_employer": "tracking",
    "offer_received": "tracking",
    "failed": "closed",
    "withdrawn": "closed",
    "expired": "closed",
    "skipped": "closed",
    "cancelled": "closed",
}

#: No transition out of these (a retry creates a *new* attempt row instead).
APPLICATION_TERMINAL_STATES: Tuple[str, ...] = (
    "rejected_by_employer",
    "offer_received",
    "withdrawn",
    "expired",
    "skipped",
    "cancelled",
)

#: States in which the user owes us something — what
#: ``GET /api/actions/required`` returns and what the SPA's badge counts.
APPLICATION_USER_ACTION_STATES: Tuple[str, ...] = (
    "blocked_input",
    "blocked_resume_approval",
    "blocked_consent",
    "blocked_credential",
    "awaiting_approval",
)

#: ``applications.blocked_reason`` codes (never free text).
APPLICATION_BLOCKED_CODES: Tuple[str, ...] = (
    "missing_required_field",
    "ambiguous_field",
    "restricted_field_needs_choice",
    "resume_pending_approval",
    "resume_fact_guard_failed",
    "consent_automation_missing",
    "consent_outreach_missing",
    "credential_missing",
    "credential_unreadable",
    "portal_domain_not_allowed",
    "policy_company_blocked",
    "policy_score_below_floor",
    "policy_daily_limit",
    "quota_exhausted",
    "autofill_unavailable",
    "assisted_apply_unavailable",
    "portal_unreachable",
)

#: ``applications.submission_channel`` — how the application actually went out.
SUBMISSION_CHANNELS: Tuple[str, ...] = ("automation", "manual_user", "assisted_dry_run")


# --------------------------------------------------------------------------- #
# Application tracking — the lightweight outcome layer (deliverable 6, v1)
#
# ``docs/contracts/07-application-state-machine.md`` §11. This is **not** the
# submission state machine above: it is what happens *after* an application went
# out — a reply, an interview, a rejection, a withdrawal — recorded so that
# "how many applications became interviews, from which source, at which match
# score, with which artefact version?" is a query instead of a guess.
#
# Two rules the shipped product broke, and that this vocabulary makes testable:
#
# 1. **A status change is an event, never an overwrite.** ``application_tracking``
#    stores the current state as a *projection*; ``application_tracking_events``
#    is append-only and is the truth (a correction adds a row, it never edits
#    one).
# 2. **An interview is never inferred.** ``TRACKING_TRUSTED_ONLY_STATES`` are
#    reachable only from a :data:`TRACKING_TRUSTED_ORIGINS` origin — the user
#    typing "I got an interview", our own submission machinery, or a verified
#    integration the user connected. A parsed recruiter email may produce a
#    *provisional suggestion* that somebody replied
#    (:data:`EMAIL_INFERRED_TRACKING_STATES`) and nothing else.
# --------------------------------------------------------------------------- #
#: Board/report grouping for the tracking layer.
TRACKING_PHASES: Tuple[str, ...] = (
    "pre_application", "applied", "in_conversation", "interviewing", "closed",
)

#: ``application_tracking.state`` — one row per ``(user_id, job_id)``.
APPLICATION_TRACKING_STATES: Tuple[str, ...] = (
    "not_applied",           # tracked, nothing sent yet
    "applied",               # the application went out (automation or the user)
    "recruiter_response",    # a human replied; no interview yet
    "interview_scheduled",   # an interview is on the calendar
    "interview_completed",   # the interview happened
    "offer_received",
    "rejected_by_employer",
    "withdrawn",             # the candidate pulled out (incl. declining an offer)
)

#: State → phase. Complete by construction; ``test_contracts_vocabulary`` fails
#: if a state is added without a phase.
TRACKING_STATE_PHASE: Dict[str, str] = {
    "not_applied": "pre_application",
    "applied": "applied",
    "recruiter_response": "in_conversation",
    "interview_scheduled": "interviewing",
    "interview_completed": "interviewing",
    "offer_received": "closed",
    "rejected_by_employer": "closed",
    "withdrawn": "closed",
}

#: No transition out of these. A mistake is fixed with a **correction** event —
#: which keeps the wrong row and writes a new one — never by "reopening" a
#: rejection, because the rejection is a fact that happened.
TRACKING_TERMINAL_STATES: Tuple[str, ...] = ("rejected_by_employer", "withdrawn")

#: A record that reached any of these counts as an interview in the report's
#: application→interview conversion. ``offer_received`` is included because an
#: offer implies at least one interview happened, even if the user never logged
#: the rounds.
TRACKING_INTERVIEW_STATES: Tuple[str, ...] = (
    "interview_scheduled", "interview_completed", "offer_received",
)

#: Projection onto the legacy ``jobs.status`` board column, so the Jobs board,
#: the dashboard counters and the funnel keep working while the tracking row is
#: the truth (the same pattern as :data:`JOB_STATUS_PROJECTION`).
TRACKING_JOB_STATUS_PROJECTION: Dict[str, str] = {
    "not_applied": "discovered",
    "applied": "applied",
    "recruiter_response": "applied",
    "interview_scheduled": "applied",
    "interview_completed": "applied",
    "offer_received": "applied",
    "rejected_by_employer": "rejected",
    "withdrawn": "skipped",
}

#: ``application_tracking_events.origin`` — **who is asserting this happened**.
#: Rendered next to every timeline row: a system observation and a user's memory
#: are not the same claim, and the report says which one it is built from.
TRACKING_ORIGINS: Tuple[str, ...] = (
    "system_observed",       # our own automation ran it / saw the portal receipt
    "user_reported",         # the user typed it ("I got an interview")
    "email_inferred",        # parsed out of an inbound message — provisional
    "verified_integration",  # a calendar/ATS integration the user connected
)

#: Origins allowed to move a record into a :data:`TRACKING_TRUSTED_ONLY_STATES`
#: state. ``email_inferred`` is deliberately absent (see rule 2 above).
TRACKING_TRUSTED_ORIGINS: Tuple[str, ...] = (
    "system_observed", "user_reported", "verified_integration",
)

#: The only state an ``email_inferred`` event may propose — and it is stored
#: ``is_provisional = true`` until the user confirms or dismisses it.
EMAIL_INFERRED_TRACKING_STATES: Tuple[str, ...] = ("recruiter_response",)

#: States that require a trusted origin. Reaching one from ``email_inferred`` is
#: a ``422 origin_not_trusted``, not a silent downgrade.
TRACKING_TRUSTED_ONLY_STATES: Tuple[str, ...] = (
    "interview_scheduled", "interview_completed", "offer_received", "rejected_by_employer",
)

#: ``application_tracking.artifact_kind`` — what was attached to the application.
#: The snapshot (id + version + sha256) is frozen when tracking opens so a later
#: regeneration cannot rewrite history.
TRACKING_ARTIFACT_KINDS: Tuple[str, ...] = ("packet", "resume_document", "resume", "none")

#: ``application_tracking.source`` values that are not a job-board id (the board
#: keeps the job's own ``source``; these are the ones the user attributes).
TRACKING_MANUAL_SOURCES: Tuple[str, ...] = (
    "referral", "company_site", "linkedin", "recruiter_inbound", "job_fair",
    "newsletter", "other",
)

#: ``payload.format`` of an ``application.interview_reported`` event.
INTERVIEW_FORMATS: Tuple[str, ...] = (
    "phone", "video", "onsite", "take_home", "assessment", "panel", "other",
)

#: ``payload.channel`` of an ``application.response_received`` event — where the
#: recruiter's reply arrived. Recorded so "which channel actually answers?" is
#: answerable; never used to *infer* the reply happened.
RESPONSE_CHANNELS: Tuple[str, ...] = (
    "email", "phone", "linkedin", "portal", "sms", "in_person", "other",
)

#: ``payload.self_assessment`` of an ``application.interview_completed`` event —
#: the user's own read on how it went. It is feedback about the *process*, not a
#: prediction, and the report never presents it as one.
INTERVIEW_SELF_ASSESSMENTS: Tuple[str, ...] = (
    "went_well", "mixed", "went_poorly", "unknown",
)


def tracking_job_status_for(state: str) -> str:
    """The legacy board status for a tracking state (``discovered`` if unknown)."""
    return TRACKING_JOB_STATUS_PROJECTION.get(state, "discovered")


def is_terminal_tracking_state(state: str) -> bool:
    """True when no further transition is allowed out of *state*."""
    return state in TRACKING_TERMINAL_STATES


# --------------------------------------------------------------------------- #
# Browser-assisted application sessions (human-in-the-loop)
#
# One ``application_sessions`` row is one isolated browser run for one
# ``(user, job)``. Automation only ever types the values it is allowed to type;
# everything else — a password, an MFA code, a CAPTCHA, an unknown or ambiguous
# field, a legal/sensitive question — turns into an ``application_actions``
# queue item and the session state becomes ``awaiting_user``.
# --------------------------------------------------------------------------- #
#: ``application_sessions.state`` — the lifecycle of one assisted browser run.
APPLICATION_SESSION_STATES: Tuple[str, ...] = (
    "created",         # the row exists; no browser context has been opened
    "launching",       # an isolated context is starting for this user
    "preparing",       # form detected and fields classified; nothing typed yet
    "active",          # filling the fields this session is allowed to touch
    "awaiting_user",   # a human step is required *in the browser* (login/MFA/CAPTCHA/field)
    "paused",          # the user paused it; nothing runs until they resume
    "resuming",        # checkpoint validation is in flight (identity re-check)
    "completed",       # every fillable field is filled; submit only if policy allows
    "expired",         # the TTL passed — re-authentication is required
    "failed",
    "cancelled",
)

#: ``application_sessions.phase`` — the UI grouping, stored so a board never has
#: to re-derive it. Complete by construction; a test fails if a state is added
#: without a phase.
APPLICATION_SESSION_PHASE: Dict[str, str] = {
    "created": "new",
    "launching": "working",
    "preparing": "working",
    "active": "working",
    "awaiting_user": "blocked",
    "paused": "blocked",
    "resuming": "working",
    "completed": "done",
    "expired": "closed",
    "failed": "closed",
    "cancelled": "closed",
}

#: No transition out of these.
APPLICATION_SESSION_TERMINAL_STATES: Tuple[str, ...] = (
    "completed", "expired", "failed", "cancelled",
)

#: States that mean "a human owes this session something" — what
#: ``GET /api/application-sessions/actions`` surfaces.
APPLICATION_SESSION_USER_ACTION_STATES: Tuple[str, ...] = ("awaiting_user",)

#: ``application_actions.kind`` — what the human is being asked to do. The
#: first three are browser handoffs (the user does it themselves, in the
#: browser, and we never see the value); the rest are answer/question items.
USER_ACTION_KINDS: Tuple[str, ...] = (
    "login",            # sign in or create the portal account personally
    "mfa",              # second factor / one-time code
    "captcha",          # a bot check the human completes — never solved for them
    "unknown_field",    # a field that could not be classified
    "ambiguous_field",  # a field whose meaning is genuinely unclear
    "sensitive_field",  # personal data we refuse to guess
    "legal_question",   # attestation / consent / eligibility statement
    "session_expired",  # the session must be re-authenticated to continue
    "review_required",  # the plan is ready and wants a human look before submit
    "ai_unavailable",   # AI field-mapping is offline — user fills the fields
)

#: ``application_actions.status``.
USER_ACTION_STATUSES: Tuple[str, ...] = (
    "pending", "in_progress", "completed", "expired", "cancelled",
)

#: Why a resume was refused (``application_sessions.last_checkpoint_failure``).
#: Never free text — a UI has to render the reason, and an operator has to
#: grep for it.
CHECKPOINT_FAILURES: Tuple[str, ...] = (
    "session_expired",
    "session_not_resumable",
    "job_not_owned",
    "job_reassigned",
    "url_changed",
    "employer_mismatch",
    "application_identity_mismatch",
    "portal_domain_not_allowed",
)

#: How a detected form field is classified before anything is typed.
FIELD_CLASSIFICATIONS: Tuple[str, ...] = (
    "canonical",   # mapped to a known profile key
    "file",        # an attachment (resume/CV)
    "voluntary",   # EEO self-identification — decline is the only safe default
    "sensitive",   # personal data: eligible to fill from a confirmed value
    "legal",       # attestation / eligibility statement — the user answers
    "unknown",     # nothing we recognise: ask, never guess
    "ambiguous",   # more than one plausible meaning: ask
    "credential",  # password/passphrase — by default the user types it in the
                   # browser; an opted-in session may type a vault credential
    "mfa",         # one-time code — the user types it, in the browser
    "captcha",     # bot check — the user completes it, never solved for them
    "submit",      # a submit control — never clicked unless policy allows
    "ignored",     # hidden/CSRF/search inputs we never touch
)

#: What the executor does with a classified field.
FIELD_ACTIONS: Tuple[str, ...] = (
    "autofill",   # safe to type from a confirmed profile value
    "ask_user",   # create an action item and pause the session
    "handoff",    # hand the browser step to the user (no value ever reaches us)
    "skip",       # leave it alone, non-blocking
    "never",      # forbid it outright
)

#: ``application_submissions.state`` — the at-most-once ledger.
SUBMISSION_STATES: Tuple[str, ...] = (
    "reserved", "submitted", "verified", "failed", "abandoned",
)

#: Why a submission was refused (``application_submissions.refusal_reason`` /
#: the API's ``refused`` payload).
SUBMISSION_REFUSALS: Tuple[str, ...] = (
    "already_submitted",
    "policy_disallows_submit",
    "session_expired",
    "checkpoint_invalid",
    "action_required",
    "no_session",
)



def is_terminal_application_state(state: str) -> bool:
    """True when no further transition is allowed out of *state*."""
    return state in APPLICATION_TERMINAL_STATES


#: Backwards-compatible projection onto the legacy ``jobs.status`` column, which
#: stays as the board's denormalised summary while ``applications.state`` is the
#: truth. Values are exactly the ones the board, the dashboard and the funnel
#: already read — ``applying``/``emailed`` are *not* here because nothing has
#: ever written them (see ``docs/contracts/07-application-state-machine.md``).
JOB_STATUS_PROJECTION: Dict[str, str] = {
    "not_started": "discovered",
    "queued": "queued",
    "preparing": "preparing",
    "blocked_input": "needs_input",
    "blocked_resume_approval": "needs_input",
    "blocked_consent": "needs_input",
    "blocked_credential": "needs_input",
    "blocked_policy": "needs_input",
    "blocked_quota": "needs_input",
    "prepared": "ready_to_apply",
    "awaiting_approval": "ready_to_apply",
    "submitting": "preparing",
    "submitted": "applied",
    "submitted_unverified": "applied",
    "interview_scheduled": "applied",
    "rejected_by_employer": "rejected",
    "offer_received": "applied",
    "failed": "failed",
    "withdrawn": "skipped",
    "expired": "skipped",
    "skipped": "skipped",
    "cancelled": "skipped",
}

#: The legacy ``jobs.status`` vocabulary (what may be stored in that column).
JOB_STATUSES: Tuple[str, ...] = (
    "discovered", "queued", "preparing", "needs_input", "ready_to_apply",
    "applied", "rejected", "failed", "skipped",
)


def job_status_for(application_state: str) -> str:
    """The legacy board status for a canonical application state."""
    return JOB_STATUS_PROJECTION.get(application_state, "discovered")


# --------------------------------------------------------------------------- #
# Application field answers: ambiguity & resolution (acceptance criterion)
# --------------------------------------------------------------------------- #
#: ``application_field_answers.ambiguity`` — an explicit verdict, never implied
#: by an empty value.
AMBIGUITY_KINDS: Tuple[str, ...] = (
    "resolved",           # exactly one canonical value, confidence high enough
    "ambiguous_mapping",  # the portal label maps to more than one profile key
    "ambiguous_value",    # several candidate values for one key
    "ambiguous_option",   # the portal's option list does not contain our value
    "missing",            # no value anywhere
    "out_of_scope",       # policy forbids answering (restricted / EEO decline)
)

#: ``application_field_answers.resolution`` — how it got a value.
FIELD_RESOLUTIONS: Tuple[str, ...] = (
    "profile",            # taken from a confirmed profile field
    "user_choice",        # the user picked one of the candidates we offered
    "user_typed",         # the user wrote a value
    "declined",           # the user chose not to answer
    "policy_default",     # the automation policy answered (e.g. EEO decline)
    "resume_file",        # satisfied by the attached document
    "vault_credential",   # satisfied by an encrypted vault entry
    "unresolved",         # still open — the application stays blocked
)

#: The shipped ``build_autofill_plan`` ``value_source`` per mapped field, kept
#: as a frozen vocabulary so a plan written before this contract stays readable.
#: ``docs/contracts/08-application-fields.md`` §2 maps each one onto
#: :data:`FIELD_RESOLUTIONS`.
AUTOFILL_VALUE_SOURCES: Tuple[str, ...] = (
    "profile", "resume_file", "generated_cover_letter", "vault", "user_answer", "unknown",
)


#: ``application_field_answers.reuse_scope`` — how far a saved answer travels.
#: Anything narrower than ``global`` must be re-asked outside its scope.
ANSWER_REUSE_SCOPES: Tuple[str, ...] = (
    "this_application", "this_portal", "this_company", "global", "never",
)


# --------------------------------------------------------------------------- #
# Match results (deliverable 5)
# --------------------------------------------------------------------------- #
#: ``match_results.score_source`` — the provenance label the UI must render.
#: These are the values already in the codebase; the docs define each one.
SCORE_SOURCES: Tuple[str, ...] = (
    "ai",                 # guardrail-verified model verdict
    "laya",               # the local typed-decision engine (owner-routed) — typed rubric verdict, not generated text
    "preliminary",        # deterministic keyword/TF-IDF estimate — labelled, never shown as an AI score
    "pending",            # AI offline; not scored yet
    "rejected",           # the model answered, the accuracy guardrail refused it
    "insufficient_data",  # not enough JD/profile text to score honestly
    "funding_context",    # funding-radar posting with no job description
    "unscored",           # never attempted
)

#: ``match_results.band`` — the recommendation word (not a score range the UI
#: invents).
MATCH_BANDS: Tuple[str, ...] = ("strong", "good", "possible", "weak", "unknown")

#: ``match_results.staleness`` — why a stored verdict may no longer hold.
MATCH_STALENESS: Tuple[str, ...] = ("fresh", "profile_changed", "job_changed", "expired", "scorer_upgraded")

#: ``match_feedback.kind`` — the user's correction / outcome signal on a
#: recommendation. The first two judge the recommendation itself; the last
#: three are post-application outcomes (the calibration dataset).
MATCH_FEEDBACK_KINDS: Tuple[str, ...] = ("relevant", "not_relevant", "applied", "rejected", "interview")

#: Feedback kinds that record an application *outcome* (applied → rejected /
#: interview). Only these count toward calibration; ``relevant`` /
#: ``not_relevant`` correct the recommendation, they are not outcomes.
MATCH_FEEDBACK_OUTCOME_KINDS: Tuple[str, ...] = ("applied", "rejected", "interview")

#: Stage-1 hard-filter check names (``match_results.hard_filters.checks[].name``).
#: Names mirror the deterministic code in ``app.services.matching`` one-for-one.
MATCH_FILTERS: Tuple[str, ...] = (
    "expired",
    "duplicate",
    "location_mismatch",
    "work_authorization",
    "sponsorship",
    "employment_type_mismatch",
    "compensation_below_minimum",
    "seniority_mismatch",
    "required_skills_missing",
)

#: Stage-2 named features of the deterministic score
#: (``match_results.features[].key``), in computation order.
MATCH_FEATURES: Tuple[str, ...] = (
    "required_skills",
    "preferred_skills",
    "relevant_experience",
    "seniority",
    "industry",
    "location",
    "compensation",
    "remote",
    "career_trajectory",
    "freshness",
    "hiring_signal",
    "application_friction",
)

#: Stage-3 AI review recommendations (``review.recommendation``). Each band is
#: only allowed to express the recommendations in
#: ``app.services.matching.RECOMMENDATION_BY_BAND`` — the UI must never render a
#: recommendation the guardrail could not have accepted.
MATCH_RECOMMENDATIONS: Tuple[str, ...] = ("apply", "apply_with_tailoring", "hold", "skip")

#: How a stage-3 requirement was established (``review.requirements_review[].basis``).
MATCH_REQUIREMENT_BASIS: Tuple[str, ...] = ("explicit", "inferred")


# --------------------------------------------------------------------------- #
# Discovery runs (deliverable 4 + 11)
# --------------------------------------------------------------------------- #
#: ``discovery_runs.status`` — mirrors the queue states a run can be in, plus
#: the two run-specific outcomes.
DISCOVERY_RUN_STATES: Tuple[str, ...] = (
    "queued", "running", "paused", "completed", "completed_empty",
    "failed", "dead", "cancelled",
)

#: ``discovery_runs.why_empty`` — exactly one, and only when the run added zero
#: jobs (an absent key means "this run found jobs"). Already implemented in
#: :mod:`app.services.discovery`; repeated here so the vocabulary is in one place.
DISCOVERY_WHY_EMPTY: Tuple[str, ...] = (
    "no_sources_configured", "all_sources_failed", "no_fresh_postings", "jobs_cap_reached",
)

#: ``discovery_runs.report.ai_rescore.skipped`` reasons.
DISCOVERY_AI_SKIP_REASONS: Tuple[str, ...] = ("no_profile", "plan_free")


# --------------------------------------------------------------------------- #
# Background jobs (deliverable 9)
# --------------------------------------------------------------------------- #
#: ``pipeline_jobs.status`` — unchanged from the shipped queue; listed here so
#: the contract and the code cannot diverge.
QUEUE_STATUSES: Tuple[str, ...] = (
    "queued", "processing", "paused", "needs_input", "done", "failed", "dead", "cancelled",
)

#: Rows the live layer treats as "still moving" (needs the user included).
QUEUE_LIVE_STATUSES: Tuple[str, ...] = ("queued", "processing", "paused", "needs_input")

#: No worker will touch these again.
QUEUE_TERMINAL_STATUSES: Tuple[str, ...] = ("done", "failed", "dead", "cancelled")

#: ``pipeline_jobs.pipeline`` — the durable work kinds.
QUEUE_PIPELINES: Tuple[str, ...] = ("discovery", "application", "email", "funding", "ai", "extraction",
                                     "onboarding", "browser_session")

#: ``payload.trigger`` — why the work exists. ``auto`` is what makes an
#: auto-mode run notify and charge differently from a clicked one.
QUEUE_TRIGGERS: Tuple[str, ...] = ("user", "auto", "retry", "recovery", "webhook", "system")

#: ``payload.progress.step`` vocabulary per pipeline. A step name is stable;
#: its index is not (steps may be skipped).
QUEUE_PROGRESS_STEPS: Dict[str, Tuple[str, ...]] = {
    "onboarding": ("validating", "stored", "extracting_text", "extracting_profile",
                   "guardrails", "saving", "building_context", "done"),
    "discovery": ("resolving_config", "fetching_sources", "deduplicating",
                  "scoring", "classifying", "persisting", "detecting_forms", "done"),
    "application": ("choosing_resume", "detecting_form", "mapping_fields",
                    "preparing_credential", "building_plan", "awaiting_input",
                    "navigating", "filling", "submitting", "verifying", "done"),
    "extraction": ("parsing_document", "extracting_fields", "guardrails",
                   "scoring_confidence", "writing_provenance", "done"),
    # One pass of a browser-assisted application session. ``awaiting_user`` is a
    # step, not a failure: the pass stops at the first human-required moment
    # (login, MFA, CAPTCHA, an unknown field) and the next pass resumes from the
    # checkpoint.
    "browser_session": ("opening", "observing", "filling", "awaiting_user",
                        "resuming", "verifying_checkpoint", "done"),
}


# --------------------------------------------------------------------------- #
# Automation policy (deliverable 8)
# --------------------------------------------------------------------------- #
#: ``automation_policies.workflow`` — the things a policy can govern. Superset
#: of the scheduler's current ``discovery|funding|application_prep``.
AUTOMATION_WORKFLOWS: Tuple[str, ...] = (
    "discovery", "funding", "application_prep", "application_submit", "outreach", "profile_refresh",
)

#: ``automation_policies.mode`` — how far the system may go without asking.
AUTOMATION_MODES: Tuple[str, ...] = (
    "off",             # never act
    "suggest",         # surface candidates, take no action
    "prepare",         # build the plan/artefact, stop before anything outbound
    "auto_submit",     # act end-to-end inside the policy's limits
)

#: ``automation_policies.scope``.
AUTOMATION_SCOPES: Tuple[str, ...] = ("global", "persona", "workflow", "company", "portal")

#: ``automation_policies.sensitive_field_policy`` — the default answer for a
#: field the policy classifies as sensitive/restricted.
SENSITIVE_FIELD_POLICIES: Tuple[str, ...] = (
    "never_answer", "ask_every_time", "use_saved_answer", "prefer_decline",
)

#: ``evaluate_policy`` decision reasons (contracts/10 §5). ``allowed`` when an
#: automation attempt went through; every other value names the *first* gate
#: that refused it, in the fixed evaluation order.
AUTOMATION_POLICY_REASONS: Tuple[str, ...] = (
    "allowed",
    "server_disabled",
    "plan_locked",
    "consent_missing",
    "consent_withdrawn",
    "disclosure_version_changed",
    "policy_off",
    "daily_limit",
    "monthly_limit",
    "quota_exhausted",
    "quiet_hours",
    "company_blocked",
    "portal_not_allowed",
    "keyword_blocked",
    "location_excluded",
    "score_below_floor",
    "compensation_below_floor",
    "review_required",
    "expired",
)


# --------------------------------------------------------------------------- #
# Events & notifications (deliverables 7 & 10)
# --------------------------------------------------------------------------- #
#: ``application_events.event_type`` / ``domain_events.event_type``.
#: Namespaced ``<aggregate>.<past_tense_verb>``; a consumer must ignore types it
#: does not know (they are additive).
EVENT_TYPES: Tuple[str, ...] = (
    # onboarding
    "onboarding.started",
    "onboarding.gate_completed",
    "onboarding.gate_skipped",
    "onboarding.gate_blocked",
    "onboarding.state_changed",
    "onboarding.completed",
    "onboarding.abandoned",
    "onboarding.resumed",
    # profile & extraction
    "profile.created",
    "profile.version_superseded",
    "profile.field_extracted",
    "profile.field_confirmed",
    "profile.field_corrected",
    "profile.field_rejected",
    "profile.review_completed",
    "profile.archived",
    "extraction.started",
    "extraction.succeeded",
    "extraction.failed",
    "extraction.rejected_by_guardrail",
    "extraction.superseded",
    # resume documents
    "resume.uploaded",
    "resume.generated",
    "resume.pending_approval",
    "resume.approved",
    "resume.rejected",
    "resume.polished",
    "resume.archived",
    "resume.deleted",
    # discovery
    "discovery.run_queued",
    "discovery.run_started",
    "discovery.source_fetched",
    "discovery.source_failed",
    "discovery.run_checkpointed",
    "discovery.run_completed",
    "discovery.run_empty",
    "discovery.run_cancelled",
    "discovery.job_persisted",
    "discovery.duplicate_skipped",
    # matching
    "match.computed",
    "match.superseded",
    "match.stale",
    "match.high_score",
    # applications
    "application.created",
    "application.queued",
    "application.preparation_started",
    "application.resume_selected",
    "application.form_detected",
    "application.fields_mapped",
    "application.field_ambiguous",
    "application.input_requested",
    "application.input_provided",
    "application.credential_created",
    "application.credential_reused",
    "application.plan_ready",
    "application.approved",
    "application.rejected_by_user",
    "application.blocked",
    "application.unblocked",
    "application.submission_started",
    "application.submitted",
    "application.submission_unverified",
    "application.submission_failed",
    "application.dry_run_completed",
    "application.marked_applied_manually",
    "application.outcome_recorded",
    "application.withdrawn",
    "application.expired",
    "application.cancelled",
    "application.retried",
    "application.dead_lettered",
    # application tracking — the lightweight outcome layer (contracts/07 §11).
    # Every one of these is a row in ``application_tracking_events``; the
    # record's ``state`` is a projection of them, never the other way round.
    "application.tracking_opened",
    "application.state_changed",
    "application.state_corrected",
    "application.interview_reported",
    "application.interview_completed",
    "application.response_received",
    "application.rejection_received",
    "application.offer_received",
    "application.note_added",
    "application.follow_up_scheduled",
    "application.follow_up_completed",
    "application.attribution_updated",
    "application.snapshot_recorded",
    "application.suggestion_recorded",
    # browser-assisted sessions (human-in-the-loop)
    "application.session_started",
    "application.session_paused",
    "application.session_resumed",
    "application.session_expired",
    "application.session_completed",
    "application.session_cancelled",
    "application.action_required",
    "application.action_completed",
    "application.handoff_issued",
    "application.observation_recorded",
    "application.submission_refused",
    # automation policy & scheduling
    "automation.policy_changed",
    "automation.run_scheduled",
    "automation.run_skipped",
    "automation.quota_exhausted",
    # background work
    "job.enqueued",
    "job.claimed",
    "job.paused",
    "job.resumed",
    "job.progressed",
    "job.checkpointed",
    "job.completed",
    "job.failed",
    "job.reclaimed",
    "job.dead_lettered",
    "job.cancelled",
    # account, consent & security (mirrors the shipped audit vocabulary)
    "account.created",
    "account.exported",
    "account.deleted",
    "consent.accepted",
    "consent.revoked",
    "disclosure.version_changed",
    # notifications
    "notification.created",
    "notification.read",
    "notification.dismissed",
)

#: ``domain_events.aggregate_type`` — the entity an event is about. Naming the
#: aggregate is what makes ``sequence`` meaningful: ordering is guaranteed per
#: aggregate, never across them. See ``docs/contracts/12-events-and-notifications.md``.
AGGREGATE_TYPES: Tuple[str, ...] = (
    "application", "job", "resume_document", "profile", "discovery_run",
    "match_result", "onboarding_session", "automation_policy", "email",
    "notification", "queue_job", "persona", "user",
)

#: ``audit_logs.action`` — the security-relevant trail. The shipped actions are
#: listed first (they are already in rows and must never be renamed); the ones
#: marked *new* belong to the entities these contracts add.
#: ``docs/contracts/12-events-and-notifications.md`` §6.
AUDIT_ACTIONS: Tuple[str, ...] = (
    # shipped
    "account.deleted", "account.exported",
    "ai.config_updated", "ai.resume_requested",
    "auth.api_key_created", "auth.api_key_revoked", "auth.bootstrap", "auth.login",
    "auth.login_failed", "auth.logout", "auth.password_changed", "auth.refresh",
    "auth.register",
    "billing.canceled", "billing.checkout_created", "billing.downgraded", "billing.upgraded",
    "consent.accepted", "consent.revoked",
    "cover_letter.generated",
    "email.approved", "email.blocked", "email.generate_queued", "email.sent", "email.unsubscribed",
    "funding.processed", "funding.refresh_queued", "funding.scan", "funding.scan_failed",
    "job.applied", "job.input_submitted",
    "persona.activated", "persona.created", "persona.deleted", "persona.reflected", "persona.updated",
    "resume.approved", "resume.deleted", "resume.downloaded", "resume.generate_queued",
    "resume.generated", "resume.polished", "resume.rejected",
    "settings.updated",
    "vault.credential_created", "vault.deleted", "vault.exported", "vault.reveal_failed", "vault.viewed",
    # new — browser-assisted sessions (human-in-the-loop)
    "application.session_started", "application.session_paused", "application.session_resumed",
    "application.session_expired", "application.session_completed", "application.session_cancelled",
    "application.action_completed", "application.handoff_issued",
    "application.submission_reserved", "application.submission_refused",
    # new — onboarding & profile
    "account.created",
    "onboarding.started", "onboarding.completed", "onboarding.abandoned",
    "onboarding.gate_skipped", "onboarding.retried",
    "profile.updated", "profile.review_completed", "profile.archived",
    "extraction.succeeded", "extraction.failed", "resume.uploaded",
    # new — discovery, application, automation
    "discovery.run_triggered", "discovery.run_cancelled",
    "job.apply_queued", "job.prepared", "job.submit_approved", "job.apply_failed", "job.apply_retried",
    "application.submitted", "application.submitted_auto", "application.submitted_unverified",
    "application.cancelled", "application.outcome_recorded",
    # new — application tracking (the outcome layer, contracts/07 §11). A
    # correction rewrites what the report says happened, so it is audited.
    "application.tracking_opened", "application.status_updated",
    "application.interview_recorded", "application.state_corrected",
    "automation.policy_updated", "automation.policy_deleted",
    "automation.auto_submit_enabled", "automation.auto_submit_disabled",
    # new — outcome reporting (contracts/07 §11.7). Downloading a report takes
    # the tenant's own outcome data off the platform, so it is audited.
    "analytics.report_exported",
)

#: Prefixes/actions that must never be dropped under load. Superset of the
#: shipped ``app.core.audit.SENSITIVE_ACTIONS`` (which matches by prefix); the
#: parity test asserts the shipped tuple is contained here.
SENSITIVE_AUDIT_ACTIONS: Tuple[str, ...] = (
    "auth.", "vault.", "account.", "consent.",
    "resume.delete", "email.send",
    "application.submitted", "application.submitted_auto", "application.submitted_unverified",
    "automation.auto_submit_enabled", "onboarding.gate_skipped", "profile.archived",
)


#: ``notifications.severity`` (and ``application_events.severity``).
NOTIFICATION_SEVERITIES: Tuple[str, ...] = ("info", "success", "warning", "error", "action_required")

#: ``notifications.kind`` — the shipped three plus the ones the contracts add.
#: A kind is also the preference key: users mute kinds, not instances.
NOTIFICATION_KINDS: Tuple[str, ...] = (
    "high_match",
    "funding_match",
    "quota_exhausted",
    "weekly_summary",
    "automation_failed",
    "action_required",
    "onboarding_step_required",
    "profile_review_required",
    "resume_ready",
    "application_submitted",
    "application_outcome",
    "application_follow_up",
    "email_reply",
    "consent_expiring",
    "security",
)

#: ``notifications.channel`` — where it may be delivered. ``email`` requires the
#: matching consent *and* the user's notification preference.
NOTIFICATION_CHANNELS: Tuple[str, ...] = ("in_app", "email")


#: Kinds the user **cannot** mute: a product that lets you switch off "we could
#: not apply for you" is a product that silently does nothing.
NOTIFICATION_UNMUTABLE_KINDS: Tuple[str, ...] = (
    "action_required", "onboarding_step_required", "profile_review_required", "security",
)

#: ``settings`` keys under the ``notifications`` category. The shipped four plus
#: ``email_enabled``; a new mutable kind adds a key here and a docs row.
NOTIFICATION_PREFERENCE_KEYS: Tuple[str, ...] = (
    "email_enabled", "high_match", "automation_failures", "weekly_summary", "new_replies",
)

# --------------------------------------------------------------------------- #
# Outcome reporting — interviews and meaningful outcomes, not activity volume
# --------------------------------------------------------------------------- #
# ``docs/REPORTING.md`` and ``docs/contracts/07-application-state-machine.md``
# §11.7. Four vocabularies, and each one exists because a report without it
# starts lying:
#
# * provenance — is this number *observed*, *reported by the user*, *derived*
#   from those, or *estimated* by a model? A dashboard that prints a model
#   output next to a counted fact without saying which is which is the bug this
#   whole file exists to prevent.
# * sufficiency — a comparison on 2 applications is noise. Groups below the
#   minimum sample are labelled, not quietly rendered as a percentage.
# * trend — "is this working?" is a comparison against the previous window, and
#   "not enough data" is a first-class answer (never a 0% improvement).
# * ranges — the named windows the report can be asked for, so a client cannot
#   invent a window that the methodology does not describe.

#: Which kind of claim a reported number is.
REPORT_PROVENANCE_KINDS: Tuple[str, ...] = (
    "observed",       # counted from a row we hold (a job, a submission, an event)
    "user_reported",  # the user said it happened (an interview, a rejection)
    "derived",        # arithmetic over the two above (every rate, average, delta)
    "estimated",      # a model's opinion (the match score, "high fit")
)

#: How much data backs a number. ``insufficient`` means the number is withheld.
REPORT_SUFFICIENCY: Tuple[str, ...] = ("sufficient", "insufficient", "not_applicable")

#: The direction of a period-over-period comparison. ``not_enough_data`` is not
#: ``flat``: "we cannot tell yet" and "nothing changed" are different answers.
REPORT_TRENDS: Tuple[str, ...] = ("improving", "declining", "flat", "not_enough_data")

#: Named report windows. ``custom`` means the caller supplied since/until.
REPORT_RANGES: Tuple[str, ...] = (
    "last_7_days", "last_30_days", "last_90_days", "this_week", "last_week", "custom",
)

#: What a report may be grouped by when comparing interview rates. Superset of
#: ``REPORT_GROUP_KEYS`` in the tracking service: the outcome report groups by
#: score band *and* score range, because "which band converts" and "which score
#: range converts" are two different questions about the same column.
REPORT_COMPARISON_DIMENSIONS: Tuple[str, ...] = (
    "source", "score_band", "score_range", "role_family", "artifact",
)

#: A job is "high fit" at or above this score. One number, one place: the
#: dashboard's ``STRONG_MATCH_SCORE`` reads this too, so "high fit" cannot mean
#: 75 on one page and 80 on another. The score is an **estimate** — the report
#: labels it as such and never turns it into an interview probability.
HIGH_FIT_SCORE: float = 75.0


#: ``GET /api/actions/required`` item kinds — the unified "what do you owe us"
#: read. See ``docs/contracts/13-api-response-shapes.md`` §6.
ACTION_KINDS: Tuple[str, ...] = (
    "application_input", "resume_approval", "profile_review", "consent",
    "credential", "policy_override", "submission_confirm", "onboarding_gate",
    "quota", "security",
)


# --------------------------------------------------------------------------- #
# Typed error codes (the ``detail.code`` of every non-2xx)
# --------------------------------------------------------------------------- #
ERROR_CODES: Tuple[str, ...] = (
    "not_authenticated",
    "credential_mismatch",
    "forbidden",
    "consent_required",
    "upgrade_required",
    "quota_exhausted",
    "storage_cap_reached",
    "rate_limited",
    "validation_error",
    "guardrail_failed",
    "fact_guard_failed",
    "ai_unavailable",
    "ai_paused",
    "ai_blocked",
    "profile_missing",
    "resume_missing",
    "resume_referenced",
    "job_not_found",
    "application_not_found",
    "application_already_submitted",
    "application_not_submittable",
    "tracking_not_found",
    "invalid_state_transition",
    "origin_not_trusted",
    "correction_reason_required",
    "duplicate_event",
    "session_expired",
    "action_required",
    "checkpoint_failed",
    "idempotency_conflict",
    "idempotency_replay",
    "onboarding_not_started",
    "onboarding_already_complete",
    "gate_not_satisfied",
    "field_needs_review",
    "restricted_field",
    "masked_api_key",
    "invalid_provider",
    "too_many_attempts",
    "weak_password",
    "unsupported_media_type",
    "payload_too_large",
    "internal_error",
)


# --------------------------------------------------------------------------- #
# One export for tooling / the drift test / a future GET /api/meta/contracts
# --------------------------------------------------------------------------- #
VOCABULARY: Dict[str, Tuple[str, ...]] = {
    "actor_types": ACTOR_TYPES,
    "provenance_sources": PROVENANCE_SOURCES,
    "confidence_bands": CONFIDENCE_BANDS,
    "review_statuses": REVIEW_STATUSES,
    "evidence_kinds": EVIDENCE_KINDS,
    "sensitivity_levels": SENSITIVITY_LEVELS,
    "sensitive_field_keys": SENSITIVE_FIELD_KEYS,
    "restricted_field_keys": RESTRICTED_FIELD_KEYS,
    "eeo_field_keys": EEO_FIELD_KEYS,
    "profile_states": PROFILE_STATES,
    "profile_link_kinds": PROFILE_LINK_KINDS,
    "review_actions": REVIEW_ACTIONS,
    "legacy_profile_extraction_sources": LEGACY_PROFILE_EXTRACTION_SOURCES,
    "onboarding_states": ONBOARDING_STATES,
    "onboarding_terminal_states": ONBOARDING_TERMINAL_STATES,
    "onboarding_gate_states": ONBOARDING_GATE_STATES,
    "resume_roles": RESUME_ROLES,
    "resume_states": RESUME_STATES,
    "extraction_states": EXTRACTION_STATES,
    "application_phases": APPLICATION_PHASES,
    "application_states": APPLICATION_STATES,
    "application_terminal_states": APPLICATION_TERMINAL_STATES,
    "application_user_action_states": APPLICATION_USER_ACTION_STATES,
    "application_blocked_codes": APPLICATION_BLOCKED_CODES,
    "submission_channels": SUBMISSION_CHANNELS,
    "application_tracking_states": APPLICATION_TRACKING_STATES,
    "tracking_phases": TRACKING_PHASES,
    "tracking_terminal_states": TRACKING_TERMINAL_STATES,
    "tracking_interview_states": TRACKING_INTERVIEW_STATES,
    "tracking_origins": TRACKING_ORIGINS,
    "tracking_trusted_origins": TRACKING_TRUSTED_ORIGINS,
    "email_inferred_tracking_states": EMAIL_INFERRED_TRACKING_STATES,
    "tracking_trusted_only_states": TRACKING_TRUSTED_ONLY_STATES,
    "tracking_artifact_kinds": TRACKING_ARTIFACT_KINDS,
    "tracking_manual_sources": TRACKING_MANUAL_SOURCES,
    "interview_formats": INTERVIEW_FORMATS,
    "response_channels": RESPONSE_CHANNELS,
    "interview_self_assessments": INTERVIEW_SELF_ASSESSMENTS,
    "application_session_states": APPLICATION_SESSION_STATES,
    "application_session_terminal_states": APPLICATION_SESSION_TERMINAL_STATES,
    "application_session_user_action_states": APPLICATION_SESSION_USER_ACTION_STATES,
    "user_action_kinds": USER_ACTION_KINDS,
    "user_action_statuses": USER_ACTION_STATUSES,
    "checkpoint_failures": CHECKPOINT_FAILURES,
    "field_classifications": FIELD_CLASSIFICATIONS,
    "field_actions": FIELD_ACTIONS,
    "submission_states": SUBMISSION_STATES,
    "submission_refusals": SUBMISSION_REFUSALS,
    "job_statuses": JOB_STATUSES,
    "ambiguity_kinds": AMBIGUITY_KINDS,
    "field_resolutions": FIELD_RESOLUTIONS,
    "autofill_value_sources": AUTOFILL_VALUE_SOURCES,
    "answer_reuse_scopes": ANSWER_REUSE_SCOPES,
    "score_sources": SCORE_SOURCES,
    "match_bands": MATCH_BANDS,
    "match_staleness": MATCH_STALENESS,
    "match_feedback_kinds": MATCH_FEEDBACK_KINDS,
    "match_feedback_outcome_kinds": MATCH_FEEDBACK_OUTCOME_KINDS,
    "match_filters": MATCH_FILTERS,
    "match_features": MATCH_FEATURES,
    "match_recommendations": MATCH_RECOMMENDATIONS,
    "match_requirement_basis": MATCH_REQUIREMENT_BASIS,
    "discovery_run_states": DISCOVERY_RUN_STATES,
    "discovery_why_empty": DISCOVERY_WHY_EMPTY,
    "discovery_ai_skip_reasons": DISCOVERY_AI_SKIP_REASONS,
    "queue_statuses": QUEUE_STATUSES,
    "queue_live_statuses": QUEUE_LIVE_STATUSES,
    "queue_terminal_statuses": QUEUE_TERMINAL_STATUSES,
    "queue_pipelines": QUEUE_PIPELINES,
    "queue_triggers": QUEUE_TRIGGERS,
    "automation_workflows": AUTOMATION_WORKFLOWS,
    "automation_modes": AUTOMATION_MODES,
    "automation_scopes": AUTOMATION_SCOPES,
    "sensitive_field_policies": SENSITIVE_FIELD_POLICIES,
    "automation_policy_reasons": AUTOMATION_POLICY_REASONS,
    "event_types": EVENT_TYPES,
    "aggregate_types": AGGREGATE_TYPES,
    "audit_actions": AUDIT_ACTIONS,
    "sensitive_audit_actions": SENSITIVE_AUDIT_ACTIONS,
    "notification_severities": NOTIFICATION_SEVERITIES,
    "notification_kinds": NOTIFICATION_KINDS,
    "notification_channels": NOTIFICATION_CHANNELS,
    "notification_unmutable_kinds": NOTIFICATION_UNMUTABLE_KINDS,
    "notification_preference_keys": NOTIFICATION_PREFERENCE_KEYS,
    "action_kinds": ACTION_KINDS,
    "report_provenance_kinds": REPORT_PROVENANCE_KINDS,
    "report_sufficiency": REPORT_SUFFICIENCY,
    "report_trends": REPORT_TRENDS,
    "report_ranges": REPORT_RANGES,
    "report_comparison_dimensions": REPORT_COMPARISON_DIMENSIONS,
    "error_codes": ERROR_CODES,
}

#: Map-form constants that are contracts too (state → phase, state → legacy
#: status). Kept separate because they are dicts, not vocabularies.
MAPPINGS: Dict[str, Dict[str, str]] = {
    "application_state_phase": APPLICATION_STATE_PHASE,
    "job_status_projection": JOB_STATUS_PROJECTION,
    "application_session_phase": APPLICATION_SESSION_PHASE,
    "tracking_state_phase": TRACKING_STATE_PHASE,
    "tracking_job_status_projection": TRACKING_JOB_STATUS_PROJECTION,
}

__all__ = [
    "ACTOR_TYPES", "ACTION_KINDS", "AGGREGATE_TYPES", "AMBIGUITY_KINDS",
    "ANSWER_REUSE_SCOPES", "APPLICATION_BLOCKED_CODES", "APPLICATION_PHASES",
    "APPLICATION_STATES", "APPLICATION_STATE_PHASE", "APPLICATION_TERMINAL_STATES",
    "APPLICATION_USER_ACTION_STATES", "AUDIT_ACTIONS", "AUTOFILL_VALUE_SOURCES",
    "APPLICATION_TRACKING_STATES", "TRACKING_PHASES", "TRACKING_STATE_PHASE",
    "TRACKING_TERMINAL_STATES", "TRACKING_INTERVIEW_STATES",
    "TRACKING_JOB_STATUS_PROJECTION", "TRACKING_ORIGINS", "TRACKING_TRUSTED_ORIGINS",
    "EMAIL_INFERRED_TRACKING_STATES", "TRACKING_TRUSTED_ONLY_STATES",
    "TRACKING_ARTIFACT_KINDS", "TRACKING_MANUAL_SOURCES",
    "INTERVIEW_FORMATS", "RESPONSE_CHANNELS", "INTERVIEW_SELF_ASSESSMENTS",
    "tracking_job_status_for", "is_terminal_tracking_state",
    "AUTOMATION_MODES", "AUTOMATION_SCOPES", "AUTOMATION_WORKFLOWS", "CONFIDENCE_BANDS",
    "AUTOMATION_POLICY_REASONS",
    "CONFIDENCE_BAND_THRESHOLDS", "CONTRACT_VERSION", "DISCOVERY_AI_SKIP_REASONS",
    "DISCOVERY_RUN_STATES", "DISCOVERY_WHY_EMPTY", "EEO_FIELD_KEYS", "ERROR_CODES",
    "EVENT_TYPES", "EVIDENCE_KINDS", "EXTRACTION_STATES", "FIELD_RESOLUTIONS",
    "IDEMPOTENCY_HEADER", "JOB_STATUSES", "JOB_STATUS_PROJECTION",
    "LEGACY_PROFILE_EXTRACTION_SOURCES", "MAPPINGS", "MATCH_BANDS",
    "MATCH_FEATURES", "MATCH_FILTERS",
    "MATCH_FEEDBACK_KINDS", "MATCH_FEEDBACK_OUTCOME_KINDS", "MATCH_RECOMMENDATIONS",
    "MATCH_REQUIREMENT_BASIS", "MATCH_STALENESS",
    "NOTIFICATION_CHANNELS", "NOTIFICATION_KINDS", "NOTIFICATION_PREFERENCE_KEYS",
    "NOTIFICATION_SEVERITIES", "NOTIFICATION_UNMUTABLE_KINDS", "ONBOARDING_GATE_STATES",
    "ONBOARDING_GATES", "ONBOARDING_STATES", "ONBOARDING_TERMINAL_STATES",
    "PROFILE_LINK_KINDS", "PROFILE_STATES", "PROVENANCE_SOURCES", "QUEUE_LIVE_STATUSES",
    "QUEUE_PIPELINES", "QUEUE_PROGRESS_STEPS", "QUEUE_STATUSES", "QUEUE_TERMINAL_STATUSES",
    "QUEUE_TRIGGERS", "RESUME_ROLES", "RESUME_STATES", "RESTRICTED_FIELD_KEYS",
    "REVIEW_ACTIONS", "REVIEW_STATUSES", "SCORE_SOURCES", "SENSITIVE_AUDIT_ACTIONS",
    "SENSITIVE_FIELD_KEYS", "SENSITIVE_FIELD_POLICIES", "SENSITIVITY_LEVELS",
    "SUBMISSION_CHANNELS", "SUBMISSION_REFUSALS", "SUBMISSION_STATES",
    "REPORT_PROVENANCE_KINDS", "REPORT_SUFFICIENCY", "REPORT_TRENDS", "REPORT_RANGES",
    "REPORT_COMPARISON_DIMENSIONS", "HIGH_FIT_SCORE",
    "USER_ACTION_KINDS", "USER_ACTION_STATUSES", "CHECKPOINT_FAILURES",
    "FIELD_ACTIONS", "FIELD_CLASSIFICATIONS",
    "APPLICATION_SESSION_PHASE", "APPLICATION_SESSION_STATES",
    "APPLICATION_SESSION_TERMINAL_STATES", "APPLICATION_SESSION_USER_ACTION_STATES",
    "VOCABULARY", "confidence_band", "is_terminal_application_state",
    "job_status_for", "requires_review",
]
