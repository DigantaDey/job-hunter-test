import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  AlertCircle, CalendarClock, Filter, History, Loader2, PieChart, RefreshCw, Search,
} from 'lucide-react'
import { apiError } from '../api/client'
import {
  artifactLabel,
  conversionText,
  dueLabel,
  formatDay,
  formatWhen,
  getFollowUps,
  getTracking,
  getTrackingMeta,
  getTrackingReport,
  groupLabel,
  listTracking,
  matchLabel,
  originBadge,
  rateText,
  refreshSnapshot,
  relativeLabel,
  REPORT_DIMENSIONS,
  stateChip,
  type FollowUpItem,
  type FollowUpsResponse,
  type TrackingDocument,
  type TrackingMeta,
  type TrackingReport,
} from '../lib/tracking'
import TrackingActions from '../components/TrackingActions'
import TrackingTimeline from '../components/TrackingTimeline'

/**
 * The application tracking board: what happened to every application, who says
 * so, and what it converted into.
 *
 * Three reads, one page:
 *
 *   • **Board** — the records, filterable by state/source, each opening a detail
 *     document that already carries its timeline, its allowed transitions and its
 *     buttons (no second call, no client-side state machine);
 *   • **Follow-ups** — the reminder agenda. Reading it also runs the sweep, so a
 *     reminder that came due while the app was closed is here;
 *   • **Report** — application→interview conversion grouped by source, role,
 *     match score or artefact version.
 *
 * The page never invents a number: a rate the backend sends as `null` renders as
 * `—`, because "nothing applied yet" and "applied, no interviews" are different
 * facts. And every fact on screen carries its origin badge — an interview here
 * exists because somebody recorded it, never because a message looked hopeful.
 */
type Tab = 'board' | 'follow-ups' | 'report'

