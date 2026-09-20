/**
 * The outcome report block — what it must and must not show.
 *
 * Pinned here, because each one is a way a reporting UI starts lying:
 *
 *   • a withheld rate (`null`) renders `—` with the reason, never `0%`;
 *   • an insufficient group still lists its counts and says "insufficient
 *     sample" instead of printing a percentage over two applications;
 *   • an `estimated` metric carries the estimate chip (a match score is not a
 *     fact);
 *   • the calibration block states the refusal — no probability anywhere;
 *   • the export link only exists when the server's privacy gate allowed it;
 *   • switching the range refetches with that range and the user's timezone.
 */
import { afterEach, describe, expect, it, vi, type Mock } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import client from '../../api/client'
import OutcomeReport from '../OutcomeReport'
import type { OutcomeReport as OutcomeReportShape } from '../../lib/reporting'

vi.mock('../../api/client', () => {
  const get = vi.fn()
  return { default: { get }, apiError: (_e: unknown, fallback?: string) => fallback ?? 'error' }
})

const get = client.get as unknown as Mock

const sufficient = { n: 12, minimum: 5, state: 'sufficient', sufficient: true, label: '12 of 5 needed' }
const insufficient = { n: 2, minimum: 5, state: 'insufficient', sufficient: false,
  label: 'Not enough data — 2 of 5 applications' }

const window = {
  range: 'last_30_days', timezone: 'Asia/Tokyo',
  start_utc: '2026-08-22T15:00:00Z', end_utc: '2026-09-21T15:00:00Z',
  start_local: '2026-08-23T00:00:00+09:00', end_local: '2026-09-22T00:00:00+09:00',
  start_date: '2026-08-23', end_date: '2026-09-21', days: 30,
  boundary: 'half-open [start_utc, end_utc)', label: '2026-08-23 → 2026-09-21 (Asia/Tokyo)',
}

const previous = { ...window, start_utc: '2026-07-23T15:00:00Z', end_utc: '2026-08-22T15:00:00Z',
  start_date: '2026-07-24', end_date: '2026-08-22' }

function metric(over: Record<string, unknown> = {}) {
  return { key: 'interviews', label: 'Interviews', value: 3, unit: 'count',
    provenance: 'user_reported', sample: sufficient, sufficiency: 'sufficient', note: '', ...over }
}

function group(over: Record<string, unknown> = {}) {
  return { key: 'lever', label: 'lever', applied: 12, responses: 4, interviews: 3, offers: 1,
    rejections: 2, withdrawn: 0, corrections: 0, events: 0, interview_rate: 25.0,
    response_rate: 33.3, rejection_rate: 16.7, offer_rate: 33.3, avg_match_score: 81.0,
    by_origin: { user_reported: 12 }, sample: sufficient, sufficiency: 'sufficient',
    insufficient_reason: null, ...over }
}

