/**
 * The reporting helpers — the presentation rules that keep a report honest.
 *
 * These are pure functions, so this file pins the three ways a dashboard starts
 * lying without anyone noticing:
 *
 *   • `null` rendered as `0%` (a withheld rate is not a zero);
 *   • an `estimated` number rendered as a fact;
 *   • `not_enough_data` rendered as "flat".
 */
import { describe, expect, it } from 'vitest'
import {
  METRIC_ORDER,
  boundaryNote,
  calibrationText,
  exportUrl,
  groupRateText,
  groupRateTitle,
  isInsufficient,
  metricText,
  orderedMetrics,
  provenanceBadge,
  rateText,
  sufficiencyLabel,
  trendLabel,
  windowLabel,
  type OutcomeReport,
  type ReportGroup,
  type ReportMetric,
  type ReportSample,
} from '../reporting'

const sufficient: ReportSample = { n: 12, minimum: 5, state: 'sufficient', sufficient: true,
  label: '12 of 5 needed' }

function group(over: Partial<ReportGroup> = {}): ReportGroup {
  return {
    key: 'lever', label: 'lever', applied: 12, responses: 4, interviews: 3, offers: 1,
    rejections: 2, withdrawn: 0, corrections: 0, events: 0,
    interview_rate: 25.0, response_rate: 33.3, rejection_rate: 16.7, offer_rate: 33.3,
    avg_match_score: 81.0, by_origin: { user_reported: 12 }, sample: sufficient,
    sufficiency: 'sufficient', insufficient_reason: null, ...over,
  }
}

function metric(over: Partial<ReportMetric> = {}): ReportMetric {
  return { key: 'interviews', label: 'Interviews', value: 3, unit: 'count',
    provenance: 'user_reported', sample: sufficient, sufficiency: 'sufficient', note: '', ...over }
}

describe('reporting helpers', () => {
  it('renders a withheld rate as a dash, never as zero', () => {
    expect(rateText(25.0)).toBe('25%')
    expect(rateText(0)).toBe('0%')
    expect(rateText(null)).toBe('—')
    expect(rateText(undefined)).toBe('—')
    expect(metricText(metric({ value: null, unit: 'percent' }))).toBe('—')
    expect(metricText(metric({ value: 41.7, unit: 'percent' }))).toBe('41.7%')
    expect(metricText(metric({ value: 6, unit: 'days' }))).toBe('6 days')
  })

  it('labels an insufficient sample with the count that is missing', () => {
    const small: ReportSample = { n: 2, minimum: 5, state: 'insufficient', sufficient: false,
      label: 'Not enough data — 2 of 5 applications' }
    expect(sufficiencyLabel(small)).toBe('Not enough data — 2 of 5 needed for a comparison')
    expect(isInsufficient(small)).toBe(true)
    expect(isInsufficient(sufficient)).toBe(false)
    expect(sufficiencyLabel(sufficient)).toBe('Sample OK — 12 of 5')
    expect(sufficiencyLabel(undefined, 'not_applicable')).toBe('No minimum applies')
  })

  it('never prints a rate for a withheld group', () => {
    const small = group({
      applied: 2, interviews: 1, interview_rate: null, sufficiency: 'insufficient',
      sample: { n: 2, minimum: 5, state: 'insufficient', sufficient: false,
        label: 'Not enough data — 2 of 5 applications' },
      insufficient_reason: 'Not enough data — 2 of 5 applications',
    })
    expect(groupRateText(small)).toBe('—')
    expect(groupRateTitle(small)).toMatch(/Not enough data/)
    expect(groupRateText(group())).toBe('25%')
  })

  it('separates an estimate from an observation', () => {
    expect(provenanceBadge('observed').label).toBe('Observed')
    expect(provenanceBadge('user_reported').label).toBe('You reported')
    expect(provenanceBadge('estimated').label).toBe('Estimated')
    expect(provenanceBadge('estimated').title).toMatch(/not a probability/)
    // An unknown provenance is shown as the least-claiming one, never invented.
    expect(provenanceBadge('astrology').label).toBe('Observed')
  })

  it('does not turn "not enough data" into "flat"', () => {
    expect(trendLabel('improving').label).toBe('Improving')
    expect(trendLabel('declining').label).toBe('Declining')
    expect(trendLabel('flat').label).toBe('Holding steady')
    expect(trendLabel('not_enough_data').label).toBe('Not enough data yet')
    expect(trendLabel(undefined).label).toBe('Not enough data yet')
    expect(trendLabel('sideways').label).toBe('Not enough data yet')
  })

  it('describes the window and its half-open boundary', () => {
    const window = {
      range: 'last_7_days', timezone: 'Asia/Tokyo',
      start_utc: '2026-09-13T15:00:00Z', end_utc: '2026-09-20T15:00:00Z',
      start_local: '2026-09-14T00:00:00+09:00', end_local: '2026-09-21T00:00:00+09:00',
      start_date: '2026-09-14', end_date: '2026-09-20', days: 7,
      boundary: 'half-open [start_utc, end_utc)', label: '',
    }
    expect(windowLabel(window)).toBe('2026-09-14 → 2026-09-20 (Asia/Tokyo)')
    expect(windowLabel(null)).toBe('—')
    expect(boundaryNote(window)).toContain('2026-09-13T15:00:00Z ≤ t < 2026-09-20T15:00:00Z')
  })

  it('says what is missing before a probability would mean anything', () => {
    const text = calibrationText({
      available: false,
      interview_probability: null,
      missing: ['recorded_interviews', 'calibrated_model_version'],
      observed: { recorded_interviews: 4, score_bands_with_outcomes: 1, share_verified_outcomes: 0.5 },
      requires: { recorded_interviews: 30, score_bands_with_outcomes: 3,
        share_verified_outcomes: 0.8, calibrated_model_version: null },
      note: 'No interview probability is reported.',
    })
    expect(text).toMatch(/No interview probability is reported/)
    expect(text).toMatch(/4 recorded interview/)
    expect(text).toMatch(/30 are needed/)
    expect(text).not.toMatch(/\d+% chance/)
  })

  it('orders the metrics the way the acceptance criteria name them', () => {
    const report = { metrics: [metric({ key: 'interviews' }), metric({ key: 'jobs_discovered' }),
      metric({ key: 'application_failure_rate' }), metric({ key: 'something_new' })] } as
      Pick<OutcomeReport, 'metrics'>
    const keys = orderedMetrics(report).map(item => item.key)
    expect(keys.slice(0, 3)).toEqual(['jobs_discovered', 'interviews', 'application_failure_rate'])
    expect(keys[keys.length - 1]).toBe('something_new')
    expect(METRIC_ORDER[0]).toBe('jobs_discovered')
  })

  it('builds the export URL from the same query the report used', () => {
    const url = exportUrl('csv', { range: 'last_30_days', timezone: 'Europe/London' })
    expect(url).toBe('/api/analytics/outcomes/export?format=csv&range=last_30_days&timezone=Europe%2FLondon')
    expect(exportUrl('json')).toBe('/api/analytics/outcomes/export?format=json')
  })
})
