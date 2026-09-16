/**
 * Queues page polling cadence — the fix for the 3 s triple poll.
 *
 * The page owns three reads: `GET /api/pipelines/stats`, `GET
 * /api/pipelines/jobs` and `GET /api/user-input-queue`. They ran on a 3 s
 * interval, which is ~60 requests a minute from one open tab — 8× the 25 s the
 * rest of the app uses — for counters and a job list that move slowly. The
 * visibility-aware wrapper around them was already correct; only the cadence
 * was wrong. After the fix the page should:
 *   • read all three on open, then every 20 s (`QUEUES_POLL_MS`), never at 3 s;
 *   • poll nothing at all while the tab is hidden — including a tab that was
 *     opened in the background, which reads only once it is shown;
 *   • re-read the moment the tab becomes visible again, not at the next tick,
 *     and resume the 20 s cadence from there;
 *   • re-read immediately when the pipeline filter changes, and not leave a
 *     second interval armed behind it;
 *   • keep the last good view when a poll fails, and say the read failed;
 *   • clean up the interval *and* the `visibilitychange` listener on unmount.
 *
 * And the part that must not regress by slowing down: the "Processing now" card
 * is fed by the global `useAIWork()` layer (`AI_QUEUE_POLL_MS`, 4 s), not by
 * this page's poll — so it still moves several times between two page reads.
 *
 * Same contract as `pages/__tests__/EmailsPolling.test.tsx`,
 * `pages/__tests__/SettingsPolling.test.tsx` and `hooks/__tests__/useAIQueue.test.tsx`.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import client from '../../api/client'
import { AIWorkProvider } from '../../context/AIWorkContext'
import { AI_QUEUE_POLL_MS } from '../../hooks/useAIQueue'
import Queues, { QUEUES_POLL_MS } from '../Queues'

/* ── mocks ──────────────────────────────────────────────────────────── */
vi.mock('../../api/client', () => {
  const get = vi.fn()
  const post = vi.fn()
  return {
    default: { get, post },
    apiError: (_e: unknown, fb?: string) => fb ?? 'error',
  }
})
const get = client.get as unknown as Mock

/* ── fixtures ───────────────────────────────────────────────────────── */
/**
 * `GET /api/pipelines/stats` — per-pipeline buckets plus the top-level ai block.
 * `done` is the one number a test can watch change: every pipeline card renders
 * it as its `done` chip, so three chips read the same value.
 */
const stats = (done = 4) => ({
  discovery: { queued: 1, processing: 0, done, failed: 0, needs_input: 0, paused: 0, dead: 0 },
  application: { queued: 2, processing: 1, done, failed: 0, needs_input: 1, paused: 0, dead: 0 },
  ai: { queued: 3, processing: 1, done, failed: 0, needs_input: 0, paused: 0, dead: 0 },
  ai_queue: { queue_preview: [], processing_now: null },
  rate_limiter: { remaining: 60, rpm: 60, throttled: 0 },
})

/** `GET /api/pipelines/jobs` — the list under the pipeline filter. */
const job = (id: number, pipeline: string) => ({
  id, pipeline, task: null, job_id: null, status: 'queued', priority: 5,
  attempts: 0, max_attempts: 3, error: '', payload: {},
  created_at: '2026-09-16T08:00:00Z', updated_at: '2026-09-16T08:00:00Z', scheduled_at: '2026-09-16T08:00:00Z',
})

/** `GET /api/queues/ai` — the global live layer behind the "Processing now" card. */
function processing(id: number, pipeline: string, task: string | null, jobId: number | null) {
  const row = {
    id, pipeline, task, job_id: jobId, status: 'processing', priority: 5,
    attempts: 0, max_attempts: 3, paused_count: 0, error: '', payload: {}, result: null,
    created_at: '2026-09-16T08:00:00Z', updated_at: '2026-09-16T08:00:00Z', scheduled_at: '2026-09-16T08:00:00Z',
  }
  return { counts: { queued: 0, processing: 1, paused: 0, needs_input: 0 }, items: [row], recent: [], processing_now: row }
}

/* ── helpers ────────────────────────────────────────────────────────── */
function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
  document.dispatchEvent(new Event('visibilitychange'))
}

