/**
 * Outcome reporting — the client half of `docs/REPORTING.md`.
 *
 * The wire shapes here are exactly what `GET /api/analytics/outcomes` (and its
 * `/weekly` and `/export` siblings) return. Nothing is recomputed locally: the
 * rates arrive already gated on the server's minimum sample sizes, so the SPA
 * cannot divide 1 interview by 2 applications and call the result a rate.
 *
 * Three presentation rules the helpers below exist to keep:
 *
 *   • `null` is not `0`. A withheld rate renders `—` with the reason attached
 *     (`rateText`, `sufficiencyLabel`).
 *   • `estimated` is not `observed`. A match score is a model's ranking and the
 *     chip says so (`provenanceBadge`).
 *   • `not_enough_data` is not `flat`. "We cannot tell yet" and "nothing
 *     changed" are different answers (`trendLabel`).
 */
import client from '../api/client'
import {
  REPORT_PROVENANCE_KINDS,
  REPORT_RANGES,
  REPORT_SUFFICIENCY,
  REPORT_TRENDS,
  type ReportComparisonDimension,
  type ReportProvenance,
  type ReportRange,
  type ReportSufficiency,
  type ReportTrend,
} from './contracts'

export type { ReportComparisonDimension, ReportProvenance, ReportRange, ReportSufficiency, ReportTrend }

export const REPORTING_API = '/api/analytics/outcomes'

// --------------------------------------------------------------------------- //
// The wire shapes
// --------------------------------------------------------------------------- //
export interface ReportWindow {
  range: string
  timezone: string
  start_utc: string
  end_utc: string
  start_local: string
  end_local: string
  start_date: string
  end_date: string
  days: number
  boundary: string
  label: string
}

export interface ReportSample {
  n: number
  minimum: number
  state: ReportSufficiency
  sufficient: boolean
  label: string
}

export interface ReportMetric {
  key: string
  label: string
  value: number | null
  unit: 'count' | 'percent' | 'days'
  provenance: ReportProvenance
  sample: ReportSample
  sufficiency: ReportSufficiency
  note: string
}

export interface ReportGroup {
  key: string
  label: string
  applied: number
  responses: number
  interviews: number
  offers: number
  rejections: number
  withdrawn: number
  corrections: number
  events: number
  /** `null` whenever the group is below the minimum sample. */
  interview_rate: number | null
  response_rate: number | null
  rejection_rate: number | null
  offer_rate: number | null
  avg_match_score: number | null
  by_origin: Record<string, number>
  sample: ReportSample
  sufficiency: ReportSufficiency
  insufficient_reason: string | null
}

export interface ReportComparison {
  dimension: string
  minimum_sample: number
  groups: ReportGroup[]
  group_count: number
  sufficient_group_count: number
  withheld_group_count: number
  best: { key: string; label: string; interview_rate: number | null; applied: number; interviews: number } | null
  truncated?: boolean
}

export interface ReportTrendBlock {
  direction: ReportTrend
  headline: string
  compared_with: ReportWindow
  measured: boolean
  sample: { current: ReportSample; previous: ReportSample }
  metrics: Record<string, { current: number | null; previous: number | null; delta: number | null }>
  verdict_basis: string
  flat_threshold_percentage_points: number
  note: string
}

export interface ReportCalibration {
  available: boolean
  interview_probability: number | null
  missing: string[]
  observed: {
    recorded_interviews: number
    score_bands_with_outcomes: number
    share_verified_outcomes: number | null
  }
  requires: Record<string, number | string | null>
  note: string
}

export interface OutcomeReport {
  report_version: string
  generated_at: string
  calculation_id: string
  timezone: string
  window: ReportWindow
  previous_window: ReportWindow
  definitions: {
    report_version: string
    minimum_sample_sizes: Record<string, number>
    high_fit_score: number
    interview_states: string[]
    formulae: Record<string, string>
  }
  metrics: ReportMetric[]
  funnel: Record<string, number>
  totals: Record<string, number | null | ReportSample | string> & { sample?: ReportSample }
  comparisons: Record<string, ReportComparison>
  timing: {
    days: number | null
    median_days: number | null
    mean_days: number | null
    p90_days: number | null
    applications_measured: number
    applications_missing_posting_date: number
    applications_dated_before_posting: number
    provenance: ReportProvenance
    sample: ReportSample
    sufficiency: ReportSufficiency
    note: string
  }
  quality: {
    tracking_coverage_percent: number | null
    applications_without_a_tracking_record: number
    provisional_records: number
    corrections: number
    events: number
    correction_rate: number | null
    by_origin: Record<string, number>
    application_attempts: number
    application_failures: number
    application_failure_rate: number | null
    note: string
  }
  trend: ReportTrendBlock
  calibration: ReportCalibration
  privacy: Record<string, unknown> & { scope: string; includes_other_users: boolean }
  export: {
    allowed: boolean
    requirements: string[]
    unmet: string[]
    formats: string[]
    note: string
  }
  signal: { kind: string; headline: string; withheld_group_count: number }
  headline: string
  disclaimer: string
  server_time: string
  is_pro?: boolean
  upgrade_hint?: string
}

