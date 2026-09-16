/**
 * Dashboard error recovery (P1 #4) and the live summary poll.
 *
 * The page fires five reads on mount — summary, jobs, performance, profile,
 * billing. Before the fix a failed read was swallowed by a silent fallback
 * (or, for the summary, a zero object) and the user got no error, no retry
 * and, on the unlucky paths, a "Loading dashboard…" screen that never left.
 * After the fix the page should:
 *   • spin only until the summary's *first* verdict — success or failure —
 *     and never on an error path again (the spinner is an explicit `loading`
 *     state that only ever closes, and the client's 30 s timeout bounds every
 *     request); a retry from the error card shows "Retrying…" in place of the
 *     spinner, and never sends the user back to the loading screen;
 *   • when every read fails, show a clear error card with a working Retry
 *     button that re-fetches all five and renders the page on success;
 *   • when some reads fail, render everything that succeeded and show a
 *     non-blocking banner naming the failed sections with their messages,
 *     whose retry re-fetches exactly those sections — nothing else;
 *   • re-read the summary every 30 s while the tab is *visible* (same
 *     visibility-aware cadence as Settings' auto-mode card): nothing at all
 *     while hidden, an immediate re-read when it becomes visible, a failed
 *     tick keeps the last read, and the timer + listener are cleaned up on
 *     unmount.
 *
 * Same contract as `pages/__tests__/SettingsPolling.test.tsx` and
 * `pages/__tests__/QueuesPolling.test.tsx`.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import client from '../../api/client'
import { AIWorkProvider } from '../../context/AIWorkContext'
import Dashboard from '../Dashboard'

/* ── mocks ──────────────────────────────────────────────────────────── */
vi.mock('../../api/client', () => {
  const get = vi.fn()
  return {
    default: { get },
    apiError: (_e: unknown, fb?: string) => fb ?? 'Cannot reach the API. Is the backend running?',
  }
})
const get = client.get as unknown as Mock

/* ── fixtures ───────────────────────────────────────────────────────── */
const SUMMARY = {
  jobs: { total: 42, last_24h: 3, needs_input: 2, high_match: 5, applied: 10 },
  entitlements: {
    plan: 'pro', plan_label: 'Pro',
    capabilities: { can_use_advanced_matching: true },
    limits: { ai_credits_per_month: 50000, outreach_per_month: 10, automation_runs_per_month: 5 },
    usage: { automation_runs_per_month: { used: 2 } },
  },
  ai_usage: { credits_used: 1234, cost_usd: 1.5 },
  applications: { submitted: 8, response_rate: 25 },
  outreach: { sent: 4, pending_approval: 1 },
  automation: { running: 1, completed: 7, failed: 2, needs_input: 0, auto_mode: false, auto_mode_active: false, next_run: null, queues: {} },
  vault: 3,
  resumes: { total: 2, pending_approval: 0 },
  notifications: { unread: 5 },
}
const JOBS = [{ id: 1, company: 'Acme Corp', title: 'Frontend Engineer', source: 'indeed', company_size: '51-200', score: 82, status: 'discovered' }]
const PERFORMANCE = { summary: { total_jobs: 20, applied: 10, interview_rate: 10, best_role: 'Engineer', best_role_rate: 15 }, skills: { strongest: [{ skill: 'React' }], weakest: [{ skill: 'Go' }] } }
const PROFILE = { data: { name: 'Ada Lovelace', email: 'ada@example.com', skills: ['React', 'TypeScript'] } }
const BILLING = { plan: 'pro', status: 'active' }
/** `GET /api/queues/ai` — the global live layer behind the queue counters. */
const LIVE = { counts: { queued: 0, processing: 0, paused: 0, needs_input: 0 }, items: [], recent: [], processing_now: null }

