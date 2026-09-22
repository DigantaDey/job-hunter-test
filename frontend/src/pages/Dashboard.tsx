import { useCallback, useEffect, useRef, useState } from 'react'
import client, { apiError } from '../api/client'
import {
  Briefcase, CalendarClock, CheckCircle2, ClipboardList, Compass, FileCheck2,
  GraduationCap, Mail, RefreshCw, Sparkles, Target, UserRound, ArrowRight, AlertTriangle,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { useAIWork } from '../context/AIWorkContext'
import { useOnline } from '../hooks/useOnline'
import { EmptyState, ErrorState, LoadingBlock, OfflineBanner, SectionCard, SectionSkeleton } from '../components/states'

/**
 * The end-user dashboard (v2.3).
 *
 * This page is the product, not the plumbing: it answers "what is my job
 * search doing and what should I do next" from one tenant-scoped aggregate
 * (`GET /api/me/dashboard`). Profile completeness, discovery status, top
 * matches with the reasons behind them, the queue of work waiting on the
 * user, applications in progress, interview activity, the week's outcomes and
 * follow-up reminders.
 *
 * Deliberately absent: AI provider configuration, token/cost meters, queue
 * internals and worker health — owner material that lives in /admin. A job
 * seeker's success metric is interviews and applications, never tokens.
 *
 * States (P1 #4 pattern kept): one aggregate read, a loading skeleton that
 * closes on the first verdict, a full error card with retry, per-section
 * empty states, and an honest offline state via `useOnline`.
 */

const POLL_MS = 60_000

type Dashboard = any

function when(iso?: string | null): string {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const diff = Date.now() - then
  const abs = Math.abs(diff)
  const mins = Math.round(abs / 60_000)
  if (mins < 1) return diff >= 0 ? 'just now' : 'in a moment'
  if (mins < 60) return diff >= 0 ? `${mins}m ago` : `in ${mins}m`
  const hours = Math.round(mins / 60)
  if (hours < 24) return diff >= 0 ? `${hours}h ago` : `in ${hours}h`
  const days = Math.round(hours / 24)
  return diff >= 0 ? `${days}d ago` : `in ${days}d`
}

const BAND_STYLES: Record<string, string> = {
  strong: 'bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-800 dark:text-emerald-300',
  good: 'bg-blue-50 border-blue-200 text-blue-700 dark:bg-blue-950 dark:border-blue-800 dark:text-blue-300',
  possible: 'bg-amber-50 border-amber-200 text-amber-700 dark:bg-amber-950 dark:border-amber-800 dark:text-amber-300',
}

function ScoreChip({ score, band }: { score: number; band: string }) {
  return (
    <span data-testid="match-score"
          className={`shrink-0 text-[11px] px-2 py-1 rounded-full border mono ${BAND_STYLES[band] || 'bg-zinc-100 border-zinc-200 text-zinc-600 dark:bg-zinc-800 dark:border-zinc-700 dark:text-zinc-300'}`}>
      {score}% match
    </span>
  )
}

export default function Dashboard() {
  const online = useOnline()
  const work = useAIWork()
  const [data, setData] = useState<Dashboard | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [retrying, setRetrying] = useState(false)
  const mounted = useRef(true)
  const inFlight = useRef(false)
  const hasData = useRef(false)

  const load = async (isRetry = false) => {
    if (inFlight.current) return
    inFlight.current = true
    if (isRetry) setRetrying(true)
    try {
      const { data: payload } = await client.get('/api/me/dashboard')
      if (!mounted.current) return
      hasData.current = true
      setData(payload)
      setError(null)
    } catch (e) {
      if (!mounted.current) return
      // A background refresh keeps the last good read on screen; only a first
      // load (or an explicit retry) swaps to the error card.
      if (!hasData.current) setError(apiError(e, 'We could not reach your dashboard.'))
    } finally {
      inFlight.current = false
      if (mounted.current) {
        setLoading(false)
        setRetrying(false)
      }
    }
  }

  useEffect(() => {
    mounted.current = true
    void load()
    return () => { mounted.current = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Visibility-aware refresh: a hidden tab polls nothing; coming back re-reads.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === 'visible') void load()
    }
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') void load()
    }, POLL_MS)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (loading && !data) {
    return (
      <div className="space-y-4" data-testid="dashboard-loading">
        <div className="h-8 w-64 rounded-lg bg-zinc-100 dark:bg-zinc-800 animate-pulse" />
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          {[0, 1, 2, 3].map(i => <div key={i} className="h-24 rounded-xl bg-zinc-100 dark:bg-zinc-800 animate-pulse" />)}
        </div>
        <SectionSkeleton rows={4} />
        <LoadingBlock label="Putting your dashboard together…" />
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="p-8" data-testid="dashboard-error">
        <div className="card max-w-lg mx-auto p-8">
          {online ? (
            <ErrorState message={error} onRetry={() => void load(true)} retrying={retrying} />
          ) : (
            <div className="text-center" data-testid="offline-error">
              <OfflineBanner />
              <p className="text-sm text-zinc-500 dark:text-zinc-400">
                Your dashboard could not load because you're offline. Reconnect and try again — nothing was lost.
              </p>
              <button
                onClick={() => void load(true)}
                disabled={retrying}
                className="mt-4 inline-flex items-center gap-2 px-5 py-2.5 rounded-full bg-blue-600 text-white text-sm font-medium disabled:opacity-60"
              >
                <RefreshCw className={`w-4 h-4 ${retrying ? 'animate-spin' : ''}`} />
                {retrying ? 'Retrying…' : 'Try again'}
              </button>
            </div>
          )}
        </div>
      </div>
    )
  }

  if (!data) return null

  const profile = data.profile ?? {}
  const discovery = data.discovery ?? {}
  const weekly = data.weekly_report ?? {}
  const interviews = data.interviews ?? {}
  const followUps: any[] = data.follow_ups?.items ?? []
  const topMatches: any[] = data.top_matches ?? []
  const needsReview: any[] = data.needs_review ?? []
  const inProgress: any[] = data.applications_in_progress ?? []
  const actionQueue: any[] = data.action_queue ?? []
  const assistant = data.assistant ?? {}
  const live = work.snapshot

  const statCards = [
    { label: 'New matches this week', value: weekly.new_matches ?? 0, icon: Sparkles, tone: 'text-blue-600 dark:text-blue-400' },
    { label: 'Applications in progress', value: inProgress.length + (data.counts?.applications_running ?? 0), icon: Briefcase, tone: 'text-violet-600 dark:text-violet-400' },
    { label: 'Applications sent', value: weekly.applications_submitted ?? 0, sub: `${weekly.applications_prev_week ?? 0} last week`, icon: CheckCircle2, tone: 'text-emerald-600 dark:text-emerald-400' },
    { label: 'Interviews', value: interviews.upcoming_count ?? 0, sub: (weekly.interviews ?? 0) > 0 ? `${weekly.interviews} this week` : 'none yet this week', icon: GraduationCap, tone: 'text-amber-600 dark:text-amber-400' },
  ]

  const discoveryTone: Record<string, string> = {
    idle: 'bg-zinc-100 border-zinc-300 text-zinc-600 dark:bg-zinc-800 dark:border-zinc-700 dark:text-zinc-300',
    running: 'bg-blue-50 border-blue-200 text-blue-700 dark:bg-blue-950 dark:border-blue-800 dark:text-blue-300',
    paused: 'bg-amber-50 border-amber-200 text-amber-700 dark:bg-amber-950 dark:border-amber-800 dark:text-amber-300',
    attention: 'bg-red-50 border-red-200 text-red-700 dark:bg-red-950 dark:border-red-800 dark:text-red-300',
    results: 'bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-800 dark:text-emerald-300',
  }

  return (
    <div className="space-y-6" data-testid="user-dashboard">
      {!online && <OfflineBanner />}

      {/* Header: outcomes first, in the user's language. */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Your job search at a glance</h1>
          <p className="text-sm text-zinc-500 dark:text-zinc-400 mt-0.5" data-testid="weekly-headline">
            {weekly.headline || 'Here is where things stand today.'}
          </p>
        </div>
        <Link to="/jobs" className="inline-flex items-center gap-2 px-4 py-2 rounded-full bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium">
          Find new matches <ArrowRight className="w-4 h-4" />
        </Link>
      </div>

      {/* Outcome stats — applications and interviews, never tokens. */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {statCards.map(card => (
          <div key={card.label} className="card p-4 flex items-center gap-3" data-testid="outcome-stat">
            <div className={`w-9 h-9 rounded-xl bg-zinc-100 dark:bg-zinc-800 flex items-center justify-center ${card.tone}`}>
              <card.icon className="w-4.5 h-4.5" />
            </div>
            <div className="min-w-0">
              <div className="text-xl font-semibold mono leading-none">{card.value}</div>
              <div className="text-xs text-zinc-500 dark:text-zinc-400 mt-1 truncate">{card.label}{card.sub ? ` • ${card.sub}` : ''}</div>
            </div>
          </div>
        ))}
      </div>

      {/* Discovery status — where the search stands. */}
      <SectionCard title="Discovery status" icon={Compass}>
        <div className="flex flex-wrap items-center gap-3">
          <span className={`text-[11px] px-2 py-1 rounded-full border mono ${discoveryTone[discovery.state] || discoveryTone.idle}`} data-testid="discovery-state">
            {discovery.state === 'running' ? 'Searching now'
              : discovery.state === 'paused' ? 'Paused — resumes automatically'
              : discovery.state === 'attention' ? 'Needs a retry'
              : discovery.state === 'results' ? 'Fresh matches ready'
              : 'Not started yet'}
          </span>
          <span className="text-sm text-zinc-600 dark:text-zinc-300">{discovery.detail}</span>
        </div>
        <div className="mt-3 flex flex-wrap gap-4 text-xs mono text-zinc-500 dark:text-zinc-400">
          <span>{discovery.total_discovered ?? 0} roles discovered</span>
          <span>{discovery.new_matches_7d ?? 0} new this week</span>
          <span>{discovery.strong_matches ?? 0} strong matches</span>
          {discovery.last_run_at && <span>last search {when(discovery.last_run_at)}</span>}
        </div>
      </SectionCard>

      <div className="grid lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 space-y-4">
          {/* Needs your attention — the actionable queue. */}
          <SectionCard title="Needs your attention" icon={AlertTriangle}
                       aside={<span className="text-xs mono text-zinc-500" data-testid="action-count">{actionQueue.length} item{actionQueue.length === 1 ? '' : 's'}</span>}>
            {actionQueue.length === 0 ? (
              <EmptyState title="You're all caught up"
                          hint="Nothing is waiting on you. New questions or approvals will appear here the moment they arrive." />
            ) : (
              <ul className="space-y-2" data-testid="action-queue">
                {actionQueue.map(item => (
                  <li key={`${item.kind}-${item.id}`}>
                    <Link to={item.route || '/'}
                          className="flex items-center gap-3 p-3 rounded-xl border dark:border-zinc-800 hover:bg-zinc-50 dark:hover:bg-zinc-800/50 transition">
                      <ClipboardList className="w-4 h-4 text-amber-600 shrink-0" />
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-medium truncate">{item.title}</div>
                        {item.subtitle && <div className="text-xs text-zinc-500 truncate">{item.subtitle} • {item.detail}</div>}
                      </div>
                      <ArrowRight className="w-4 h-4 text-zinc-400 shrink-0" />
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </SectionCard>

          {/* Top matches — each with its why. */}
          <SectionCard title="Top matches for you" icon={Target}
                       aside={<Link to="/jobs" className="text-xs text-blue-600 dark:text-blue-400">See all jobs →</Link>}>
            {topMatches.length === 0 ? (
              <EmptyState
                title="No matches yet"
                hint={discovery.never_discovered
                  ? 'Run your first discovery and we will rank every posting against your profile — and explain why each one fits.'
                  : 'Nothing scored highly yet. Add more profile detail or run discovery again to widen the search.'}
                action={<Link to="/jobs" className="inline-flex items-center gap-2 px-4 py-2 rounded-full bg-zinc-900 dark:bg-white text-white dark:text-zinc-900 text-sm">Run discovery</Link>}
              />
            ) : (
              <ul className="space-y-3" data-testid="top-matches">
                {topMatches.map(match => (
                  <li key={match.job_id} className="p-4 rounded-xl border dark:border-zinc-800">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="text-sm font-medium truncate">{match.title}</div>
                        <div className="text-xs text-zinc-500 truncate">{match.company}{match.location ? ` • ${match.location}` : ''}</div>
                      </div>
                      <ScoreChip score={match.score} band={match.band} />
                    </div>
                    {match.why?.length > 0 && (
                      <ul className="mt-2 space-y-1" data-testid="match-why">
                        {match.why.map((reason: string, i: number) => (
                          <li key={i} className="text-xs text-zinc-600 dark:text-zinc-300 flex gap-1.5">
                            <span className="text-emerald-600 dark:text-emerald-400 shrink-0">✓</span>
                            <span>{reason}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                    {match.missing_skills?.length > 0 && (
                      <div className="mt-2 text-xs text-zinc-500 dark:text-zinc-400">
                        Gaps you could address: <span className="mono">{match.missing_skills.join(', ')}</span>
                      </div>
                    )}
                    <div className="mt-3 flex items-center gap-2">
                      <Link to={match.route || '/jobs'} className="text-xs px-3 py-1.5 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900">Review this match</Link>
                      {match.discovered_at && <span className="text-[11px] mono text-zinc-400">found {when(match.discovered_at)}</span>}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </SectionCard>

          {/* Applications in progress. */}
          <SectionCard title="Applications in progress" icon={FileCheck2}
                       aside={<Link to="/tracking" className="text-xs text-blue-600 dark:text-blue-400">Open tracking board →</Link>}>
            {inProgress.length === 0 ? (
              <EmptyState title="Nothing in the pipeline right now"
                          hint="Applications you prepare — or approve — move through here stage by stage, until they're submitted." />
            ) : (
              <ul className="space-y-2" data-testid="applications-in-progress">
                {inProgress.map(app => (
                  <li key={`${app.job_id}-${app.stage}`}>
                    <Link to={app.route || '/tracking'}
                          className="flex items-center gap-3 p-3 rounded-xl border dark:border-zinc-800 hover:bg-zinc-50 dark:hover:bg-zinc-800/50 transition">
                      <Briefcase className="w-4 h-4 text-zinc-400 shrink-0" />
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-medium truncate">{app.title}</div>
                        <div className="text-xs text-zinc-500 truncate">{app.company}</div>
                      </div>
                      <div className="text-right shrink-0">
                        <span className="text-[11px] px-2 py-1 rounded-full bg-zinc-100 dark:bg-zinc-800 mono">{app.stage_label}</span>
                        {app.updated_at && <div className="text-[10px] text-zinc-400 mt-1">{when(app.updated_at)}</div>}
                      </div>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </SectionCard>
        </div>

        {/* Right rail: profile, interviews, follow-ups, weekly report. */}
        <div className="space-y-4">
          <SectionCard title="Profile completeness" icon={UserRound}>
            <div className="flex items-center gap-3">
              <div className="flex-1 h-2 rounded-full bg-zinc-100 dark:bg-zinc-800 overflow-hidden">
                <div className="h-full bg-gradient-to-r from-blue-600 to-violet-600 rounded-full transition-all"
                     style={{ width: `${Math.min(100, Math.max(0, Number(profile.percent ?? 0)))}%` }} data-testid="profile-completeness-bar" />
              </div>
              <span className="text-sm mono font-semibold" data-testid="profile-completeness">{Math.min(100, Math.max(0, Number(profile.percent ?? 0)))}%</span>
            </div>
            <p className="mt-2 text-xs text-zinc-500 dark:text-zinc-400">{profile.next_step}</p>
            {profile.missing?.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {profile.missing.slice(0, 4).map((m: any) => (
                  <span key={m.field} className="text-[11px] px-2 py-0.5 rounded-full bg-amber-50 border border-amber-200 text-amber-700 dark:bg-amber-950 dark:border-amber-800 dark:text-amber-300">
                    {m.label}
                  </span>
                ))}
              </div>
            )}
            <Link to="/profile-review" className="mt-3 inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-full border dark:border-zinc-700">
              Complete your profile <ArrowRight className="w-3 h-3" />
            </Link>
          </SectionCard>

          <SectionCard title="Interview activity" icon={GraduationCap}
                       aside={<Link to="/interview" className="text-xs text-blue-600 dark:text-blue-400">Practice →</Link>}>
            {interviews.upcoming?.length === 0 && interviews.recent?.length === 0 && (interviews.practice_sessions?.length ?? 0) === 0 ? (
              <EmptyState title="No interviews yet"
                          hint="When you record an interview on the tracking board — or start a practice session — it shows up here." />
            ) : (
              <div className="space-y-3">
                {interviews.upcoming?.map((it: any, i: number) => (
                  <Link key={i} to={it.route || '/tracking'} className="block p-3 rounded-xl border border-blue-200 bg-blue-50 dark:bg-blue-950/40 dark:border-blue-900">
                    <div className="text-sm font-medium truncate">{it.company}</div>
                    <div className="text-xs text-zinc-600 dark:text-zinc-300 truncate">{it.title}</div>
                    {it.when && <div className="text-xs mono text-blue-700 dark:text-blue-300 mt-1">scheduled {when(it.when)}</div>}
                  </Link>
                ))}
                {interviews.recent?.slice(0, 2).map((it: any, i: number) => (
                  <div key={`r-${i}`} className="p-3 rounded-xl border dark:border-zinc-800">
                    <div className="text-sm font-medium truncate">{it.company}</div>
                    <div className="text-xs text-zinc-500">interview completed {when(it.completed_at)}</div>
                  </div>
                ))}
                {(interviews.practice_sessions?.length ?? 0) > 0 && (
                  <div className="text-xs mono text-zinc-500">
                    {interviews.practice_completed ?? 0} practice session{(interviews.practice_completed ?? 0) === 1 ? '' : 's'} completed
                  </div>
                )}
              </div>
            )}
          </SectionCard>

          <SectionCard title="Follow-up reminders" icon={CalendarClock}
                       aside={<Link to="/tracking" className="text-xs text-blue-600 dark:text-blue-400">Set a reminder →</Link>}>
            {followUps.length === 0 ? (
              <EmptyState title="No follow-ups due"
                          hint="We will nudge you here when an application has been quiet for a week — or you can set your own reminders on the tracking board." />
            ) : (
              <ul className="space-y-2" data-testid="follow-ups">
                {followUps.slice(0, 5).map((item, i) => (
                  <li key={`${item.tracking_id}-${i}`}>
                    <Link to={item.route || '/tracking'} className={`block p-3 rounded-xl border ${item.overdue ? 'border-amber-300 bg-amber-50 dark:bg-amber-950/40 dark:border-amber-800' : 'dark:border-zinc-800'}`}>
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-sm font-medium truncate">{item.company}</span>
                        <span className={`text-[11px] mono shrink-0 ${item.overdue ? 'text-amber-700 dark:text-amber-300' : 'text-zinc-500'}`}>
                          {item.overdue ? 'due now' : item.due_at ? `due ${when(item.due_at)}` : 'suggested'}
                        </span>
                      </div>
                      <div className="text-xs text-zinc-500 truncate">{item.title}</div>
                      {item.suggestion && <div className="text-xs text-zinc-600 dark:text-zinc-300 mt-1">{item.suggestion}</div>}
                      {item.note && <div className="text-xs text-zinc-500 mt-1 italic truncate">“{item.note}”</div>}
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </SectionCard>

          <SectionCard title="This week's outcomes" icon={Mail}
                       aside={<span className="text-xs mono text-zinc-400">last 7 days</span>}>
            <dl className="grid grid-cols-2 gap-2 text-sm" data-testid="weekly-report">
              {[
                ['Applications sent', weekly.applications_submitted ?? 0],
                ['Responses received', weekly.responses ?? 0],
                ['Interviews', weekly.interviews ?? 0],
                ['Rejections', weekly.rejections ?? 0],
                ['New matches', weekly.new_matches ?? 0],
                ['Outreach replies', weekly.outreach_replies ?? 0],
              ].map(([label, value]) => (
                <div key={String(label)} className="p-2.5 rounded-xl bg-zinc-50 dark:bg-zinc-800/60">
                  <dt className="text-[11px] text-zinc-500 dark:text-zinc-400">{label}</dt>
                  <dd className="text-lg font-semibold mono">{value}</dd>
                </div>
              ))}
            </dl>
            {weekly.top_match && (
              <p className="mt-2 text-xs text-zinc-500 dark:text-zinc-400">
                Strongest open match: <span className="font-medium text-zinc-700 dark:text-zinc-200">{weekly.top_match.title}</span> at {weekly.top_match.company} ({weekly.top_match.score}% match)
              </p>
            )}
          </SectionCard>

          {/* Assistant status — honest, user-language, no provider detail. */}
          <SectionCard title="Your assistant" icon={Sparkles}>
            <p className="text-sm text-zinc-600 dark:text-zinc-300" data-testid="assistant-message">{assistant.message}</p>
            {(assistant.paused_items ?? 0) > 0 && (
              <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
                {assistant.paused_items} piece{assistant.paused_items === 1 ? '' : 's'} of your work will resume automatically.
              </p>
            )}
          </SectionCard>
        </div>
      </div>
    </div>
  )
}
