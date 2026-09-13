import type { ReactNode } from 'react'
import { AlertTriangle, CloudOff, Inbox, KeyRound, RefreshCw, Search, Settings2, Timer } from 'lucide-react'
import { Link } from 'react-router-dom'

/**
 * Why the job board is empty (v2.2.4).
 *
 * "No fresh jobs" used to be the whole story, and it covered three situations
 * with three different fixes: no source was ever attempted (configuration —
 * fix it in Settings), every attempted source failed (an outage — retry), and
 * healthy sources with nothing inside the freshness window (a quiet market —
 * widen the window or come back later). The backend classifies the run once,
 * when it happens, and `GET /api/jobs/discovery/last-run` serves that verdict;
 * this component only renders it, so the banner can never disagree with the
 * queue row or the run's log line.
 *
 * The fifth state is the fallback: a completed run with no `why_empty` (it found
 * jobs) on a board that is empty anyway — the jobs were deleted since, or the
 * board is filtered to something that run did not cover. It exists so the UI
 * never renders a reason-less empty state.
 */

/** The backend's closed vocabulary — `app.services.discovery.WHY_*`. */
export const WHY_NO_SOURCES_CONFIGURED = 'no_sources_configured'
export const WHY_ALL_SOURCES_FAILED = 'all_sources_failed'
export const WHY_NO_FRESH_POSTINGS = 'no_fresh_postings'

export const WHY_EMPTY_REASONS = [
  WHY_NO_SOURCES_CONFIGURED,
  WHY_ALL_SOURCES_FAILED,
  WHY_NO_FRESH_POSTINGS,
] as const

export type WhyEmpty = (typeof WHY_EMPTY_REASONS)[number]

/** The accounting behind the verdict (`report.summary`), served verbatim. */
export type LastRunSummary = {
  configured_sources?: string[]
  failed_sources?: Record<string, string>
  needs_credentials?: string[]
  demo_pool_enabled?: boolean
  freshness_window_hours?: number
  fetched?: number
  fresh?: number
}

/** `GET /api/jobs/discovery/last-run` — summary blocks only, never the report. */
export type LastRun = {
  at?: string | null
  added: number
  why_empty?: WhyEmpty | string
  summary?: LastRunSummary
  ai_rescore?: { enabled?: boolean; skipped?: string | null; scored?: number }
}

export type EmptyBoardState =
  | 'unknown'
  | 'no_run'
  | WhyEmpty
  | 'unexplained'

/**
 * `undefined` = not known (the read is in flight, or it failed for a reason
 * other than 404) — the page keeps its plain empty copy rather than claiming a
 * reason it has not read.
 * `null` = the API answered 404: this user has no completed discovery run.
 */
export function emptyBoardState(run: LastRun | null | undefined): EmptyBoardState {
  if (run === undefined) return 'unknown'
  if (run === null) return 'no_run'
  const why = String(run.why_empty || '')
  return (WHY_EMPTY_REASONS as readonly string[]).includes(why) ? (why as WhyEmpty) : 'unexplained'
}

/** A source error is a chip, not a paragraph: keep the id readable. */
export function truncateError(message: string, max = 60): string {
  const text = String(message || '').replace(/\s+/g, ' ').trim()
  return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text
}

/** Deterministic UTC date — no locale/ICU dependency, so it is testable. */
export function formatRunDate(iso?: string | null): string {
  if (!iso) return 'an unknown date'
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return String(iso).slice(0, 10)
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
  return `${parsed.getUTCDate()} ${months[parsed.getUTCMonth()]} ${parsed.getUTCFullYear()}`
}

const TONES = {
  neutral: 'bg-zinc-50 dark:bg-zinc-800/60 border-zinc-200 dark:border-zinc-700',
  warn: 'bg-amber-50 dark:bg-amber-950/40 border-amber-200 dark:border-amber-800',
  bad: 'bg-red-50 dark:bg-red-950/40 border-red-200 dark:border-red-800',
} as const

function Chip({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className="text-[11px] mono px-2 py-0.5 rounded-full bg-white dark:bg-zinc-900 border border-red-200 dark:border-red-900 text-red-700 dark:text-red-300 max-w-full truncate"
    >
      {children}
    </span>
  )
}

/** Settings → the source configuration block (the anchor the card carries). */
export function SourcesSettingsLink({ label = 'Settings → Sources' }: { label?: string }) {
  return (
    <Link to="/settings#scraping" className="text-xs mono font-medium underline decoration-dotted inline-flex items-center gap-1">
      <Settings2 className="w-3.5 h-3.5" /> {label}
    </Link>
  )
}

