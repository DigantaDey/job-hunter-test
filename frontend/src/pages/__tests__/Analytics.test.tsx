/**
 * Analytics — the interview number and where it came from.
 *
 * This page used to print "Interviews (est)" over a count the backend invented
 * by treating every applied job scoring ≥75 as an interview. The number now
 * comes from the tracking timeline, and the two honest edge cases are pinned
 * here because they are exactly where a dashboard starts lying:
 *
 *   • no recorded outcomes → 0 interviews, and the note says where to fix it;
 *   • no applications at all → the rate is `—`, never `0%`;
 *   • a role family with applications but no interviews → a real `0%`.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import client from '../../api/client'
import Analytics from '../Analytics'

vi.mock('../../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, email: 'owner@example.com', name: 'Owner', role: 'owner', is_owner: true } }),
}))

vi.mock('../../api/client', () => {
  const get = vi.fn()
  return { default: { get }, apiError: (_e: unknown, fb?: string) => fb ?? 'error' }
})

const get = client.get as unknown as Mock

const basePerformance = {
  summary: {
    total_jobs: 12, applied: 3, interviews: 2, interview_rate: 66.7, offers: 1, rejections: 1,
    failed: 0, discovered: 8, needs_input: 1, best_role: 'Backend Engineer', best_role_rate: 100.0,
    interview_data: 'application_tracking', tracked_applications: 3, tracking_coverage: 100.0,
    interview_note: 'Interviews are counted from your tracking timeline — nothing is inferred from email or from a match score.',
  },
  roles: [
    { role: 'Backend Engineer', total: 6, applied: 2, interviews: 2, interview_rate: 100.0 },
    { role: 'Data Engineer', total: 4, applied: 1, interviews: 0, interview_rate: 0.0 },
    { role: 'ML Engineer', total: 2, applied: 0, interviews: 0, interview_rate: null },
  ],
  skills: { strongest: [{ skill: 'python', count: 5 }], weakest: [{ skill: 'kubernetes', count: 2 }] },
  weekly_trend: [{ date: '2026-09-14', discovered: 4, applied: 1 }],
  ai_usage_daily: {},
  is_pro: true,
  conversion: {
    group_by: 'source', totals: { tracked: 3, applied: 3, responses: 2, interviews: 2, offers: 1,
      rejections: 1, withdrawn: 0, provisional: 0, corrections: 0,
      application_to_interview_rate: 66.7, applied_to_response_rate: 66.7,
      interview_to_offer_rate: 50.0, avg_match_score: 81.0, avg_days_to_interview: 5.0,
      by_origin: { system_observed: 1, user_reported: 2 } },
    groups: [
      { key: 'lever', label: 'lever', records: 2, tracked: 2, applied: 2, responses: 2,
        interviews: 2, offers: 1, rejections: 0, withdrawn: 0, provisional: 0, corrections: 0,
        application_to_interview_rate: 100.0, applied_to_response_rate: 100.0,
        interview_to_offer_rate: 50.0, avg_match_score: 81.0, avg_days_to_interview: 5.0,
        by_origin: { user_reported: 2 } },
      { key: 'remoteok', label: 'remoteok', records: 1, tracked: 1, applied: 1, responses: 0,
        interviews: 0, offers: 0, rejections: 1, withdrawn: 0, provisional: 0, corrections: 0,
        application_to_interview_rate: 0.0, applied_to_response_rate: 0.0,
        interview_to_offer_rate: null, avg_match_score: null, avg_days_to_interview: null,
        by_origin: { system_observed: 1 } },
    ],
    group_count: 2,
    disclaimer: 'An interview is counted only when it was recorded — never inferred from email.',
    interview_definition: ['interview_scheduled', 'interview_completed', 'offer_received'],
    server_time: '2026-09-19T12:00:00Z',
  },
  disclaimer: 'An interview is counted only when it was recorded — never inferred from email.',
}

function mockPerformance(summary: Record<string, unknown>, extra: Record<string, unknown> = {}): void {
  get.mockImplementation((url: string) => {
    if (url === '/api/analytics/performance') {
      return Promise.resolve({ data: { ...basePerformance, summary: { ...basePerformance.summary, ...summary }, ...extra } })
    }
    if (url === '/api/analytics/funnel') {
      return Promise.resolve({ data: { funnel: { discovered: 8, applied: 3 }, conversion_rate: 37.5 } })
    }
    if (url === '/api/analytics/costs') {
      return Promise.resolve({ data: { total_cost_usd: 1.2, total_tokens: 1000,
        average_cost_per_application: 0.4, by_workflow: {} } })
    }
    return Promise.resolve({ data: {} })
  })
}

describe('Analytics page — recorded outcomes only', () => {
  beforeEach(() => get.mockReset())
  afterEach(() => cleanup())

  it('labels the interview count as recorded, and never as an estimate', async () => {
    mockPerformance({})
    render(<Analytics />)
    await waitFor(() => expect(screen.getByTestId('interviews')).toBeTruthy())

    expect(screen.queryByText(/Interviews \(est\)/)).toBeNull()
    expect(screen.getByText('Interviews recorded')).toBeTruthy()
    expect(screen.getByTestId('interviews').textContent).toBe('2')
    expect(screen.getByTestId('interview-rate').textContent).toBe('66.7%')
    expect(screen.getByTestId('interview-note').textContent)
      .toMatch(/nothing is inferred from email/)
    // A role family with applications but no interviews is a real zero.
    expect(screen.getByTestId('role-data-engineer').textContent).toMatch(/0%/)
    // …and one with no applications has no rate at all.
    expect(screen.getByTestId('role-ml-engineer').textContent).toMatch(/—/)
  })

  it('says so when nothing has been recorded, instead of showing a number nobody earned', async () => {
    mockPerformance({ interviews: 0, interview_rate: null, interview_data: 'none',
                      tracked_applications: 0, tracking_coverage: null, best_role: null,
                      best_role_rate: null,
                      interview_note: 'No application outcomes recorded yet. Add an interview, a rejection or a withdrawal on the Tracking page and this becomes a real rate.' })
    render(<Analytics />)
    await waitFor(() => expect(screen.getByTestId('interviews')).toBeTruthy())

    expect(screen.getByTestId('interviews').textContent).toBe('0')
    expect(screen.getByTestId('interview-rate').textContent).toBe('—')
    expect(screen.getByText(/nothing recorded yet/)).toBeTruthy()
    expect(screen.getByTestId('interview-note').textContent)
      .toMatch(/Add an interview, a rejection or a withdrawal on the Tracking page/)
    expect(screen.getByText(/Open the tracking board/)).toBeTruthy()
    // No insight is claimed from an empty funnel.
    expect(screen.queryByText(/Insight:/)).toBeNull()
  })

  it('shows the conversion by source, with a dash where there is no denominator', async () => {
    mockPerformance({})
    render(<Analytics />)
    await waitFor(() => expect(screen.getByTestId('conversion-summary')).toBeTruthy())

    expect(screen.getByTestId('conversion-summary').textContent)
      .toBe('2 of 3 applications reached an interview (66.7%)')
    // The columns here are source / applied / interviews / conversion / who said so.
    expect(screen.getByTestId('conversion-row-lever').textContent)
      .toBe('lever22100%user_reported: 2')
    // remoteok has applications and no interviews: a real 0%, and the row says
    // the claim came from an observation rather than from the user's memory.
    expect(screen.getByTestId('conversion-row-remoteok').textContent)
      .toBe('remoteok100%system_observed: 1')
    expect(screen.getByText(/never inferred from email/)).toBeTruthy()
  })

  it('renders an empty conversion block without inventing rows', async () => {
    mockPerformance({ interviews: 0, interview_rate: null, interview_data: 'none' }, {
      conversion: { ...basePerformance.conversion,
        totals: { ...basePerformance.conversion.totals, tracked: 0, applied: 0, interviews: 0,
                  application_to_interview_rate: null, by_origin: {} },
        groups: [], group_count: 0 },
    })
    render(<Analytics />)
    await waitFor(() => expect(screen.getByTestId('conversion-summary')).toBeTruthy())
    expect(screen.getByTestId('conversion-summary').textContent)
      .toBe('No applications recorded yet')
  })
})
