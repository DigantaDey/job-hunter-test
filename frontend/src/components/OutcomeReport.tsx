/**
 * The outcome report — interviews and meaningful outcomes, not activity volume.
 *
 * Rendered at the top of the Analytics page. Four things this component must
 * never do, each of which the backend already refuses to send it the data for:
 *
 *   • print a percentage the sample cannot carry — a withheld rate arrives as
 *     `null` and renders `—` with the reason from `sufficiencyLabel`;
 *   • present an `estimated` number (a match score) as a counted fact — every
 *     metric carries a provenance chip;
 *   • claim a trend it cannot measure — `not_enough_data` is rendered as
 *     "not enough data yet", never as a flat 0%;
 *   • offer a download the privacy gate refused — the export block comes from
 *     the server's own check.
 */
import { useCallback, useEffect, useState } from 'react'
import { CalendarClock, Download, Gauge, Info, RefreshCw, ShieldCheck, TrendingUp } from 'lucide-react'
import client from '../api/client'
import {
  COMPARISON_LABELS,
  RANGE_OPTIONS,
  boundaryNote,
  calibrationText,
  exportUrl,
  fetchOutcomeReport,
  groupRateText,
  groupRateTitle,
  isInsufficient,
  metricText,
  orderedMetrics,
  provenanceBadge,
  sufficiencyLabel,
  trendLabel,
  windowLabel,
  type OutcomeReport,
  type ReportRange,
} from '../lib/reporting'
import type { ReportComparisonDimension } from '../lib/contracts'

const DIMENSIONS: ReportComparisonDimension[] = ['source', 'score_band', 'score_range',
  'role_family', 'artifact']

