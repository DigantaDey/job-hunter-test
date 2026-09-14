import { useCallback, useEffect, useRef, useState } from 'react'
import client from '../api/client'

/**
 * The live AI-queue read (v2.2.8).
 *
 * One poll of `GET /api/queues/ai` — the tenant's in-flight `pipeline_jobs`
 * rows, the outcomes of the last half hour (with each handler's `result`),
 * the header-chip counts and whatever a worker is executing right now.
 *
 * Deliberately thin: nothing is derived here. The rows *are* the truth about
 * what the worker is doing; pages derive their own labels from them
 * (`deriveApplyState` & friends in `lib/aiWork.ts`). The hook only owns the
 * cadence: 4s while the tab is visible, paused while it is hidden
 * (`visibilitychange`), and an immediate re-read when it becomes visible
 * again or when a caller asks (`refresh()` after a click).
 */

export const AI_QUEUE_ENDPOINT = '/api/queues/ai'
export const AI_QUEUE_POLL_MS = 4000

export type QueueStatus = 'queued' | 'processing' | 'paused' | 'needs_input' | 'done' | 'dead' | 'failed'

export interface QueueItem {
  id: number
  pipeline: 'discovery' | 'application' | 'email' | 'funding' | 'ai' | string
  task?: string | null
  job_id?: number | null
  status: QueueStatus | string
  priority: number
  attempts: number
  max_attempts: number
  paused_count: number
  error: string
  created_at: string
  updated_at: string
  scheduled_at: string
  finished_at?: string | null
  payload: Record<string, any>
  result?: Record<string, any> | null
}

export interface AIQueueSnapshot {
  counts: { queued: number; processing: number; paused: number; needs_input: number }
  items: QueueItem[]
  recent: QueueItem[]
  processing_now: QueueItem | null
  server_time?: string
}

export interface AIQueueState {
  /** `null` until the first successful read. */
  snapshot: AIQueueSnapshot | null
  /** The last read failed (the previous snapshot is kept — the last known signal stands). */
  stale: boolean
  /** Re-read now (used right after a trigger so the chip appears without waiting a tick). */
  refresh: () => Promise<void>
  /** Wall-clock of the last successful read. */
  lastReadAt: number | null
}

export const EMPTY_SNAPSHOT: AIQueueSnapshot = {
  counts: { queued: 0, processing: 0, paused: 0, needs_input: 0 },
  items: [],
  recent: [],
  processing_now: null,
}

export function useAIQueue(enabled = true, intervalMs = AI_QUEUE_POLL_MS): AIQueueState {
  const [snapshot, setSnapshot] = useState<AIQueueSnapshot | null>(null)
  const [stale, setStale] = useState(false)
  const [lastReadAt, setLastReadAt] = useState<number | null>(null)
  const inFlight = useRef<Promise<void> | null>(null)
  const alive = useRef(true)

  const refresh = useCallback(async () => {
    if (!enabled) return
    // Coalesce: a click-triggered refresh while a poll is in flight shares it.
    if (inFlight.current) return inFlight.current
    inFlight.current = client
      .get(AI_QUEUE_ENDPOINT)
      .then((response) => {
        if (!alive.current) return
        setSnapshot(response.data as AIQueueSnapshot)
        setStale(false)
        setLastReadAt(Date.now())
      })
      .catch(() => {
        if (alive.current) setStale(true)
      })
      .finally(() => {
        inFlight.current = null
      })
    return inFlight.current
  }, [enabled])

  useEffect(() => {
    alive.current = true
    if (!enabled) return
    let timer: ReturnType<typeof setInterval> | null = null
    const start = () => {
      if (timer) return
      void refresh()
      timer = setInterval(() => void refresh(), intervalMs)
    }
    const stop = () => {
      if (timer) clearInterval(timer)
      timer = null
    }
    const onVisibility = () => {
      if (typeof document === 'undefined') return
      if (document.visibilityState === 'hidden') stop()
      else start()
    }
    if (typeof document === 'undefined' || document.visibilityState !== 'hidden') start()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      alive.current = false
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [enabled, intervalMs, refresh])

  return { snapshot, stale, refresh, lastReadAt }
}

export default useAIQueue
