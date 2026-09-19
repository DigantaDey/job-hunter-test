/**
 * Application tracking — the lifecycle & outcome-feedback layer's client.
 *
 * The shapes here are the `/api/application-tracking` contract exactly as
 * `app.api.routers.application_tracking` returns them. Nothing is recomputed
 * locally: the state labels, the allowed transitions, the buttons and the
 * conversion rates all arrive from the server (`GET …/meta`, and `actions[]` /
 * `transitions` on every document), so two screens cannot word the same fact
 * differently and a state this build has never seen still renders.
 *
 * What *is* local is presentation only: turning an ISO timestamp into "in 3
 * days", picking a chip colour, and slicing a timeline for a compact panel.
 *
 * Two rules the UI has to honour, both from the contract set:
 *
 *   • `origin` is part of every fact. "System observed" (our own machinery sent
 *     it and saw the receipt), "You reported" (typed in this app) and
 *     "Inferred from email — unconfirmed" are different claims, and the badge
 *     travels with the row that carries it. An email inference is never
 *     rendered as an interview: the backend refuses to store one, and the only
 *     thing it can produce here is a *suggestion* the user confirms or dismisses.
 *   • a rate of `null` is not 0%. "No applications yet" and "applications, no
 *     interviews" are different facts (`rateText` keeps them apart).
 */
import client from '../api/client'
import {
  TRACKING_EVENT_LABELS,
  TRACKING_ORIGIN_LABELS,
  TRACKING_STATE_CHIP,
  TRACKING_STATE_LABELS,
  countsAsInterview,
  isTerminalTrackingState,
  trackingStateLabel,
} from './contracts'

export const TRACKING_API = '/api/application-tracking'

// --------------------------------------------------------------------------- //
// The wire shapes
// --------------------------------------------------------------------------- //
export interface TrackingJobBrief {
  id: number
  title: string
  company: string
  url: string
  status: string | null
  source: string
}

/** One append-only timeline row (`contracts/09`). Immutable once written. */
export interface TrackingEvent {
  id: number
  event_id: string
  sequence: number
  event_type: string
  state_from: string | null
  state_from_label: string | null
  state_to: string | null
  state_to_label: string | null
  phase: string | null
  /** Who is making this claim — the point of the whole layer. */
  origin: string
  actor_type: string
  actor_label: string
  trigger: string
  severity: string
  message: string
  note: string
  payload: Record<string, unknown>
  evidence: { kind: string; locator?: string; captured_at?: string }[]
  is_correction: boolean
  correction_of_event_id: number | null
  correction_reason: string
  is_provisional: boolean
  follow_up_at: string | null
  occurred_at: string | null
  recorded_at: string | null
  /** Reported long after it happened — the gap is information, not noise. */
  late_reported: boolean
}

/** A button the server says may be rendered (`contracts/13 §7`). */
export interface TrackingAction {
  key: string
  label: string
  method: string
  route: string
  primary: boolean
  disabled: boolean
  disabled_reason: string | null
}

export interface TrackingDocument {
  tracking_id: number
  job_id: number
  job: TrackingJobBrief
  state: string
  state_label: string
  phase: string
  phase_label: string
  /** The projection onto the board's own status column. */
  job_status: string
  state_origin: string
  is_provisional: boolean
  provisional_evidence: Record<string, unknown>
  attribution: { source: string; channel: string | null; role_family: string | null }
  snapshots: {
    match: {
      score: number | null
      band: string | null
      score_source: string | null
      scorer_version: string | null
      match_id: number | null
    }
    artifact: {
      kind: string
      id: number | null
      version: number | null
      label: string | null
      sha256: string | null
    }
  }
  timestamps: {
    applied_at: string | null
    first_response_at: string | null
    interview_scheduled_at: string | null
    interview_at: string | null
    interview_completed_at: string | null
    outcome_at: string | null
    closed_at: string | null
    created_at: string | null
    updated_at: string | null
  }
  durations_days: {
    applied_to_first_response: number | null
    applied_to_interview: number | null
    applied_to_outcome: number | null
  }
  follow_up: {
    due_at: string | null
    note: string
    notified_at: string | null
    completed_at: string | null
    overdue: boolean
    pending: boolean
  }
  counts: { events: number; corrections: number; last_sequence: number }
  note: string
  transitions: { allowed: string[]; allowed_labels: string[]; terminal: boolean }
  timeline: TrackingEvent[]
  actions: TrackingAction[]
  server_time: string
  /** Present on `GET …/{id}` and `GET …/job/{id}`. */
  exists?: boolean
}

