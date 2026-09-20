/**
 * Settings page polling cadence — visibility-aware, like the rest of the app.
 *
 * The page owns two live reads: the auto-mode schedule (`GET /api/automation`)
 * and the AI status badge (`GET /api/me/assistant`). They used to sit on a
 * bare `setInterval(…, 30_000)` that ran whether or not anyone was looking, so
 * every background tab burned ~2 requests a minute forever. After the fix the
 * page should:
 *   • read both on open (the mount `load()` does it — the timer must not
 *     immediately fire a duplicate pair), then re-read every 30 s;
 *   • poll nothing at all while the tab is hidden;
 *   • re-read the moment the tab becomes visible again, not at the next tick;
 *   • clean up the interval *and* the `visibilitychange` listener on unmount.
 *
 * Same contract as `pages/__tests__/EmailsPolling.test.tsx` and
 * `hooks/__tests__/useAIQueue.test.tsx`.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import client from '../../api/client'
import Settings from '../Settings'

/* ── mocks ──────────────────────────────────────────────────────────── */
vi.mock('../../api/client', () => {
  const get = vi.fn()
  const post = vi.fn()
  const put = vi.fn()
  return {
    default: { get, post, put },
    apiError: (_e: unknown, fb?: string) => fb ?? 'error',
  }
})
const get = client.get as unknown as Mock

vi.mock('../../components/AIBanner', () => ({ AIStatusBanner: () => null }))

vi.mock('../../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, email: 'owner@example.com', name: 'Owner', role: 'owner', is_owner: true } }),
}))

/* ── fixtures ───────────────────────────────────────────────────────── */
/** `GET /api/settings` — every category the form reads on first paint. */
const SETTINGS = {
  ai: {
    base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini', api_key_set: true, api_key_masked: '***abc',
    rpm: 60, timeout: 300, max_input_tokens: 0, max_output_tokens: 0,
    platform_max_input_tokens: 0, platform_max_output_tokens: 0, token_limits_editable: true, is_owner: true,
  },
  scraping: { keywords: [], freshness_hours: 48, sources: ['curated'], live_enabled: true },
  funding: { context_notes: '', industries: '' },
  general: { strict_skeleton: false, auto_approve_email: false, default_resume_format: 'ai_generated' },
  email: { host: '', port: 587, username: '' },
  workflows: { parse: true },
  automation: { auto_mode: true, can_use: true, cadence: { discovery: 43200, funding: 86400 }, locked_reason: null },
  _meta: { writable: { scraping: ['keywords'] } },
}

const ASSISTANT = { configured: true, state: 'online', online: true, reason: null, hint: null, paused_items: 0, message: 'Your assistant is ready to help.' }

/** `GET /api/automation` — the card's schedule, quota and history. */
const automation = (used = 3) => ({
  enabled: true, can_use: true, active: true, schedulable: true, plan: 'pro_plus',
  cadence: { discovery: 43200, funding: 86400 }, toggles: { discovery: true, funding: true },
  consent_ok: true, sweep_interval_seconds: 300, blocking: [],
  last_run: { discovery: { at: '2026-09-16T06:00:00Z', state: 'done' } },
  next_run: { discovery: '2026-09-16T18:00:00Z' },
  recent: [{ workflow: 'discovery', at: '2026-09-16T06:00:00Z', state: 'done' }],
  quota: { used, limit: 10, remaining: 10 - used },
})

/* ── helpers ────────────────────────────────────────────────────────── */
function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
  document.dispatchEvent(new Event('visibilitychange'))
}

function wrapper({ children }: { children: React.ReactNode }) {
  return <MemoryRouter>{children}</MemoryRouter>
}

/** `load()` chains five awaits; drain the microtask queue until they settle. */
async function flush(rounds = 12) {
  for (let i = 0; i < rounds; i++) await act(async () => { await Promise.resolve() })
}

const callsTo = (url: string) => get.mock.calls.filter((c: any[]) => c[0] === url).length
const autoCalls = () => callsTo('/api/automation')
const statusCalls = () => callsTo('/api/me/assistant')

