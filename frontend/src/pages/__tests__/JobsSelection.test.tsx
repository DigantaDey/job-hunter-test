/**
 * Jobs board — selection under rapid clicking, and the retry the scoring
 * diagnosis promised without offering (v2.5).
 *
 * Two reports, one panel:
 *
 *  1. *"If the user rapidly clicks different discovered jobs, after a few
 *     clicks the side panel stops changing."* Selection was a network round
 *     trip: `selectJob` awaited the detail read and then called `setSelected`
 *     unconditionally, so with several clicks in flight the panel showed
 *     whichever response landed last — and a burst of clicks left the AI read
 *     (a 30-minute budget) holding connections open, which queues every later
 *     request behind them. The panel now switches on the click itself, a stale
 *     response is dropped instead of overwriting a newer selection, and the
 *     superseded reads are aborted.
 *
 *  2. *"Most of the matching score is unavailable [guardrail_failed] and there
 *     is no retry option."* A verdict the accuracy guardrail refused used to
 *     dead-end: the stored payload is served to every later read, so the dead
 *     end was permanent. The row now wears its provenance ("rejected", not a
 *     `0 score` claim) and the panel offers the retry the backend announces in
 *     `actions.retry` — the same read with `?refresh=1`.
 *
 * Hermetic: the API client is mocked, so nothing reaches a backend.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import client from '../../api/client'
import { AIWorkProvider } from '../../context/AIWorkContext'
import Jobs from '../Jobs'

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn() },
  apiError: (_error: unknown, fallback?: string) => fallback ?? 'Request failed',
  aiOutage: () => null,
  downloadResume: vi.fn(),
  AI_REQUEST_TIMEOUT_MS: 50,
}))
const get = client.get as unknown as Mock

/* ── fixtures ───────────────────────────────────────────────────────── */

const listRow = (id: number, extra: Record<string, unknown> = {}) => ({
  id,
  title: `Role ${id}`,
  company: `Co ${id}`,
  location: 'Remote',
  url: `https://list.example/${id}`,
  source: 'greenhouse',
  status: 'discovered',
  score: 0,
  company_size: 'big',
  score_source: 'preliminary',
  ...extra,
})

/** The authoritative row read — a different URL, so the panel's own read is
 *  distinguishable from the list row it was optimistically selected from. */
const detailRow = (id: number, extra: Record<string, unknown> = {}) => ({
  ...listRow(id),
  url: `https://detail.example/${id}`,
  description: `Description for role ${id}`,
  ...extra,
})

const QUEUES = { counts: { queued: 0, processing: 0, paused: 0, needs_input: 0 }, items: [], recent: [] }

function deferred() {
  let resolve!: (value: unknown) => void
  const promise = new Promise((r) => { resolve = r })
  return { promise, resolve }
}

let routes: Record<string, unknown> = {}

/** A route that fails, created *inside* the mock so the rejection is never
 *  unhandled between test setup and the request that consumes it. */
const failing = (error: unknown) => ({ __reject: error })

function answer(url: string): Promise<{ data: unknown }> {
  if (url in routes) {
    const value = routes[url]
    if (value && typeof value === 'object' && '__reject' in (value as object)) {
      return Promise.reject((value as { __reject: unknown }).__reject)
    }
    return value instanceof Promise ? (value as Promise<{ data: unknown }>) : Promise.resolve({ data: value })
  }
  if (url.startsWith('/api/queues')) return Promise.resolve({ data: QUEUES })
  if (url.startsWith('/api/application-tracking')) {
    return Promise.resolve({ data: { exists: false, job: { id: 1 }, actions: [], snapshot: {}, server_time: '' } })
  }
  return Promise.resolve({ data: {} })
}

function renderPage() {
  render(<MemoryRouter><AIWorkProvider><Jobs /></AIWorkProvider></MemoryRouter>)
}

/** Click a *list row* — its URL text exists only on the board, never in the panel. */
function clickRow(id: number) {
  fireEvent.click(screen.getByText(`https://list.example/${id}`))
}