/** The conversion tally — the same keys at total and group level. */
export interface TrackingTally {
  tracked: number
  applied: number
  responses: number
  interviews: number
  offers: number
  rejections: number
  withdrawn: number
  provisional: number
  corrections: number
  /** `null` when there is no denominator: never rendered as 0%. */
  application_to_interview_rate: number | null
  applied_to_response_rate: number | null
  interview_to_offer_rate: number | null
  avg_match_score: number | null
  avg_days_to_interview: number | null
  by_origin: Record<string, number>
}

export interface TrackingReportGroup extends TrackingTally {
  key: string
  label: string
  records: number
}

export interface TrackingReport {
  group_by: string
  window: { since: string | null; until: string | null }
  filters: { origin: string | null; state: string | null; include_unapplied: boolean }
  totals: TrackingTally
  groups: TrackingReportGroup[]
  group_count: number
  disclaimer: string
  interview_definition: string[]
  server_time: string
}

export interface TrackingListResponse {
  items: TrackingDocument[]
  page: number
  page_size: number
  total: number
  has_more: boolean
  counts: Record<string, number>
  states: string[]
  state_labels: Record<string, string>
  server_time: string
}

export interface TrackingMeta {
  states: string[]
  state_labels: Record<string, string>
  phases: string[]
  origins: string[]
  transitions: Record<string, string[]>
  terminal_states: string[]
  interview_states: string[]
  manual_sources: string[]
  channels: string[]
  interview_formats: string[]
  self_assessments: string[]
  response_channels: string[]
  report_group_keys: string[]
  counts: Record<string, number>
  disclaimer: string
}

export interface FollowUpItem {
  tracking_id: number
  job_id: number
  title: string
  company: string
  state: string
  state_label: string
  due_at: string | null
  note: string
  overdue: boolean
  notified_at: string | null
  route: string
}

export interface FollowUpsResponse {
  items: FollowUpItem[]
  sweep: { due: number; notified: number; skipped: number }
  notification_kind: string
  server_time: string
}

export interface TrackingEventsResponse {
  tracking_id: number
  items: TrackingEvent[]
  last_sequence: number
  has_more: boolean
  count: number
  corrections: number
  server_time: string
}

/** `GET …/job/{id}` for a job nobody tracks yet: an honest "not tracked". */
export interface UntrackedJob {
  exists: false
  job: TrackingJobBrief
  actions: TrackingAction[]
  snapshot: Record<string, unknown>
  server_time: string
}

export type JobTrackingLookup = UntrackedJob | (TrackingDocument & { exists: true })

/** The one shape every write answers with. */
export interface TrackingWriteResponse {
  ok: boolean
  /** True when the write was a replay of a fact already on the timeline. */
  duplicate: boolean
  state_changed: boolean
  event: TrackingEvent | null
  events: TrackingEvent[]
  document: TrackingDocument
  state: string
  phase: string
  server_time: string
}

// --------------------------------------------------------------------------- //
// Request bodies — field names the backend declares, nothing invented
// --------------------------------------------------------------------------- //
export interface OpenTrackingBody {
  job_id: number
  source?: string | null
  channel?: string | null
  note?: string | null
  state?: string
  occurred_at?: string | null
}

export interface StatusBody {
  state: string
  note?: string | null
  occurred_at?: string | null
  follow_up_at?: string | null
  follow_up_note?: string
  origin?: string
  channel?: string | null
  source?: string | null
}

export interface InterviewBody {
  interview_at?: string | null
  format?: string
  round_name?: string
  interviewer_role?: string
  location?: string
  note?: string | null
  occurred_at?: string | null
  follow_up_at?: string | null
  follow_up_note?: string
}

export interface InterviewCompleteBody {
  self_assessment?: string
  next_step?: string
  note?: string | null
  occurred_at?: string | null
  follow_up_at?: string | null
  follow_up_note?: string
}

export interface ResponseBody {
  channel?: string
  response_kind?: string
  summary?: string
  note?: string | null
  occurred_at?: string | null
  follow_up_at?: string | null
  follow_up_note?: string
}

export interface OutcomeBody {
  reason?: string
  note?: string | null
  occurred_at?: string | null
}