export default function Tracking() {
  const [params, setParams] = useSearchParams()
  const tab = (params.get('tab') as Tab) || 'board'
  const [records, setRecords] = useState<TrackingDocument[]>([])
  const [total, setTotal] = useState(0)
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [meta, setMeta] = useState<TrackingMeta | null>(null)
  const [selected, setSelected] = useState<TrackingDocument | null>(null)
  const [filters, setFilters] = useState({ state: '', source: '', q: '', due_only: false })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [followUps, setFollowUps] = useState<FollowUpsResponse | null>(null)
  const [dimension, setDimension] = useState('source')
  const [report, setReport] = useState<TrackingReport | null>(null)
  const [reportLoading, setReportLoading] = useState(false)

  const trackingId = Number(params.get('tracking_id') || 0) || null

  const setTab = (next: Tab): void => {
    const query = new URLSearchParams(params)
    query.set('tab', next)
    setParams(query, { replace: true })
  }

  /** Read one record's document. Never touches the URL — callers decide that. */
  const loadRecord = useCallback(async (id: number): Promise<void> => {
    setError('')
    try {
      setSelected(await getTracking(id, 100))
    } catch (caught) {
      setError(apiError(caught, 'Could not load that application'))
    }
  }, [])

  /**
   * One URL write per user action.
   *
   * `setSearchParams` twice in the same handler would make the second call win
   * with a *stale* copy of the query, so "open this reminder" would set
   * `tracking_id` and silently drop the `tab=board` it had just asked for —
   * the record would be selected behind a tab that does not show it. Hence a
   * single merge helper, and a `nextTab` for the cross-tab jumps.
   */
  const openRecord = useCallback((id: number, nextTab?: Tab): void => {
    const query = new URLSearchParams(params)
    if (nextTab) query.set('tab', nextTab)
    query.set('tracking_id', String(id))
    setParams(query, { replace: true })
    void loadRecord(id)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params, setParams, loadRecord])

  const loadBoard = useCallback(async (): Promise<void> => {
    setLoading(true)
    setError('')
    try {
      const body = await listTracking({
        state: filters.state || undefined,
        source: filters.source || undefined,
        q: filters.q || undefined,
        due_only: filters.due_only || undefined,
        page: 1,
        page_size: 100,
      })
      // Defensive defaults: a partial or empty envelope renders an empty board
      // rather than throwing inside the render (which would blank the page).
      setRecords(body.items ?? [])
      setTotal(body.total ?? 0)
      setCounts(body.counts ?? {})
    } catch (caught) {
      setError(apiError(caught, 'Could not load the tracking board'))
    } finally {
      setLoading(false)
    }
  }, [filters.state, filters.source, filters.q, filters.due_only])

  useEffect(() => { void loadBoard() }, [loadBoard])
  useEffect(() => {
    getTrackingMeta().then(setMeta).catch(() => {})
  }, [])

  // Deep link: a notification or the Jobs drawer points at ?tracking_id=.
  useEffect(() => {
    if (trackingId && (!selected || selected.tracking_id !== trackingId)) void loadRecord(trackingId)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trackingId, loadRecord])

  useEffect(() => {
    if (tab !== 'follow-ups') return
    getFollowUps(30).then(setFollowUps).catch((caught) => setError(apiError(caught, 'Could not load reminders')))
  }, [tab])

  useEffect(() => {
    if (tab !== 'report') return
    setReportLoading(true)
    getTrackingReport({ group_by: dimension })
      .then(setReport)
      .catch((caught) => setError(apiError(caught, 'Could not build the report')))
      .finally(() => setReportLoading(false))
  }, [tab, dimension])

  const states = meta?.states || []
  const stateLabels = meta?.state_labels || {}
  const chips = useMemo(
    () => states.filter((state) => (counts[state] || 0) > 0),
    [states, counts],
  )

  const afterWrite = (document: TrackingDocument): void => {
    setSelected(document)
    void loadBoard()
    setNotice(document.follow_up.pending
      ? `Recorded. ${dueLabel(document.follow_up)}`
      : 'Recorded on the timeline.')
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-3 items-center justify-between">
        <h1 className="text-xl font-semibold tracking-tight flex items-center gap-2">
          <History className="w-5 h-5" /> Application tracking
        </h1>
        <div className="flex gap-1 bg-zinc-100 dark:bg-zinc-800 rounded-full p-1">
          {([['board', 'Board'], ['follow-ups', 'Follow-ups'], ['report', 'Report']] as [Tab, string][]).map(
            ([key, title]) => (
              <button key={key} type="button" onClick={() => setTab(key)} data-testid={`tab-${key}`}
                      className={`px-4 py-1.5 rounded-full text-xs mono min-h-[36px] ${tab === key ? 'bg-white dark:bg-zinc-900 shadow font-medium' : 'text-zinc-500'}`}>
                {title}
              </button>
            ),
          )}
        </div>
      </div>

      {notice && (
        <div className="text-xs mono p-2 rounded-lg bg-emerald-50 dark:bg-emerald-950 border border-emerald-200 dark:border-emerald-800 text-emerald-800 dark:text-emerald-200"
             role="status" data-testid="notice">{notice}</div>
      )}
      {error && (
        <div className="text-xs mono p-2 rounded-lg bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 text-red-700 dark:text-red-300 flex gap-2"
             role="alert" data-testid="error">
          <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />{error}
        </div>
      )}

      {tab === 'board' && (
        <>
          <div className="card p-3 flex flex-col sm:flex-row flex-wrap gap-2 items-start sm:items-center">
            <div className="flex items-center gap-2 text-xs mono text-zinc-500"><Filter className="w-3.5 h-3.5" /> Filter:</div>
            <select value={filters.state} data-testid="filter-state"
                    onChange={(event) => setFilters({ ...filters, state: event.target.value })}
                    className="text-sm border rounded-full px-3 py-2 bg-white dark:bg-zinc-900 dark:border-zinc-700 min-h-[44px]">
              <option value="">All statuses</option>
              {states.map((state) => (
                <option key={state} value={state}>{stateLabels[state] || state}</option>
              ))}
            </select>
            <label className="flex items-center gap-2 text-xs mono text-zinc-600 dark:text-zinc-300 min-h-[44px] px-2">
              <input type="checkbox" checked={filters.due_only} data-testid="filter-due"
                     onChange={(event) => setFilters({ ...filters, due_only: event.target.checked })} />
              Follow-up open
            </label>
            <div className="relative ml-auto w-full sm:w-64">
              <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-zinc-400" />
              <input value={filters.q} placeholder="Search title, company, note…" data-testid="filter-q"
                     onChange={(event) => setFilters({ ...filters, q: event.target.value })}
                     className="w-full pl-9 pr-3 py-2 rounded-full border text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700 min-h-[44px]" />
            </div>
          </div>

          <div className="flex flex-wrap gap-2" data-testid="state-counts">
            {chips.map((state) => (
              <button key={state} type="button"
                      onClick={() => setFilters({ ...filters, state: filters.state === state ? '' : state })}
                      className={`text-[11px] px-3 py-1.5 rounded-full mono border dark:border-zinc-700 min-h-[32px] ${stateChip(state)}`}>
                {stateLabels[state] || state} · {counts[state]}
              </button>
            ))}
            {!chips.length && <span className="text-xs mono text-zinc-500">Nothing tracked yet.</span>}
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-[0.95fr_1.05fr] gap-4">
            <div className="card overflow-hidden">
              <div className="p-3 border-b dark:border-zinc-800 flex items-center justify-between">
                <span className="text-sm font-medium mono">{total} applications</span>
                <button type="button" onClick={() => void loadBoard()} data-testid="reload-board"
                        className="text-xs mono text-zinc-500 inline-flex items-center gap-1 min-h-[32px] px-2 rounded hover:bg-zinc-100 dark:hover:bg-zinc-800">
                  <RefreshCw className="w-3.5 h-3.5" /> Refresh
                </button>
              </div>
              <div className="max-h-[70vh] overflow-auto divide-y dark:divide-zinc-800">
                {loading && !records.length ? (
                  <div className="p-6 text-center text-xs mono text-zinc-500 flex items-center justify-center gap-2">
                    <Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading…
                  </div>
                ) : records.length === 0 ? (
                  <div className="p-6 text-center text-xs mono text-zinc-500" data-testid="empty-board">
                    No applications tracked yet. Open a job on the Jobs board and press
                    “Track this application” — or apply through JobHunter and the record starts itself.
                  </div>
                ) : records.map((record) => (
                  <button key={record.tracking_id} type="button" data-testid={`record-${record.tracking_id}`}
                          onClick={() => openRecord(record.tracking_id)}
                          className={`w-full text-left p-3 hover:bg-zinc-50 dark:hover:bg-zinc-800/50 min-h-[44px] focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-500 ${selected?.tracking_id === record.tracking_id ? 'bg-blue-50 dark:bg-blue-950/30' : ''}`}>
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className={`text-[11px] px-2 py-0.5 rounded-full mono ${stateChip(record.state)}`}>
                        {record.state_label}
                      </span>
                      <span className="text-sm font-medium truncate">{record.job.title || 'Untitled role'}</span>
                      <span className="text-xs text-zinc-500 truncate">{record.job.company}</span>
                      {record.is_provisional && (
                        <span className="text-[10px] mono px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300">
                          unconfirmed
                        </span>
                      )}
                    </div>
                    <div className="text-[11px] mono text-zinc-500 mt-1 flex flex-wrap gap-x-2">
                      <span>{record.attribution.source}</span>
                      {record.attribution.role_family && <span>{record.attribution.role_family}</span>}
                      <span>{matchLabel(record)}</span>
                      {record.follow_up.pending && (
                        <span className={record.follow_up.overdue ? 'text-red-600 dark:text-red-400' : ''}>
                          ⏰ {dueLabel(record.follow_up)}
                        </span>
                      )}
                      <span>{record.counts.events} rows{record.counts.corrections ? ` · ${record.counts.corrections} corrected` : ''}</span>
                    </div>
                  </button>
                ))}
              </div>
            </div>

            <div className="card p-4 lg:sticky lg:top-6 h-fit max-h-[85vh] overflow-auto space-y-3">
              {!selected ? (
                <div className="py-16 text-center">
                  <History className="w-8 h-8 mx-auto text-zinc-300 dark:text-zinc-700" />
                  <div className="mono text-sm text-zinc-500 mt-2">Select an application to see its timeline</div>
                  <div className="text-xs text-zinc-400 mt-1 mono">Every row is kept — status changes append, they never overwrite</div>
                </div>
              ) : (
                <>
                  <div>
                    <div className="text-lg font-semibold leading-tight">{selected.job.title}</div>
                    <div className="text-sm text-zinc-600 dark:text-zinc-400">{selected.job.company}</div>
                    <div className="flex flex-wrap items-center gap-2 mt-2">
                      <span data-testid="selected-state"
                            className={`text-[11px] px-2 py-0.5 rounded-full mono ${stateChip(selected.state)}`}>
                        {selected.state_label}
                      </span>
                      <span className="text-[11px] px-2 py-0.5 rounded-full mono bg-zinc-100 dark:bg-zinc-800">
                        {selected.phase_label}
                      </span>
                      {(() => {
                        const origin = originBadge(selected.state_origin)
                        return (
                          <span title={origin.title} data-testid="selected-origin"
                                className={`text-[11px] px-2 py-0.5 rounded-full mono ${origin.className}`}>
                            {origin.label}
                          </span>
                        )
                      })()}
                      <span className="text-[11px] mono text-zinc-500">board: {selected.job_status}</span>
                    </div>
                  </div>

                  {selected.is_provisional && (
                    <div className="text-xs p-2 rounded-lg bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 text-amber-900 dark:text-amber-200">
                      This record carries an <strong>unconfirmed suggestion</strong> read from a message. Nothing was
                      recorded as an interview — confirm it as your own report, or dismiss it.
                    </div>
                  )}

                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px] mono">
                    <div className="bg-zinc-50 dark:bg-zinc-800 rounded-lg p-2">
                      <div className="text-zinc-500 uppercase text-[10px]">Applied</div>
                      <div>{selected.timestamps.applied_at ? formatDay(selected.timestamps.applied_at) : '—'}</div>
                    </div>
                    <div className="bg-zinc-50 dark:bg-zinc-800 rounded-lg p-2">
                      <div className="text-zinc-500 uppercase text-[10px]">First response</div>
                      <div>{selected.timestamps.first_response_at ? relativeLabel(selected.timestamps.first_response_at) : '—'}</div>
                    </div>
                    <div className="bg-zinc-50 dark:bg-zinc-800 rounded-lg p-2">
                      <div className="text-zinc-500 uppercase text-[10px]">Interview</div>
                      <div>{selected.timestamps.interview_at ? formatDay(selected.timestamps.interview_at) : '—'}</div>
                    </div>
                    <div className="bg-zinc-50 dark:bg-zinc-800 rounded-lg p-2">
                      <div className="text-zinc-500 uppercase text-[10px]">Outcome</div>
                      <div>{selected.timestamps.outcome_at ? formatDay(selected.timestamps.outcome_at) : '—'}</div>
                    </div>
                  </div>

                  <div className="grid sm:grid-cols-2 gap-2 text-[11px] mono">
                    <div className="bg-zinc-50 dark:bg-zinc-800 rounded-lg p-2">
                      <div className="text-zinc-500 uppercase text-[10px]">Match score when tracking started</div>
                      <div data-testid="snapshot-match">{matchLabel(selected)}</div>
                    </div>
                    <div className="bg-zinc-50 dark:bg-zinc-800 rounded-lg p-2">
                      <div className="text-zinc-500 uppercase text-[10px]">Sent with</div>
                      <div data-testid="snapshot-artifact">{artifactLabel(selected)}</div>
                      <button type="button" data-testid="refresh-snapshot"
                              onClick={async () => {
                                try {
                                  const result = await refreshSnapshot(selected.tracking_id)
                                  afterWrite(result.document)
                                  setNotice(result.event
                                    ? 'Snapshots refreshed — the change is a timeline row.'
                                    : 'Snapshots unchanged — nothing to record.')
                                } catch (caught) {
                                  setError(apiError(caught, 'Could not refresh the snapshots'))
                                }
                              }}
                              className="mt-1 text-[11px] mono text-blue-600 dark:text-blue-400 inline-flex items-center gap-1 min-h-[28px]">
                                <RefreshCw className="w-3 h-3" /> Re-read what we would send now
                              </button>
                    </div>
                  </div>

                  <div className="flex flex-wrap items-center gap-2 text-[11px] mono text-zinc-500"
                       data-testid="follow-up-line">
                    <CalendarClock className="w-3.5 h-3.5" />
                    <span className={selected.follow_up.overdue ? 'text-red-600 dark:text-red-400' : ''}>
                      {dueLabel(selected.follow_up)}
                    </span>
                    {selected.follow_up.note && <span>“{selected.follow_up.note}”</span>}
                    {selected.follow_up.notified_at && (
                      <span title="When the reminder notification was written">
                        reminded {formatWhen(selected.follow_up.notified_at)}
                      </span>
                    )}
                  </div>

                  {selected.note && (
                    <div className="text-xs p-2 rounded-lg bg-white dark:bg-zinc-900 border dark:border-zinc-800 whitespace-pre-wrap">
                      {selected.note}
                    </div>
                  )}

                  <TrackingActions document={selected} onWritten={(result) => afterWrite(result.document)} />

                  <div>
                    <div className="text-xs mono font-medium mb-2 flex items-center justify-between">
                      <span>Timeline — {selected.counts.events} rows, append-only</span>
                      {selected.counts.corrections > 0 && (
                        <span className="text-amber-600 dark:text-amber-400">{selected.counts.corrections} correction(s)</span>
                      )}
                    </div>
                    <TrackingTimeline events={selected.timeline} />
                  </div>

                  {selected.transitions.terminal && (
                    <p className="text-[11px] mono text-zinc-500">
                      This application is closed. Its rows are kept forever — a correction is the only way to change
                      what it says, and it always names the row it corrects.
                    </p>
                  )}
                </>
              )}
            </div>
          </div>
        </>
      )}

      {tab === 'follow-ups' && (
        <div className="card p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium flex items-center gap-2">
              <CalendarClock className="w-4 h-4" /> Reminders you asked for
            </h2>
            {followUps && (
              <span className="text-[11px] mono text-zinc-500" data-testid="sweep">
                {followUps.sweep.notified
                  ? `${followUps.sweep.notified} reminder(s) written just now`
                  : 'nothing due right now'}
              </span>
            )}
          </div>
          <p className="text-xs text-zinc-600 dark:text-zinc-400">
            A follow-up is a promise you made yourself. The worker reminds you when it comes due even if the app is
            closed; opening this tab reminds you too. Nothing is sent to anybody — these are in-app notifications
            of kind <span className="mono">{followUps?.notification_kind || 'application_follow_up'}</span>.
          </p>
          {!followUps ? (
            <div className="text-xs mono text-zinc-500 flex items-center gap-2"><Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading…</div>
          ) : followUps.items.length === 0 ? (
            <div className="text-xs mono text-zinc-500 py-6 text-center" data-testid="no-follow-ups">
              No follow-ups in the next 30 days. Set one from any application with “Set follow-up”.
            </div>
          ) : (
            <ul className="space-y-2">
              {followUps.items.map((item: FollowUpItem) => (
                <li key={item.tracking_id} data-testid={`follow-up-${item.tracking_id}`}
                    className="p-3 rounded-xl border dark:border-zinc-800 flex flex-wrap items-center gap-2">
                  <span className={`text-[11px] px-2 py-0.5 rounded-full mono ${stateChip(item.state)}`}>{item.state_label}</span>
                  <span className="text-sm font-medium">{item.title}</span>
                  <span className="text-xs text-zinc-500">{item.company}</span>
                  <span className={`text-[11px] mono ml-auto ${item.overdue ? 'text-red-600 dark:text-red-400 font-medium' : 'text-zinc-500'}`}
                        data-testid={`due-${item.tracking_id}`}>
                    {item.overdue ? 'Overdue · ' : ''}{formatDay(item.due_at)} ({relativeLabel(item.due_at)})
                  </span>
                  {item.note && <span className="w-full text-xs text-zinc-600 dark:text-zinc-400">“{item.note}”</span>}
                  <button type="button" onClick={() => openRecord(item.tracking_id, 'board')}
                          data-testid={`open-follow-up-${item.tracking_id}`}
                          className="text-[11px] mono text-blue-600 dark:text-blue-400 underline min-h-[32px]">
                    Open the timeline
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {tab === 'report' && (
        <div className="space-y-4">
          <div className="card p-4 space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-sm font-medium flex items-center gap-2"><PieChart className="w-4 h-4" /> Application → interview conversion</h2>
              <select value={dimension} onChange={(event) => setDimension(event.target.value)} data-testid="group-by"
                      className="ml-auto text-sm border rounded-full px-3 py-2 bg-white dark:bg-zinc-900 dark:border-zinc-700 min-h-[44px]">
                {REPORT_DIMENSIONS.map((option) => (
                  <option key={option.key} value={option.key}>{option.label}</option>
                ))}
              </select>
            </div>
            {reportLoading && !report ? (
              <div className="text-xs mono text-zinc-500 flex items-center gap-2"><Loader2 className="w-3.5 h-3.5 animate-spin" /> Building…</div>
            ) : report ? (
              <>
                <div className="text-sm" data-testid="conversion-summary">{conversionText(report.totals)}</div>
                <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-[11px] mono">
                  {[
                    ['Tracked', report.totals.tracked],
                    ['Applied', report.totals.applied],
                    ['Responses', report.totals.responses],
                    ['Offers', report.totals.offers],
                    ['Rejections', report.totals.rejections],
                  ].map(([title, value]) => (
                    <div key={String(title)} className="bg-zinc-50 dark:bg-zinc-800 rounded-lg p-2">
                      <div className="text-zinc-500 uppercase text-[10px]">{title}</div>
                      <div className="text-base font-bold">{value}</div>
                    </div>
                  ))}
                </div>
                <div className="text-[11px] mono text-zinc-500 flex flex-wrap gap-x-3" data-testid="totals-extra">
                  <span>Response rate: {rateText(report.totals.applied_to_response_rate)}</span>
                  <span>Interview → offer: {rateText(report.totals.interview_to_offer_rate)}</span>
                  <span>Avg match score: {report.totals.avg_match_score ?? '—'}</span>
                  <span>Avg days to interview: {report.totals.avg_days_to_interview ?? '—'}</span>
                  <span>Corrections: {report.totals.corrections}</span>
                </div>
                <div className="text-[11px] mono text-zinc-500" data-testid="by-origin">
                  Recorded by: {Object.entries(report.totals.by_origin)
                    .map(([origin, count]) => `${origin.replace(/_/g, ' ')} ${count}`).join(' • ') || '—'}
                </div>

                <div className="overflow-x-auto">
                  <table className="w-full text-xs mono">
                    <thead className="text-zinc-500 text-left">
                      <tr>
                        <th className="py-1 pr-3 font-medium">{REPORT_DIMENSIONS.find((item) => item.key === dimension)?.label || 'Group'}</th>
                        <th className="py-1 pr-3 font-medium">Applied</th>
                        <th className="py-1 pr-3 font-medium">Responses</th>
                        <th className="py-1 pr-3 font-medium">Interviews</th>
                        <th className="py-1 pr-3 font-medium">Conversion</th>
                        <th className="py-1 pr-3 font-medium">Offers</th>
                        <th className="py-1 pr-3 font-medium">Avg score</th>
                      </tr>
                    </thead>
                    <tbody data-testid="report-rows">
                      {report.groups.map((group) => (
                        <tr key={group.key} data-testid={`group-${group.key}`} className="border-t dark:border-zinc-800">
                          <td className="py-1.5 pr-3">{groupLabel(group.key, report.group_by)}</td>
                          <td className="py-1.5 pr-3">{group.applied}</td>
                          <td className="py-1.5 pr-3">{group.responses}</td>
                          <td className="py-1.5 pr-3">{group.interviews}</td>
                          <td className="py-1.5 pr-3 font-medium">{rateText(group.application_to_interview_rate)}</td>
                          <td className="py-1.5 pr-3">{group.offers}</td>
                          <td className="py-1.5 pr-3">{group.avg_match_score ?? '—'}</td>
                        </tr>
                      ))}
                      {!report.groups.length && (
                        <tr><td colSpan={7} className="py-4 text-center text-zinc-500">Nothing applied yet — no conversion to report.</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>

                <div className="text-[11px] mono text-zinc-500 space-y-1">
                  <div data-testid="report-disclaimer">{report.disclaimer}</div>
                  <div>
                    Counts as an interview: {report.interview_definition
                      .map((state) => stateLabels[state] || state).join(', ')}.
                  </div>
                </div>
              </>
            ) : null}
          </div>
        </div>
      )}
    </div>
  )
}