/** Which job the side panel is showing, read from its "Open original" link. */
function panelUrl(): string {
  const link = screen.getByRole('link', { name: /open original/i }) as HTMLAnchorElement
  return link.getAttribute('href') || ''
}

async function boardLoaded() {
  await waitFor(() => expect(screen.getByText('https://list.example/11')).toBeTruthy())
}

const intelligenceCalls = () => get.mock.calls.map((c) => String(c[0])).filter((u) => u.includes('/intelligence'))

beforeEach(() => {
  routes = {
    '/api/jobs': [listRow(11), listRow(12), listRow(13), listRow(14)],
    '/api/jobs/11': detailRow(11),
    '/api/jobs/12': detailRow(12),
    '/api/jobs/13': detailRow(13),
    '/api/jobs/14': detailRow(14),
  }
  get.mockImplementation((url: string) => answer(url))
})

afterEach(cleanup)

/* ── selection ──────────────────────────────────────────────────────── */

describe('job selection under rapid clicking', () => {
  it('switches the panel on the click itself, without waiting for the network', async () => {
    const slow = deferred()
    routes['/api/jobs/11'] = slow.promise
    renderPage()
    await boardLoaded()

    clickRow(11)
    // The clicked row is what the panel shows from this instant — the detail
    // read has not answered yet (and never will, in this test).
    expect(panelUrl()).toContain('/11')
    expect(screen.getByText('Loading…')).toBeTruthy()

    await act(async () => { slow.resolve({ data: detailRow(11) }) })
    await waitFor(() => expect(panelUrl()).toContain('detail.example/11'))
    expect(screen.getByText('Description for role 11')).toBeTruthy()
  })

  it('a late answer for an earlier click cannot take the panel back', async () => {
    const first = deferred()
    routes['/api/jobs/11'] = first.promise
    renderPage()
    await boardLoaded()

    clickRow(11)
    clickRow(12)
    await waitFor(() => expect(panelUrl()).toContain('detail.example/12'))

    // The slow answer for the *previous* click arrives last — it is stale and
    // must be dropped rather than overwrite the newer selection.
    await act(async () => { first.resolve({ data: detailRow(11) }) })
    await act(async () => { await Promise.resolve() })

    expect(panelUrl()).toContain('/12')
    expect(screen.queryByText('Description for role 11')).toBeNull()
    expect(screen.getByText('Description for role 12')).toBeTruthy()
  })

  it('keeps switching the panel through a burst of clicks', async () => {
    renderPage()
    await boardLoaded()

    for (const id of [11, 12, 13]) clickRow(id)
    await waitFor(() => expect(panelUrl()).toContain('/13'))
    clickRow(14)
    await waitFor(() => expect(panelUrl()).toContain('/14'))
    expect(screen.getByText('Description for role 14')).toBeTruthy()
  })

  it('waits for the clicks to settle before paying for one breakdown read', async () => {
    renderPage()
    await boardLoaded()

    for (const id of [11, 12, 13]) clickRow(id)
    expect(intelligenceCalls()).toEqual([])   // nothing has been spent yet
    clickRow(14)
    await act(async () => { await new Promise((r) => setTimeout(r, 350)) })

    expect(intelligenceCalls()).toEqual(['/api/jobs/14/intelligence'])
  })

  it('a failed detail read is reported, not swallowed — and the panel still switches', async () => {
    routes['/api/jobs/12'] = failing(new Error('boom'))
    renderPage()
    await boardLoaded()

    clickRow(12)
    await waitFor(() => expect(screen.getByText('Could not load this job')).toBeTruthy())
    expect(panelUrl()).toContain('/12')
  })
})

/* ── honest scores and the retry ────────────────────────────────────── */

