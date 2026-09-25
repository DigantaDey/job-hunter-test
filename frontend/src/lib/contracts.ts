/**
 * Canonical domain vocabulary — the frontend mirror of
 * `backend/app/contracts/vocabulary.py` (`docs/contracts/`).
 *
 * These are *contract* strings: they are stored in the database, returned by
 * the API and rendered here. Nothing in this file is derived, fetched or
 * guessed, and nothing may be renamed on one side only —
 * `backend/tests/test_contracts_vocabulary.py` parses this file and fails CI
 * when the two lists disagree.
 *
 * Rule for the SPA: render a label from the maps below, and fall back to the
 * raw string for a value this build has never seen. A backend that ships a new
 * state must produce a readable chip, not a blank one and not a crash.
 */

/** Contract version of the backend vocabulary this file mirrors. */
export const CONTRACT_VERSION = '1.5.0'

/** Header the SPA sends on state-changing POSTs to make a retry safe. */
export const IDEMPOTENCY_HEADER = 'Idempotency-Key'

/** Who may initiate a transition. A transition with no actor is a bug. */
export const ACTOR_TYPES = [
  'user',
  'owner',
  'system_worker',
  'system_scheduler',
  'system_watchdog',
  'system_api',
  'external_source',
  'external_portal',
  'external_ai',
  'external_smtp',
  'external_provider',
] as const
export type ActorType = (typeof ACTOR_TYPES)[number]

// ---------------------------------------------------------------------------
// Provenance / confidence / sensitivity
// ---------------------------------------------------------------------------

export const PROVENANCE_SOURCES = [
  'resume_extraction',
  'document_text',
  'user_provided',
  'user_confirmed',
  'user_corrected',
  'ai_inferred',
  'portal_html',
  'ats_api',
  'provider_api',
  'heuristic',
  'imported',
  'derived',
  'email_inferred',
] as const
export type ProvenanceSource = (typeof PROVENANCE_SOURCES)[number]

/** `provenance.evidence[*].kind` — what the pointer points at. */
export const EVIDENCE_KINDS = [
  'text_span',
  'url',
  'document',
  'provider_record',
  'user_statement',
  'portal_response',
  'email_message',
] as const
export type EvidenceKind = (typeof EVIDENCE_KINDS)[number]

export const CONFIDENCE_BANDS = ['high', 'medium', 'low', 'none'] as const
export type ConfidenceBand = (typeof CONFIDENCE_BANDS)[number]

export const CONFIDENCE_BAND_THRESHOLDS: Record<string, number> = {
  high: 0.85,
  medium: 0.6,
  low: 0.0,
}

export const REVIEW_STATUSES = [
  'unreviewed',
  'needs_review',
  'auto_accepted',
  'confirmed',
  'corrected',
  'rejected',
  'deferred',
] as const
export type ReviewStatus = (typeof REVIEW_STATUSES)[number]

export const SENSITIVITY_LEVELS = ['public', 'internal', 'sensitive', 'restricted'] as const
export type Sensitivity = (typeof SENSITIVITY_LEVELS)[number]

export const SENSITIVE_FIELD_KEYS = [
  'email',
  'phone',
  'location',
  'address',
  'dateOfBirth',
  'salaryExpectation',
  'currentCompensation',
  'noticePeriod',
  'availability',
  'workAuthorization',
  'sponsorshipRequired',
  'relocationWilling',
  'linkedin',
  'github',
] as const

export const RESTRICTED_FIELD_KEYS = [
  'governmentId',
  'nationalId',
  'passportNumber',
  'visaNumber',
  'portalPassword',
  'otpCode',
  'dateOfBirth',
  'gender',
  'race',
  'ethnicity',
  'veteranStatus',
  'disabilityStatus',
  'referenceContact',
] as const

/** Voluntary self-identification questions — default answer is always decline. */
export const EEO_FIELD_KEYS = ['gender', 'race', 'ethnicity', 'veteranStatus', 'disabilityStatus'] as const

/** The band label for a 0..1 confidence (`none` when there is no score). */
export function confidenceBand(confidence?: number | null): ConfidenceBand {
  if (confidence == null || Number.isNaN(Number(confidence))) return 'none'
  const value = Number(confidence)
  if (value >= CONFIDENCE_BAND_THRESHOLDS.high) return 'high'
  if (value >= CONFIDENCE_BAND_THRESHOLDS.medium) return 'medium'
  if (value >= CONFIDENCE_BAND_THRESHOLDS.low) return 'low'
  return 'none'
}

/** Must a human review this value before automation may use it? */
export function requiresReview(sensitivity: string, band: string): boolean {
  if (sensitivity === 'sensitive' || sensitivity === 'restricted') return true
  return band === 'low' || band === 'none'
}

// ---------------------------------------------------------------------------
// Candidate profile
// ---------------------------------------------------------------------------

/** `candidate_profiles.state` — the profile is versioned, never edited in place. */
export const PROFILE_STATES = [
  'draft',
  'review_required',
  'active',
  'superseded',
  'archived',
] as const
export type ProfileState = (typeof PROFILE_STATES)[number]

/** Frozen legacy values already stored in `candidate_profiles.extraction_source`. */
export const LEGACY_PROFILE_EXTRACTION_SOURCES = [
  'ai',
  'user_edited',
  'manual',
  'imported',
] as const

/** `document.contact.links[*].kind`. */
export const PROFILE_LINK_KINDS = [
  'linkedin',
  'github',
  'portfolio',
  'website',
  'other',
] as const

/** What a human may do with a field that needs review. `defer` is refused for `restricted` values. */
export const REVIEW_ACTIONS = [
  'confirm',
  'correct',
  'reject',
  'defer',
] as const
export type ReviewAction = (typeof REVIEW_ACTIONS)[number]

// ---------------------------------------------------------------------------
// Onboarding
// ---------------------------------------------------------------------------

export const ONBOARDING_STATES = [
  'not_started',
  'account_created',
  'consents_required',
  'awaiting_resume',
  'resume_processing',
  'extraction_blocked',
  'profile_review_required',
  'preferences_required',
  'discovery_setup',
  'automation_policy_optional',
  'ready',
  'abandoned',
] as const
export type OnboardingState = (typeof ONBOARDING_STATES)[number]

export const ONBOARDING_TERMINAL_STATES = ['ready', 'abandoned'] as const

export const ONBOARDING_GATE_STATES = [
  'pending',
  'in_progress',
  'blocked',
  'completed',
  'skipped',
] as const
export type OnboardingGateState = (typeof ONBOARDING_GATE_STATES)[number]

/** Gate keys, in the order the derived state walks them. */
export const ONBOARDING_GATES = [
  { key: 'account', label: 'Account created', required: true, blocking: true },
  { key: 'consents', label: 'Terms & data processing accepted', required: true, blocking: true },
  { key: 'resume', label: 'Master resume uploaded & extracted', required: true, blocking: true },
  { key: 'profile_review', label: 'Extracted profile reviewed', required: true, blocking: true },
  { key: 'preferences', label: 'Search & compensation preferences', required: true, blocking: true },
  { key: 'discovery_setup', label: 'Job sources chosen', required: false, blocking: false },
  { key: 'automation_policy', label: 'Automation mode chosen', required: false, blocking: false },
] as const

