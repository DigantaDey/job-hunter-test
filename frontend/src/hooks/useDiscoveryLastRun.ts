import { useCallback, useEffect, useState } from 'react'
import client from '../api/client'
import type { LastRun } from '../components/EmptyBoardBanner'

export const LAST_RUN_ENDPOINT = '/api/jobs/discovery/last-run'

export type DiscoveryLastRun = {
  /**
   * `undefined` — not known yet: the read is in flight, or it failed for a
   * reason other than 404. The banner keeps the plain empty copy rather than
   * claiming a reason it has not read.
   * `null` — the API answered 404: this user has no completed discovery run.
   */
  run: LastRun | null | undefined
  loading: boolean
  reload: () => void
}

/**
 * Read the last completed discovery run — the report behind the empty board.
 *
 * Deliberately thin: the backend classifies the run once, at run time, and this
 * only fetches that verdict, so the banner cannot disagree with the queue row or
 * the run's log line. Nothing is derived here.
 *
 * @param enabled  only read while it can be shown (the board is empty)
 * @param personaId  the track the board is filtered by, when it is — forwarded so
 *   the answer is about *that* track's last run, not the newest run of any track
 */
export function useDiscoveryLastRun(enabled: boolean, personaId?: number | null): DiscoveryLastRun {
  const [run, setRun] = useState<LastRun | null | undefined>(undefined)
  const [loading, setLoading] = useState(false)
  const [nonce, setNonce] = useState(0)

  const reload = useCallback(() => {
    setNonce((value) => value + 1)
  }, [])

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    setLoading(true)
    client
      .get(LAST_RUN_ENDPOINT, { params: personaId == null ? undefined : { persona_id: personaId } })
      .then((response) => {
        if (!cancelled) setRun(response.data as LastRun)
      })
      .catch((error: unknown) => {
        if (cancelled) return
        const status = (error as { response?: { status?: number } })?.response?.status
        // 404 is an answer ("no completed run"), not a failure. Anything else
        // leaves the state unknown so the banner claims nothing.
        setRun(status === 404 ? null : undefined)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [enabled, personaId, nonce])

  return { run, loading, reload }
}

export default useDiscoveryLastRun
