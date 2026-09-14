import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useAIQueue, type AIQueueSnapshot, type QueueItem } from '../hooks/useAIQueue'
import { latestRowFor, readTracked, rowById, track as trackInStorage, untrack as untrackInStorage, type TrackedWork } from '../lib/aiWork'

/**
 * The global live layer (v2.2.8), mounted once in `Layout`.
 *
 * Every page reads its AI work from here instead of a private `useState`: the
 * header chip, the "Status: preparing • resume: master" line on Jobs, the
 * scan line on Funding, the generation line on Resume Studio, the Dashboard's
 * "Open queues" counter. One poll feeds all of them, so they can never
 * disagree, and none of them can lose the state on navigation.
 *
 * Re-attaching after an F5: a trigger's `{pipeline_job_id}` receipt is stored
 * in `sessionStorage` (`track`). On mount the context reads those ids back,
 * and until the first poll answers it exposes them as *optimistic* rows, so
 * the page shows "Queued…" immediately instead of a blank. Once the snapshot
 * arrives the real row wins — and `findRow` falls back to matching by
 * (pipeline, job_id) so a run started in another tab shows up too.
 */

export interface AIWorkValue {
  snapshot: AIQueueSnapshot | null
  stale: boolean
  /** Remember a receipt and re-poll right away. */
  track: (entry: Omit<TrackedWork, 'at'>) => void
  untrack: (key: string) => void
  /** Everything remembered in this tab's session. */
  tracked: TrackedWork[]
  /** The row behind a tracked key (real if polled, optimistic until then), or a (pipeline, job) match. */
  findRow: (opts: { key?: string; pipeline?: string; jobId?: number | null; task?: string | null }) => QueueItem | null
  refresh: () => Promise<void>
  /** Header-chip counts. */
  counts: AIQueueSnapshot['counts']
  /** Any in-flight row at all — the chip is shown when true. */
  active: boolean
}

const AIWorkContext = createContext<AIWorkValue | null>(null)

/** A placeholder row for a receipt the poll has not confirmed yet. */
function optimisticRow(t: TrackedWork): QueueItem {
  const now = new Date(t.at).toISOString()
  const jobId = /:(\d+)$/.exec(t.key)?.[1]
  return {
    id: t.id,
    pipeline: t.pipeline,
    task: t.pipeline === 'ai' && t.key.startsWith('resume:') ? 'generate_resume' : null,
    job_id: jobId ? Number(jobId) : null,
    status: 'queued',
    priority: 5,
    attempts: 0,
    max_attempts: 3,
    paused_count: 0,
    error: '',
    created_at: now,
    updated_at: now,
    scheduled_at: now,
    payload: {},
    result: null,
  }
}

export function AIWorkProvider({ children, enabled = true }: { children: ReactNode; enabled?: boolean }) {
  const { snapshot, stale, refresh } = useAIQueue(enabled)
  const [tracked, setTracked] = useState<TrackedWork[]>(() => readTracked())

  // Another tab may have tracked work — pick it up when we regain focus.
  useEffect(() => {
    const sync = () => setTracked(readTracked())
    window.addEventListener('focus', sync)
    return () => window.removeEventListener('focus', sync)
  }, [])

  const track = useCallback((entry: Omit<TrackedWork, 'at'>) => {
    setTracked(trackInStorage(entry))
    void refresh()
  }, [refresh])

  const untrack = useCallback((key: string) => {
    setTracked(untrackInStorage(key))
  }, [])

  const findRow = useCallback<AIWorkValue['findRow']>(({ key, pipeline, jobId, task }) => {
    const t = key ? tracked.find((x) => x.key === key) : undefined
    if (t) {
      const real = rowById(snapshot, t.id)
      if (real) return real
      // Not in the snapshot: either the poll has not answered yet (optimistic),
      // or the row aged out of `recent` (then fall through to the job match).
      if (!snapshot) return optimisticRow(t)
    }
    if (pipeline) {
      const match = latestRowFor(snapshot, pipeline, jobId, task)
      if (match) return match
    }
    return t && !snapshot ? optimisticRow(t) : null
  }, [snapshot, tracked])

  const counts = snapshot?.counts || { queued: 0, processing: 0, paused: 0, needs_input: 0 }
  const active = counts.queued + counts.processing + counts.paused + counts.needs_input > 0 || (!snapshot && tracked.length > 0)

  const value = useMemo<AIWorkValue>(() => ({ snapshot, stale, track, untrack, tracked, findRow, refresh, counts, active }),
    [snapshot, stale, track, untrack, tracked, findRow, refresh, counts, active])

  return <AIWorkContext.Provider value={value}>{children}</AIWorkContext.Provider>
}

/** Read the live layer. Outside a provider (tests, Landing) it degrades to an inert value. */
export function useAIWork(): AIWorkValue {
  const ctx = useContext(AIWorkContext)
  if (ctx) return ctx
  return INERT
}

const INERT: AIWorkValue = {
  snapshot: null,
  stale: false,
  track: () => {},
  untrack: () => {},
  tracked: [],
  findRow: () => null,
  refresh: async () => {},
  counts: { queued: 0, processing: 0, paused: 0, needs_input: 0 },
  active: false,
}

export default AIWorkContext