export const ONBOARDING_STATE_LABELS: Record<string, string> = {
  not_started: 'Not started',
  account_created: 'Account created',
  consents_required: 'Consents needed',
  awaiting_resume: 'Resume needed',
  resume_processing: 'Reading your resume',
  extraction_blocked: 'Extraction blocked',
  profile_review_required: 'Review your profile',
  preferences_required: 'Preferences needed',
  discovery_setup: 'Choose job sources',
  automation_policy_optional: 'Choose an automation mode',
  ready: 'Ready',
  abandoned: 'Paused',
}

// ---------------------------------------------------------------------------
// Resume documents & extraction
// ---------------------------------------------------------------------------

export const RESUME_ROLES = ['master', 'tailored', 'polished', 'cover_letter', 'attachment'] as const
export type ResumeRole = (typeof RESUME_ROLES)[number]

export const RESUME_STATES = [
  'uploading',
  'stored',
  'extracting',
  'pending_approval',
  'approved',
  'rejected',
  'archived',
  'deleted',
] as const
export type ResumeState = (typeof RESUME_STATES)[number]

export const EXTRACTION_STATES = [
  'pending',
  'running',
  'succeeded',
  'failed',
  'rejected',
  'superseded',
] as const
export type ExtractionState = (typeof EXTRACTION_STATES)[number]

// ---------------------------------------------------------------------------
// Application lifecycle
// ---------------------------------------------------------------------------

export const APPLICATION_PHASES = [
  'intake',
  'preparation',
  'blocked',
  'review',
  'submission',
  'tracking',
  'closed',
] as const
export type ApplicationPhase = (typeof APPLICATION_PHASES)[number]

export const APPLICATION_STATES = [
  'not_started',
  'queued',
  'preparing',
  'blocked_input',
  'blocked_resume_approval',
  'blocked_consent',
  'blocked_credential',
  'blocked_policy',
  'blocked_quota',
  'prepared',
  'awaiting_approval',
  'submitting',
  'submitted',
  'submitted_unverified',
  'interview_scheduled',
  'rejected_by_employer',
  'offer_received',
  'failed',
  'withdrawn',
  'expired',
  'skipped',
  'cancelled',
] as const
export type ApplicationState = (typeof APPLICATION_STATES)[number]

export const APPLICATION_TERMINAL_STATES = [
  'rejected_by_employer',
  'offer_received',
  'withdrawn',
  'expired',
  'skipped',
  'cancelled',
] as const

/** States where the user owes the system something — the badge count. */
export const APPLICATION_USER_ACTION_STATES = [
  'blocked_input',
  'blocked_resume_approval',
  'blocked_consent',
  'blocked_credential',
  'awaiting_approval',
] as const

export const APPLICATION_BLOCKED_CODES = [
  'missing_required_field',
  'ambiguous_field',
  'restricted_field_needs_choice',
  'resume_pending_approval',
  'resume_fact_guard_failed',
  'consent_automation_missing',
  'consent_outreach_missing',
  'credential_missing',
  'credential_unreadable',
  'portal_domain_not_allowed',
  'policy_company_blocked',
  'policy_score_below_floor',
  'policy_daily_limit',
  'quota_exhausted',
  'autofill_unavailable',
  'assisted_apply_unavailable',
  'portal_unreachable',
] as const
export type ApplicationBlockedCode = (typeof APPLICATION_BLOCKED_CODES)[number]

export const SUBMISSION_CHANNELS = ['automation', 'manual_user', 'assisted_dry_run'] as const

// ---------------------------------------------------------------------------
// Application tracking — the lightweight outcome layer
//
// `docs/contracts/07-application-state-machine.md` §11. Not the submission state
// machine above: this is what happened *after* an application went out (a reply,
// an interview, a rejection, a withdrawal), kept as an append-only timeline so
// "how many applications became interviews, from which source, at which match
// score, with which artefact version?" is a query and not a guess.
// ---------------------------------------------------------------------------

/** Board/report grouping for the tracking layer. */
export const TRACKING_PHASES = [
  'pre_application',
  'applied',
  'in_conversation',
  'interviewing',
  'closed',
] as const
export type TrackingPhase = (typeof TRACKING_PHASES)[number]

/** `application_tracking.state` — one row per (user, job). */
export const APPLICATION_TRACKING_STATES = [
  'not_applied',
  'applied',
  'recruiter_response',
  'interview_scheduled',
  'interview_completed',
  'offer_received',
  'rejected_by_employer',
  'withdrawn',
] as const
export type ApplicationTrackingState = (typeof APPLICATION_TRACKING_STATES)[number]

export const TRACKING_STATE_PHASE: Record<string, string> = {
  not_applied: 'pre_application',
  applied: 'applied',
  recruiter_response: 'in_conversation',
  interview_scheduled: 'interviewing',
  interview_completed: 'interviewing',
  offer_received: 'closed',
  rejected_by_employer: 'closed',
  withdrawn: 'closed',
}

/** No transition out of these; a mistake is fixed with a correction event. */
export const TRACKING_TERMINAL_STATES = ['rejected_by_employer', 'withdrawn'] as const

/** Reaching any of these counts as an interview in the conversion report. */
export const TRACKING_INTERVIEW_STATES = [
  'interview_scheduled',
  'interview_completed',
  'offer_received',
] as const

/** Projection onto the legacy `jobs.status` board column. */
export const TRACKING_JOB_STATUS_PROJECTION: Record<string, string> = {
  not_applied: 'discovered',
  applied: 'applied',
  recruiter_response: 'applied',
  interview_scheduled: 'applied',
  interview_completed: 'applied',
  offer_received: 'applied',
  rejected_by_employer: 'rejected',
  withdrawn: 'skipped',
}

/**
 * `application_tracking_events.origin` — who is asserting this happened. The
 * timeline renders it on every row: a system observation and a user's memory are
 * not the same claim.
 */
export const TRACKING_ORIGINS = [
  'system_observed',
  'user_reported',
  'email_inferred',
  'verified_integration',
] as const
export type TrackingOrigin = (typeof TRACKING_ORIGINS)[number]

/**
 * Origins that may move a record into a `TRACKING_TRUSTED_ONLY_STATES` state.
 * `email_inferred` is absent on purpose: an interview is never inferred from a
 * parsed email.
 */
export const TRACKING_TRUSTED_ORIGINS = [
  'system_observed',
  'user_reported',
  'verified_integration',
] as const

/** The only state an `email_inferred` event may propose (and it stays provisional). */
export const EMAIL_INFERRED_TRACKING_STATES = ['recruiter_response'] as const

/** States that require a trusted origin — `422 origin_not_trusted` otherwise. */
export const TRACKING_TRUSTED_ONLY_STATES = [
  'interview_scheduled',
  'interview_completed',
  'offer_received',
  'rejected_by_employer',
] as const

/** `application_tracking.artifact_kind` — what was attached, snapshotted. */
export const TRACKING_ARTIFACT_KINDS = ['packet', 'resume_document', 'resume', 'none'] as const

/** Attribution values that are not a job-board id (the ones the user picks). */
export const TRACKING_MANUAL_SOURCES = [
  'referral',
  'company_site',
  'linkedin',
  'recruiter_inbound',
  'job_fair',
  'newsletter',
  'other',
] as const