function report(over: Partial<OutcomeReportShape> = {}): OutcomeReportShape {
  const base = {
    report_version: '1.0.0',
    generated_at: '2026-09-21T12:00:00Z',
    calculation_id: 'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789',
    timezone: 'Asia/Tokyo',
    window,
    previous_window: previous,
    definitions: { report_version: '1.0.0', minimum_sample_sizes: { comparison_applied: 5 },
      high_fit_score: 75, interview_states: ['interview_scheduled'], formulae: {} },
    metrics: [
      metric({ key: 'jobs_discovered', label: 'Jobs discovered', value: 40, provenance: 'observed' }),
      metric({ key: 'high_fit_jobs', label: 'High-fit jobs found', value: 9, provenance: 'estimated',
        note: 'Estimated fit at or above 75.' }),
      metric({ key: 'applications_submitted', label: 'Applications submitted', value: 12,
        provenance: 'observed' }),
      metric({ key: 'interviews', label: 'Interviews', value: 3, provenance: 'user_reported' }),
      metric({ key: 'interview_rate', label: 'Interview rate', value: 25.0, unit: 'percent',
        provenance: 'derived' }),
      metric({ key: 'rejection_rate', label: 'Rejection rate', value: 16.7, unit: 'percent',
        provenance: 'derived' }),
      metric({ key: 'user_correction_rate', label: 'User correction rate', value: null,
        unit: 'percent', provenance: 'derived', sample: insufficient, sufficiency: 'insufficient' }),
      metric({ key: 'application_failure_rate', label: 'Application failure rate', value: null,
        unit: 'percent', provenance: 'derived', sample: insufficient, sufficiency: 'insufficient' }),
    ],
    funnel: { jobs_discovered: 40, high_fit_jobs: 9, applications_prepared: 6,
      applications_submitted: 12, applications_tracked: 12, recruiter_responses: 4,
      interviews: 3, offers: 1, rejections: 2 },
    totals: { applied: 12, interviews: 3, interview_rate: 25.0, applications_submitted: 12,
      applications_tracked: 12, responses: 4, rejections: 2, offers: 1, jobs_discovered: 40,
      high_fit_jobs: 9, applications_prepared: 6, sample: sufficient },
    comparisons: {
      source: { dimension: 'source', minimum_sample: 5, group_count: 2, sufficient_group_count: 1,
        withheld_group_count: 1, best: { key: 'lever', label: 'lever', interview_rate: 25.0,
          applied: 12, interviews: 3 },
        groups: [group(), group({ key: 'tinyboard', label: 'tinyboard', applied: 2, interviews: 1,
          interview_rate: null, sufficiency: 'insufficient', sample: insufficient,
          insufficient_reason: 'Not enough data — 2 of 5 applications' })] },
      score_band: { dimension: 'score_band', minimum_sample: 5, group_count: 1,
        sufficient_group_count: 1, withheld_group_count: 0, best: null, groups: [group()] },
      score_range: { dimension: 'score_range', minimum_sample: 5, group_count: 0,
        sufficient_group_count: 0, withheld_group_count: 0, best: null, groups: [] },
      role_family: { dimension: 'role_family', minimum_sample: 5, group_count: 1,
        sufficient_group_count: 1, withheld_group_count: 0, best: null, groups: [group()] },
      artifact: { dimension: 'artifact', minimum_sample: 5, group_count: 1,
        sufficient_group_count: 1, withheld_group_count: 0, best: null,
        groups: [group({ key: 'packet:v3', label: 'packet:v3' })] },
    },
    timing: { days: 6.0, median_days: 6.0, mean_days: 6.4, p90_days: 11.0,
      applications_measured: 9, applications_missing_posting_date: 3,
      applications_dated_before_posting: 0, provenance: 'derived', sample: sufficient,
      sufficiency: 'sufficient', note: 'Days between the posting and the application.' },
    quality: { tracking_coverage_percent: 100.0, applications_without_a_tracking_record: 0,
      provisional_records: 0, corrections: 0, events: 0, correction_rate: null,
      by_origin: { user_reported: 12 }, application_attempts: 8, application_failures: 1,
      application_failure_rate: 12.5, note: '' },
    trend: { direction: 'improving', headline: 'Interview rate is up: 25% of 12 applications this period vs 8% of 10 last period.',
      compared_with: previous, measured: true,
      sample: { current: sufficient, previous: sufficient },
      metrics: { interview_rate: { current: 25.0, previous: 8.0, delta: 17.0 } },
      verdict_basis: 'interview_rate', flat_threshold_percentage_points: 1.0, note: '' },
    calibration: { available: false, interview_probability: null,
      missing: ['recorded_interviews', 'calibrated_model_version'],
      observed: { recorded_interviews: 3, score_bands_with_outcomes: 1, share_verified_outcomes: 0.5 },
      requires: { recorded_interviews: 30, score_bands_with_outcomes: 3,
        share_verified_outcomes: 0.8, calibrated_model_version: null },
      note: 'No interview probability is reported.' },
    privacy: { scope: 'self_only', includes_other_users: false },
    export: { allowed: true, requirements: ['self_scope_only'], unmet: [], formats: ['csv', 'json'],
      note: 'Your own outcome data only.' },
    signal: { kind: 'working', headline: 'Interviews are up this period.', withheld_group_count: 1 },
    headline: '12 application(s) submitted, 3 interview(s) — interview rate 25.0%.',
    disclaimer: 'An interview is counted only when it was recorded — never inferred from email. '
      + 'A match score is an estimated fit, not an interview probability.',
    server_time: '2026-09-21T12:00:00Z',
  } as unknown as OutcomeReportShape
  return { ...base, ...over }
}

function mockReport(payload = report()) {
  get.mockImplementation((url: string) => {
    if (url === '/api/analytics/outcomes') return Promise.resolve({ data: payload })
    return Promise.resolve({ data: {} })
  })
}

