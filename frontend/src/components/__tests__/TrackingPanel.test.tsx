/**
 * The Jobs drawer's tracking panel — "the timeline is visible from the
 * application detail page".
 *
 * What must not drift:
 *
 *   • an untracked job renders an honest "not tracked yet" with the one button
 *     that starts it, and a `GET` never creates a row;
 *   • a tracked job renders its history with the origin of every row, and a
 *     correction leaves the row it corrects on screen;
 *   • "I got an interview" posts the user's own date to the interview route and
 *     renders the document the write answered with (no second read);
 *   • a replayed write is told "already recorded" instead of adding a row;
 *   • a button the server disabled stays disabled and says why.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import client from '../../api/client'
import TrackingPanel from '../TrackingPanel'
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

function timelineEvent(over: Partial<TrackingEvent> = {}): TrackingEvent {
  return {
    id: 1, event_id: 'e-1', sequence: 1, event_type: 'application.tracking_opened',
    state_from: null, state_from_label: null, state_to: 'applied', state_to_label: 'Applied',
    phase: 'applied', origin: 'system_observed', actor_type: 'system_worker',
    actor_label: 'system_worker', trigger: 'auto', severity: 'info',
    message: 'Application submitted by JobHunter (automation).', note: '',
    payload: { external_receipt_id: 'abc-123' }, evidence: [], is_correction: false,
    correction_of_event_id: null, correction_reason: '', is_provisional: false,
    follow_up_at: null, occurred_at: '2026-09-10T09:00:00Z', recorded_at: '2026-09-10T09:00:00Z',
    late_reported: false, ...over,
  }
}

const opened = timelineEvent()
const interview = timelineEvent({
  id: 2, event_id: 'e-2', sequence: 2, event_type: 'application.interview_reported',
  state_from: 'applied', state_from_label: 'Applied', state_to: 'interview_scheduled',
  state_to_label: 'Interview scheduled', origin: 'user_reported', actor_type: 'user',
  actor_label: 'ada@example.com', trigger: 'user', severity: 'success',
  message: 'Interview reported.', note: 'Prepare the payments case study',
  payload: { format: 'video', round: 'Hiring manager' },
  occurred_at: '2026-09-12T18:00:00Z', recorded_at: '2026-09-19T08:00:00Z', late_reported: true,
})
const correction = timelineEvent({
  id: 3, event_id: 'e-3', sequence: 3, event_type: 'application.state_corrected',
  state_from: 'interview_scheduled', state_from_label: 'Interview scheduled',
  state_to: 'recruiter_response', state_to_label: 'Recruiter responded', origin: 'user_reported',
  actor_type: 'user', actor_label: 'ada@example.com', trigger: 'user', severity: 'warning',
  message: 'Corrected to Recruiter responded (was Interview scheduled).', note: '',
  is_correction: true, correction_of_event_id: 2,
  correction_reason: 'That was a screening call, not an interview',
  payload: { correction_of_sequence: 2 }, occurred_at: '2026-09-19T09:00:00Z',
  recorded_at: '2026-09-19T09:00:00Z',
})

function document(over: Partial<TrackingDocument> = {}): TrackingDocument {
  return {
    tracking_id: 4, job_id: 9,
    job: { id: 9, title: 'Senior Backend Engineer', company: 'FinCo',
           url: 'https://jobs.lever.co/finco/1', status: 'applied', source: 'lever' },
    state: 'recruiter_response', state_label: 'Recruiter responded', phase: 'in_conversation',
    phase_label: 'In Conversation', job_status: 'applied', state_origin: 'user_reported',
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
    follow_up: { due_at: '2099-01-01T09:00:00Z', note: 'Send the thank-you note',
                 notified_at: null, completed_at: null, overdue: false, pending: true },
    counts: { events: 3, corrections: 1, last_sequence: 3 },
    note: '',
    transitions: { allowed: ['interview_scheduled', 'interview_completed', 'offer_received',
                             'rejected_by_employer', 'withdrawn'],
                   allowed_labels: [], terminal: false },
    timeline: [opened, interview, correction],
    actions: [
      { key: 'interview', label: 'I got an interview', method: 'POST',
        route: '/api/application-tracking/4/interview', primary: true, disabled: false,
        disabled_reason: null },
      { key: 'response', label: 'Recruiter responded', method: 'POST',
        route: '/api/application-tracking/4/response', primary: false, disabled: true,
        disabled_reason: 'Not reachable from this state.' },
      { key: 'note', label: 'Add note', method: 'POST', route: '/api/application-tracking/4/note',
        primary: false, disabled: false, disabled_reason: null },
      { key: 'follow_up', label: 'Set follow-up', method: 'POST',
        route: '/api/application-tracking/4/follow-up', primary: false, disabled: false,
        disabled_reason: null },
      { key: 'follow_up_done', label: 'Follow-up done', method: 'POST',
        route: '/api/application-tracking/4/follow-up/complete', primary: false, disabled: false,
        disabled_reason: null },
      { key: 'correct', label: 'Correct status', method: 'POST',
        route: '/api/application-tracking/4/correction', primary: false, disabled: false,
        disabled_reason: null },
      { key: 'open_job', label: 'Open job', method: 'GET', route: '/api/jobs/9', primary: false,
        disabled: false, disabled_reason: null },
    ],
    server_time: '2026-09-19T12:00:00Z', exists: true, ...over,
  }
}

const untracked = {
  exists: false,
  job: { id: 9, title: 'Senior Backend Engineer', company: 'FinCo',
         url: 'https://jobs.lever.co/finco/1', status: 'discovered', source: 'lever' },
  actions: [{ key: 'open', label: 'Track this application', method: 'POST',
              route: '/api/application-tracking', primary: true, disabled: false,
              disabled_reason: null }],
  snapshot: { source: 'lever', role_family: 'Backend Engineer', match_score: 88,
              match_band: 'strong', artifact_kind: 'none' },
  server_time: '2026-09-19T12:00:00Z',
}

const wrapper = ({ children }: { children: React.ReactNode }) => <MemoryRouter>{children}</MemoryRouter>

describe('TrackingPanel (the Jobs drawer)', () => {
  beforeEach(() => {
    get.mockReset()
    post.mockReset()
    get.mockImplementation((url: string) => {
      if (url.startsWith('/api/application-tracking/job/9')) {
        return Promise.resolve({ data: untracked })
      }
      return Promise.resolve({ data: {} })
    })
  })
  afterEach(() => cleanup())

  it('renders an untracked job honestly and starts the record on request', async () => {
    post.mockResolvedValue({ data: { ok: true, duplicate: false, state_changed: true, event: null,
                                     events: [], document: document({ state: 'not_applied',
                                       state_label: 'Not applied', timeline: [opened] }),
                                     state: 'not_applied', phase: 'pre_application',
                                     server_time: '2026-09-19T12:00:01Z' } })
    render(<TrackingPanel jobId={9} />, { wrapper })

    await waitFor(() => expect(screen.getByTestId('tracking-untracked')).toBeTruthy())
    expect(screen.getByText(/Not tracked yet/)).toBeTruthy()
    // A read must not have created anything: only the one GET happened.
    expect(post).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTestId('start-tracking'))
    await waitFor(() => expect(screen.getByTestId('tracking-panel')).toBeTruthy())
    expect(post).toHaveBeenCalledTimes(1)
    expect(post.mock.calls[0][0]).toBe('/api/application-tracking')
    expect(post.mock.calls[0][1]).toEqual({ job_id: 9 })
    expect(screen.getByTestId('tracking-state').textContent).toBe('Not applied')
  })

  it('shows the history with the origin of every row, corrections included', async () => {
    get.mockImplementation((url: string) => Promise.resolve(
      url.startsWith('/api/application-tracking/job/9') ? { data: document() } : { data: {} },
    ))
    render(<TrackingPanel jobId={9} />, { wrapper })

    await waitFor(() => expect(screen.getByTestId('tracking-panel')).toBeTruthy())
    expect(screen.getByTestId('tracking-state').textContent).toBe('Recruiter responded')
    expect(screen.getByTestId('follow-up-line').textContent).toContain('Due')
    expect(screen.getByTestId('snapshot-match').textContent).toBe('88 (strong) — AI verified')
    expect(screen.getByTestId('snapshot-artifact').textContent).toBe('Application packet v3')

    // All three rows, oldest first, each saying who claims it.
    expect(screen.getByTestId('timeline-event-1')).toBeTruthy()
    expect(screen.getByTestId('timeline-event-3')).toBeTruthy()
    expect(screen.getByTestId('origin-1').textContent).toBe('System observed')
    expect(screen.getByTestId('origin-2').textContent).toBe('You reported')
    // The corrected row is still there, and the correction names its reason.
    expect(screen.getByTestId('timeline-event-2').getAttribute('data-correction')).toBe('false')
    expect(screen.getByTestId('timeline-event-3').getAttribute('data-correction')).toBe('true')
    expect(screen.getByText(/That was a screening call, not an interview/)).toBeTruthy()
    expect(screen.getByText(/the original row is still above/)).toBeTruthy()
    // A fact written down a week after it happened says so.
    expect(screen.getByTitle(/Happened 12 Sep 2026, 18:00, written down 19 Sep 2026, 08:00/)).toBeTruthy()
    // The drawer links to the full board.
    expect(screen.getByTestId('open-full-timeline').getAttribute('href'))
      .toBe('/tracking?tracking_id=4')
  })

  it('records an interview the user typed, and renders the answer it got back', async () => {
    get.mockImplementation((url: string) => Promise.resolve(
      url.startsWith('/api/application-tracking/job/9') ? { data: document() } : { data: {} },
    ))
    const after = document({ state: 'interview_scheduled', state_label: 'Interview scheduled',
                             timeline: [opened, interview, correction,
                                        timelineEvent({ id: 4, event_id: 'e-4', sequence: 4,
                                          event_type: 'application.interview_reported',
                                          state_to: 'interview_scheduled',
                                          state_to_label: 'Interview scheduled',
                                          origin: 'user_reported', actor_type: 'user' })] })
    post.mockResolvedValue({ data: { ok: true, duplicate: false, state_changed: true, event: null,
                                     events: [], document: after, state: 'interview_scheduled',
                                     phase: 'interviewing', server_time: '2026-09-19T12:00:02Z' } })
    render(<TrackingPanel jobId={9} />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('action-interview')).toBeTruthy())

    fireEvent.click(screen.getByTestId('action-interview'))
    await waitFor(() => expect(screen.getByTestId('form-interview')).toBeTruthy())
    fireEvent.change(screen.getByTestId('interview-at'), { target: { value: '2026-09-25T14:00' } })
    fireEvent.change(screen.getByTestId('follow-up-at'), { target: { value: '2026-09-28T09:00' } })
    fireEvent.click(screen.getByTestId('form-submit'))

    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post.mock.calls[0][0]).toBe('/api/application-tracking/4/interview')
    const body = post.mock.calls[0][1]
    expect(String(body.interview_at)).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/)
    expect(body.format).toBe('video')
    expect(String(body.follow_up_at)).toMatch(/Z$/)
    // The write's own document is what renders — no second read.
    await waitFor(() => expect(screen.getByTestId('tracking-state').textContent)
      .toBe('Interview scheduled'))
    expect(get).toHaveBeenCalledTimes(1)
  })

  it('tells the user when a write was a replay rather than a new fact', async () => {
    get.mockImplementation((url: string) => Promise.resolve(
      url.startsWith('/api/application-tracking/job/9') ? { data: document() } : { data: {} },
    ))
    post.mockResolvedValue({ data: { ok: true, duplicate: true, state_changed: false, event: null,
                                     events: [], document: document(), state: 'recruiter_response',
                                     phase: 'in_conversation', server_time: '2026-09-19T12:00:03Z' } })
    render(<TrackingPanel jobId={9} />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('action-note')).toBeTruthy())

    fireEvent.click(screen.getByTestId('action-note'))
    fireEvent.change(screen.getByTestId('note-input'), { target: { value: 'Still waiting' } })
    fireEvent.click(screen.getByTestId('form-submit'))

    await waitFor(() => expect(screen.getByTestId('tracking-message')).toBeTruthy())
    expect(screen.getByTestId('tracking-message').textContent).toMatch(/Already recorded/)
    expect(post.mock.calls[0][0]).toBe('/api/application-tracking/4/note')
    expect(post.mock.calls[0][1]).toEqual({ note: 'Still waiting' })
  })

  it('keeps a correction honest: the reason is required before anything is sent', async () => {
    get.mockImplementation((url: string) => Promise.resolve(
      url.startsWith('/api/application-tracking/job/9') ? { data: document() } : { data: {} },
    ))
    render(<TrackingPanel jobId={9} />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('action-correct')).toBeTruthy())

    fireEvent.click(screen.getByTestId('action-correct'))
    fireEvent.change(screen.getByTestId('correction-state'), { target: { value: 'interview_scheduled' } })
    fireEvent.change(screen.getByTestId('correction-reason'), { target: { value: 'x' } })
    fireEvent.click(screen.getByTestId('form-submit'))

    await waitFor(() => expect(screen.getByTestId('tracking-error')).toBeTruthy())
    expect(screen.getByTestId('tracking-error').textContent).toMatch(/needs a reason/)
    expect(post).not.toHaveBeenCalled()

    fireEvent.change(screen.getByTestId('correction-reason'),
      { target: { value: 'It really was an interview, I mis-clicked' } })
    post.mockResolvedValue({ data: { ok: true, duplicate: false, state_changed: true, event: null,
                                     events: [], document: document({ state: 'interview_scheduled',
                                       state_label: 'Interview scheduled' }),
                                     state: 'interview_scheduled', phase: 'interviewing',
                                     server_time: '2026-09-19T12:00:04Z' } })
    fireEvent.click(screen.getByTestId('form-submit'))
    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post.mock.calls[0][0]).toBe('/api/application-tracking/4/correction')
    expect(post.mock.calls[0][1]).toMatchObject({ state: 'interview_scheduled',
      reason: 'It really was an interview, I mis-clicked' })
  })

  it('renders a button the server disabled as disabled, with its reason', async () => {
    get.mockImplementation((url: string) => Promise.resolve(
      url.startsWith('/api/application-tracking/job/9') ? { data: document() } : { data: {} },
    ))
    render(<TrackingPanel jobId={9} />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('action-response')).toBeTruthy())
    const disabled = screen.getByTestId('action-response') as HTMLButtonElement
    expect(disabled.disabled).toBe(true)
    expect(disabled.title).toBe('Not reachable from this state.')
    // "Open job" is the drawer's own business, not a tracking action.
    expect(screen.queryByTestId('action-open_job')).toBeNull()
  })

  it('shows the refusal when a write fails', async () => {
    get.mockImplementation((url: string) => Promise.resolve(
      url.startsWith('/api/application-tracking/job/9') ? { data: document() } : { data: {} },
    ))
    post.mockRejectedValue({ response: { status: 409, data: { detail: {
      code: 'invalid_state_transition',
      message: 'Cannot move from ‘Recruiter responded’ to ‘Offer received’.' } } } })
    render(<TrackingPanel jobId={9} />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('action-note')).toBeTruthy())
    fireEvent.click(screen.getByTestId('action-note'))
    fireEvent.change(screen.getByTestId('note-input'), { target: { value: 'They offered me the job' } })
    fireEvent.click(screen.getByTestId('form-submit'))

    await waitFor(() => expect(screen.getByTestId('tracking-error')).toBeTruthy())
    expect(screen.getByTestId('tracking-error').textContent).toMatch(/Cannot move from/)
  })
})
