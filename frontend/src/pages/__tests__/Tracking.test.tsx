/**
 * The tracking board page.
 *
 * The three reads the acceptance criteria depend on: the board (with a record's
 * own timeline one click away), the reminder agenda, and the report that turns
 * recorded outcomes into an application→interview conversion. The rules pinned
 * here are the ones a UI can quietly break: a `null` rate renders as `—` and
 * never as 0%, the group dimension is passed to the server rather than computed
 * locally, and the sweep that makes a due reminder exist runs with the read.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import client from '../../api/client'
import Tracking from '../Tracking'
import type { TrackingDocument, TrackingEvent } from '../../lib/tracking'

vi.mock('../../api/client', () => {
  const get = vi.fn()
  const post = vi.fn()
  const del = vi.fn()
  return {
    default: { get, post, delete: del },
    apiError: (error: unknown, fallback?: string) => {
      const detail = (error as { response?: { data?: { detail?: { message?: string } } } })
        ?.response?.data?.detail
      return detail?.message || fallback || 'error'
    },
  }
})

const get = client.get as unknown as Mock
const post = client.post as unknown as Mock

const API = '/api/application-tracking'

function timelineEvent(over: Partial<TrackingEvent> = {}): TrackingEvent {
  return {
    id: 1, event_id: 'e-1', sequence: 1, event_type: 'application.tracking_opened',
    state_from: null, state_from_label: null, state_to: 'applied', state_to_label: 'Applied',
    phase: 'applied', origin: 'system_observed', actor_type: 'system_worker',
    actor_label: 'system_worker', trigger: 'auto', severity: 'info',
    message: 'Application submitted by JobHunter (automation).', note: '', payload: {},
    evidence: [], is_correction: false, correction_of_event_id: null, correction_reason: '',
    is_provisional: false, follow_up_at: null, occurred_at: '2026-09-10T09:00:00Z',
    recorded_at: '2026-09-10T09:00:00Z', late_reported: false, ...over,
  }
}

function document(over: Partial<TrackingDocument> = {}): TrackingDocument {
  return {
    tracking_id: 4, job_id: 9,
    job: { id: 9, title: 'Senior Backend Engineer', company: 'FinCo',
           url: 'https://jobs.lever.co/finco/1', status: 'applied', source: 'lever' },
    state: 'interview_scheduled', state_label: 'Interview scheduled', phase: 'interviewing',
    phase_label: 'Interviewing', job_status: 'applied', state_origin: 'user_reported',
    is_provisional: false, provisional_evidence: {},
    attribution: { source: 'lever', channel: 'automation', role_family: 'Backend Engineer' },
    snapshots: {
      match: { score: 88, band: 'strong', score_source: 'ai', scorer_version: '1.4.0', match_id: 3 },
      artifact: { kind: 'packet', id: 12, version: 3, label: null, sha256: null },
    },
    timestamps: {
      applied_at: '2026-09-10T09:00:00Z', first_response_at: '2026-09-12T18:00:00Z',
      interview_scheduled_at: '2026-09-12T18:00:00Z', interview_at: '2026-09-25T14:00:00Z',
      interview_completed_at: null, outcome_at: null, closed_at: null,
      created_at: '2026-09-10T08:00:00Z', updated_at: '2026-09-19T09:00:00Z',
    },
    durations_days: { applied_to_first_response: 2, applied_to_interview: 2,
                      applied_to_outcome: null },
    follow_up: { due_at: '2020-01-01T09:00:00Z', note: 'Send the thank-you note',
                 notified_at: '2020-01-01T09:05:00Z', completed_at: null, overdue: true,
                 pending: true },
    counts: { events: 2, corrections: 0, last_sequence: 2 },
    note: 'Bring the payments case study',
    transitions: { allowed: ['interview_scheduled', 'interview_completed', 'offer_received',
                             'rejected_by_employer', 'withdrawn'],
                   allowed_labels: ['Interview scheduled', 'Interview done', 'Offer received',
                                    'Rejected', 'Withdrawn'], terminal: false },
    timeline: [opened, interview],
    actions: [
      { key: 'interview', label: 'I got an interview', method: 'POST', route: `${API}/4/interview`,
        primary: true, disabled: false, disabled_reason: null },
      { key: 'interview_completed', label: 'Interview completed', method: 'POST',
        route: `${API}/4/interview-complete`, primary: false, disabled: false,
        disabled_reason: null },
      { key: 'note', label: 'Add note', method: 'POST', route: `${API}/4/note`, primary: false,
        disabled: false, disabled_reason: null },
    ],
    server_time: '2026-09-19T12:00:00Z', exists: true, ...over,
  }
}

const opened = timelineEvent()
const interview = timelineEvent({
  id: 2, event_id: 'e-2', sequence: 2, event_type: 'application.interview_reported',
  state_from: 'applied', state_from_label: 'Applied', state_to: 'interview_scheduled',
  state_to_label: 'Interview scheduled', origin: 'user_reported', actor_type: 'user',
  actor_label: 'ada@example.com', trigger: 'user', severity: 'success',
  message: 'Interview reported.', occurred_at: '2026-09-12T18:00:00Z',
})

const listBody = {
  items: [
    document({ timeline: [], counts: { events: 2, corrections: 0, last_sequence: 2 } }),
    document({ tracking_id: 5, job_id: 11, state: 'rejected_by_employer', state_label: 'Rejected',
               phase: 'closed', phase_label: 'Closed', job_status: 'rejected',
               state_origin: 'system_observed', is_provisional: true,
               job: { id: 11, title: 'Data Analyst', company: 'OtherCo',
                      url: 'https://remoteok.com/3', status: 'rejected', source: 'remoteok' },
               attribution: { source: 'remoteok', channel: null, role_family: 'Data Engineer' },
               follow_up: { due_at: null, note: '', notified_at: null, completed_at: null,
                            overdue: false, pending: false },
               counts: { events: 1, corrections: 0, last_sequence: 1 }, timeline: [], actions: [] }),
  ],
  page: 1, page_size: 100, total: 2, has_more: false,
  counts: { not_applied: 0, applied: 0, recruiter_response: 0, interview_scheduled: 1,
            interview_completed: 0, offer_received: 0, rejected_by_employer: 1, withdrawn: 0 },
  states: ['not_applied', 'applied', 'recruiter_response', 'interview_scheduled',
           'interview_completed', 'offer_received', 'rejected_by_employer', 'withdrawn'],
  state_labels: { not_applied: 'Not applied', applied: 'Applied',
                  recruiter_response: 'Recruiter responded',
                  interview_scheduled: 'Interview scheduled',
                  interview_completed: 'Interview done', offer_received: 'Offer received',
                  rejected_by_employer: 'Rejected', withdrawn: 'Withdrawn' },
  server_time: '2026-09-19T12:00:00Z',
}

const meta = {
  states: listBody.states, state_labels: listBody.state_labels,
  phases: ['pre_application', 'applied', 'in_conversation', 'interviewing', 'closed'],
  origins: ['system_observed', 'user_reported', 'email_inferred', 'verified_integration'],
  transitions: { applied: ['recruiter_response', 'interview_scheduled'] },
  terminal_states: ['rejected_by_employer', 'withdrawn'],
  interview_states: ['interview_scheduled', 'interview_completed', 'offer_received'],
  manual_sources: ['referral', 'company_site'], channels: ['automation', 'manual_user'],
  interview_formats: ['phone', 'video'], self_assessments: ['went_well', 'mixed'],
  response_channels: ['email', 'phone'],
  report_group_keys: ['source', 'role', 'match_band', 'score_bucket', 'artifact_version',
                      'artifact_kind', 'channel', 'state'],
  counts: listBody.counts,
  disclaimer: 'Interviews are counted only from what was recorded.',
}

const report = {
  group_by: 'source', window: { since: null, until: null },
  filters: { origin: null, state: null, include_unapplied: false },
  totals: { tracked: 4, applied: 4, responses: 2, interviews: 2, offers: 1, rejections: 1,
            withdrawn: 0, provisional: 0, corrections: 1, application_to_interview_rate: 50.0,
            applied_to_response_rate: 50.0, interview_to_offer_rate: 50.0, avg_match_score: 81.0,
            avg_days_to_interview: 6.5,
            by_origin: { system_observed: 1, user_reported: 3 } },
  groups: [
    { key: 'lever', label: 'lever', records: 2, tracked: 2, applied: 2, responses: 2,
      interviews: 2, offers: 1, rejections: 0, withdrawn: 0, provisional: 0, corrections: 0,
      application_to_interview_rate: 100.0, applied_to_response_rate: 100.0,
      interview_to_offer_rate: 50.0, avg_match_score: 81.0, avg_days_to_interview: 4.0,
      by_origin: { user_reported: 2 } },
    { key: 'remoteok', label: 'remoteok', records: 2, tracked: 2, applied: 2, responses: 0,
      interviews: 0, offers: 0, rejections: 1, withdrawn: 0, provisional: 0, corrections: 1,
      application_to_interview_rate: 0.0, applied_to_response_rate: 0.0,
      interview_to_offer_rate: null, avg_match_score: null, avg_days_to_interview: null,
      by_origin: { system_observed: 1, user_reported: 1 } },
  ],
  group_count: 2,
  disclaimer: 'An interview is counted only when it was recorded — never inferred from email.',
  interview_definition: ['interview_scheduled', 'interview_completed', 'offer_received'],
  server_time: '2026-09-19T12:00:00Z',
}

const followUps = {
  items: [
    { tracking_id: 4, job_id: 9, title: 'Senior Backend Engineer', company: 'FinCo',
      state: 'interview_scheduled', state_label: 'Interview scheduled',
      due_at: '2020-01-01T09:00:00Z', note: 'Send the thank-you note', overdue: true,
      notified_at: '2020-01-01T09:05:00Z', route: '/tracking?tracking_id=4' },
    { tracking_id: 6, job_id: 12, title: 'Platform Engineer', company: 'GridCo',
      state: 'applied', state_label: 'Applied', due_at: '2099-05-05T09:00:00Z',
      note: 'Chase the recruiter', overdue: false, notified_at: null,
      route: '/tracking?tracking_id=6' },
  ],
  sweep: { due: 1, notified: 1, skipped: 0 },
  notification_kind: 'application_follow_up',
  server_time: '2026-09-19T12:00:00Z',
}

function mockReads(): void {
  get.mockImplementation((url: string) => {
    if (url === API) return Promise.resolve({ data: listBody })
    if (url === `${API}/meta`) return Promise.resolve({ data: meta })
    if (url === `${API}/report`) return Promise.resolve({ data: report })
    if (url === `${API}/follow-ups`) return Promise.resolve({ data: followUps })
    if (/^\/api\/application-tracking\/\d+$/.test(url)) {
      const id = Number(url.split('/').pop())
      return Promise.resolve({ data: document({ tracking_id: id, job_id: id + 5 }) })
    }
    if (url.startsWith(`${API}/4`)) return Promise.resolve({ data: document() })
    return Promise.resolve({ data: {} })
  })
}

const renderPage = (search = ''): void => {
  render(<Tracking />, {
    wrapper: ({ children }) => <MemoryRouter initialEntries={[`/tracking${search}`]}>{children}</MemoryRouter>,
  })
}

describe('Tracking board', () => {
  beforeEach(() => {
    get.mockReset()
    post.mockReset()
    mockReads()
  })
  afterEach(() => cleanup())

  it('lists the records with their state counts, and reads the vocabulary from the server', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByTestId('record-4')).toBeTruthy())

    expect(screen.getByText(/2 applications/)).toBeTruthy()
    expect(screen.getByTestId('record-5')).toBeTruthy()
    // The count chips are the server's grouped counts, filtered to the non-zero.
    const chips = screen.getByTestId('state-counts')
    expect(chips.textContent).toContain('Interview scheduled · 1')
    expect(chips.textContent).toContain('Rejected · 1')
    expect(chips.textContent).not.toContain('Withdrawn ·')
    // The filter's options come from /meta, so a state this build has never seen
    // still appears instead of vanishing from the picker.
    const options = Array.from(
      (screen.getByTestId('filter-state') as HTMLSelectElement).options,
    ).map((option) => option.value)
    expect(options).toContain('interview_scheduled')
    expect(options[0]).toBe('')
  })

  it('shows an untracked user how a record starts', async () => {
    get.mockImplementation((url: string) => {
      if (url === API) return Promise.resolve({ data: { ...listBody, items: [], total: 0, counts: {} } })
      if (url === `${API}/meta`) return Promise.resolve({ data: meta })
      return Promise.resolve({ data: {} })
    })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('empty-board')).toBeTruthy())
    expect(screen.getByTestId('empty-board').textContent).toMatch(/Track this application/)
  })

  it('opens a record’s own timeline, with the origin of each row', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByTestId('record-4')).toBeTruthy())
    fireEvent.click(screen.getByTestId('record-4'))

    await waitFor(() => expect(screen.getByTestId('selected-state')).toBeTruthy())
    expect(screen.getByTestId('selected-state').textContent).toBe('Interview scheduled')
    expect(screen.getByTestId('selected-origin').textContent).toBe('You reported')
    expect(screen.getByTestId('selected-origin').getAttribute('title')).toMatch(/typed this in/)
    expect(screen.getByTestId('snapshot-match').textContent).toBe('88 (strong) — AI verified')
    expect(screen.getByTestId('snapshot-artifact').textContent).toBe('Application packet v3')
    // An overdue reminder is said to be overdue.
    expect(screen.getByTestId('follow-up-line').textContent).toMatch(/Overdue/)
    expect(screen.getByTestId('timeline-event-1')).toBeTruthy()
    expect(screen.getByTestId('timeline-event-2')).toBeTruthy()
    // The detail read asked for the whole timeline, not a page of it.
    const detailCall = get.mock.calls.find((call: unknown[]) => String(call[0]).startsWith(`${API}/4`))
    expect(detailCall?.[1]).toMatchObject({ params: { timeline_limit: 100 } })
    // And the buttons are the server's.
    expect(screen.getByTestId('action-interview')).toBeTruthy()
    expect(screen.getByTestId('action-interview_completed')).toBeTruthy()
  })

  it('follows a deep link from a notification', async () => {
    renderPage('?tracking_id=4')
    await waitFor(() => expect(screen.getByTestId('selected-state')).toBeTruthy())
    expect(screen.getByTestId('selected-state').textContent).toBe('Interview scheduled')
  })

  it('filters by state and by search text', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByTestId('record-4')).toBeTruthy())

    fireEvent.change(screen.getByTestId('filter-state'), { target: { value: 'rejected_by_employer' } })
    await waitFor(() => expect(get.mock.calls.some((call: unknown[]) => call[0] === API
      && (call[1] as { params?: Record<string, unknown> })?.params?.state === 'rejected_by_employer'))
      .toBe(true))

    fireEvent.change(screen.getByTestId('filter-q'), { target: { value: 'FinCo' } })
    await waitFor(() => expect(get.mock.calls.some((call: unknown[]) => call[0] === API
      && (call[1] as { params?: Record<string, unknown> })?.params?.q === 'FinCo')).toBe(true))

    fireEvent.click(screen.getByTestId('filter-due'))
    await waitFor(() => expect(get.mock.calls.some((call: unknown[]) => call[0] === API
      && (call[1] as { params?: Record<string, unknown> })?.params?.due_only === true)).toBe(true))
  })

  it('records an interview from the board and re-reads the list', async () => {
    post.mockResolvedValue({ data: { ok: true, duplicate: false, state_changed: true, event: null,
                                     events: [], document: document(), state: 'interview_scheduled',
                                     phase: 'interviewing', server_time: '2026-09-19T12:00:09Z' } })
    renderPage('?tracking_id=4')
    await waitFor(() => expect(screen.getByTestId('action-interview')).toBeTruthy())

    fireEvent.click(screen.getByTestId('action-interview'))
    fireEvent.change(screen.getByTestId('interview-at'), { target: { value: '2026-09-25T14:00' } })
    fireEvent.click(screen.getByTestId('form-submit'))

    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post.mock.calls[0][0]).toBe(`${API}/4/interview`)
    expect(String(post.mock.calls[0][1].interview_at)).toMatch(/Z$/)
    // The board is refreshed so the list row and the count chips catch up.
    await waitFor(() => expect(get.mock.calls.filter((call: unknown[]) => call[0] === API).length)
      .toBeGreaterThan(1))
    expect(screen.getByTestId('notice').textContent).toMatch(/Overdue|Recorded/)
  })

  it('renders the report’s conversion, its groups and a null rate as a dash', async () => {
    renderPage('?tab=report')
    await waitFor(() => expect(screen.getByTestId('conversion-summary')).toBeTruthy())

    expect(screen.getByTestId('conversion-summary').textContent)
      .toBe('2 of 4 applications reached an interview (50%)')
    expect(screen.getByTestId('group-lever')).toBeTruthy()
    // A real 0% is a fact: two applications, no interviews.
    expect(screen.getByTestId('group-remoteok').textContent).toContain('0%')
    // A group with nothing to average renders a dash, never a fabricated 0.
    expect(screen.getByTestId('group-remoteok').textContent).toMatch(/—/)
    expect(screen.getByTestId('report-disclaimer').textContent).toMatch(/never inferred from email/)
    expect(screen.getByTestId('by-origin').textContent).toMatch(/system observed 1/i)
    expect(screen.getByText(/Counts as an interview: Interview scheduled, Interview done, Offer received/))
      .toBeTruthy()

    // The dimension is the server's to interpret: changing it re-reads.
    fireEvent.change(screen.getByTestId('group-by'), { target: { value: 'artifact_version' } })
    await waitFor(() => expect(get.mock.calls.some((call: unknown[]) => call[0] === `${API}/report`
      && (call[1] as { params?: Record<string, unknown> })?.params?.group_by === 'artifact_version'))
      .toBe(true))
  })

  it('reports an empty funnel as empty, not as zero percent', async () => {
    get.mockImplementation((url: string) => {
      if (url === `${API}/report`) {
        return Promise.resolve({ data: { ...report, totals: { ...report.totals, tracked: 1, applied: 0,
          interviews: 0, application_to_interview_rate: null, applied_to_response_rate: null,
          interview_to_offer_rate: null, by_origin: {} }, groups: [], group_count: 0 } })
      }
      if (url === `${API}/meta`) return Promise.resolve({ data: meta })
      return Promise.resolve({ data: {} })
    })
    renderPage('?tab=report')
    await waitFor(() => expect(screen.getByTestId('conversion-summary')).toBeTruthy())
    expect(screen.getByTestId('conversion-summary').textContent).toBe('No applications recorded yet')
    expect(screen.getByText(/Nothing applied yet — no conversion to report/)).toBeTruthy()
    expect(screen.getByTestId('by-origin').textContent).toMatch(/—/)
  })

  it('shows the reminder agenda and the sweep that made it exist', async () => {
    renderPage('?tab=follow-ups')
    await waitFor(() => expect(screen.getByTestId('follow-up-4')).toBeTruthy())

    expect(screen.getByTestId('sweep').textContent).toMatch(/1 reminder\(s\) written just now/)
    expect(screen.getByTestId('due-4').textContent).toMatch(/Overdue/)
    expect(screen.getByTestId('due-6').textContent).not.toMatch(/Overdue/)
    expect(screen.getByText(/application_follow_up/)).toBeTruthy()
    expect(screen.getByText(/Nothing is sent to anybody/)).toBeTruthy()
    expect(get.mock.calls.some((call: unknown[]) => call[0] === `${API}/follow-ups`
      && (call[1] as { params?: Record<string, unknown> })?.params?.days === 30)).toBe(true)

    // Opening a reminder takes the user to that record's timeline.
    fireEvent.click(screen.getByTestId('open-follow-up-6'))
    await waitFor(() => expect(get.mock.calls.some((call: unknown[]) => String(call[0])
      .startsWith(`${API}/6`))).toBe(true))
    await waitFor(() => expect(screen.getByTestId('selected-state')).toBeTruthy())
    expect(screen.getByTestId('tab-board').className).toMatch(/shadow/)
  })

  it('renders an empty agenda as an empty agenda', async () => {
    get.mockImplementation((url: string) => {
      if (url === `${API}/follow-ups`) {
        return Promise.resolve({ data: { ...followUps, items: [], sweep: { due: 0, notified: 0,
          skipped: 0 } } })
      }
      if (url === `${API}/meta`) return Promise.resolve({ data: meta })
      return Promise.resolve({ data: {} })
    })
    renderPage('?tab=follow-ups')
    await waitFor(() => expect(screen.getByTestId('no-follow-ups')).toBeTruthy())
    expect(screen.getByTestId('sweep').textContent).toMatch(/nothing due right now/)
  })

  it('says when a read fails instead of rendering an empty board', async () => {
    get.mockImplementation((url: string) => {
      if (url === API) {
        return Promise.reject({ response: { status: 500, data: { detail: { message: 'boom' } } } })
      }
      if (url === `${API}/meta`) return Promise.resolve({ data: meta })
      return Promise.resolve({ data: {} })
    })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('error')).toBeTruthy())
    expect(screen.getByTestId('error').textContent).toBe('boom')
  })
})