/** The real provider: the card under test reads the global poll, not this page's. */
function wrapper({ children }: { children: React.ReactNode }) {
  return <MemoryRouter><AIWorkProvider>{children}</AIWorkProvider></MemoryRouter>
}

/** `load()` chains three awaits and the live layer another; drain them. */
async function flush(rounds = 12) {
  for (let i = 0; i < rounds; i++) await act(async () => { await Promise.resolve() })
}

async function advance(ms: number) {
  await act(async () => { vi.advanceTimersByTime(ms) })
  await flush()
}

const callsTo = (url: string) => get.mock.calls.filter((c: any[]) => c[0] === url).length
const statsCalls = () => callsTo('/api/pipelines/stats')
const jobsCalls = () => callsTo('/api/pipelines/jobs')
const inputCalls = () => callsTo('/api/user-input-queue')
/** Everything this page polls — three reads per round. */
const pageCalls = () => statsCalls() + jobsCalls() + inputCalls()
/** The global live layer, on its own cadence. */
const liveCalls = () => callsTo('/api/queues/ai')

/* ── suite ──────────────────────────────────────────────────────────── */
describe('Queues page polling', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setVisibility('visible')
    get.mockReset()
    get.mockImplementation((url: string) => {
      if (url === '/api/pipelines/stats') return Promise.resolve({ data: stats() })
      if (url === '/api/pipelines/jobs') return Promise.resolve({ data: [job(11, 'application')] })
      if (url === '/api/user-input-queue') return Promise.resolve({ data: [] })
      if (url === '/api/queues/ai') return Promise.resolve({ data: processing(7, 'application', null, 12) })
      return Promise.resolve({ data: {} })
    })
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('reads all three endpoints on open, then every 20 s — and not a moment before', async () => {
    render(<Queues />, { wrapper })
    await flush()
    expect([statsCalls(), jobsCalls(), inputCalls()]).toEqual([1, 1, 1])

    // One millisecond short of the interval: nothing has fired yet.
    await advance(QUEUES_POLL_MS - 1)
    expect([statsCalls(), jobsCalls(), inputCalls()]).toEqual([1, 1, 1])

    await advance(1)
    expect([statsCalls(), jobsCalls(), inputCalls()]).toEqual([2, 2, 2])

    await advance(QUEUES_POLL_MS)
    expect([statsCalls(), jobsCalls(), inputCalls()]).toEqual([3, 3, 3])
  })

  it('does NOT poll at the old 3 s cadence — a minute on this page is 12 requests, not 63', async () => {
    expect(QUEUES_POLL_MS).toBeGreaterThanOrEqual(15_000)
    expect(QUEUES_POLL_MS).toBeLessThanOrEqual(25_000)

    render(<Queues />, { wrapper })
    await flush()
    expect(pageCalls()).toBe(3)

    // The old interval would have fired five times by here.
    await advance(15_000)
    expect(pageCalls()).toBe(3)

    // One minute of an open tab: the mount read plus three 20 s ticks.
    await advance(45_000)
    expect(pageCalls()).toBe(12)
  })

  it('the "Processing now" card keeps moving on the global 4 s read while this page waits', async () => {
    render(<Queues />, { wrapper })
    await flush()
    const card = screen.getByTestId('processing-now')
    expect(card.textContent).toContain('#7 Auto-apply job #12')
    expect(card.textContent).toContain('Preparing')
    expect(pageCalls()).toBe(3)

    // A different worker picks up a different run — served by the live layer.
    get.mockImplementation((url: string) => {
      if (url === '/api/pipelines/stats') return Promise.resolve({ data: stats() })
      if (url === '/api/pipelines/jobs') return Promise.resolve({ data: [job(11, 'application')] })
      if (url === '/api/user-input-queue') return Promise.resolve({ data: [] })
      if (url === '/api/queues/ai') return Promise.resolve({ data: processing(9, 'ai', 'generate_resume', 3) })
      return Promise.resolve({ data: {} })
    })

    await advance(AI_QUEUE_POLL_MS)
    expect(liveCalls()).toBe(2)
    expect(screen.getByTestId('processing-now').textContent).toContain('#9 Tailored resume job #3')
    expect(screen.getByTestId('processing-now').textContent).toContain('Generating')
    // …and this page's own three reads have not moved yet — they are 20 s apart.
    expect(pageCalls()).toBe(3)

    await advance(QUEUES_POLL_MS - AI_QUEUE_POLL_MS)
    expect(pageCalls()).toBe(6)
  })

  it('stops polling completely while the tab is hidden', async () => {
    render(<Queues />, { wrapper })
    await flush()
    expect(pageCalls()).toBe(3)

    await act(async () => { setVisibility('hidden') })
    // Five minutes of hidden time: not one request, from any of the three.
    await advance(300_000)
    expect(pageCalls()).toBe(3)
    expect(liveCalls()).toBe(1)
  })

  it('re-reads immediately when the tab becomes visible again', async () => {
    render(<Queues />, { wrapper })
    await flush()

    await act(async () => { setVisibility('hidden') })
    await advance(120_000)
    expect(pageCalls()).toBe(3)

    // Visible again → read now, without waiting out the interval.
    await act(async () => { setVisibility('visible') })
    await flush()
    expect(pageCalls()).toBe(6)

    // …and the 20 s cadence resumes from there, one interval armed.
    await advance(QUEUES_POLL_MS)
    expect(pageCalls()).toBe(9)
  })

  it('a tab opened in the background reads nothing until it is shown', async () => {
    setVisibility('hidden')
    render(<Queues />, { wrapper })
    await flush()
    expect(screen.getByText('Loading queues…')).toBeTruthy()
    expect(pageCalls()).toBe(0)

    await advance(300_000)
    expect(pageCalls()).toBe(0)

    await act(async () => { setVisibility('visible') })
    await flush()
    expect(pageCalls()).toBe(3)
    expect(screen.getByText('Pipelines & Queues')).toBeTruthy()
  })

  it('changing the pipeline filter re-reads at once with the new filter — and arms one interval, not two', async () => {
    render(<Queues />, { wrapper })
    await flush()
    expect(get).toHaveBeenCalledWith('/api/pipelines/jobs', { params: { pipeline: 'application' } })
    expect(pageCalls()).toBe(3)

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'discovery' })) })
    await flush()
    expect(get).toHaveBeenCalledWith('/api/pipelines/jobs', { params: { pipeline: 'discovery' } })
    expect(pageCalls()).toBe(6)

    // A leaked interval from the previous filter would double this round.
    await advance(QUEUES_POLL_MS)
    expect(pageCalls()).toBe(9)
    expect(statsCalls()).toBe(3)
  })

  it('a failed poll keeps the last good view and says the read failed', async () => {
    render(<Queues />, { wrapper })
    await flush()
    // One `done` chip per pipeline card, all reading the same number.
    expect(screen.getAllByText('4')).toHaveLength(3)
    expect(document.body.textContent).toContain('live • updated')

    // A successful tick first, so "nothing changed" below is a real assertion.
    get.mockImplementation((url: string) => {
      if (url === '/api/pipelines/stats') return Promise.resolve({ data: stats(8) })
      if (url === '/api/pipelines/jobs') return Promise.resolve({ data: [job(11, 'application')] })
      if (url === '/api/user-input-queue') return Promise.resolve({ data: [] })
      if (url === '/api/queues/ai') return Promise.resolve({ data: processing(7, 'application', null, 12) })
      return Promise.resolve({ data: {} })
    })
    await advance(QUEUES_POLL_MS)
    expect(screen.getAllByText('8')).toHaveLength(3)

    // Now the three reads break while the live layer keeps answering.
    get.mockImplementation((url: string) => {
      if (url === '/api/queues/ai') return Promise.resolve({ data: processing(7, 'application', null, 12) })
      return Promise.reject(new Error('boom'))
    })
    await advance(QUEUES_POLL_MS)

    // Still the previous counters — a poll is a refresh, never a reset — and the
    // pill says the read failed instead of pretending to be live.
    expect(screen.getAllByText('8')).toHaveLength(3)
    expect(document.body.textContent).toContain('live update failed')
  })

  it('cleans up on unmount — no leaked timer or listener', async () => {
    const spy = vi.spyOn(document, 'removeEventListener')
    const { unmount } = render(<Queues />, { wrapper })
    await flush()
    unmount()

    expect(spy).toHaveBeenCalledWith('visibilitychange', expect.any(Function))

    const before = [pageCalls(), liveCalls()]
    await advance(300_000)
    expect([pageCalls(), liveCalls()]).toEqual(before)
  })
})
