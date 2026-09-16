/**
 * Emails page polling cadence — the fix for the 4 s non-stop poll.
 *
 * After the fix the page should:
 *   • read once on mount and then every 25 s;
 *   • stop while the tab is hidden and re-read immediately when it is visible;
 *   • coalesce overlapping `load()` calls (from a poll and a user mutation)
 *     into a single in-flight request;
 *   • clean up timers and listeners on unmount (no memory leaks).
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { act, cleanup, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import client from '../../api/client'
import Emails from '../Emails'

/* ── mocks ──────────────────────────────────────────────────────────── */
vi.mock('../../api/client', () => {
  const get = vi.fn()
  const post = vi.fn()
  const put = vi.fn()
  return {
    default: { get, post, put },
    apiError: (_e: unknown, fb: string) => fb,
    aiOutage: () => null,
  }
})
const get = client.get as unknown as Mock

vi.mock('../../components/AIBanner', () => ({ AIOutageBanner: () => null }))
vi.mock('../../components/AIWorkChip', () => ({ WorkStatusLine: () => null }))

/* ── helpers ────────────────────────────────────────────────────────── */
const EMAILS = [{ id: 1, company: 'Acme', to_name: 'Bob', to_email: 'bob@acme.com', status: 'pending_approval', subject: 'Hi', body: '', created_at: '2024-01-01', verified: true, source: 'hunter', confidence: 0.9, ai_used: true, recipient_type: 'hiring_manager' }]

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
  document.dispatchEvent(new Event('visibilitychange'))
}

/** Minimal wrapper — no AIWorkProvider; useAIWork() falls back to its inert value. */
function wrapper({ children }: { children: React.ReactNode }) {
  return <MemoryRouter>{children}</MemoryRouter>
}

const emailsCalls = () => get.mock.calls.filter((c: any[]) => c[0] === '/api/emails').length

/* ── suite ──────────────────────────────────────────────────────────── */
describe('Emails page polling', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setVisibility('visible')
    get.mockReset()
    get.mockImplementation((url: string) => {
      if (url === '/api/emails') return Promise.resolve({ data: EMAILS })
      if (url === '/api/jobs') return Promise.resolve({ data: [] })
      return Promise.resolve({ data: [] })
    })
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('reads once on mount and then every 25 s', async () => {
    render(<Emails />, { wrapper })
    await act(async () => { await Promise.resolve() })
    expect(emailsCalls()).toBe(1)

    // 25 s interval hit.
    await act(async () => { vi.advanceTimersByTime(25_000) })
    await act(async () => { await Promise.resolve() })
    expect(emailsCalls()).toBe(2)

    // Another 25 s.
    await act(async () => { vi.advanceTimersByTime(25_000) })
    await act(async () => { await Promise.resolve() })
    expect(emailsCalls()).toBe(3)
  })

  it('does NOT poll every 4 s (old cadence)', async () => {
    render(<Emails />, { wrapper })
    await act(async () => { await Promise.resolve() })
    expect(emailsCalls()).toBe(1)

    // After 4 s only the mount read should have happened — the interval is 25 s.
    await act(async () => { vi.advanceTimersByTime(4_000) })
    await act(async () => { await Promise.resolve() })
    expect(emailsCalls()).toBe(1)
  })

  it('stops polling while the tab is hidden', async () => {
    render(<Emails />, { wrapper })
    await act(async () => { await Promise.resolve() })
    expect(emailsCalls()).toBe(1)

    await act(async () => { setVisibility('hidden') })
    // 100 s of hidden time — no new reads.
    await act(async () => { vi.advanceTimersByTime(100_000) })
    expect(emailsCalls()).toBe(1)
  })

  it('resumes with an immediate read when the tab becomes visible again', async () => {
    render(<Emails />, { wrapper })
    await act(async () => { await Promise.resolve() })

    // Hide → wait → show.
    await act(async () => { setVisibility('hidden') })
    await act(async () => { vi.advanceTimersByTime(60_000) })
    expect(emailsCalls()).toBe(1)

    await act(async () => { setVisibility('visible') })
    await act(async () => { await Promise.resolve() })
    expect(emailsCalls()).toBe(2)
  })

  it('coalesces overlapping load() calls into one request', async () => {
    let resolveEmails!: (v: unknown) => void
    get.mockImplementation((url: string) => {
      if (url === '/api/emails') return new Promise((r) => { resolveEmails = r })
      if (url === '/api/jobs') return Promise.resolve({ data: [] })
      return Promise.resolve({ data: [] })
    })

    render(<Emails />, { wrapper })
    // The mount load is in flight.  Advance to the next tick to hit the interval
    // while the first request is still pending — it should piggyback, not fire another.
    await act(async () => { vi.advanceTimersByTime(25_000) })
    await act(async () => { await Promise.resolve() })

    // Only one /api/emails request.
    expect(emailsCalls()).toBe(1)

    // Resolve it.
    await act(async () => { resolveEmails({ data: EMAILS }) })
  })

  it('cleans up on unmount — no leaked timers or listeners', async () => {
    const spy = vi.spyOn(document, 'removeEventListener')
    const { unmount } = render(<Emails />, { wrapper })
    await act(async () => { await Promise.resolve() })
    unmount()

    // The visibilitychange listener must have been removed.
    expect(spy).toHaveBeenCalledWith('visibilitychange', expect.any(Function))

    // No more reads after unmount.
    const callsBefore = emailsCalls()
    await act(async () => { vi.advanceTimersByTime(100_000) })
    expect(emailsCalls()).toBe(callsBefore)
  })
})
