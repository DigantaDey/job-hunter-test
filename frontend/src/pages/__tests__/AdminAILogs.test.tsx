/**
 * Owner-only AI log page (v2.3).
 *
 * Two things the owner console could not answer before, pinned here:
 *
 *  • **How is Laya doing?** — the per-task status mix (ok / low confidence /
 *    timeout / error / parked), latency and confidence, read from
 *    `GET /api/admin/laya/stats`.
 *  • **What exactly did we send?** — the call list from
 *    `GET /api/admin/ai/calls` (metadata plus a preview) and, for one call, the
 *    bodies per attempt plus the answer excerpt from
 *    `GET /api/admin/ai/calls/{id}`.
 *
 * Both endpoints are owner-only server-side; this page is mounted behind
 * `<RequireOwner>` and is in `ADMIN_ROUTES`, so a member never sees it — the
 * test asserts the route registration as the client half of that contract.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import client from '../../api/client'
import { ADMIN_ROUTES } from '../../lib/role'

vi.mock('../../api/client', () => {
  const get = vi.fn()
  return {
    default: { get },
    apiError: (e: unknown, fb?: string) => fb ?? 'error',
  }
})

import AdminAILogs from '../admin/AdminAILogs'

const get = client.get as unknown as Mock

const CALL = {
  id: 7,
  workflow: 'scoring',
  status: 'error',
  reason: 'truncated_response',
  model: 'gpt-4o-mini',
  provider: 'openai_compatible',
  base_url: 'https://api.example/v1',
  http_status: 200,
  attempts: 2,
  latency_ms: 812,
  total_tokens: 4100,
  estimated_cost_usd: 0.0123,
  preview: 'score how well this candidate matches the job',
  error: 'invalid_json: preview was empty',
  created_at: '2026-09-26T09:30:00',
}

const DETAIL = {
  ...CALL,
  request_url: 'https://api.example/v1/chat/completions',
  request_params: { temperature: 0.2, stream: true },
  request: {
    attempts: [
      { attempt: 1, body: { model: 'gpt-4o-mini', messages: [{ role: 'user', content: 'score how well this candidate matches the job' }], max_tokens: 1200 } },
      { attempt: 2, body: { model: 'gpt-4o-mini', messages: [{ role: 'user', content: 'score how well this candidate matches the job' }], max_tokens: 3400 } },
    ],
  },
  response: '{"score": 88, "reason": "strong overlap"}',
}

function installGets(overrides: { laya?: Record<string, unknown>; stats?: Record<string, unknown> } = {}) {
  get.mockImplementation((url: string) => {
    if (url === '/api/admin/ai/calls') return Promise.resolve({ data: { items: [CALL] } })
    if (url === '/api/admin/ai/calls/7') return Promise.resolve({ data: DETAIL })
    if (url === '/api/admin/ai/stats') {
      return Promise.resolve({ data: overrides.stats ?? {
        enabled: true,
        retention_days: 7,
        totals: { calls: 12, errors: 2, tokens: 41000, cost_usd: 0.42, avg_latency_ms: 640 },
        failures_by_reason: [{ reason: 'truncated_response', calls: 1 }],
      } })
    }
    if (url === '/api/admin/laya/stats') {
      return Promise.resolve({ data: overrides.laya ?? {
        enabled: true,
        retention_days: 7,
        totals: { decisions: 20, ok: 15, low_confidence: 3, timeout: 1, error: 0, parked: 1,
                  avg_latency_ms: 74, avg_confidence: 0.83 },
        by_task: [
          { task: 'ranking', total: 12, statuses: { ok: 10, low_confidence: 2 }, avg_latency_ms: 72 },
          { task: 'classification', total: 5, statuses: { ok: 4, timeout: 1 }, avg_latency_ms: 90 },
        ],
      } })
    }
    return Promise.resolve({ data: {} })
  })
}

function renderPage() {
  return render(<MemoryRouter><AdminAILogs /></MemoryRouter>)
}

describe('Admin AI log: how Laya is doing', () => {
  beforeEach(() => { get.mockReset() })
  afterEach(cleanup)

  it('renders the status mix, per-task latency and the retention window', async () => {
    installGets()
    renderPage()

    await waitFor(() => expect(screen.getByTestId('laya-health')).toBeTruthy())
    expect(screen.getByTestId('laya-decisions').textContent).toBe('20')
    expect(screen.getByTestId('laya-low-confidence').textContent).toBe('3')
    expect(screen.getByTestId('laya-timeouts').textContent).toBe('1')
    expect(screen.getByTestId('laya-parked').textContent).toBe('1')
    expect(screen.getByText(/avg latency 74ms/)).toBeTruthy()

    const tasks = screen.getByTestId('laya-tasks')
    expect(tasks.textContent).toContain('ranking')
    expect(tasks.textContent).toContain('12 pass(es) • 72ms avg')
    expect(tasks.textContent).toContain('low confidence 2')
    expect(tasks.textContent).toContain('timeout 1')

    const urls = get.mock.calls.map((call: any[]) => call[0])
    expect(urls).toContain('/api/admin/laya/stats')
    expect(urls).toContain('/api/admin/ai/stats')
  })

  it('says so honestly when decision logging is switched off', async () => {
    installGets({ laya: { enabled: false, totals: {}, by_task: [] },
                  stats: { enabled: false, totals: {}, by_workflow: [], failures_by_reason: [] } })
    renderPage()

    await waitFor(() => expect(screen.getByText('Decision logging disabled')).toBeTruthy())
    expect(screen.getByText('Logging disabled')).toBeTruthy()
    expect(screen.getAllByText(/AI_LOG_ENABLED=false/).length).toBeGreaterThan(0)
  })
})

describe('Admin AI log: the exact request sent', () => {
  beforeEach(() => { get.mockReset() })
  afterEach(cleanup)

  it('lists calls and shows every attempt body plus the answer excerpt', async () => {
    installGets()
    renderPage()

    await waitFor(() => expect(screen.getByTestId('ai-call-7')).toBeTruthy())
    expect(screen.getByText('scoring')).toBeTruthy()
    expect(screen.getByText('truncated_response')).toBeTruthy()
    expect(screen.getByText(/4100 tok • 812ms • 2 attempt\(s\)/)).toBeTruthy()

    fireEvent.click(screen.getByTestId('ai-call-open-7'))

    await waitFor(() => expect(screen.getByTestId('ai-call-detail')).toBeTruthy())
    const first = screen.getByTestId('ai-call-attempt-1').textContent || ''
    const second = screen.getByTestId('ai-call-attempt-2').textContent || ''
    expect(first).toContain('score how well this candidate matches the job')
    expect(first).toContain('1200')
    expect(second).toContain('3400')
    expect(screen.getByTestId('ai-call-response').textContent).toContain('strong overlap')
    expect(screen.getByTestId('ai-call-detail').textContent).toContain('https://api.example/v1/chat/completions')
    expect(get.mock.calls.map((call: any[]) => call[0])).toContain('/api/admin/ai/calls/7')
  })

  it('refetches with the owner-chosen filters', async () => {
    installGets()
    renderPage()
    await waitFor(() => expect(screen.getByTestId('ai-call-7')).toBeTruthy())

    fireEvent.change(screen.getByTestId('ai-log-status'), { target: { value: 'error' } })
    fireEvent.change(screen.getByTestId('ai-log-workflow'), { target: { value: 'scoring' } })
    fireEvent.click(screen.getByTestId('ai-log-apply'))

    await waitFor(() => {
      const filtered = get.mock.calls
        .filter((call: any[]) => call[0] === '/api/admin/ai/calls')
        .map((call: any[]) => call[1]?.params)
      expect(filtered[filtered.length - 1]).toEqual({ limit: 25, status: 'error', workflow: 'scoring' })
    })

    fireEvent.change(screen.getByTestId('ai-log-window'), { target: { value: '168' } })
    await waitFor(() => {
      const windows = get.mock.calls.filter((call: any[]) => call[0] === '/api/admin/ai/stats')
      expect(windows[windows.length - 1][1]?.params).toEqual({ window_hours: 168 })
    })
  })

  it('is registered as an owner-only admin route', () => {
    expect(ADMIN_ROUTES).toContain('/admin/ai')
  })
})