/** `payload.format` of an `application.interview_reported` event. */
export const INTERVIEW_FORMATS = [
  'phone',
  'video',
  'onsite',
  'take_home',
  'assessment',
  'panel',
  'other',
] as const
export type InterviewFormat = (typeof INTERVIEW_FORMATS)[number]

/** `payload.channel` of an `application.response_received` event. */
export const RESPONSE_CHANNELS = [
  'email',
  'phone',
  'linkedin',
  'portal',
  'sms',
  'in_person',
  'other',
] as const
export type ResponseChannel = (typeof RESPONSE_CHANNELS)[number]

/**
 * `payload.self_assessment` of an `application.interview_completed` event — the
 * user's own read on how it went. Feedback about the process, never a prediction.
 */
export const INTERVIEW_SELF_ASSESSMENTS = [
  'went_well',
  'mixed',
  'went_poorly',
  'unknown',
] as const
export type InterviewSelfAssessment = (typeof INTERVIEW_SELF_ASSESSMENTS)[number]

export const INTERVIEW_FORMAT_LABELS: Record<string, string> = {
  phone: 'Phone',
  video: 'Video call',
  onsite: 'On site',
  take_home: 'Take-home',
  assessment: 'Assessment',
  panel: 'Panel',
  other: 'Other',
}

export const INTERVIEW_SELF_ASSESSMENT_LABELS: Record<string, string> = {
  went_well: 'Went well',
  mixed: 'Mixed',
  went_poorly: 'Went poorly',
  unknown: 'Not sure yet',
}

/** Human copy. An unknown value renders as itself — never a blank chip. */
export const TRACKING_STATE_LABELS: Record<string, string> = {
  not_applied: 'Not applied',
  applied: 'Applied',
  recruiter_response: 'Recruiter responded',
  interview_scheduled: 'Interview scheduled',
  interview_completed: 'Interview done',
  offer_received: 'Offer received',
  rejected_by_employer: 'Rejected',
  withdrawn: 'Withdrawn',
}

export const TRACKING_PHASE_LABELS: Record<string, string> = {
  pre_application: 'Before applying',
  applied: 'Applied',
  in_conversation: 'In conversation',
  interviewing: 'Interviewing',
  closed: 'Closed',
}

export const TRACKING_ORIGIN_LABELS: Record<string, string> = {
  system_observed: 'System observed',
  user_reported: 'You reported',
  email_inferred: 'Inferred from email — unconfirmed',
  verified_integration: 'Verified integration',
}

export const TRACKING_EVENT_LABELS: Record<string, string> = {
  'application.tracking_opened': 'Tracking started',
  'application.state_changed': 'Status changed',
  'application.state_corrected': 'Correction',
  'application.interview_reported': 'Interview reported',
  'application.interview_completed': 'Interview completed',
  'application.response_received': 'Recruiter response',
  'application.rejection_received': 'Rejection',
  'application.offer_received': 'Offer',
  'application.note_added': 'Note',
  'application.follow_up_scheduled': 'Follow-up set',
  'application.follow_up_completed': 'Follow-up done',
  'application.attribution_updated': 'Source attribution',
  'application.snapshot_recorded': 'Snapshot',
  'application.suggestion_recorded': 'Suggestion (unconfirmed)',
  'application.withdrawn': 'Withdrawn',
  'application.submitted': 'Submitted',
  'application.marked_applied_manually': 'Applied manually',
}

/** Chip colour per state — the one place the board and the timeline agree. */
export const TRACKING_STATE_CHIP: Record<string, string> = {
  not_applied: 'bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300',
  applied: 'bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300',
  recruiter_response: 'bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-300',
  interview_scheduled: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  interview_completed: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300',
  offer_received: 'bg-emerald-600 text-white',
  rejected_by_employer: 'bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300',
  withdrawn: 'bg-zinc-200 text-zinc-600 dark:bg-zinc-700 dark:text-zinc-300',
}

export function trackingStateLabel(state?: string | null): string {
  if (!state) return 'Unknown'
  return TRACKING_STATE_LABELS[state] || state
}

export function trackingPhaseFor(state?: string | null): string {
  return TRACKING_STATE_PHASE[state || ''] || 'pre_application'
}

export function trackingJobStatusFor(state?: string | null): string {
  return TRACKING_JOB_STATUS_PROJECTION[state || ''] || 'discovered'
}

export function isTerminalTrackingState(state?: string | null): boolean {
  return TRACKING_TERMINAL_STATES.includes(state as (typeof TRACKING_TERMINAL_STATES)[number])
}

export function countsAsInterview(state?: string | null): boolean {
  return TRACKING_INTERVIEW_STATES.includes(state as (typeof TRACKING_INTERVIEW_STATES)[number])
}

// ---------------------------------------------------------------------------
// Browser-assisted application sessions (human-in-the-loop)
// ---------------------------------------------------------------------------

/**
 * The lifecycle of one isolated browser run for one (user, job). Anything that
 * needs a human — a password, an MFA code, a CAPTCHA, an unknown field, a legal
 * question — moves the session to `awaiting_user` and creates an action item;
 * it never gets "worked around".
 */
export const APPLICATION_SESSION_STATES = [
  'created',
  'launching',
  'preparing',
  'active',
  'awaiting_user',
  'paused',
  'resuming',
  'completed',
  'expired',
  'failed',
  'cancelled',
] as const
export type ApplicationSessionState = (typeof APPLICATION_SESSION_STATES)[number]

export const APPLICATION_SESSION_TERMINAL_STATES = [
  'completed',
  'expired',
  'failed',
  'cancelled',
] as const

/** States that mean a human owes the session something. */
export const APPLICATION_SESSION_USER_ACTION_STATES = ['awaiting_user'] as const

/** What the human is being asked to do. The first three are browser handoffs. */
export const USER_ACTION_KINDS = [
  'login',
  'mfa',
  'captcha',
  'unknown_field',
  'ambiguous_field',
  'sensitive_field',
  'legal_question',
  'session_expired',
  'review_required',
  'ai_unavailable',
] as const
export type UserActionKind = (typeof USER_ACTION_KINDS)[number]

export const USER_ACTION_STATUSES = [
  'pending',
  'in_progress',
  'completed',
  'expired',
  'cancelled',
] as const

/** Why a resume was refused. Never free text. */
export const CHECKPOINT_FAILURES = [
  'session_expired',
  'session_not_resumable',
  'job_not_owned',
  'job_reassigned',
  'url_changed',
  'employer_mismatch',
  'application_identity_mismatch',
  'portal_domain_not_allowed',
] as const

/** How a detected form field is classified before anything is typed. */
export const FIELD_CLASSIFICATIONS = [
  'canonical',
  'file',
  'voluntary',
  'sensitive',
  'legal',
  'unknown',
  'ambiguous',
  'credential',
  'mfa',
  'captcha',
  'submit',
  'ignored',
] as const

/** What the executor does with a classified field. */
export const FIELD_ACTIONS = ['autofill', 'ask_user', 'handoff', 'skip', 'never'] as const