const dataFor = (url: string) => {
  if (url === '/api/dashboard/summary') return SUMMARY
  if (url === '/api/jobs?limit=6') return JOBS
  if (url === '/api/analytics/performance') return PERFORMANCE
  if (url === '/api/profile/current') return PROFILE
  if (url === '/api/billing/subscription') return BILLING
  if (url === '/api/queues/ai') return LIVE
  return {}
}
const defaultOk = (url: string) => Promise.resolve({ data: dataFor(url) })

/* ── helpers ────────────────────────────────────────────────────────── */
function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
  document.dispatchEvent(new Event('visibilitychange'))
}

/** The real live-layer provider, like the other polling tests. */
function wrapper({ children }: { children: React.ReactNode }) {
  return <MemoryRouter><AIWorkProvider>{children}</AIWorkProvider></MemoryRouter>
}

/** The five reads settle on resolved microtasks; drain the queue. */
async function flush(rounds = 12) {
  for (let i = 0; i < rounds; i++) await act(async () => { await Promise.resolve() })
}

async function advance(ms: number) {
  await act(async () => { vi.advanceTimersByTime(ms) })
  await flush()
}

const callsTo = (url: string) => get.mock.calls.filter((c: any[]) => c[0] === url).length
const summaryCalls = () => callsTo('/api/dashboard/summary')
const jobsCalls = () => callsTo('/api/jobs?limit=6')
const performanceCalls = () => callsTo('/api/analytics/performance')