export function EmptyBoardBanner({ lastRun, onDiscover, busy = false }: {
  /** `undefined` while the last run is unknown, `null` when the API says 404. */
  lastRun?: LastRun | null
  onDiscover: () => void
  busy?: boolean
}) {
  const state = emptyBoardState(lastRun)

  if (state === 'unknown') {
    // The pre-existing copy, unchanged: nothing is claimed before the read lands.
    return (
      <div data-testid="empty-board-loading" className="p-8 text-center text-sm mono text-zinc-500">
        No jobs. Try Discover jobs — uses extracted keywords + freshness, with AI form detection.
      </div>
    )
  }

  const summary = lastRun?.summary
  const failed = Object.entries(summary?.failed_sources || {})
  const attempted = summary?.configured_sources || []
  const windowHours = summary?.freshness_window_hours
  const needsKeys = summary?.needs_credentials || []
  const added = lastRun?.added ?? 0

  const tone = state === 'all_sources_failed' ? TONES.bad
    : state === 'no_run' || state === 'unexplained' ? TONES.neutral
      : TONES.warn
  const Icon = state === 'all_sources_failed' ? CloudOff
    : state === 'no_sources_configured' ? Inbox
      : state === 'no_fresh_postings' ? Timer
        : state === 'unexplained' ? AlertTriangle
          : Search
  const headline = state === 'no_run' ? 'No discovery run yet'
    : state === 'no_sources_configured' ? 'No job sources are enabled'
      : state === 'all_sources_failed' ? 'All sources failed on the last run'
        : state === 'no_fresh_postings' ? 'Sources are healthy, but nothing fresh in the window'
          : `Last run found ${added} ${added === 1 ? 'job' : 'jobs'} on ${formatRunDate(lastRun?.at)}; your board is empty`
  const retry = state === 'all_sources_failed' || state === 'no_fresh_postings'

  return (
    <div data-testid="empty-board-banner" data-state={state} className={`p-5 rounded-xl border ${tone} text-left space-y-3`}>
      <div className="flex items-start gap-2">
        <Icon className="w-4 h-4 shrink-0 mt-0.5 text-zinc-500 dark:text-zinc-300" data-testid="empty-board-icon" />
        <div className="min-w-0 flex-1">
          <div data-testid="empty-board-headline" className="text-sm font-medium">{headline}</div>

          {state === 'no_run' && (
            <p className="text-xs mono text-zinc-600 dark:text-zinc-400 mt-1">
              Nothing has been fetched for this board yet. Discovery queries the sources you have enabled with the
              keywords extracted from your resume.
            </p>
          )}

          {state === 'no_sources_configured' && (
            <p className="text-xs mono text-zinc-600 dark:text-zinc-400 mt-1">
              The last run attempted <strong>no sources at all</strong> — every source is deselected, or live job
              sources are switched off. Nothing was fetched, so nothing could be found.
            </p>
          )}

          {state === 'all_sources_failed' && (
            <p className="text-xs mono text-zinc-600 dark:text-zinc-400 mt-1">
              {attempted.length} {attempted.length === 1 ? 'source was' : 'sources were'} attempted and every one of
              them errored. That is an outage or a credential problem, not an empty market — the individual failures
              are listed below.
            </p>
          )}

          {state === 'no_fresh_postings' && (
            <p className="text-xs mono text-zinc-600 dark:text-zinc-400 mt-1">
              The last run reached {attempted.length} {attempted.length === 1 ? 'source' : 'sources'} and kept{' '}
              {summary?.fetched ?? 0} posting{summary?.fetched === 1 ? '' : 's'} — nothing inside the{' '}
              <strong data-testid="freshness-window">{windowHours ?? 'configured'}-hour</strong> freshness window.
              Widen the window or try again later.
            </p>
          )}

          {state === 'unexplained' && (
            <p className="text-xs mono text-zinc-600 dark:text-zinc-400 mt-1">
              That run reported no reason to be empty, so the jobs it found were removed since (or this board is
              filtered to something that run did not cover). Run discovery again to refill it.
            </p>
          )}

          {failed.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-2" data-testid="failed-source-chips">
              {failed.map(([sourceId, message]) => (
                <Chip key={sourceId} title={String(message || '')}>
                  <span data-testid="failed-source-id">{sourceId}</span>
                  {' — '}
                  {truncateError(String(message || ''))}
                </Chip>
              ))}
            </div>
          )}

          {needsKeys.length > 0 && (
            <div className="text-[11px] mono text-zinc-600 dark:text-zinc-400 mt-2 inline-flex items-start gap-1"
                 data-testid="needs-credentials">
              <KeyRound className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              {needsKeys.length} {needsKeys.length === 1 ? 'source needs' : 'sources need'} credentials the operator
              never provided: {needsKeys.join(', ')}
            </div>
          )}

          {lastRun && (
            <div className="text-[11px] mono text-zinc-500 mt-2" data-testid="last-run-accounting">
              Last run {formatRunDate(lastRun.at)}
              {/* A report from before v2.2.4 carries no summary: show the counts
                  only when they were actually reported, never as zeros. */}
              {summary ? ` • ${attempted.length} source(s) attempted • ${summary.fetched ?? 0} fetched • ${summary.fresh ?? 0} fresh` : ''}
              {' '}• {added} added
              {summary?.demo_pool_enabled === false && (
                <span title="INCLUDE_DEMO_POOL is off in this deployment">
                  {' '}• demo seed data off, so nothing is fabricated to fill this board
                </span>
              )}
            </div>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          onClick={onDiscover}
          disabled={busy}
          data-testid="empty-board-cta"
          className="px-4 py-2 rounded-full bg-blue-600 text-white text-sm font-medium inline-flex items-center gap-2 disabled:opacity-50"
        >
          {busy ? <RefreshCw className="w-4 h-4 animate-spin" /> : retry ? <RefreshCw className="w-4 h-4" /> : <Search className="w-4 h-4" />}
          {retry ? 'Retry discovery' : 'Discover jobs now'}
        </button>
        {(state === 'no_sources_configured' || state === 'all_sources_failed' || state === 'no_fresh_postings') && (
          <SourcesSettingsLink />
        )}
      </div>
    </div>
  )
}

export default EmptyBoardBanner
