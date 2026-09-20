import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { CalendarClock, ExternalLink, History, Loader2, Plus } from 'lucide-react'
import { apiError } from '../api/client'
import {
  artifactLabel,
  dueLabel,
  getTrackingForJob,
  matchLabel,
  openTracking,
  originBadge,
  stateChip,
  timelineSlice,
  type JobTrackingLookup,
  type TrackingDocument,
  type TrackingWriteResponse,
} from '../lib/tracking'
import TrackingActions from './TrackingActions'
import TrackingTimeline from './TrackingTimeline'

/**
 * The application detail panel: the timeline, in the place the user is standing.
 *
 * This is what makes the acceptance criterion "timeline visible from the
 * application detail page" true — the Jobs drawer shows the recorded history of
 * the selected job without a page change, and the full board (`/tracking`) is
 * one link away.
 *
 * `GET …/job/{id}` answers `exists: false` for a job nobody tracks yet, and that
 * is a fact worth rendering rather than an empty box: it says what the record
 * would freeze (source, match score, artefact) and offers the one button that
 * starts it. A `GET` never creates a row.
 */
export default function TrackingPanel({ jobId, onTracked }: {
  jobId: number
  /** Lets the caller refresh the board's own status column after a write. */
  onTracked?: (document: TrackingDocument) => void
}) {
  const [lookup, setLookup] = useState<JobTrackingLookup | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async (): Promise<void> => {
    setLoading(true)
    setError('')
    try {
      setLookup(await getTrackingForJob(jobId))
    } catch (caught) {
      setError(apiError(caught, 'Could not load the tracking timeline'))
    } finally {
      setLoading(false)
    }
  }, [jobId])

  useEffect(() => { void load() }, [load])

  const written = (result: TrackingWriteResponse): void => {
    // The write answers with the fresh document — no second read needed.
    setLookup({ ...result.document, exists: true })
    onTracked?.(result.document)
  }

  if (loading && !lookup) {
    return (
      <div className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3 flex items-center gap-2 text-xs mono text-zinc-500">
        <Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading the application timeline…
      </div>
    )
  }

  if (error) {
    return (
      <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 rounded-xl p-3 text-xs mono text-red-700 dark:text-red-300"
           role="alert" data-testid="tracking-panel-error">
        {error}
      </div>
    )
  }

  if (!lookup) return null

  if (lookup.exists === false) {
    return (
      <div className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3 space-y-2" data-testid="tracking-untracked">
        <div className="text-xs mono font-medium flex items-center gap-1">
          <History className="w-3.5 h-3.5" /> Application timeline
        </div>
        <p className="text-xs text-zinc-600 dark:text-zinc-400">
          Not tracked yet. Starting a record freezes what we know now — the source, the match score
          and the artefact version — so a later re-score cannot rewrite the story of this application.
        </p>
        <button
          type="button"
          data-testid="start-tracking"
          onClick={async () => {
            setError('')
            try {
              const result = await openTracking({ job_id: jobId })
              written(result)
            } catch (caught) {
              setError(apiError(caught, 'Could not start tracking'))
            }
          }}
          className="min-h-[44px] px-3 py-2 rounded-full bg-blue-600 text-white text-xs font-medium inline-flex items-center gap-1.5 focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <Plus className="w-3.5 h-3.5" /> Track this application
        </button>
        {error && <div className="text-xs mono text-red-700 dark:text-red-300" role="alert">{error}</div>}
      </div>
    )
  }

  const document = lookup
  const origin = originBadge(document.state_origin)
  const rows = timelineSlice(document, 6)

  return (
    <div className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3 space-y-3" data-testid="tracking-panel">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs mono font-medium flex items-center gap-1">
          <History className="w-3.5 h-3.5" /> Application timeline
        </span>
        <span data-testid="tracking-state"
              className={`text-[11px] px-2 py-0.5 rounded-full mono ${stateChip(document.state)}`}>
          {document.state_label}
        </span>
        <span title={origin.title} className="text-[10px] px-2 py-0.5 rounded-full mono bg-white dark:bg-zinc-900 border dark:border-zinc-700">
          {origin.label}
        </span>
        {document.is_provisional && (
          <span className="text-[10px] px-2 py-0.5 rounded-full mono bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300">
            unconfirmed suggestion
          </span>
        )}
        <Link to={`/tracking?tracking_id=${document.tracking_id}`}
              data-testid="open-full-timeline"
              className="ml-auto text-[11px] mono text-blue-600 dark:text-blue-400 inline-flex items-center gap-1 min-h-[32px]">
          Full timeline <ExternalLink className="w-3 h-3" />
        </Link>
      </div>

      <div className="grid sm:grid-cols-3 gap-2 text-[11px] mono">
        <div className="bg-white dark:bg-zinc-900 rounded-lg p-2">
          <div className="text-zinc-500 uppercase text-[10px]">Frozen match score</div>
          <div className="text-zinc-800 dark:text-zinc-200" data-testid="snapshot-match">{matchLabel(document)}</div>
        </div>
        <div className="bg-white dark:bg-zinc-900 rounded-lg p-2">
          <div className="text-zinc-500 uppercase text-[10px]">Sent with</div>
          <div className="text-zinc-800 dark:text-zinc-200" data-testid="snapshot-artifact">{artifactLabel(document)}</div>
        </div>
        <div className="bg-white dark:bg-zinc-900 rounded-lg p-2">
          <div className="text-zinc-500 uppercase text-[10px] flex items-center gap-1">
            <CalendarClock className="w-3 h-3" /> Follow-up
          </div>
          <div className={document.follow_up.overdue ? 'text-red-600 dark:text-red-400' : 'text-zinc-800 dark:text-zinc-200'}
               data-testid="follow-up-line">
            {dueLabel(document.follow_up)}
          </div>
        </div>
      </div>

      <TrackingTimeline events={rows} empty="Nothing recorded yet — the buttons below start the history." />
      {document.counts.events > rows.length && (
        <div className="text-[11px] mono text-zinc-500">
          Showing the newest {rows.length} of {document.counts.events} rows.
        </div>
      )}

      <TrackingActions document={document} onWritten={written} dense />
      {error && <div className="text-xs mono text-red-700 dark:text-red-300" role="alert">{error}</div>}
    </div>
  )
}