export interface CorrectionBody {
  state: string
  /** Required by the contract; the refusal is `correction_reason_required`. */
  reason: string
  correction_of_event_id?: number | null
  note?: string | null
  occurred_at?: string | null
}

export interface AttributionBody {
  source?: string | null
  channel?: string | null
  reason?: string
}

export interface FollowUpBody {
  follow_up_at: string
  note?: string
}

export interface ListTrackingParams {
  state?: string
  phase?: string
  source?: string
  job_id?: number
  q?: string
  due_only?: boolean
  page?: number
  page_size?: number
}

export interface ReportParams {
  group_by?: string
  since?: string
  until?: string
  origin?: string
  state?: string
  include_unapplied?: boolean
}

// --------------------------------------------------------------------------- //
// Reads
// --------------------------------------------------------------------------- //
/**
 * Send the caller's retry key when it has one, so a re-sent write is answered
 * with the first result instead of becoming a second fact.
 */
function idempotency(key?: string | null): Record<string, string> | undefined {
  return key ? { 'Idempotency-Key': key } : undefined
}

export async function listTracking(params: ListTrackingParams = {}): Promise<TrackingListResponse> {
  const { data } = await client.get<TrackingListResponse>(TRACKING_API, { params })
  return data
}

export async function getTrackingMeta(): Promise<TrackingMeta> {
  const { data } = await client.get<TrackingMeta>(`${TRACKING_API}/meta`)
  return data
}

export async function getTrackingReport(params: ReportParams = {}): Promise<TrackingReport> {
  const { data } = await client.get<TrackingReport>(`${TRACKING_API}/report`, { params })
  return data
}

/** The reminder agenda. The read also runs the sweep, so due reminders exist. */
export async function getFollowUps(days = 14, limit = 50): Promise<FollowUpsResponse> {
  const { data } = await client.get<FollowUpsResponse>(`${TRACKING_API}/follow-ups`,
    { params: { days, limit } })
  return data
}

export async function getTracking(trackingId: number, timelineLimit = 50): Promise<TrackingDocument> {
  const { data } = await client.get<TrackingDocument>(`${TRACKING_API}/${trackingId}`,
    { params: { timeline_limit: timelineLimit } })
  return data
}

/** The Jobs drawer's read: `exists: false` is an answer, not an error. */
export async function getTrackingForJob(jobId: number): Promise<JobTrackingLookup> {
  const { data } = await client.get<JobTrackingLookup>(`${TRACKING_API}/job/${jobId}`)
  return data
}

export async function getTrackingEvents(
  trackingId: number,
  params: { after_sequence?: number; limit?: number; order?: 'asc' | 'desc' } = {},
): Promise<TrackingEventsResponse> {
  const { data } = await client.get<TrackingEventsResponse>(`${TRACKING_API}/${trackingId}/events`,
    { params })
  return data
}

// --------------------------------------------------------------------------- //
// Writes — one function per fact the user may assert
// --------------------------------------------------------------------------- //
export async function openTracking(body: OpenTrackingBody,
                                   key?: string | null): Promise<TrackingWriteResponse> {
  const { data } = await client.post<TrackingWriteResponse>(TRACKING_API, body,
    { headers: idempotency(key) })
  return data
}

export async function updateStatus(trackingId: number, body: StatusBody,
                                   key?: string | null): Promise<TrackingWriteResponse> {
  const { data } = await client.post<TrackingWriteResponse>(`${TRACKING_API}/${trackingId}/status`,
    body, { headers: idempotency(key) })
  return data
}

/** "I got an interview." The single most important write on this page. */
export async function recordInterview(trackingId: number, body: InterviewBody = {},
                                      key?: string | null): Promise<TrackingWriteResponse> {
  const { data } = await client.post<TrackingWriteResponse>(`${TRACKING_API}/${trackingId}/interview`,
    body, { headers: idempotency(key) })
  return data
}

export async function completeInterview(trackingId: number, body: InterviewCompleteBody = {},
                                        key?: string | null): Promise<TrackingWriteResponse> {
  const { data } = await client.post<TrackingWriteResponse>(
    `${TRACKING_API}/${trackingId}/interview-complete`, body, { headers: idempotency(key) })
  return data
}