/** The at-most-once submission ledger. */
export const SUBMISSION_STATES = ['reserved', 'submitted', 'verified', 'failed', 'abandoned'] as const

/** Why a submission was refused. */
export const SUBMISSION_REFUSALS = [
  'already_submitted',
  'policy_disallows_submit',
  'session_expired',
  'checkpoint_invalid',
  'action_required',
  'no_session',
] as const

/**
 * Canonical state → the legacy `jobs.status` the board still renders. Kept in
 * sync with the backend projection so a board and an application drawer can
 * never disagree about the same run.
 */
export const JOB_STATUS_PROJECTION: Record<string, string> = {
  not_started: 'discovered',
  queued: 'queued',
  preparing: 'preparing',
  blocked_input: 'needs_input',
  blocked_resume_approval: 'needs_input',
  blocked_consent: 'needs_input',
  blocked_credential: 'needs_input',
  blocked_policy: 'needs_input',
  blocked_quota: 'needs_input',
  prepared: 'ready_to_apply',
  awaiting_approval: 'ready_to_apply',
  submitting: 'preparing',
  submitted: 'applied',
  submitted_unverified: 'applied',
  interview_scheduled: 'applied',
  rejected_by_employer: 'rejected',
  offer_received: 'applied',
  failed: 'failed',
  withdrawn: 'skipped',
  expired: 'skipped',
  skipped: 'skipped',
  cancelled: 'skipped',
}

/** Canonical application state -> UI/queue phase (`APPLICATION_PHASES`). */
export const APPLICATION_STATE_PHASE: Record<string, string> = {
  not_started: 'intake',
  queued: 'intake',
  preparing: 'preparation',
  blocked_input: 'blocked',
  blocked_resume_approval: 'blocked',
  blocked_consent: 'blocked',
  blocked_credential: 'blocked',
  blocked_policy: 'blocked',
  blocked_quota: 'blocked',
  prepared: 'review',
  awaiting_approval: 'review',
  submitting: 'submission',
  submitted: 'submission',
  submitted_unverified: 'submission',
  interview_scheduled: 'tracking',
  rejected_by_employer: 'tracking',
  offer_received: 'tracking',
  failed: 'closed',
  withdrawn: 'closed',
  expired: 'closed',
  skipped: 'closed',
  cancelled: 'closed',
}

/** Session state -> UI phase. Complete by construction (backend pins it). */
export const APPLICATION_SESSION_PHASE: Record<string, string> = {
  created: 'new',
  launching: 'working',
  preparing: 'working',
  active: 'working',
  awaiting_user: 'blocked',
  paused: 'blocked',
  resuming: 'working',
  completed: 'done',
  expired: 'closed',
  failed: 'closed',
  cancelled: 'closed',
}

/** The only values `jobs.status` may hold. */
export const JOB_STATUSES = [
  'discovered',
  'queued',
  'preparing',
  'needs_input',
  'ready_to_apply',
  'applied',
  'rejected',
  'failed',
  'skipped',
] as const
export type JobStatus = (typeof JOB_STATUSES)[number]

export const APPLICATION_STATE_LABELS: Record<string, string> = {
  not_started: 'Not started',
  queued: 'Queued',
  preparing: 'Preparing',
  blocked_input: 'Needs your input',
  blocked_resume_approval: 'Resume needs approval',
  blocked_consent: 'Consent needed',
  blocked_credential: 'Portal login needed',
  blocked_policy: 'Stopped by your policy',
  blocked_quota: 'Limit reached',
  prepared: 'Ready to review',
  awaiting_approval: 'Waiting for your approval',
  submitting: 'Submitting',
  submitted: 'Submitted',
  submitted_unverified: 'Submitted (unconfirmed)',
  interview_scheduled: 'Interview scheduled',
  rejected_by_employer: 'Rejected',
  offer_received: 'Offer received',
  failed: 'Failed',
  withdrawn: 'Withdrawn',
  expired: 'Expired',
  skipped: 'Skipped',
  cancelled: 'Cancelled',
}

export const JOB_STATUS_LABELS: Record<string, string> = {
  discovered: 'Discovered',
  queued: 'Queued',
  preparing: 'Preparing',
  needs_input: 'Needs input',
  ready_to_apply: 'Ready to apply',
  applied: 'Applied',
  rejected: 'Rejected',
  failed: 'Failed',
  skipped: 'Skipped',
}

// ---------------------------------------------------------------------------
// Application field answers — ambiguity is explicit, never implied
// ---------------------------------------------------------------------------

export const AMBIGUITY_KINDS = [
  'resolved',
  'ambiguous_mapping',
  'ambiguous_value',
  'ambiguous_option',
  'missing',
  'out_of_scope',
] as const
export type AmbiguityKind = (typeof AMBIGUITY_KINDS)[number]

export const FIELD_RESOLUTIONS = [
  'profile',
  'user_choice',
  'user_typed',
  'declined',
  'policy_default',
  'resume_file',
  'vault_credential',
  'unresolved',
] as const
export type FieldResolution = (typeof FIELD_RESOLUTIONS)[number]

export const ANSWER_REUSE_SCOPES = [
  'this_application',
  'this_portal',
  'this_company',
  'global',
  'never',
] as const
export type AnswerReuseScope = (typeof ANSWER_REUSE_SCOPES)[number]

/** Frozen `value_source` vocabulary of the shipped autofill plan. */
export const AUTOFILL_VALUE_SOURCES = [
  'profile',
  'resume_file',
  'generated_cover_letter',
  'vault',
  'user_answer',
  'unknown',
] as const

// ---------------------------------------------------------------------------
// Match results
// ---------------------------------------------------------------------------

export const SCORE_SOURCES = [
  'ai',
  'laya',
  'preliminary',
  'pending',
  'rejected',
  'insufficient_data',
  'funding_context',
  'unscored',
] as const
export type ScoreSource = (typeof SCORE_SOURCES)[number]

export const MATCH_BANDS = ['strong', 'good', 'possible', 'weak', 'unknown'] as const
export type MatchBand = (typeof MATCH_BANDS)[number]

export const MATCH_STALENESS = [
  'fresh',
  'profile_changed',
  'job_changed',
  'expired',
  'scorer_upgraded',
] as const

/**
 * The user's correction / outcome signal on a match recommendation. The first
 * two judge the recommendation itself (`not_relevant` is the "this
 * recommendation is wrong" correction); the last three are post-application
 * outcomes — the only data a future calibrated interview probability could be
 * built from (never claimed before enough exists).
 */
export const MATCH_FEEDBACK_KINDS = [
  'relevant',
  'not_relevant',
  'applied',
  'rejected',
  'interview',
] as const
export type MatchFeedbackKind = (typeof MATCH_FEEDBACK_KINDS)[number]

/** Feedback kinds that are application outcomes (calibration dataset). */
export const MATCH_FEEDBACK_OUTCOME_KINDS = [
  'applied',
  'rejected',
  'interview',
] as const

// ---------------------------------------------------------------------------
// Multi-stage matching (v2.2.22): hard-filter check names, named score
// features, AI review recommendations. Mirrors
// backend/app/contracts/vocabulary.py exactly — the match UI renders these
// keys and must not improvise labels for unknown ones.
// ---------------------------------------------------------------------------

