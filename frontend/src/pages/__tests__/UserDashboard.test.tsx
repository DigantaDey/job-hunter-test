/**
 * The end-user dashboard (v2.3) — states and content contract.
 *
 * The dashboard is the product surface: one aggregate read
 * (`GET /api/me/dashboard`) rendered with the four honest states —
 *
 *   • loading  — a skeleton until the first verdict, never a stuck spinner;
 *   • data     — outcomes in user language (matches with reasons, the
 *                attention queue, applications, interviews, weekly report);
 *   • empty    — guidance, not blanks ("run your first discovery");
 *   • error    — a retry card when the first load fails, "Retrying…" on the
 *                button, never back to the skeleton;
 *   • offline  — a distinct offline banner/card driven by browser events.
 *
 * It also pins the metric rule: the user surface never renders token or cost
 * vocabulary — success is interviews and applications, and the API contract
 * keeps provider material on the owner surface.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import client from '../../api/client'
import { AIWorkProvider } from '../../context/AIWorkContext'
import Dashboard from '../Dashboard'

vi.mock('../../api/client', () => {
  const get = vi.fn()
  const post = vi.fn()
  const put = vi.fn()
  return {
    default: { get, post, put },
    apiError: (_e: unknown, fb?: string) => fb ?? 'Cannot reach the API.',
  }
})
const get = client.get as unknown as Mock

/* ── fixtures ───────────────────────────────────────────────────────── */

const DASHBOARD = {
  server_time: '2026-09-20T10:00:00',
  profile: {
    percent: 80, missing: [{ field: 'phone', label: 'Phone number', weight: 10 }],
    has_master_resume: true, next_step: 'Add your phone number to improve your matches.',
  },
  discovery: {
    state: 'results', detail: 'Fresh matches are waiting on your Jobs page.',
    total_discovered: 42, new_matches_7d: 6, strong_matches: 5,
    last_run_at: '2026-09-19T18:00:00', last_run_state: 'done', never_discovered: false,
  },
  top_matches: [{
    job_id: 1, title: 'Senior Backend Engineer', company: 'Acme', location: 'Remote',
    score: 86, band: 'strong', status: 'discovered', score_source: 'ai',
    why: ['8 of 10 required skills match your profile', 'Your Python and FastAPI depth covers the core stack'],
    missing_skills: ['Kafka'], discovered_at: '2026-09-19T18:00:00', route: '/jobs?job=1',
  }],
  needs_review: [{ job_id: 2, title: 'Platform Engineer', company: 'BetaCo', reason: '1 question(s) waiting for your answers', pending_questions: 1, route: '/queues' }],
  applications_in_progress: [{ job_id: 3, title: 'Data Engineer', company: 'GammaCo', stage: 'preparing', stage_label: 'Preparing your application', updated_at: '2026-09-19T12:00:00', route: '/jobs?job=3' }],
  action_queue: [
    { kind: 'questions', id: 7, title: 'Answer 1 question(s) for BetaCo', subtitle: 'Platform Engineer', detail: '1 required', created_at: null, route: '/queues' },
  ],
  interviews: {
    upcoming: [{ job_id: 1, title: 'Senior Backend Engineer', company: 'Acme', when: '2026-09-25T10:00:00', state: 'interview_scheduled', route: '/tracking?tracking_id=1' }],
    recent: [], practice_sessions: [], practice_completed: 1, upcoming_count: 1,
  },
  weekly_report: {
    week_started_at: '2026-09-13T10:00:00', generated_at: '2026-09-20T10:00:00',
    applications_submitted: 3, applications_prev_week: 1, responses: 1, interviews: 1,
    rejections: 0, new_matches: 6, strong_matches_open: 5, outreach_sent: 2, outreach_replies: 1,
    headline: 'You submitted 3 applications this week. 1 responded. 1 interview — nice work.',
    top_match: { title: 'Senior Backend Engineer', company: 'Acme', score: 86 },
  },
  follow_ups: {
    items: [{ tracking_id: 1, job_id: 1, title: 'Senior Backend Engineer', company: 'Acme', state: 'applied', state_label: 'Applied', due_at: '2026-09-18T10:00:00', note: '', overdue: true, notified_at: null, route: '/tracking?tracking_id=1' }],
    overdue_count: 1,
  },
  assistant: { configured: true, paused_items: 0, message: 'Your assistant is ready to help.' },
  counts: { jobs_total: 42, needs_input: 1, applied: 3, applications_running: 1 },
}