describe('a verdict the guardrail refused', () => {
  it('is shown as its provenance on the board, never as "0 score"', async () => {
    routes['/api/jobs'] = [
      listRow(11, { score: 0, score_source: 'rejected' }),
      listRow(12, { score: 0, score_source: 'pending' }),
      listRow(13, { score: 88, score_source: 'ai' }),
      listRow(14, { score: 41, score_source: 'preliminary' }),
    ]
    renderPage()
    await boardLoaded()

    expect(screen.getByText('rejected')).toBeTruthy()
    expect(screen.getByText('AI pending')).toBeTruthy()
    expect(screen.getByText('88 score')).toBeTruthy()
    expect(screen.getByText('41 score')).toBeTruthy()
    expect(screen.queryByText('0 score')).toBeNull()
  })

  it('offers the retry the diagnosis asks for, and the retry re-scores', async () => {
    routes['/api/jobs/13'] = detailRow(13)
    routes['/api/jobs/13/intelligence'] = {
      score: 0,
      overall: 0,
      score_source: 'rejected',
      recommendation: 'LOW',
      recommendation_reason: 'No verdict.',
      ai_error: {
        message: 'The model answered, but the result failed the accuracy guardrail after a repair attempt.',
        reason: 'guardrail_failed',
        fix: 'Retry — or use a stronger model for this workflow in Settings → Per-workflow AI override.',
      },
      actions: { retry: { allowed: true, route: '/api/jobs/13/intelligence?refresh=1', method: 'GET', costs_quota: true } },
    }
    renderPage()
    await boardLoaded()

    clickRow(13)
    await waitFor(() => expect(screen.getByText('Retry scoring')).toBeTruthy())
    expect(screen.getByText(/AI scoring unavailable/)).toBeTruthy()

    // The retry answers with a real verdict.
    routes['/api/jobs/13/intelligence?refresh=1'] = {
      score: 88, overall: 88, score_source: 'ai', recommendation: 'HIGH PRIORITY',
      recommendation_reason: 'Strong overlap on the core stack.',
    }
    await act(async () => { fireEvent.click(screen.getByText('Retry scoring')) })

    await waitFor(() => expect(screen.getByText('AI verified')).toBeTruthy())
    expect(screen.queryByText(/AI scoring unavailable/)).toBeNull()
    expect(intelligenceCalls()).toContain('/api/jobs/13/intelligence?refresh=1')
  })

  it('marks a low-confidence local answer as the last resort it is', async () => {
    routes['/api/jobs/14'] = detailRow(14)
    routes['/api/jobs/14/intelligence'] = {
      score: 52, overall: 52, score_source: 'laya', low_confidence: true,
      recommendation: 'MODERATE', recommendation_reason: 'Weighted rubric score 52/100 …',
      guardrail: { engine: 'laya', confidence: 0.2, low_confidence: true,
                   rescued_from: 'ai_guardrail_failed', rejected_issues: [] },
    }
    renderPage()
    await boardLoaded()

    clickRow(14)
    await waitFor(() => expect(screen.getByText('Laya (low confidence)')).toBeTruthy())
    expect(screen.getByText(/Low-confidence local answer/)).toBeTruthy()
    // The retry is still offered: a model verdict is what the user asked for.
    expect(screen.getByText('Retry scoring')).toBeTruthy()
  })

  it('a failed re-score is reported instead of leaving the panel blank', async () => {
    routes['/api/jobs/13'] = detailRow(13)
    routes['/api/jobs/13/intelligence'] = {
      score: 0, overall: 0, score_source: 'pending',
      ai_error: { message: 'AI offline', reason: 'unreachable', fix: 'Start the provider.' },
      actions: { retry: { allowed: true, route: '/api/jobs/13/intelligence?refresh=1' } },
    }
    renderPage()
    await boardLoaded()

    clickRow(13)
    await waitFor(() => expect(screen.getByText('Retry scoring')).toBeTruthy())

    routes['/api/jobs/13/intelligence?refresh=1'] = failing(new Error('still down'))
    await act(async () => { fireEvent.click(screen.getByText('Retry scoring')) })
    await waitFor(() => expect(screen.getByText('Could not load the match breakdown')).toBeTruthy())
  })
})