/** Stage 1 hard-filter check names (see `match_result.hard_filters.checks`). */
export const MATCH_FILTERS = [
  'expired',
  'duplicate',
  'location_mismatch',
  'work_authorization',
  'sponsorship',
  'employment_type_mismatch',
  'compensation_below_minimum',
  'seniority_mismatch',
  'required_skills_missing',
] as const
export type MatchFilter = (typeof MATCH_FILTERS)[number]

/** Stage 2 named features of the deterministic score (see `features[]`). */
export const MATCH_FEATURES = [
  'required_skills',
  'preferred_skills',
  'relevant_experience',
  'seniority',
  'industry',
  'location',
  'compensation',
  'remote',
  'career_trajectory',
  'freshness',
  'hiring_signal',
  'application_friction',
] as const
export type MatchFeature = (typeof MATCH_FEATURES)[number]

/** Stage 3 AI review recommendations (never a raw probability). */
export const MATCH_RECOMMENDATIONS = [
  'apply',
  'apply_with_tailoring',
  'hold',
  'skip',
] as const
export type MatchRecommendation = (typeof MATCH_RECOMMENDATIONS)[number]

/** How a stage-3 requirement was established (explicit in the JD vs inferred). */
export const MATCH_REQUIREMENT_BASIS = ['explicit', 'inferred'] as const
export type MatchRequirementBasis = (typeof MATCH_REQUIREMENT_BASIS)[number]

/**
 * What each `score_source` means, in the user's words. The list must never
 * present a keyword-overlap estimate as a model verdict.
 */
export const SCORE_SOURCE_LABELS: Record<string, string> = {
  ai: 'AI verdict',
  laya: 'Laya verdict (local decision engine)',
  preliminary: 'Keyword estimate',
  pending: 'Not scored yet (AI offline)',
  rejected: 'AI answer rejected by the accuracy guard',
  insufficient_data: 'Not enough text to score',
  funding_context: 'Funding-radar posting (no job description)',
  unscored: 'Not scored',
}

// ---------------------------------------------------------------------------
// Discovery runs
// ---------------------------------------------------------------------------

export const DISCOVERY_RUN_STATES = [
  'queued',
  'running',
  'paused',
  'completed',
  'completed_empty',
  'failed',
  'dead',
  'cancelled',
] as const
export type DiscoveryRunState = (typeof DISCOVERY_RUN_STATES)[number]

export const DISCOVERY_WHY_EMPTY = [
  'no_sources_configured',
  'all_sources_failed',
  'no_fresh_postings',
  'jobs_cap_reached',
] as const
export type DiscoveryWhyEmpty = (typeof DISCOVERY_WHY_EMPTY)[number]

export const DISCOVERY_AI_SKIP_REASONS = ['no_profile', 'plan_free'] as const

export const DISCOVERY_WHY_EMPTY_LABELS: Record<string, string> = {
  no_sources_configured: 'No job sources were attempted — enable one in Settings.',
  all_sources_failed: 'Every source that was tried failed — retry shortly.',
  no_fresh_postings: 'Sources answered, nothing new inside the freshness window.',
  jobs_cap_reached: 'Your board is full — delete jobs or upgrade the plan.',
}

// ---------------------------------------------------------------------------
// Background jobs
// ---------------------------------------------------------------------------

export const QUEUE_STATUSES = [
  'queued',
  'processing',
  'paused',
  'needs_input',
  'done',
  'failed',
  'dead',
  'cancelled',
] as const
export type QueueStatus = (typeof QUEUE_STATUSES)[number]

export const QUEUE_LIVE_STATUSES = ['queued', 'processing', 'paused', 'needs_input'] as const
export const QUEUE_TERMINAL_STATUSES = ['done', 'failed', 'dead', 'cancelled'] as const

export const QUEUE_PIPELINES = [
  'discovery',
  'application',
  'email',
  'funding',
  'ai',
  'extraction',
  'onboarding',
  // Browser-assisted application sessions: one pass per item, stopping at the
  // first human-required step (login / MFA / CAPTCHA / an unknown field).
  'browser_session',
] as const
export type QueuePipeline = (typeof QUEUE_PIPELINES)[number]

export const QUEUE_TRIGGERS = ['user', 'auto', 'retry', 'recovery', 'webhook', 'system'] as const

// ---------------------------------------------------------------------------
// Automation policy
// ---------------------------------------------------------------------------

export const AUTOMATION_WORKFLOWS = [
  'discovery',
  'funding',
  'application_prep',
  'application_submit',
  'outreach',
  'profile_refresh',
] as const
export type AutomationWorkflow = (typeof AUTOMATION_WORKFLOWS)[number]

export const AUTOMATION_MODES = ['off', 'suggest', 'prepare', 'auto_submit'] as const
export type AutomationMode = (typeof AUTOMATION_MODES)[number]

export const AUTOMATION_SCOPES = ['global', 'persona', 'workflow', 'company', 'portal'] as const

export const SENSITIVE_FIELD_POLICIES = [
  'never_answer',
  'ask_every_time',
  'use_saved_answer',
  'prefer_decline',
] as const
export type SensitiveFieldPolicy = (typeof SENSITIVE_FIELD_POLICIES)[number]

/** `evaluate_policy` decision reasons — `allowed`, or the first gate that refused. */
export const AUTOMATION_POLICY_REASONS = [
  'allowed',
  'server_disabled',
  'plan_locked',
  'consent_missing',
  'consent_withdrawn',
  'disclosure_version_changed',
  'policy_off',
  'daily_limit',
  'monthly_limit',
  'quota_exhausted',
  'quiet_hours',
  'company_blocked',
  'portal_not_allowed',
  'keyword_blocked',
  'location_excluded',
  'score_below_floor',
  'compensation_below_floor',
  'review_required',
  'expired',
] as const
export type AutomationPolicyReason = (typeof AUTOMATION_POLICY_REASONS)[number]

export const AUTOMATION_MODE_LABELS: Record<string, string> = {
  off: 'Off — never act for me',
  suggest: 'Suggest — show candidates, do nothing',
  prepare: 'Prepare — build everything, stop before anything is sent',
  auto_submit: 'Auto-submit — apply within my limits',
}

// ---------------------------------------------------------------------------
// Events & notifications
// ---------------------------------------------------------------------------

export const NOTIFICATION_SEVERITIES = [
  'info',
  'success',
  'warning',
  'error',
  'action_required',
] as const
export type NotificationSeverity = (typeof NOTIFICATION_SEVERITIES)[number]

export const NOTIFICATION_KINDS = [
  'high_match',
  'funding_match',
  'quota_exhausted',
  'weekly_summary',
  'automation_failed',
  'action_required',
  'onboarding_step_required',
  'profile_review_required',
  'resume_ready',
  'application_submitted',
  'application_outcome',
  'application_follow_up',
  'email_reply',
  'consent_expiring',
  'security',
] as const
export type NotificationKind = (typeof NOTIFICATION_KINDS)[number]

export const NOTIFICATION_CHANNELS = ['in_app', 'email'] as const

/** Kinds the user cannot mute: switching off 'we could not apply for you' would make the product silently do nothing. */
export const NOTIFICATION_UNMUTABLE_KINDS = [
  'action_required',
  'onboarding_step_required',
  'profile_review_required',
  'security',
] as const