const EMPTY_DASHBOARD = {
  server_time: '2026-09-20T10:00:00',
  profile: { percent: 20, missing: [{ field: 'master_resume', label: 'Master resume upload', weight: 10 }], has_master_resume: false, next_step: 'Add your master resume upload to improve your matches.' },
  discovery: { state: 'idle', detail: 'Discovery has not run yet — find your first matches to get started.', total_discovered: 0, new_matches_7d: 0, strong_matches: 0, last_run_at: null, last_run_state: '', never_discovered: true },
  top_matches: [], needs_review: [], applications_in_progress: [], action_queue: [],
  interviews: { upcoming: [], recent: [], practice_sessions: [], practice_completed: 0, upcoming_count: 0 },
  weekly_report: { applications_submitted: 0, responses: 0, interviews: 0, rejections: 0, new_matches: 0, applications_prev_week: 0, outreach_replies: 0, strong_matches_open: 0, headline: 'No new outcomes this week yet — run discovery and review your top matches.', top_match: null },
  follow_ups: { items: [], overdue_count: 0 },
  assistant: { configured: true, paused_items: 0, message: 'Your assistant is ready to help.' },
  counts: { jobs_total: 0, needs_input: 0, applied: 0, applications_running: 0 },
}

/** `GET /api/queues/ai` — the live layer behind AIWorkProvider. */
const LIVE = { counts: { queued: 0, processing: 0, paused: 0, needs_input: 0 }, items: [], recent: [], processing_now: null }

const dataFor = (url: string) => (url === '/api/me/dashboard' ? DASHBOARD : url === '/api/queues/ai' ? LIVE : {})
const defaultOk = (url: string) => Promise.resolve({ data: dataFor(url) })

/* ── helpers ────────────────────────────────────────────────────────── */
function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
  document.dispatchEvent(new Event('visibilitychange'))
}

function goOnline(online: boolean) {
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: online })
  window.dispatchEvent(new Event(online ? 'online' : 'offline'))
}

function wrapper({ children }: { children: React.ReactNode }) {
  return <MemoryRouter><AIWorkProvider>{children}</AIWorkProvider></MemoryRouter>
}

/** Drain microtasks until the fetch chain settles. */
async function flush(rounds = 10) {
  for (let i = 0; i < rounds; i++) await act(async () => { await Promise.resolve() })
}

