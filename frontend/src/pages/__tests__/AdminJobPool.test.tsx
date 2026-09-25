/**
 * Shared job pool on the admin console (v2.3).
 *
 * The owner console is the only surface that shows the pool's aggregates, and
 * the aggregates are all it may show for a posting retention has already
 * deleted: counters, a hashed identity, the public company name. These tests pin
 * the panel, the "gone" rollup, and the honest disabled state — a deployment
 * with `JOB_POOL_ENABLED=false` must not claim a corpus it does not keep.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import client from '../../api/client'

vi.mock('../../api/client', () => {
  const get = vi.fn()
  const post = vi.fn()
  const put = vi.fn()
  const del = vi.fn()
  return {
    default: { get, post, put, delete: del },
    apiError: (e: unknown, fb?: string) => fb ?? 'error',
  }
})

import AdminConsole from '../admin/AdminConsole'

const get = client.get as unknown as Mock

function installGets(jobPool: Record<string, unknown>) {
  get.mockImplementation((url: string) => {
    if (url === '/api/admin/overview') {
      return Promise.resolve({ data: {
        queues: { totals: {}, pipelines: {} },
        usage_cost: {},
        failure_rates: {},
        automation_health: {},
        accounts: {},
        sources: { job_sources: [] },
        job_pool: jobPool,
      } })
    }
    if (url === '/api/ai/config') return Promise.resolve({ data: { workflows: {}, overrides: {} } })
    if (url === '/api/settings') return Promise.resolve({ data: { ai: {}, laya: {} } })
    if (url === '/api/admin/flags') return Promise.resolve({ data: { flags: [] } })
    return Promise.resolve({ data: {} })
  })
}

describe('Admin console: shared job pool', () => {
  beforeEach(() => {
    get.mockReset()
  })
  afterEach(cleanup)

  it('shows the live corpus and the aggregate-only rollup for gone postings', async () => {
    installGets({
      enabled: true,
      retention_days: 7,
      live_entries: 1234,
      contributors: 5,
      distinct_companies: 321,
      window_days: 30,
      added_in_window: 400,
      by_source: [{ source: 'greenhouse', entries: 800 }],
      gone: {
        all_time_by_reason: [
          { reason: 'expired', entries: 12, times_seen: 30, max_distinct_users: 4 },
          { reason: 'deleted', entries: 3, times_seen: 6, max_distinct_users: 2 },
        ],
        companies: [{ company: 'gone ltd', entries: 2, times_seen: 3 }],
      },
    })
    render(<MemoryRouter><AdminConsole /></MemoryRouter>)

    await waitFor(() => expect(screen.getByTestId('job-pool')).toBeTruthy())
    expect(screen.getByText('1234')).toBeTruthy()
    expect(screen.getByText('live postings')).toBeTruthy()
    expect(screen.getByText(/Retention 7d/)).toBeTruthy()
    expect(screen.getByText(/greenhouse: 800/)).toBeTruthy()

    const gone = screen.getByTestId('job-pool-gone')
    expect(gone.textContent).toContain('expired: 12 posting(s)')
    expect(gone.textContent).toContain('deleted: 3 posting(s)')
    // Aggregates only: no posting content is claimed for a deleted job.
    expect(gone.textContent).not.toMatch(/Backend Engineer|https?:\/\//)
    expect(screen.getByText(/Churn by company: gone ltd: 2/)).toBeTruthy()
  })

  it('says the pool is disabled instead of showing an empty corpus', async () => {
    installGets({ enabled: false, retention_days: 7 })
    render(<MemoryRouter><AdminConsole /></MemoryRouter>)

    await waitFor(() => expect(screen.getByText('Pool disabled')).toBeTruthy())
    expect(screen.queryByTestId('job-pool')).toBeNull()
    expect(screen.getByText(/JOB_POOL_ENABLED=false/)).toBeTruthy()
  })
})