/** `settings` keys under the `notifications` category. */
export const NOTIFICATION_PREFERENCE_KEYS = [
  'email_enabled',
  'high_match',
  'automation_failures',
  'weekly_summary',
  'new_replies',
] as const

/** Item kinds of `GET /api/actions/required` — the unified 'what do you owe us' read. */
// ---------------------------------------------------------------------------
// Outcome reporting — interviews and meaningful outcomes, not activity volume
// ---------------------------------------------------------------------------
// Mirror of `docs/REPORTING.md` / `contracts/07 §11.7`. The SPA renders the
// provenance and sufficiency labels from these; it must never compute a rate,
// downgrade an `estimated` number to a fact, or turn `not_enough_data` into 0%.

/** What kind of claim a reported number is. */
export const REPORT_PROVENANCE_KINDS = [
  'observed',
  'user_reported',
  'derived',
  'estimated',
] as const
export type ReportProvenance = (typeof REPORT_PROVENANCE_KINDS)[number]

/** How much data backs a number. `insufficient` means the number is withheld. */
export const REPORT_SUFFICIENCY = [
  'sufficient',
  'insufficient',
  'not_applicable',
] as const
export type ReportSufficiency = (typeof REPORT_SUFFICIENCY)[number]

/** The direction of a period-over-period comparison. `not_enough_data` ≠ flat. */
export const REPORT_TRENDS = [
  'improving',
  'declining',
  'flat',
  'not_enough_data',
] as const
export type ReportTrend = (typeof REPORT_TRENDS)[number]

/** Named report windows. `custom` means the caller supplied since/until. */
export const REPORT_RANGES = [
  'last_7_days',
  'last_30_days',
  'last_90_days',
  'this_week',
  'last_week',
  'custom',
] as const
export type ReportRange = (typeof REPORT_RANGES)[number]

/** What a report may be grouped by when comparing interview rates. */
export const REPORT_COMPARISON_DIMENSIONS = [
  'source',
  'score_band',
  'score_range',
  'role_family',
  'artifact',
] as const
export type ReportComparisonDimension = (typeof REPORT_COMPARISON_DIMENSIONS)[number]

/**
 * A job is "high fit" at or above this score — one number, one place, shared
 * with the backend's `HIGH_FIT_SCORE` and the dashboard's strong-match
 * threshold. It is an *estimate*: the UI labels it as one.
 */
export const HIGH_FIT_SCORE = 75.0

export const ACTION_KINDS = [
  'application_input',
  'resume_approval',
  'profile_review',
  'consent',
  'credential',
  'policy_override',
  'submission_confirm',
  'onboarding_gate',
  'quota',
  'security',
] as const
export type ActionKind = (typeof ACTION_KINDS)[number]

/** `domain_events.aggregate_type`. Ordering is guaranteed per aggregate, never across them. */
export const AGGREGATE_TYPES = [
  'application',
  'job',
  'resume_document',
  'profile',
  'discovery_run',
  'match_result',
  'onboarding_session',
  'automation_policy',
  'email',
  'notification',
  'queue_job',
  'persona',
  'user',
] as const
export type AggregateType = (typeof AGGREGATE_TYPES)[number]

/** The full backend event vocabulary. `RENDERED_EVENT_TYPES` below is the subset the SPA switches on; anything unknown must be ignored, not crashed on. */
export const EVENT_TYPES = [
  'onboarding.started',
  'onboarding.gate_completed',
  'onboarding.gate_skipped',
  'onboarding.gate_blocked',
  'onboarding.state_changed',
  'onboarding.completed',
  'onboarding.abandoned',
  'onboarding.resumed',
  'profile.created',
  'profile.version_superseded',
  'profile.field_extracted',
  'profile.field_confirmed',
  'profile.field_corrected',
  'profile.field_rejected',
  'profile.review_completed',
  'profile.archived',
  'extraction.started',
  'extraction.succeeded',
  'extraction.failed',
  'extraction.rejected_by_guardrail',
  'extraction.superseded',
  'resume.uploaded',
  'resume.generated',
  'resume.pending_approval',
  'resume.approved',
  'resume.rejected',
  'resume.polished',
  'resume.archived',
  'resume.deleted',
  'discovery.run_queued',
  'discovery.run_started',
  'discovery.source_fetched',
  'discovery.source_failed',
  'discovery.run_checkpointed',
  'discovery.run_completed',
  'discovery.run_empty',
  'discovery.run_cancelled',
  'discovery.job_persisted',
  'discovery.duplicate_skipped',
  'match.computed',
  'match.superseded',
  'match.stale',
  'match.high_score',
  'application.created',
  'application.queued',
  'application.preparation_started',
  'application.resume_selected',
  'application.form_detected',
  'application.fields_mapped',
  'application.field_ambiguous',
  'application.input_requested',
  'application.input_provided',
  'application.credential_created',
  'application.credential_reused',
  'application.plan_ready',
  'application.approved',
  'application.rejected_by_user',
  'application.blocked',
  'application.unblocked',
  'application.submission_started',
  'application.submitted',
  'application.submission_unverified',
  'application.submission_failed',
  'application.dry_run_completed',
  'application.marked_applied_manually',
  'application.outcome_recorded',
  'application.withdrawn',
  'application.expired',
  'application.cancelled',
  'application.retried',
  'application.dead_lettered',
  'application.tracking_opened',
  'application.state_changed',
  'application.state_corrected',
  'application.interview_reported',
  'application.interview_completed',
  'application.response_received',
  'application.rejection_received',
  'application.offer_received',
  'application.note_added',
  'application.follow_up_scheduled',
  'application.follow_up_completed',
  'application.attribution_updated',
  'application.snapshot_recorded',
  'application.suggestion_recorded',
  'application.session_started',
  'application.session_paused',
  'application.session_resumed',
  'application.session_expired',
  'application.session_completed',
  'application.session_cancelled',
  'application.action_required',
  'application.action_completed',
  'application.handoff_issued',
  'application.observation_recorded',
  'application.submission_refused',
  'automation.policy_changed',
  'automation.run_scheduled',
  'automation.run_skipped',
  'automation.quota_exhausted',
  'job.enqueued',
  'job.claimed',
  'job.paused',
  'job.resumed',
  'job.progressed',
  'job.checkpointed',
  'job.completed',
  'job.failed',
  'job.reclaimed',
  'job.dead_lettered',
  'job.cancelled',
  'account.created',
  'account.exported',
  'account.deleted',
  'consent.accepted',
  'consent.revoked',
  'disclosure.version_changed',
  'notification.created',
  'notification.read',
  'notification.dismissed',
] as const
export type EventType = (typeof EVENT_TYPES)[number]