export interface WeeklyOutcome {
  headline: string
  trend: Pick<ReportTrendBlock, 'direction' | 'headline' | 'metrics' | 'compared_with'>
  metrics: Record<string, ReportMetric>
  totals: OutcomeReport['totals']
  sufficiency: ReportSample
  range: ReportWindow
  previous_range: ReportWindow
  timezone: string
  calculation_id: string
  calibration: { available: boolean; note: string }
  next_step: string
  disclaimer: string
  server_time: string
}

export interface ReportQuery {
  range?: ReportRange
  since?: string
  until?: string
  timezone?: string
}

// --------------------------------------------------------------------------- //
// Reads
// --------------------------------------------------------------------------- //
export async function fetchOutcomeReport(query: ReportQuery = {}): Promise<OutcomeReport> {
  const { data } = await client.get<OutcomeReport>(REPORTING_API, { params: query })
  return data
}

export async function fetchWeeklyOutcome(range: 'last_7_days' | 'this_week' = 'last_7_days',
                                         timezone?: string): Promise<WeeklyOutcome> {
  const { data } = await client.get<WeeklyOutcome>(`${REPORTING_API}/weekly`,
    { params: { range, timezone } })
  return data
}

/** The export URL for a download link. The server audits the fetch. */
export function exportUrl(format: 'csv' | 'json', query: ReportQuery = {}): string {
  const params = new URLSearchParams({ format, ...Object.fromEntries(
    Object.entries(query).filter(([, value]) => value !== undefined && value !== null && value !== '') as [string, string][],
  ) })
  return `${REPORTING_API}/export?${params.toString()}`
}

// --------------------------------------------------------------------------- //
// Presentation helpers (pure — these are what the tests pin)
// --------------------------------------------------------------------------- //
const DASH = '—'

/** A number, or `—` when it was withheld. Never `0` for "we do not know". */
export function rateText(rate?: number | null, unit = '%'): string {
  if (rate === null || rate === undefined) return DASH
  return unit === '%' ? `${rate}%` : `${rate} ${unit}`
}

export function metricText(metric: Pick<ReportMetric, 'value' | 'unit'>): string {
  if (metric.value === null || metric.value === undefined) return DASH
  if (metric.unit === 'percent') return `${metric.value}%`
  if (metric.unit === 'days') return `${metric.value} days`
  return `${metric.value}`
}

/** "Observed", "You reported", "Derived", "Estimated" — never blurred. */
export function provenanceBadge(provenance?: string | null): { label: string; title: string; className: string } {
  const value = (provenance || 'observed') as ReportProvenance
  const known = (REPORT_PROVENANCE_KINDS as readonly string[]).includes(value)
  const labels: Record<string, string> = {
    observed: 'Observed',
    user_reported: 'You reported',
    derived: 'Derived',
    estimated: 'Estimated',
  }
  const titles: Record<string, string> = {
    observed: 'Counted from rows we hold: a job we found, a submission we saw, an event on the timeline.',
    user_reported: 'You said this happened. Nothing here was inferred from email or from a score.',
    derived: 'Arithmetic over the observed and reported numbers on this page.',
    estimated: 'A model’s opinion — the match score. It ranks fit; it is not a probability of an interview.',
  }
  const styles: Record<string, string> = {
    observed: 'bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300',
    user_reported: 'bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300',
    derived: 'bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-300',
    estimated: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  }
  const key = known ? value : 'observed'
  return { label: labels[key] || key, title: titles[key] || '', className: styles[key] || styles.observed }
}

/**
 * The sufficiency chip. `insufficient` is a first-class state, not an error: it
 * says how far off the sample is rather than hiding the row.
 */
export function sufficiencyLabel(sample?: ReportSample | null, sufficiency?: ReportSufficiency): string {
  const state = sufficiency || sample?.state || 'not_applicable'
  if ((REPORT_SUFFICIENCY as readonly string[]).includes(state) === false) return 'No minimum applies'
  if (state === 'sufficient') return `Sample OK — ${sample?.n ?? 0} of ${sample?.minimum ?? 0}`
  if (state === 'insufficient') {
    const n = sample?.n ?? 0
    const minimum = sample?.minimum ?? 0
    return `Not enough data — ${n} of ${minimum} needed for a comparison`
  }
  return 'No minimum applies'
}