describe('OutcomeReport', () => {
  // `mockClear` (not `mockReset`): a reset mock reports the implementation's
  // thrown error as an unhandled failure in vitest 5, and every test here
  // installs its own implementation anyway.
  // No `mockReset`/`mockClear` here on purpose: in vitest 5 a cleared mock
  // reports the implementation's own rejection as an unhandled test failure,
  // and this file deliberately exercises the rejection path. Every test
  // installs its own implementation, so nothing leaks between them.
  afterEach(() => cleanup())

  it('leads with the period, the headline and whether it is improving', async () => {
    mockReport()
    render(<OutcomeReport timezone="Asia/Tokyo" />)
    await waitFor(() => expect(screen.getByTestId('report-headline')).toBeTruthy())

    expect(screen.getByTestId('report-window').textContent).toContain('2026-08-23 → 2026-09-21')
    expect(screen.getByTestId('report-window').textContent).toContain('Asia/Tokyo')
    expect(screen.getByTestId('trend-chip').textContent).toBe('Improving')
    expect(screen.getByTestId('report-headline').textContent).toContain('12 application(s) submitted')
    expect(screen.getByTestId('trend-headline').textContent).toMatch(/Interview rate is up/)
    expect(screen.getByTestId('signal').textContent).toMatch(/Interviews are up/)
  })

  it('shows a dash and a reason where the sample cannot carry a rate', async () => {
    mockReport()
    render(<OutcomeReport />)
    await waitFor(() => expect(screen.getByTestId('report-metrics')).toBeTruthy())

    // A withheld metric: no number, and the shortfall spelled out.
    expect(screen.getByTestId('value-user_correction_rate').textContent).toBe('—')
    expect(screen.getByTestId('sample-user_correction_rate').textContent)
      .toBe('Not enough data — 2 of 5 needed for a comparison')

    // The same rule inside a comparison group: counts stay, the rate does not.
    const tiny = screen.getByTestId('group-source-tinyboard')
    expect(tiny.textContent).toContain('tinyboard')
    expect(tiny.textContent).toContain('2')
    expect(tiny.textContent).toContain('—')
    expect(tiny.textContent).toContain('insufficient sample')
    // …while a comparable group shows its real number.
    expect(screen.getByTestId('group-source-lever').textContent).toContain('25%')
    expect(screen.getByTestId('comparison-source').textContent).toContain('1 group(s) withheld')
  })

  it('marks an estimate as an estimate', async () => {
    mockReport()
    render(<OutcomeReport />)
    await waitFor(() => expect(screen.getByTestId('report-metrics')).toBeTruthy())

    expect(screen.getByTestId('provenance-high_fit_jobs').textContent).toBe('Estimated')
    expect(screen.getByTestId('provenance-jobs_discovered').textContent).toBe('Observed')
    expect(screen.getByTestId('provenance-interviews').textContent).toBe('You reported')
    expect(screen.getByTestId('provenance-interview_rate').textContent).toBe('Derived')
  })

  it('states the calibration refusal instead of a probability', async () => {
    mockReport()
    render(<OutcomeReport />)
    await waitFor(() => expect(screen.getByTestId('report-calibration')).toBeTruthy())

    const block = screen.getByTestId('report-calibration')
    expect(block.textContent).toMatch(/No interview probability is reported/)
    expect(block.textContent).toMatch(/3 recorded interview/)
    expect(block.textContent).toMatch(/30 are needed/)
    expect(block.textContent).toMatch(/not an interview probability/)
    expect(block.textContent).not.toMatch(/\d+% (chance|probability)/)
    expect(screen.getByTestId('report-privacy').textContent).toMatch(/your account only/)
  })

  it('offers the export only when the privacy gate allowed it', async () => {
    mockReport()
    render(<OutcomeReport />)
    await waitFor(() => expect(screen.getByTestId('report-export')).toBeTruthy())
    expect(screen.getByTestId('report-export').getAttribute('href'))
      .toBe('/api/analytics/outcomes/export?format=csv&range=last_30_days')

    cleanup()
    get.mockReset()
    mockReport(report({ export: { allowed: false, requirements: ['no_other_tenants'],
      unmet: ['no_other_tenants'], formats: [], note: 'Withheld.' } as never }))
    render(<OutcomeReport />)
    await waitFor(() => expect(screen.getByTestId('report-metrics')).toBeTruthy())
    expect(screen.queryByTestId('report-export')).toBeNull()
  })

  it('refetches with the chosen range and the account timezone', async () => {
    mockReport()
    render(<OutcomeReport timezone="Europe/London" />)
    const lastCall = () => get.mock.calls[get.mock.calls.length - 1]
    await waitFor(() => expect(lastCall()).toEqual(['/api/analytics/outcomes',
      { params: { range: 'last_30_days', timezone: 'Europe/London' } }]))

    fireEvent.change(screen.getByTestId('report-range'), { target: { value: 'last_7_days' } })
    await waitFor(() => expect(lastCall()).toEqual(['/api/analytics/outcomes',
      { params: { range: 'last_7_days', timezone: 'Europe/London' } }]))
  })

  it('says so, and offers a retry, when the read fails', async () => {
    get.mockImplementation(() => Promise.reject(
      { response: { data: { detail: { message: 'The end of the window must be after the start.' } } } }))
    render(<OutcomeReport />)
    await waitFor(() => expect(screen.getByTestId('report-error')).toBeTruthy())
    expect(screen.getByTestId('report-error').textContent)
      .toContain('The end of the window must be after the start.')
  })
})