/** `audit_logs.action`. Shipped names first — they are already in rows and must never be renamed. */
export const AUDIT_ACTIONS = [
  'account.deleted',
  'account.exported',
  'ai.config_updated',
  'ai.resume_requested',
  'auth.api_key_created',
  'auth.api_key_revoked',
  'auth.bootstrap',
  'auth.login',
  'auth.login_failed',
  'auth.logout',
  'auth.password_changed',
  'auth.refresh',
  'auth.register',
  'billing.canceled',
  'billing.checkout_created',
  'billing.downgraded',
  'billing.upgraded',
  'consent.accepted',
  'consent.revoked',
  'cover_letter.generated',
  'email.approved',
  'email.blocked',
  'email.generate_queued',
  'email.sent',
  'email.unsubscribed',
  'funding.processed',
  'funding.refresh_queued',
  'funding.scan',
  'funding.scan_failed',
  'job.applied',
  'job.input_submitted',
  'persona.activated',
  'persona.created',
  'persona.deleted',
  'persona.reflected',
  'persona.updated',
  'resume.approved',
  'resume.deleted',
  'resume.downloaded',
  'resume.generate_queued',
  'resume.generated',
  'resume.polished',
  'resume.rejected',
  'settings.updated',
  'vault.credential_created',
  'vault.deleted',
  'vault.exported',
  'vault.reveal_failed',
  'vault.viewed',
  'application.session_started',
  'application.session_paused',
  'application.session_resumed',
  'application.session_expired',
  'application.session_completed',
  'application.session_cancelled',
  'application.action_completed',
  'application.handoff_issued',
  'application.submission_reserved',
  'application.submission_refused',
  'account.created',
  'onboarding.started',
  'onboarding.completed',
  'onboarding.abandoned',
  'onboarding.gate_skipped',
  'onboarding.retried',
  'profile.updated',
  'profile.review_completed',
  'profile.archived',
  'extraction.succeeded',
  'extraction.failed',
  'resume.uploaded',
  'discovery.run_triggered',
  'discovery.run_cancelled',
  'job.apply_queued',
  'job.prepared',
  'job.submit_approved',
  'job.apply_failed',
  'job.apply_retried',
  'application.submitted',
  'application.submitted_auto',
  'application.submitted_unverified',
  'application.cancelled',
  'application.outcome_recorded',
  'application.tracking_opened',
  'application.status_updated',
  'application.interview_recorded',
  'application.state_corrected',
  'automation.policy_updated',
  'automation.policy_deleted',
  'automation.auto_submit_enabled',
  'automation.auto_submit_disabled',
  'analytics.report_exported',
] as const
export type AuditAction = (typeof AUDIT_ACTIONS)[number]

/** Prefixes/actions that are never dropped under load (superset of the backend's `SENSITIVE_ACTIONS`). */
export const SENSITIVE_AUDIT_ACTIONS = [
  'auth.',
  'vault.',
  'account.',
  'consent.',
  'resume.delete',
  'email.send',
  'application.submitted',
  'application.submitted_auto',
  'application.submitted_unverified',
  'automation.auto_submit_enabled',
  'onboarding.gate_skipped',
  'profile.archived',
] as const

/**
 * Event types the SPA may switch on. The backend list is longer (it includes
 * events no screen renders); anything unknown here must be ignored, not
 * crashed on — types are additive.
 */
export const RENDERED_EVENT_TYPES = [
  'application.session_started',
  'application.session_paused',
  'application.session_resumed',
  'application.session_expired',
  'application.session_completed',
  'application.session_cancelled',
  'application.action_required',
  'application.action_completed',
  'application.handoff_issued',
  'application.submission_refused',
  'onboarding.state_changed',
  'onboarding.gate_blocked',
  'onboarding.completed',
  'profile.field_extracted',
  'profile.field_confirmed',
  'profile.field_corrected',
  'profile.review_completed',
  'extraction.started',
  'extraction.succeeded',
  'extraction.failed',
  'extraction.rejected_by_guardrail',
  'resume.uploaded',
  'resume.generated',
  'resume.approved',
  'resume.rejected',
  'discovery.run_queued',
  'discovery.run_started',
  'discovery.run_completed',
  'discovery.run_empty',
  'discovery.source_failed',
  'match.computed',
  'match.high_score',
  'application.created',
  'application.queued',
  'application.preparation_started',
  'application.resume_selected',
  'application.form_detected',
  'application.fields_mapped',
  'application.field_ambiguous',
  'application.input_requested',
  'application.input_provided',
  'application.plan_ready',
  'application.approved',
  'application.blocked',
  'application.unblocked',
  'application.submission_started',
  'application.submitted',
  'application.submission_unverified',
  'application.submission_failed',
  'application.dry_run_completed',
  'application.marked_applied_manually',
  'application.outcome_recorded',
  'application.withdrawn',
  'application.expired',
  'application.cancelled',
  'application.retried',
  'application.dead_lettered',
  'application.tracking_opened',
  'application.state_changed',
  'application.state_corrected',
  'application.interview_reported',
  'application.interview_completed',
  'application.response_received',
  'application.rejection_received',
  'application.offer_received',
  'application.note_added',
  'application.follow_up_scheduled',
  'application.follow_up_completed',
  'application.attribution_updated',
  'application.snapshot_recorded',
  'application.suggestion_recorded',
  'automation.policy_changed',
  'automation.run_skipped',
  'automation.quota_exhausted',
  'job.paused',
  'job.resumed',
  'job.failed',
  'job.dead_lettered',
  'job.cancelled',
] as const

// ---------------------------------------------------------------------------
// Typed error codes (`detail.code`)
// ---------------------------------------------------------------------------

export const ERROR_CODES = [
  'not_authenticated',
  'credential_mismatch',
  'forbidden',
  'consent_required',
  'upgrade_required',
  'quota_exhausted',
  'storage_cap_reached',
  'rate_limited',
  'validation_error',
  'guardrail_failed',
  'fact_guard_failed',
  'ai_unavailable',
  'ai_paused',
  'ai_blocked',
  'profile_missing',
  'resume_missing',
  'resume_referenced',
  'job_not_found',
  'application_not_found',
  'application_already_submitted',
  'application_not_submittable',
  'tracking_not_found',
  'invalid_state_transition',
  'origin_not_trusted',
  'correction_reason_required',
  'duplicate_event',
  'session_expired',
  'action_required',
  'checkpoint_failed',
  'idempotency_conflict',
  'idempotency_replay',
  'onboarding_not_started',
  'onboarding_already_complete',
  'gate_not_satisfied',
  'field_needs_review',
  'restricted_field',
  'masked_api_key',
  'invalid_provider',
  'too_many_attempts',
  'weak_password',
  'unsupported_media_type',
  'payload_too_large',
  'internal_error',
] as const
export type ErrorCode = (typeof ERROR_CODES)[number]

/**
 * Human copy for an unknown-but-typed failure. Anything not listed here is
 * rendered from `detail.message`, which the backend always sends.
 */
export const ERROR_CODE_LABELS: Record<string, string> = {
  consent_required: 'A disclosure has to be accepted first.',
  upgrade_required: 'Your plan does not include this.',
  quota_exhausted: 'You have reached this limit for the period.',
  storage_cap_reached: 'Your board is full — delete rows or upgrade.',
  ai_paused: 'The AI provider is having an outage — this resumes on its own.',
  ai_blocked: 'The AI provider needs attention (key, billing, model).',
  profile_missing: 'Upload a master resume first.',
  application_already_submitted: 'This application was already submitted.',
  idempotency_replay: 'That request was already processed — showing the original result.',
  field_needs_review: 'Confirm this field before automation may use it.',
  restricted_field: 'This field is never filled in automatically.',
}