export function isInsufficient(sample?: ReportSample | null, sufficiency?: ReportSufficiency): boolean {
  return (sufficiency || sample?.state) === 'insufficient'
}

export function trendLabel(direction?: string | null): { label: string; className: string } {
  const value = (direction || 'not_enough_data') as ReportTrend
  const known = (REPORT_TRENDS as readonly string[]).includes(value)
  const labels: Record<string, string> = {
    improving: 'Improving',
    declining: 'Declining',
    flat: 'Holding steady',
    not_enough_data: 'Not enough data yet',
  }
  const styles: Record<string, string> = {
    improving: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
    declining: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300',
    flat: 'bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300',
    not_enough_data: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  }
  const key = known ? value : 'not_enough_data'
  return { label: labels[key] || key, className: styles[key] || styles.not_enough_data }
}

/** The window in the user's own words: "14 Sep → 20 Sep 2026 (Europe/London)". */
export function windowLabel(window?: ReportWindow | null): string {
  if (!window) return DASH
  const start = window.start_date || ''
  const end = window.end_date || ''
  const span = start === end ? start : `${start} → ${end}`
  return `${span} (${window.timezone})`
}

/** True when `end_utc` is an exclusive instant — always, but the UI says so. */
export function boundaryNote(window?: ReportWindow | null): string {
  if (!window) return ''
  return `${window.days} local day(s), half-open: ${window.start_utc} ≤ t < ${window.end_utc}`
}

export const RANGE_OPTIONS: { key: ReportRange; label: string }[] = [
  { key: 'last_7_days', label: 'Last 7 days' },
  { key: 'last_30_days', label: 'Last 30 days' },
  { key: 'last_90_days', label: 'Last 90 days' },
  { key: 'this_week', label: 'This week' },
  { key: 'last_week', label: 'Last week' },
]

/** The dimension picker, in the order the contract lists them. */
export const COMPARISON_LABELS: Record<ReportComparisonDimension, string> = {
  source: 'Source',
  score_band: 'Score band',
  score_range: 'Score range',
  role_family: 'Role family',
  artifact: 'Resume / artefact',
}

export const METRIC_ORDER: string[] = [
  'jobs_discovered',
  'high_fit_jobs',
  'applications_prepared',
  'applications_submitted',
  'recruiter_responses',
  'interviews',
  'interview_rate',
  'rejection_rate',
  'time_from_posting_to_application',
  'user_correction_rate',
  'application_failure_rate',
  'tracking_coverage',
]

/** The metrics in the order the acceptance criteria name them; unknown keys last. */
export function orderedMetrics(report: Pick<OutcomeReport, 'metrics'>): ReportMetric[] {
  const byKey = new Map(report.metrics.map(metric => [metric.key, metric]))
  const ordered = METRIC_ORDER
    .map(key => byKey.get(key))
    .filter((metric): metric is ReportMetric => !!metric)
  const rest = report.metrics.filter(metric => !METRIC_ORDER.includes(metric.key))
  return [...ordered, ...rest]
}

/** A comparison row's rate cell, with the reason when it is withheld. */
export function groupRateText(group: ReportGroup): string {
  if (isInsufficient(group.sample, group.sufficiency)) return DASH
  return rateText(group.interview_rate)
}

export function groupRateTitle(group: ReportGroup): string {
  if (isInsufficient(group.sample, group.sufficiency)) {
    return group.insufficient_reason || sufficiencyLabel(group.sample, group.sufficiency)
  }
  return `${group.interviews} of ${group.applied} applications reached an interview`
}

/**
 * The calibration sentence. There is no branch here that produces a probability:
 * the field is `null` until the backend's gate opens, and the UI says what is
 * missing rather than showing a model's guess as a promise.
 */
export function calibrationText(calibration?: ReportCalibration | null): string {
  if (!calibration) return 'Calibration status unavailable.'
  if (calibration.available) return calibration.note
  const observed = calibration.observed || { recorded_interviews: 0, score_bands_with_outcomes: 0 }
  const needed = Number(calibration.requires?.recorded_interviews ?? 0)
  return `${calibration.note} So far ${observed.recorded_interviews} recorded interview(s) across ` +
    `${observed.score_bands_with_outcomes} score range(s); ${needed} are needed before a probability ` +
    'would mean anything.'
}

export function isKnownRange(value: string): value is ReportRange {
  return (REPORT_RANGES as readonly string[]).includes(value)
}