/** A recruiter replied. Recorded as a response — never promoted to an interview. */
export async function recordResponse(trackingId: number, body: ResponseBody = {},
                                     key?: string | null): Promise<TrackingWriteResponse> {
  const { data } = await client.post<TrackingWriteResponse>(`${TRACKING_API}/${trackingId}/response`,
    body, { headers: idempotency(key) })
  return data
}

export async function recordRejection(trackingId: number, body: OutcomeBody = {},
                                      key?: string | null): Promise<TrackingWriteResponse> {
  const { data } = await client.post<TrackingWriteResponse>(`${TRACKING_API}/${trackingId}/rejection`,
    body, { headers: idempotency(key) })
  return data
}

export async function recordOffer(trackingId: number, body: OutcomeBody = {},
                                  key?: string | null): Promise<TrackingWriteResponse> {
  const { data } = await client.post<TrackingWriteResponse>(`${TRACKING_API}/${trackingId}/offer`,
    body, { headers: idempotency(key) })
  return data
}

export async function recordWithdrawal(trackingId: number, body: OutcomeBody = {},
                                       key?: string | null): Promise<TrackingWriteResponse> {
  const { data } = await client.post<TrackingWriteResponse>(`${TRACKING_API}/${trackingId}/withdraw`,
    body, { headers: idempotency(key) })
  return data
}

export async function addNote(trackingId: number, note: string,
                              key?: string | null): Promise<TrackingWriteResponse> {
  const { data } = await client.post<TrackingWriteResponse>(`${TRACKING_API}/${trackingId}/note`,
    { note }, { headers: idempotency(key) })
  return data
}

export async function setFollowUp(trackingId: number, body: FollowUpBody,
                                  key?: string | null): Promise<TrackingWriteResponse> {
  const { data } = await client.post<TrackingWriteResponse>(`${TRACKING_API}/${trackingId}/follow-up`,
    body, { headers: idempotency(key) })
  return data
}

export async function completeFollowUp(trackingId: number,
                                       note?: string | null): Promise<TrackingWriteResponse> {
  const { data } = await client.post<TrackingWriteResponse>(
    `${TRACKING_API}/${trackingId}/follow-up/complete`, { note: note ?? null })
  return data
}

export async function cancelFollowUp(trackingId: number): Promise<TrackingWriteResponse> {
  const { data } = await client.delete<TrackingWriteResponse>(`${TRACKING_API}/${trackingId}/follow-up`)
  return data
}

/** A manual correction: the transition table does not apply, a reason does. */
export async function correctState(trackingId: number, body: CorrectionBody,
                                   key?: string | null): Promise<TrackingWriteResponse> {
  const { data } = await client.post<TrackingWriteResponse>(`${TRACKING_API}/${trackingId}/correction`,
    body, { headers: idempotency(key) })
  return data
}

export async function updateAttribution(trackingId: number, body: AttributionBody,
                                        key?: string | null): Promise<TrackingWriteResponse> {
  const { data } = await client.post<TrackingWriteResponse>(
    `${TRACKING_API}/${trackingId}/attribution`, body, { headers: idempotency(key) })
  return data
}

/** Re-read the frozen match-score / artefact snapshots. No body: nothing to say. */
export async function refreshSnapshot(trackingId: number): Promise<TrackingWriteResponse> {
  const { data } = await client.post<TrackingWriteResponse>(`${TRACKING_API}/${trackingId}/snapshot`)
  return data
}

export async function confirmSuggestion(trackingId: number,
                                        body: { reason?: string; state?: string | null; note?: string | null } = {},
                                        ): Promise<TrackingWriteResponse> {
  const { data } = await client.post<TrackingWriteResponse>(
    `${TRACKING_API}/${trackingId}/suggestion/confirm`, body)
  return data
}

export async function dismissSuggestion(trackingId: number,
                                        body: { reason?: string } = {}): Promise<TrackingWriteResponse> {
  const { data } = await client.post<TrackingWriteResponse>(
    `${TRACKING_API}/${trackingId}/suggestion/dismiss`, body)
  return data
}