/* ── suite ──────────────────────────────────────────────────────────── */
describe('Dashboard error recovery (P1 #4)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setVisibility('visible')
    get.mockReset()
    get.mockImplementation(defaultOk)
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('spins only until the summary settles — even if the other reads are still in flight', async () => {
    let resolveSummary: (v: { data: any }) => void
    const held = new Promise<{ data: any }>(r => { resolveSummary = r })
    get.mockImplementation((url: string) => url === '/api/dashboard/summary' ? held : defaultOk(url))

    render(<Dashboard />, { wrapper })
    await flush()
    expect(screen.getByText('Loading dashboard…')).toBeTruthy()

    await act(async () => { resolveSummary!({ data: SUMMARY }) })
    await flush()
    expect(screen.queryByText('Loading dashboard…')).toBeNull()
    expect(screen.getByText('42')).toBeTruthy()
    expect(screen.getByText('Welcome back, Ada.')).toBeTruthy()
  })

  it('shows an error card with a Retry button when every read fails — no spinner, no zeros', async () => {
    get.mockImplementation(() => Promise.reject(new Error('network down')))
    render(<Dashboard />, { wrapper })
    await flush()

    expect(screen.queryByText('Loading dashboard…')).toBeNull()
    expect(screen.getByText(/Couldn't load the dashboard/)).toBeTruthy()
    expect(screen.getByText(/Cannot reach the API/)).toBeTruthy()

    // Backend comes back; the same button now loads the whole page.
    get.mockImplementation(defaultOk)
    fireEvent.click(screen.getByRole('button', { name: /Retry/ }))
    await flush()

    expect(screen.queryByText(/Couldn't load the dashboard/)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByText('42')).toBeTruthy()
    expect(screen.getByText('Frontend Engineer')).toBeTruthy()
    expect([summaryCalls(), jobsCalls(), performanceCalls()]).toEqual([2, 2, 2])
  })

  it('re-fetches all five sections on retry — and re-fails into the error card again', async () => {
    get.mockImplementation(() => Promise.reject(new Error('network down')))
    render(<Dashboard />, { wrapper })
    await flush()

    fireEvent.click(screen.getByRole('button', { name: /Retry/ }))
    await flush()
    // One full round per attempt: five reads, five failures, same card again.
    expect([summaryCalls(), jobsCalls(), performanceCalls()]).toEqual([2, 2, 2])
    expect(screen.getByText(/Couldn't load the dashboard/)).toBeTruthy()
  })

  it('a retry from the error card shows "Retrying…" in place of the spinner — never back to loading', async () => {
    get.mockImplementation(() => Promise.reject(new Error('network down')))
    render(<Dashboard />, { wrapper })
    await flush()
    expect(screen.getByText(/Couldn't load the dashboard/)).toBeTruthy()

    // Hold the whole retry round open to inspect the in-flight state.
    const held = new Map<string, (v: { data: any }) => void>()
    get.mockImplementation((url: string) => new Promise(r => held.set(url, r)))
    fireEvent.click(screen.getByRole('button', { name: /Retry/ }))
    await flush()

    // Mid-retry: still the error card, in its retry state — the loading
    // screen is gone for good and must not reappear.
    expect(screen.queryByText('Loading dashboard…')).toBeNull()
    expect(screen.getByRole('button', { name: /Retrying/ })).toBeTruthy()

    // The backend comes back and the round settles: the page renders.
    for (const [url, resolve] of held) {
      await act(async () => { resolve({ data: dataFor(url) }) })
    }
    await flush()
    expect(screen.queryByText('Loading dashboard…')).toBeNull()
    expect(screen.queryByText(/Couldn't load the dashboard/)).toBeNull()
    expect(screen.getByText('42')).toBeTruthy()
  })

  it('renders what loaded when only some reads fail, and names the rest in a banner', async () => {
    get.mockImplementation((url: string) => {
      if (url === '/api/jobs?limit=6' || url === '/api/analytics/performance') return Promise.reject(new Error('boom'))
      return defaultOk(url)
    })
    render(<Dashboard />, { wrapper })
    await flush()

    // Everything that succeeded is on screen — no spinner, no error card.
    expect(screen.queryByText('Loading dashboard…')).toBeNull()
    expect(screen.queryByText(/Couldn't load the dashboard/)).toBeNull()
    expect(screen.getByText('42')).toBeTruthy()                     // stat card ← summary
    expect(screen.getByText('Welcome back, Ada.')).toBeTruthy()     // hero ← profile
    expect(screen.getByText('ada@example.com')).toBeTruthy()        // profile card

    // The banner lists exactly the two failed sections, each with its message.
    const banner = screen.getByRole('alert')
    expect(banner).toBeTruthy()
    expect(banner.textContent).toContain('2 parts of the dashboard failed to load')
    expect(banner.textContent).toContain('Recent jobs')
    expect(banner.textContent).toContain('Application analytics')
    expect(banner.textContent).toContain('Cannot reach the API. Is the backend running?')

    // And each section was fetched exactly once so far.
    expect([summaryCalls(), jobsCalls(), performanceCalls(), callsTo('/api/profile/current'), callsTo('/api/billing/subscription')]).toEqual([1, 1, 1, 1, 1])
  })

  it('the banner retry re-fetches only the failed sections, then the banner goes away', async () => {
    let failJobs = true
    get.mockImplementation((url: string) => {
      if (url === '/api/jobs?limit=6' && failJobs) return Promise.reject(new Error('boom'))
      return defaultOk(url)
    })
    render(<Dashboard />, { wrapper })
    await flush()
    expect(screen.getByRole('alert').textContent).toContain('Recent jobs failed to load')
    expect(screen.getByText('No jobs yet. Trigger discovery in Jobs →')).toBeTruthy()

    // The other four sections must NOT be re-fetched — only the failed one.
    failJobs = false
    fireEvent.click(screen.getByRole('button', { name: /Retry this section/ }))
    await flush()

    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByText('Frontend Engineer')).toBeTruthy()
    expect([summaryCalls(), jobsCalls(), performanceCalls(), callsTo('/api/profile/current'), callsTo('/api/billing/subscription')]).toEqual([1, 2, 1, 1, 1])
  })

  it('a summary failure renders the page with zeros under a banner — not the spinner, not the error card', async () => {
    get.mockImplementation((url: string) => url === '/api/dashboard/summary' ? Promise.reject(new Error('boom')) : defaultOk(url))
    render(<Dashboard />, { wrapper })
    await flush()

    expect(screen.queryByText('Loading dashboard…')).toBeNull()
    expect(screen.queryByText(/Couldn't load the dashboard/)).toBeNull()
    const banner = screen.getByRole('alert')
    expect(banner.textContent).toContain('Dashboard summary failed to load')
    // Degraded data: profile still greets by name, stat cards render 0s.
    expect(screen.getByText('Welcome back, Ada.')).toBeTruthy()
  })
})

describe('Dashboard summary poll (P1 #4)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setVisibility('visible')
    get.mockReset()
    get.mockImplementation(defaultOk)
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('re-reads the summary every 30 s while the tab is visible — and only the summary', async () => {
    render(<Dashboard />, { wrapper })
    await flush()
    expect(summaryCalls()).toBe(1)

    await advance(29_000)
    expect(summaryCalls()).toBe(1)
    await advance(1_000)
    expect(summaryCalls()).toBe(2)
    await advance(30_000)
    expect(summaryCalls()).toBe(3)

    // The other sections are mount reads, not polls.
    expect(jobsCalls()).toBe(1)
    expect(performanceCalls()).toBe(1)
  })

  it('shows what the latest summary poll read', async () => {
    render(<Dashboard />, { wrapper })
    await flush()
    expect(screen.getByText('42')).toBeTruthy()

    get.mockImplementation((url: string) =>
      url === '/api/dashboard/summary' ? Promise.resolve({ data: { ...SUMMARY, jobs: { ...SUMMARY.jobs, total: 57 } } }) : defaultOk(url))
    await advance(30_000)
    expect(screen.getByText('57')).toBeTruthy()
    expect(screen.queryByText('42')).toBeNull()
  })

  it('polls nothing while the tab is hidden, and re-reads immediately when it is shown', async () => {
    render(<Dashboard />, { wrapper })
    await flush()
    expect(summaryCalls()).toBe(1)

    await act(async () => { setVisibility('hidden') })
    await advance(180_000)
    expect(summaryCalls()).toBe(1)

    await act(async () => { setVisibility('visible') })
    await flush()
    expect(summaryCalls()).toBe(2)

    // …and the 30 s cadence resumes from there.
    await advance(30_000)
    expect(summaryCalls()).toBe(3)
  })

  it('a tab opened hidden polls nothing until it is shown', async () => {
    setVisibility('hidden')
    render(<Dashboard />, { wrapper })
    await flush()
    // The one-off mount read still happens (the page has to render), but no
    // timer was armed behind an invisible tab.
    expect(summaryCalls()).toBe(1)

    await advance(300_000)
    expect(summaryCalls()).toBe(1)

    await act(async () => { setVisibility('visible') })
    await flush()
    expect(summaryCalls()).toBe(2)
  })

  it('a failed summary poll keeps the last read — no blank, no banner', async () => {
    render(<Dashboard />, { wrapper })
    await flush()
    expect(screen.getByText('42')).toBeTruthy()

    get.mockImplementation((url: string) => url === '/api/dashboard/summary' ? Promise.reject(new Error('boom')) : defaultOk(url))
    await advance(30_000)

    expect(screen.getByText('42')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('a summary that never loaded is not polled — the Retry button is the recovery path', async () => {
    get.mockImplementation(() => Promise.reject(new Error('network down')))
    render(<Dashboard />, { wrapper })
    await flush()
    expect(summaryCalls()).toBe(1)

    await advance(120_000)
    expect(summaryCalls()).toBe(1)
  })

  it('cleans up the timer and the visibility listener on unmount', async () => {
    const spy = vi.spyOn(document, 'removeEventListener')
    const { unmount } = render(<Dashboard />, { wrapper })
    await flush()
    unmount()

    expect(spy).toHaveBeenCalledWith('visibilitychange', expect.any(Function))

    const before = summaryCalls()
    await advance(300_000)
    expect(summaryCalls()).toBe(before)
  })
})