/* ── suite ──────────────────────────────────────────────────────────── */
describe('User dashboard', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setVisibility('visible')
    goOnline(true)
    get.mockReset()
    get.mockImplementation(defaultOk)
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('shows a loading skeleton first, then the product sections — never a stuck spinner', async () => {
    render(<Dashboard />, { wrapper })
    expect(screen.getByTestId('dashboard-loading')).toBeTruthy()
    await flush()
    expect(screen.getByTestId('user-dashboard')).toBeTruthy()
    expect(screen.queryByTestId('dashboard-loading')).toBeNull()
  })

  it('renders the actionable work: attention queue, matches with reasons, stage labels', async () => {
    render(<Dashboard />, { wrapper })
    await flush()

    // The attention queue is the user's work, not infrastructure.
    expect(screen.getByTestId('action-count').textContent).toContain('1 item')
    expect(screen.getByText('Answer 1 question(s) for BetaCo')).toBeTruthy()

    // Top matches explain WHY.
    expect(screen.getAllByText('Senior Backend Engineer').length).toBeGreaterThan(0)
    const whys = screen.getAllByTestId('match-why')
    expect(whys.length).toBeGreaterThan(0)
    expect(whys[0].textContent).toContain('8 of 10 required skills match your profile')

    // Applications carry a human stage label.
    expect(screen.getByText('Preparing your application')).toBeTruthy()

    // The weekly report leads with outcomes.
    expect(screen.getByTestId('weekly-headline').textContent).toContain('3 applications this week')
    expect(screen.getByText('Interviews')).toBeTruthy()

    // Follow-ups: the overdue reminder is visible.
    expect(screen.getByTestId('follow-ups').textContent).toContain('due now')
  })

  it('never renders token, cost or provider vocabulary', async () => {
    render(<Dashboard />, { wrapper })
    await flush()
    const text = document.body.textContent || ''
    for (const forbidden in { 'token': 1, 'cost': 1, 'api key': 1, 'base_url': 1, 'RPM': 1 }) {
      expect(text.toLowerCase()).not.toContain(forbidden)
    }
  })

  it('shows guidance empty states when there is no data yet', async () => {
    get.mockImplementation((url: string) =>
      Promise.resolve({ data: url === '/api/me/dashboard' ? EMPTY_DASHBOARD : url === '/api/queues/ai' ? LIVE : {} }))
    render(<Dashboard />, { wrapper })
    await flush()

    expect(screen.getByText('No matches yet')).toBeTruthy()
    expect(screen.getByText('Run discovery')).toBeTruthy()
    expect(screen.getByText("You're all caught up")).toBeTruthy()
    expect(screen.getByText('Nothing in the pipeline right now')).toBeTruthy()
    expect(screen.getByText('No interviews yet')).toBeTruthy()
    expect(screen.getByText('No follow-ups due')).toBeTruthy()
    expect(screen.getByTestId('discovery-state').textContent).toContain('Not started yet')
  })

  it('the first failed load shows an error card with a retry that recovers — never the skeleton again', async () => {
    get.mockImplementation((url: string) =>
      url === '/api/me/dashboard' ? Promise.reject(new Error('boom')) : Promise.resolve({ data: LIVE }))
    render(<Dashboard />, { wrapper })
    await flush()

    expect(screen.getByTestId('dashboard-error')).toBeTruthy()
    expect(screen.getByTestId('error-state')).toBeTruthy()
    expect(screen.queryByTestId('dashboard-loading')).toBeNull()

    // Retry → the aggregate read happens again and the page recovers.
    get.mockImplementation(defaultOk)
    fireEvent.click(screen.getByText('Try again'))
    await flush()
    expect(screen.getByTestId('user-dashboard')).toBeTruthy()
    expect(get.mock.calls.filter((c: any[]) => c[0] === '/api/me/dashboard').length).toBe(2)
  })

  it('offline: a failed load shows the offline card, and the banner shows when the connection drops', async () => {
    goOnline(false)
    get.mockImplementation((url: string) =>
      url === '/api/me/dashboard' ? Promise.reject(new Error('network down')) : Promise.resolve({ data: LIVE }))
    render(<Dashboard />, { wrapper })
    await flush()

    expect(screen.getByTestId('offline-error')).toBeTruthy()
    expect(screen.getByTestId('offline-banner')).toBeTruthy()
  })

  it('a load that succeeded before the connection dropped keeps rendering with the offline banner', async () => {
    render(<Dashboard />, { wrapper })
    await flush()
    expect(screen.getByTestId('user-dashboard')).toBeTruthy()

    goOnline(false)
    await act(async () => { await Promise.resolve() })
    expect(screen.getByTestId('offline-banner')).toBeTruthy()
    // The last synced data is still on screen — nothing blanked out.
    expect(screen.getAllByText('Senior Backend Engineer').length).toBeGreaterThan(0)
  })

  it('re-reads every 60 s while visible, and nothing while hidden', async () => {
    render(<Dashboard />, { wrapper })
    await flush()
    const calls = () => get.mock.calls.filter((c: any[]) => c[0] === '/api/me/dashboard').length
    expect(calls()).toBe(1)

    await act(async () => { vi.advanceTimersByTime(60_000) })
    await flush()
    expect(calls()).toBe(2)

    setVisibility('hidden')
    await act(async () => { vi.advanceTimersByTime(600_000) })
    await flush()
    expect(calls()).toBe(2)

    setVisibility('visible')
    await flush()
    expect(calls()).toBe(3)
  })

  it('a failed background refresh keeps the last good read on screen', async () => {
    render(<Dashboard />, { wrapper })
    await flush()
    expect(screen.getByTestId('user-dashboard')).toBeTruthy()

    get.mockImplementation((url: string) =>
      url === '/api/me/dashboard' ? Promise.reject(new Error('late tick')) : Promise.resolve({ data: LIVE }))
    await act(async () => { vi.advanceTimersByTime(60_000) })
    await flush()
    // No error card swapped in, no blank page: the stale-but-real data stays.
    expect(screen.getByTestId('user-dashboard')).toBeTruthy()
    expect(screen.queryByTestId('dashboard-error')).toBeNull()
  })
})