// --------------------------------------------------------------------------- //
// Presentation helpers (pure — these are what the tests pin)
// --------------------------------------------------------------------------- //
export function parseWhen(iso?: string | null): Date | null {
  if (!iso) return null
  const when = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`)
  return Number.isNaN(when.getTime()) ? null : when
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

const pad = (value: number): string => String(value).padStart(2, '0')

/**
 * "19 Sep 2026, 14:00" — the timestamps are UTC on the wire and shown as UTC.
 *
 * Built from the UTC getters rather than `Intl.DateTimeFormat` on purpose: the
 * rendered month name is otherwise a function of the browser's ICU build ("Sep"
 * vs "Sept"), and a fact's timestamp should not be spelled differently on two
 * machines. UTC (not browser-local) because every other time on this page is
 * UTC too, and mixing them would put an interview an hour off for a traveller.
 */
export function formatWhen(iso?: string | null): string {
  const when = parseWhen(iso)
  if (!when) return '—'
  return `${pad(when.getUTCDate())} ${MONTHS[when.getUTCMonth()]} ${when.getUTCFullYear()}, ` +
    `${pad(when.getUTCHours())}:${pad(when.getUTCMinutes())}`
}

/** Just the date part, for an interview slot or a follow-up day. */
export function formatDay(iso?: string | null): string {
  const when = parseWhen(iso)
  if (!when) return '—'
  return `${WEEKDAYS[when.getUTCDay()]}, ${pad(when.getUTCDate())} ${MONTHS[when.getUTCMonth()]} ` +
    `${when.getUTCFullYear()}`
}

/**
 * How far away a moment is, in the words a person uses: "3 days ago",
 * "today", "in 2 weeks". Falls back to the date when it is further out than a
 * year, where a relative phrase stops being useful.
 */
export function relativeLabel(iso?: string | null, now: Date = new Date()): string {
  const when = parseWhen(iso)
  if (!when) return '—'
  const days = Math.round((when.getTime() - now.getTime()) / 86_400_000)
  if (days === 0) return 'today'
  if (days === 1) return 'tomorrow'
  if (days === -1) return 'yesterday'
  const abs = Math.abs(days)
  if (abs > 365) return formatDay(iso)
  const unit = abs >= 14 ? `${Math.round(abs / 7)} weeks` : `${abs} days`
  return days > 0 ? `in ${unit}` : `${unit} ago`
}

/** The follow-up line: what is promised, when, and whether it is already late. */
export function dueLabel(followUp: TrackingDocument['follow_up'] | null | undefined,
                         now: Date = new Date()): string {
  if (!followUp || !followUp.pending) return 'No follow-up set'
  const when = relativeLabel(followUp.due_at, now)
  // `relativeLabel` already says "3 days ago" for a past date, so an overdue
  // reminder reads as one sentence rather than "overdue — in -3 days".
  return followUp.overdue ? `Overdue — was due ${when}` : `Due ${when}`
}

/** The badge that says who is claiming this fact. Never dropped, never merged. */
export function originBadge(origin?: string | null): { label: string; title: string; className: string } {
  const value = origin || 'user_reported'
  const label = TRACKING_ORIGIN_LABELS[value] || value
  const styles: Record<string, string> = {
    system_observed: 'bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300',
    user_reported: 'bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300',
    email_inferred: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
    verified_integration: 'bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-300',
  }
  const titles: Record<string, string> = {
    system_observed: 'JobHunter sent this application and saw the portal’s own confirmation',
    user_reported: 'You typed this in — it is your account of what happened',
    email_inferred: 'A guess from a message. Nothing was recorded as an interview; confirm or dismiss it',
    verified_integration: 'An integration you connected reported this',
  }
  return { label, title: titles[value] || '', className: styles[value] || styles.user_reported }
}

export function stateChip(state?: string | null): string {
  return TRACKING_STATE_CHIP[state || ''] || 'bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300'
}

/**
 * A rate as text. `null` means "no denominator" and must never render as 0%:
 * a dashboard that shows 0% for somebody who has not applied yet is telling
 * them their applications are failing.
 */
export function rateText(rate?: number | null, empty = '—'): string {
  if (rate === null || rate === undefined) return empty
  return `${rate}%`
}

/** The conversion sentence the report header uses. */
export function conversionText(totals?: TrackingTally | null): string {
  if (!totals || !totals.applied) return 'No applications recorded yet'
  const rate = rateText(totals.application_to_interview_rate)
  return `${totals.interviews} of ${totals.applied} applications reached an interview (${rate})`
}

/** One timeline row's headline: what happened, in the contract's own words. */
export function eventHeadline(event: TrackingEvent): string {
  if (event.is_correction) {
    const from = event.state_from_label || trackingStateLabel(event.state_from)
    const to = event.state_to_label || trackingStateLabel(event.state_to)
    return event.state_to ? `Corrected ${from} → ${to}` : 'Correction'
  }
  if (event.state_to) {
    return event.state_from
      ? `${event.state_from_label || trackingStateLabel(event.state_from)} → ${event.state_to_label || trackingStateLabel(event.state_to)}`
      : event.state_to_label || trackingStateLabel(event.state_to)
  }
  return TRACKING_EVENT_LABELS[event.event_type] || event.event_type
}

/** The newest `limit` rows, oldest first — the compact panel's slice. */
export function timelineSlice(document: TrackingDocument | null | undefined, limit = 5): TrackingEvent[] {
  const rows = document?.timeline || []
  return limit > 0 ? rows.slice(-limit) : rows
}

/** The server's own button, by key — the client renders it, it does not reason. */
export function actionFor(document: TrackingDocument | null | undefined, key: string): TrackingAction | null {
  return (document?.actions || []).find((action) => action.key === key) || null
}

export function isProvisional(document: TrackingDocument | null | undefined): boolean {
  return !!document?.is_provisional
}

/** Whether "I got an interview" may be pressed right now. */
export function canRecordInterview(document: TrackingDocument | null | undefined): boolean {
  const action = actionFor(document, 'interview')
  return !!action && !action.disabled
}

export function isClosed(document: TrackingDocument | null | undefined): boolean {
  return !!document?.transitions?.terminal || isTerminalTrackingState(document?.state)
}

export function reachedInterview(document: TrackingDocument | null | undefined): boolean {
  return countsAsInterview(document?.state)
}

/** The frozen artefact, named the way the report groups it. */
export function artifactLabel(document: TrackingDocument | null | undefined): string {
  const artifact = document?.snapshots?.artifact
  if (!artifact || artifact.kind === 'none') return 'No artefact snapshot'
  const version = artifact.version === null || artifact.version === undefined ? '' : ` v${artifact.version}`
  const kind = artifact.kind === 'packet' ? 'Application packet'
    : artifact.kind === 'resume_document' ? 'Resume document' : 'Resume'
  return `${kind}${version}${artifact.label ? ` — ${artifact.label}` : ''}`
}

/** The frozen match score with its provenance, or an honest "not scored". */
export function matchLabel(document: TrackingDocument | null | undefined): string {
  const match = document?.snapshots?.match
  if (!match || match.score === null || match.score === undefined) return 'No match score recorded'
  const band = match.band ? ` (${match.band})` : ''
  const source = match.score_source === 'ai' ? 'AI verified'
    : match.score_source === 'preliminary' ? 'keyword estimate' : match.score_source || 'unscored source'
  return `${match.score}${band} — ${source}`
}

/** Group key → the words on screen (report rows keep the raw key too). */
export function groupLabel(key: string, groupBy: string): string {
  if (groupBy === 'state') return TRACKING_STATE_LABELS[key] || trackingStateLabel(key)
  if (key === 'unknown') return 'Unknown'
  if (key === 'none') return groupBy.startsWith('artifact') ? 'No artefact' : 'Not recorded'
  if (key === 'unscored') return 'Not scored'
  return key
}

/** The report's dimension picker, in the order the contract lists them. */
export const REPORT_DIMENSIONS: { key: string; label: string }[] = [
  { key: 'source', label: 'Source' },
  { key: 'role', label: 'Role family' },
  { key: 'match_band', label: 'Match band' },
  { key: 'score_bucket', label: 'Match score' },
  { key: 'artifact_version', label: 'Artefact version' },
  { key: 'artifact_kind', label: 'Artefact kind' },
  { key: 'channel', label: 'Submission channel' },
  { key: 'state', label: 'Current status' },
]

/** A date-time input's value (`YYYY-MM-DDTHH:mm`) as the UTC ISO the API wants. */
export function toIsoUtc(local: string): string | null {
  if (!local) return null
  const when = new Date(local)
  if (Number.isNaN(when.getTime())) return null
  return when.toISOString().replace(/\.\d{3}Z$/, 'Z')
}

/** The reverse: an ISO timestamp as a `datetime-local` input's value. */
export function toLocalInput(iso?: string | null): string {
  const when = parseWhen(iso)
  if (!when) return ''
  return `${when.getUTCFullYear()}-${pad(when.getUTCMonth() + 1)}-${pad(when.getUTCDate())}` +
    `T${pad(when.getUTCHours())}:${pad(when.getUTCMinutes())}`
}