/* ── suite ──────────────────────────────────────────────────────────── */
describe('Settings page polling', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setVisibility('visible')
    get.mockReset()
    get.mockImplementation((url: string) => {
      if (url === '/api/settings') return Promise.resolve({ data: SETTINGS })
      if (url === '/api/me/assistant') return Promise.resolve({ data: ASSISTANT })
      if (url === '/api/context/keywords') return Promise.resolve({ data: { keywords: ['python'], source: 'ai' } })
      if (url === '/api/automation') return Promise.resolve({ data: automation() })
      return Promise.resolve({ data: {} })
    })
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('reads the schedule and the AI status on open, then every 30 s', async () => {
    render(<Settings />, { wrapper })
    await flush()
    expect(autoCalls()).toBe(1)
    expect(statusCalls()).toBe(1)

    await act(async () => { vi.advanceTimersByTime(30_000) })
    await flush()
    expect(autoCalls()).toBe(2)
    expect(statusCalls()).toBe(2)

    await act(async () => { vi.advanceTimersByTime(30_000) })
    await flush()
    expect(autoCalls()).toBe(3)
    expect(statusCalls()).toBe(3)
  })

  it('does not fire a duplicate pair of reads on open — the mount load already read both', async () => {
    render(<Settings />, { wrapper })
    await flush()
    // A visible tab arms the timer without an immediate tick: `load()` has just
    // read `/api/automation` and `/api/me/assistant` a moment ago.
    expect(autoCalls()).toBe(1)
    expect(statusCalls()).toBe(1)

    // And the first tick is a full 30 s away, not immediate.
    await act(async () => { vi.advanceTimersByTime(29_000) })
    await flush()
    expect(autoCalls()).toBe(1)
  })

  it('stops polling completely while the tab is hidden', async () => {
    render(<Settings />, { wrapper })
    await flush()

    await act(async () => { setVisibility('hidden') })
    // Five minutes of hidden time: not one request, from either endpoint.
    await act(async () => { vi.advanceTimersByTime(300_000) })
    await flush()
    expect(autoCalls()).toBe(1)
    expect(statusCalls()).toBe(1)
  })

  it('re-reads immediately when the tab becomes visible again', async () => {
    render(<Settings />, { wrapper })
    await flush()

    await act(async () => { setVisibility('hidden') })
    await act(async () => { vi.advanceTimersByTime(120_000) })
    expect(autoCalls()).toBe(1)

    // Visible again → read now, without waiting out the interval.
    await act(async () => { setVisibility('visible') })
    await flush()
    expect(autoCalls()).toBe(2)
    expect(statusCalls()).toBe(2)

    // …and the 30 s cadence resumes from there.
    await act(async () => { vi.advanceTimersByTime(30_000) })
    await flush()
    expect(autoCalls()).toBe(3)
  })

  it('a tab opened in the background polls nothing until it is shown', async () => {
    setVisibility('hidden')
    render(<Settings />, { wrapper })
    await flush()
    // The one-off mount load still happens (the page has to render), but no
    // timer was armed behind an invisible tab.
    expect(autoCalls()).toBe(1)

    await act(async () => { vi.advanceTimersByTime(300_000) })
    await flush()
    expect(autoCalls()).toBe(1)

    await act(async () => { setVisibility('visible') })
    await flush()
    expect(autoCalls()).toBe(2)
  })

  it('the card and the badge show what the latest poll read', async () => {
    render(<Settings />, { wrapper })
    await flush()
    expect(screen.getByText(/Automation runs this month/)).toBeTruthy()
    expect(document.body.textContent).toContain('3/10')
    expect(document.body.textContent).toContain('Your assistant is ready to help.')

    // The next tick brings a new quota and a slower probe; both are on screen.
    get.mockImplementation((url: string) => {
      if (url === '/api/automation') return Promise.resolve({ data: automation(7) })
      if (url === '/api/me/assistant') return Promise.resolve({ data: { ...ASSISTANT } })
      return Promise.resolve({ data: {} })
    })
    await act(async () => { vi.advanceTimersByTime(30_000) })
    await flush()
    expect(document.body.textContent).toContain('7/10')
    expect(document.body.textContent).toContain('Your assistant is ready to help.')
  })

  it('cleans up on unmount — no leaked timer or listener', async () => {
    const spy = vi.spyOn(document, 'removeEventListener')
    const { unmount } = render(<Settings />, { wrapper })
    await flush()
    unmount()

    expect(spy).toHaveBeenCalledWith('visibilitychange', expect.any(Function))

    const before = [autoCalls(), statusCalls()]
    await act(async () => { vi.advanceTimersByTime(300_000) })
    await flush()
    expect([autoCalls(), statusCalls()]).toEqual(before)
  })

  it('a failed poll keeps the last read instead of blanking the card', async () => {
    render(<Settings />, { wrapper })
    await flush()
    expect(document.body.textContent).toContain('3/10')

    get.mockImplementation((url: string) => {
      if (url === '/api/automation') return Promise.reject(new Error('boom'))
      if (url === '/api/me/assistant') return Promise.reject(new Error('boom'))
      return Promise.resolve({ data: {} })
    })
    await act(async () => { vi.advanceTimersByTime(30_000) })
    await flush()

    // Still the previous schedule and status — the poll is a refresh, never a reset.
    expect(document.body.textContent).toContain('3/10')
    expect(document.body.textContent).toContain('Your assistant is ready to help.')
  })
})
