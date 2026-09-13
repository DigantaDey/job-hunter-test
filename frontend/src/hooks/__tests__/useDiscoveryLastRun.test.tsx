/**
 * The last-run read (v2.2.4).
 *
 * Two things are worth pinning beyond "it fetches":
 *
 * * a 404 is an *answer* ("no completed discovery run"), not a failure — it is
 *   what turns into the "No discovery run yet" state, while a 500 must leave the
 *   state unknown so the banner claims nothing;
 * * the persona the board is filtered by travels to the call, so the verdict is
 *   about that track's last run and not the newest run of any track.
 */
import { afterEach, describe, expect, it, vi, type Mock } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import client from '../../api/client'
import { LAST_RUN_ENDPOINT, useDiscoveryLastRun } from '../useDiscoveryLastRun'
import type { LastRun } from '../../components/EmptyBoardBanner'

afterEach(cleanup)

vi.mock('../../api/client', () => ({ default: { get: vi.fn() } }))

const get = client.get as unknown as Mock

function Harness({ enabled = true, personaId = null }: { enabled?: boolean; personaId?: number | null }) {
  const { run, loading } = useDiscoveryLastRun(enabled, personaId)
  const state = loading ? 'loading' : run === undefined ? 'unknown' : run === null ? 'none' : 'run'
  return (
    <div>
      <span data-testid="state">{state}</span>
      <span data-testid="added">{run ? run.added : ''}</span>
      <span data-testid="why">{run?.why_empty || ''}</span>
    </div>
  )
}

function rejectWith(status: number) {
  return Promise.reject({ response: { status }, isAxiosError: true })
}

describe('useDiscoveryLastRun', () => {
  it('serves the completed run the endpoint returned', async () => {
    const run: LastRun = { at: '2026-09-12T09:30:00', added: 0, why_empty: 'no_fresh_postings' }
    get.mockResolvedValue({ data: run })

    render(<Harness />)

    await waitFor(() => expect(screen.getByTestId('state').textContent).toBe('run'))
    expect(screen.getByTestId('why').textContent).toBe('no_fresh_postings')
    expect(get).toHaveBeenCalledWith(LAST_RUN_ENDPOINT, { params: undefined })
  })

  it('reads a 404 as "no completed run", not as an error', async () => {
    get.mockImplementation(() => rejectWith(404))

    render(<Harness />)

    await waitFor(() => expect(screen.getByTestId('state').textContent).toBe('none'))
  })

  it('leaves the state unknown when the read fails for any other reason', async () => {
    get.mockImplementation(() => rejectWith(500))

    render(<Harness />)

    await waitFor(() => expect(screen.getByTestId('state').textContent).toBe('unknown'))
    expect(get).toHaveBeenCalledTimes(1)
  })

  it('forwards the persona the board is filtered by', async () => {
    get.mockResolvedValue({ data: { at: '2026-09-12T09:30:00', added: 2 } })

    render(<Harness personaId={7} />)

    await waitFor(() => expect(screen.getByTestId('state').textContent).toBe('run'))
    expect(get).toHaveBeenCalledWith(LAST_RUN_ENDPOINT, { params: { persona_id: 7 } })
    expect(screen.getByTestId('added').textContent).toBe('2')
  })

  it('does not read while the board has jobs', async () => {
    get.mockResolvedValue({ data: { at: '2026-09-12T09:30:00', added: 1 } })

    render(<Harness enabled={false} />)

    expect(get).not.toHaveBeenCalled()
    expect(screen.getByTestId('state').textContent).toBe('unknown')
  })
})
