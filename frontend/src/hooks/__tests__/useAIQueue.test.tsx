/**
 * The live poll (v2.2.8): cadence is the whole contract.
 *
 * * reads once on mount and then every `intervalMs`;
 * * stops while the tab is hidden and re-reads the moment it is visible;
 * * a failed read flags `stale` but keeps the last snapshot;
 * * concurrent `refresh()` calls share one request.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import client from '../../api/client'
import { useAIQueue } from '../useAIQueue'

afterEach(cleanup)
vi.mock('../../api/client', () => ({ default: { get: vi.fn() } }))
const get = client.get as unknown as Mock

const payload = (queued: number) => ({ data: { counts: { queued, processing: 0, paused: 0, needs_input: 0 }, items: [], recent: [], processing_now: null } })

let refreshFn: () => Promise<void>
function Harness() {
  const { snapshot, stale, refresh } = useAIQueue(true, 1000)
  refreshFn = refresh
  return <span data-testid="s">{snapshot ? `${snapshot.counts.queued}${stale ? ' stale' : ''}` : 'none'}</span>
}

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
  document.dispatchEvent(new Event('visibilitychange'))
}

describe('useAIQueue', () => {
  beforeEach(() => { vi.useFakeTimers(); get.mockReset(); setVisibility('visible') })
  afterEach(() => vi.useRealTimers())

  it('polls on an interval and pauses while hidden', async () => {
    get.mockResolvedValue(payload(1))
    render(<Harness />)
    await act(async () => { await Promise.resolve() })
    expect(get).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('s').textContent).toBe('1')
    await act(async () => { vi.advanceTimersByTime(1000) })
    await act(async () => { vi.advanceTimersByTime(1000) })
    expect(get).toHaveBeenCalledTimes(3)
    await act(async () => { setVisibility('hidden') })
    await act(async () => { vi.advanceTimersByTime(5000) })
    expect(get).toHaveBeenCalledTimes(3)
    get.mockResolvedValue(payload(4))
    await act(async () => { setVisibility('visible') })
    expect(get).toHaveBeenCalledTimes(4)
    expect(screen.getByTestId('s').textContent).toBe('4')
  })

  it('keeps the last snapshot and flags stale on a failed read', async () => {
    get.mockResolvedValueOnce(payload(2)).mockRejectedValueOnce(new Error('boom'))
    render(<Harness />)
    await act(async () => { await Promise.resolve() })
    await act(async () => { vi.advanceTimersByTime(1000) })
    expect(screen.getByTestId('s').textContent).toBe('2 stale')
  })

  it('coalesces overlapping refresh() calls into one request', async () => {
    let resolve!: (v: unknown) => void
    get.mockImplementation(() => new Promise((r) => { resolve = r }))
    render(<Harness />)
    await act(async () => { void refreshFn(); void refreshFn() })
    expect(get).toHaveBeenCalledTimes(1) // mount read shared with both refreshes
    await act(async () => { resolve(payload(9)); await Promise.resolve() })
    expect(screen.getByTestId('s').textContent).toBe('9')
  })
})