export default function OutcomeReport({ timezone }: { timezone?: string }) {
  const [range, setRange] = useState<ReportRange>('last_30_days')
  const [report, setReport] = useState<OutcomeReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async (next: ReportRange, tz?: string) => {
    setLoading(true)
    setError('')
    try {
      setReport(await fetchOutcomeReport({ range: next, timezone: tz }))
    } catch (err: unknown) {
      setError((err as { response?: { data?: { detail?: { message?: string } } } })
        ?.response?.data?.detail?.message || 'Could not load the outcome report.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load(range, timezone) }, [load, range, timezone])

  const trend = report?.trend
  const trendChip = trendLabel(trend?.direction)
  // Anything that is not a report renders a notice, never a blank page: the
  // strip is one card of the Analytics page, and a payload this component
  // cannot read (an error body, a stale cache, a server that answered with
  // something else) must not take the rest of the page down with it. The
  // check is deliberately about the shape this component dereferences, so it
  // cannot go stale the way a hand-written schema would.
  const usable = Boolean(
    report && Array.isArray(report.metrics) && report.signal && report.window
    && report.timing && report.quality && report.privacy && report.calibration,
  )

  return (
    <section className="space-y-4" data-testid="outcome-report">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold flex items-center gap-2">
          <Gauge className="w-5 h-5" /> Outcomes
        </h2>
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-xs mono text-zinc-500" htmlFor="report-range">Range</label>
          <select
            id="report-range"
            data-testid="report-range"
            className="text-xs mono rounded-lg border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-2 py-1"
            value={range}
            onChange={event => setRange(event.target.value as ReportRange)}
          >
            {RANGE_OPTIONS.map(option => (
              <option key={option.key} value={option.key}>{option.label}</option>
            ))}
          </select>
          {report?.export?.allowed && (
            <a
              data-testid="report-export"
              href={exportUrl('csv', { range, timezone })}
              className="inline-flex items-center gap-1 text-xs mono px-2 py-1 rounded-lg border border-zinc-300 dark:border-zinc-700 hover:bg-zinc-50 dark:hover:bg-zinc-800"
              title={report.export.note}
            >
              <Download className="w-3.5 h-3.5" /> CSV
            </a>
          )}
        </div>
      </div>

      {loading && !report && <div className="card p-4 text-sm mono">Loading outcomes…</div>}
      {error && (
        <div className="card p-4 text-sm" data-testid="report-error">
          <div className="flex items-center gap-2">
            <Info className="w-4 h-4 text-amber-500" />
            <span>{error}</span>
            <button className="mono text-xs underline" onClick={() => void load(range, timezone)}>
              Retry
            </button>
          </div>
        </div>
      )}

      {report && !usable && (
        <div className="card p-4 text-sm" data-testid="report-unavailable">
          <div className="flex items-center gap-2">
            <Info className="w-4 h-4 text-amber-500" />
            <span>
              The outcome report came back in a shape this page cannot read — nothing is shown
              rather than a number that cannot be trusted.
            </span>
            <button className="mono text-xs underline" onClick={() => void load(range, timezone)}>
              Retry
            </button>
          </div>
        </div>
      )}

      {usable && report && (
        <>
          {/* The week in one sentence, plus whether it beat the last one. */}
          <div className="card p-4 space-y-2" data-testid="report-headline">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`text-[11px] mono px-2 py-0.5 rounded-full ${trendChip.className}`}
                    data-testid="trend-chip">
                {trendChip.label}
              </span>
              <span className="text-[11px] mono text-zinc-500" data-testid="report-window">
                <CalendarClock className="w-3 h-3 inline mr-1" />
                {windowLabel(report.window)}
              </span>
              <span className="text-[11px] mono text-zinc-400">{boundaryNote(report.window)}</span>
              {loading && <RefreshCw className="w-3 h-3 animate-spin text-zinc-400" />}
            </div>
            <p className="text-sm">{report.headline}</p>
            <p className="text-xs text-zinc-600 dark:text-zinc-400" data-testid="trend-headline">
              {trend?.headline}
            </p>
            <p className="text-xs text-zinc-500 dark:text-zinc-400" data-testid="signal">
              {report.signal.headline}
            </p>
          </div>

          {/* Every metric: the number, what backs it, and how big the sample is. */}
          <div className="card p-4" data-testid="report-metrics">
            <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {orderedMetrics(report).map(metric => {
                const badge = provenanceBadge(metric.provenance)
                const short = isInsufficient(metric.sample, metric.sufficiency)
                return (
                  <div key={metric.key} className="p-3 rounded-xl bg-zinc-50 dark:bg-zinc-800"
                       data-testid={`metric-${metric.key}`}>
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs text-zinc-500 dark:text-zinc-400">{metric.label}</span>
                      <span className={`text-[10px] mono px-1.5 py-0.5 rounded-full ${badge.className}`}
                            title={badge.title} data-testid={`provenance-${metric.key}`}>
                        {badge.label}
                      </span>
                    </div>
                    <div className="text-xl font-semibold mono mt-1"
                         data-testid={`value-${metric.key}`}>
                      {metricText(metric)}
                    </div>
                    <div className={`text-[10px] mono mt-1 ${short ? 'text-amber-600 dark:text-amber-400' : 'text-zinc-400'}`}
                         data-testid={`sample-${metric.key}`}>
                      {sufficiencyLabel(metric.sample, metric.sufficiency)}
                    </div>
                    {metric.note && (
                      <div className="text-[10px] text-zinc-400 mt-1">{metric.note}</div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>

          {/* Comparisons — grouped, and only where the group can carry a rate. */}
          <div className="card p-4 space-y-4" data-testid="report-comparisons">
            <h3 className="font-medium flex items-center gap-2">
              <TrendingUp className="w-4 h-4" /> Interview rate by …
            </h3>
            <div className="grid lg:grid-cols-2 gap-4">
              {DIMENSIONS.map(dimension => {
                const block = report.comparisons?.[dimension]
                if (!block) return null
                return (
                  <div key={dimension} data-testid={`comparison-${dimension}`}>
                    <div className="flex items-baseline justify-between">
                      <div className="text-xs mono uppercase text-zinc-500">
                        {COMPARISON_LABELS[dimension]}
                      </div>
                      <div className="text-[10px] mono text-zinc-400">
                        {block.withheld_group_count > 0
                          ? `${block.withheld_group_count} group(s) withheld`
                          : `${block.sufficient_group_count} comparable group(s)`}
                      </div>
                    </div>
                    {block.groups.length === 0 ? (
                      <div className="text-xs mono text-zinc-500 mt-2">No applications in range.</div>
                    ) : (
                      <table className="w-full text-xs mono mt-2">
                        <thead className="text-zinc-500 text-left">
                          <tr>
                            <th className="py-1 pr-2 font-medium">Group</th>
                            <th className="py-1 pr-2 font-medium">Applied</th>
                            <th className="py-1 pr-2 font-medium">Interviews</th>
                            <th className="py-1 pr-2 font-medium">Rate</th>
                          </tr>
                        </thead>
                        <tbody>
                          {block.groups.map(group => (
                            <tr key={group.key} className="border-t dark:border-zinc-800"
                                data-testid={`group-${dimension}-${group.key}`}>
                              <td className="py-1 pr-2">{group.label}</td>
                              <td className="py-1 pr-2">{group.applied}</td>
                              <td className="py-1 pr-2">{group.interviews}</td>
                              <td className={`py-1 pr-2 ${isInsufficient(group.sample, group.sufficiency)
                                ? 'text-amber-600 dark:text-amber-400' : ''}`}
                                  title={groupRateTitle(group)}>
                                {groupRateText(group)}
                                {isInsufficient(group.sample, group.sufficiency) && (
                                  <span className="ml-1 text-[10px] text-zinc-400">
                                    (insufficient sample)
                                  </span>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                    {block.best && (
                      <div className="text-[11px] text-zinc-500 mt-1">
                        Best: {block.best.label} — {block.best.interviews} of {block.best.applied}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>

          {/* Speed, quality and the calibration refusal. */}
          <div className="grid lg:grid-cols-2 gap-4">
            <div className="card p-4" data-testid="report-timing">
              <h3 className="font-medium">Time from posting to application</h3>
              <div className="text-2xl font-semibold mono mt-1">
                {metricText({ value: report.timing.median_days, unit: 'days' })}
              </div>
              <div className="text-xs mono text-zinc-500 mt-1">
                median • {report.timing.applications_measured} measured •{' '}
                {sufficiencyLabel(report.timing.sample, report.timing.sufficiency)}
              </div>
              {report.timing.note && (
                <div className="text-[11px] text-zinc-500 mt-1">{report.timing.note}</div>
              )}
              <div className="mt-3 text-xs mono space-y-1" data-testid="report-quality">
                <div className="flex justify-between">
                  <span>Outcome data coverage</span>
                  <span>{report.quality.tracking_coverage_percent === null
                    ? '—' : `${report.quality.tracking_coverage_percent}%`}</span>
                </div>
                <div className="flex justify-between">
                  <span>Applications without a record</span>
                  <span>{report.quality.applications_without_a_tracking_record}</span>
                </div>
                <div className="flex justify-between">
                  <span>Application failure rate</span>
                  <span>{report.quality.application_failure_rate === null
                    ? '—' : `${report.quality.application_failure_rate}%`}</span>
                </div>
                <div className="flex justify-between">
                  <span>User correction rate</span>
                  <span>{report.quality.correction_rate === null
                    ? '—' : `${report.quality.correction_rate}%`}</span>
                </div>
              </div>
            </div>

            <div className="card p-4" data-testid="report-calibration">
              <h3 className="font-medium flex items-center gap-2">
                <ShieldCheck className="w-4 h-4" /> What we will not claim
              </h3>
              <p className="text-xs text-zinc-600 dark:text-zinc-400 mt-2">
                {calibrationText(report.calibration)}
              </p>
              <div className="text-[11px] mono text-zinc-500 mt-3" data-testid="report-privacy">
                Scope: {report.privacy.scope === 'self_only' ? 'your account only' : String(report.privacy.scope)}
                {' • '}no other users’ data • no cross-tenant aggregates
              </div>
              <div className="text-[11px] mono text-zinc-400 mt-1">
                report {report.report_version} • calculation {report.calculation_id.slice(0, 12)}
              </div>
              <div className="text-[11px] text-zinc-500 mt-2">{report.disclaimer}</div>
            </div>
          </div>
        </>
      )}
    </section>
  )
}

/** Kept exported for the page-level test that asserts the API client is used. */
export const reportClient = client