/** Progress step names per pipeline, in order. `progress.step` must come from this list. */
export const QUEUE_PROGRESS_STEPS: Record<string, readonly string[]> = {
  onboarding: [
    'validating',
    'stored',
    'extracting_text',
    'extracting_profile',
    'guardrails',
    'saving',
    'building_context',
    'done',
  ] as const,
  discovery: [
    'resolving_config',
    'fetching_sources',
    'deduplicating',
    'scoring',
    'classifying',
    'persisting',
    'detecting_forms',
    'done',
  ] as const,
  application: [
    'choosing_resume',
    'detecting_form',
    'mapping_fields',
    'preparing_credential',
    'building_plan',
    'awaiting_input',
    'navigating',
    'filling',
    'submitting',
    'verifying',
    'done',
  ] as const,
  extraction: [
    'parsing_document',
    'extracting_fields',
    'guardrails',
    'scoring_confidence',
    'writing_provenance',
    'done',
  ] as const,
  browser_session: [
    'opening',
    'observing',
    'filling',
    'awaiting_user',
    'resuming',
    'verifying_checkpoint',
    'done',
  ] as const,
}

/**
 * Every vocabulary above, keyed the way the backend's `VOCABULARY` dict is.
 * References the constants (never copies them), so this record cannot drift
 * from the lists it summarises.
 */
export const VOCABULARY: Record<string, readonly string[]> = {
  actor_types: ACTOR_TYPES,
  provenance_sources: PROVENANCE_SOURCES,
  confidence_bands: CONFIDENCE_BANDS,
  review_statuses: REVIEW_STATUSES,
  evidence_kinds: EVIDENCE_KINDS,
  sensitivity_levels: SENSITIVITY_LEVELS,
  sensitive_field_keys: SENSITIVE_FIELD_KEYS,
  restricted_field_keys: RESTRICTED_FIELD_KEYS,
  eeo_field_keys: EEO_FIELD_KEYS,
  profile_states: PROFILE_STATES,
  profile_link_kinds: PROFILE_LINK_KINDS,
  review_actions: REVIEW_ACTIONS,
  legacy_profile_extraction_sources: LEGACY_PROFILE_EXTRACTION_SOURCES,
  onboarding_states: ONBOARDING_STATES,
  onboarding_terminal_states: ONBOARDING_TERMINAL_STATES,
  onboarding_gate_states: ONBOARDING_GATE_STATES,
  resume_roles: RESUME_ROLES,
  resume_states: RESUME_STATES,
  extraction_states: EXTRACTION_STATES,
  application_phases: APPLICATION_PHASES,
  application_states: APPLICATION_STATES,
  application_terminal_states: APPLICATION_TERMINAL_STATES,
  application_user_action_states: APPLICATION_USER_ACTION_STATES,
  application_blocked_codes: APPLICATION_BLOCKED_CODES,
  submission_channels: SUBMISSION_CHANNELS,
  application_tracking_states: APPLICATION_TRACKING_STATES,
  tracking_phases: TRACKING_PHASES,
  tracking_terminal_states: TRACKING_TERMINAL_STATES,
  tracking_interview_states: TRACKING_INTERVIEW_STATES,
  tracking_origins: TRACKING_ORIGINS,
  tracking_trusted_origins: TRACKING_TRUSTED_ORIGINS,
  email_inferred_tracking_states: EMAIL_INFERRED_TRACKING_STATES,
  tracking_trusted_only_states: TRACKING_TRUSTED_ONLY_STATES,
  tracking_artifact_kinds: TRACKING_ARTIFACT_KINDS,
  tracking_manual_sources: TRACKING_MANUAL_SOURCES,
  interview_formats: INTERVIEW_FORMATS,
  response_channels: RESPONSE_CHANNELS,
  interview_self_assessments: INTERVIEW_SELF_ASSESSMENTS,
  application_session_states: APPLICATION_SESSION_STATES,
  application_session_terminal_states: APPLICATION_SESSION_TERMINAL_STATES,
  application_session_user_action_states: APPLICATION_SESSION_USER_ACTION_STATES,
  user_action_kinds: USER_ACTION_KINDS,
  user_action_statuses: USER_ACTION_STATUSES,
  checkpoint_failures: CHECKPOINT_FAILURES,
  field_classifications: FIELD_CLASSIFICATIONS,
  field_actions: FIELD_ACTIONS,
  submission_states: SUBMISSION_STATES,
  submission_refusals: SUBMISSION_REFUSALS,
  job_statuses: JOB_STATUSES,
  ambiguity_kinds: AMBIGUITY_KINDS,
  field_resolutions: FIELD_RESOLUTIONS,
  autofill_value_sources: AUTOFILL_VALUE_SOURCES,
  answer_reuse_scopes: ANSWER_REUSE_SCOPES,
  score_sources: SCORE_SOURCES,
  match_bands: MATCH_BANDS,
  match_staleness: MATCH_STALENESS,
  match_feedback_kinds: MATCH_FEEDBACK_KINDS,
  match_feedback_outcome_kinds: MATCH_FEEDBACK_OUTCOME_KINDS,
  match_filters: MATCH_FILTERS,
  match_features: MATCH_FEATURES,
  match_recommendations: MATCH_RECOMMENDATIONS,
  match_requirement_basis: MATCH_REQUIREMENT_BASIS,
  discovery_run_states: DISCOVERY_RUN_STATES,
  discovery_why_empty: DISCOVERY_WHY_EMPTY,
  discovery_ai_skip_reasons: DISCOVERY_AI_SKIP_REASONS,
  queue_statuses: QUEUE_STATUSES,
  queue_live_statuses: QUEUE_LIVE_STATUSES,
  queue_terminal_statuses: QUEUE_TERMINAL_STATUSES,
  queue_pipelines: QUEUE_PIPELINES,
  queue_triggers: QUEUE_TRIGGERS,
  automation_workflows: AUTOMATION_WORKFLOWS,
  automation_modes: AUTOMATION_MODES,
  automation_scopes: AUTOMATION_SCOPES,
  sensitive_field_policies: SENSITIVE_FIELD_POLICIES,
  automation_policy_reasons: AUTOMATION_POLICY_REASONS,
  event_types: EVENT_TYPES,
  aggregate_types: AGGREGATE_TYPES,
  audit_actions: AUDIT_ACTIONS,
  sensitive_audit_actions: SENSITIVE_AUDIT_ACTIONS,
  notification_severities: NOTIFICATION_SEVERITIES,
  notification_kinds: NOTIFICATION_KINDS,
  notification_channels: NOTIFICATION_CHANNELS,
  notification_unmutable_kinds: NOTIFICATION_UNMUTABLE_KINDS,
  notification_preference_keys: NOTIFICATION_PREFERENCE_KEYS,
  action_kinds: ACTION_KINDS,
  report_provenance_kinds: REPORT_PROVENANCE_KINDS,
  report_sufficiency: REPORT_SUFFICIENCY,
  report_trends: REPORT_TRENDS,
  report_ranges: REPORT_RANGES,
  report_comparison_dimensions: REPORT_COMPARISON_DIMENSIONS,
  error_codes: ERROR_CODES,
}
