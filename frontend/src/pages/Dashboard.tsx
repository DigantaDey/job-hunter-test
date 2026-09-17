import { useEffect, useRef, useState } from 'react'
import client, { apiError } from '../api/client'
import { Briefcase, CheckCircle, AlertTriangle, Clock, Mail, FileText, Vault, Sparkles, TrendingUp, ArrowRight, Activity, Search, ListChecks, Zap, BarChart3, Target, Brain, DollarSign, Bell, RefreshCw } from 'lucide-react'
import { Link } from 'react-router-dom'
import { fmtRelativeToNow } from '../lib/automation'
import { creditLabel, usagePercent } from '../lib/credits'
import { useAIWork } from '../context/AIWorkContext'
import { StateIcon } from '../components/AIWorkChip'
import { describeRow, deriveState, toneClasses } from '../lib/aiWork'

/**
 * The dashboard's five independent reads (P1 #4 error recovery).
 *
 * All five load in parallel on mount and each settles on its own:
 *   • success → apply the data and clear the section's error;
 *   • failure → record a human message (`apiError`) and keep what did load.
 * The page renders everything that succeeded and names what failed in a
 * banner whose retry re-fetches exactly the failed sections; only when every
 * read fails does the page swap to a full error card with a Retry button.
 * No error path can spin forever: the loading screen is a `loading` state
 * that closes on the summary's first verdict — success or failure — and every
 * request is bounded by the client's 30 s timeout.
 */
type Section = 'summary' | 'jobs' | 'performance' | 'profile' | 'billing'

const SECTION_LABEL: Record<Section, string> = {
  summary: 'Dashboard summary',
  jobs: 'Recent jobs',
  performance: 'Application analytics',
  profile: 'Profile',
  billing: 'Billing',
}

const SECTIONS: { key: Section; label: string; url: string }[] = [
  { key: 'summary', label: SECTION_LABEL.summary, url: '/api/dashboard/summary' },
  { key: 'jobs', label: SECTION_LABEL.jobs, url: '/api/jobs?limit=6' },
  { key: 'performance', label: SECTION_LABEL.performance, url: '/api/analytics/performance' },
  { key: 'profile', label: SECTION_LABEL.profile, url: '/api/profile/current' },
  { key: 'billing', label: SECTION_LABEL.billing, url: '/api/billing/subscription' },
]
const ALL_SECTIONS = SECTIONS.map(s => s.key)

/** How often the summary re-reads while the tab is visible (visibility-aware; stops when hidden). */
const SUMMARY_POLL_MS = 60_000

/** Zero state applied when the summary read fails: the page renders 0s under the error banner instead of crashing on a missing field. */
const EMPTY_SUMMARY = { jobs: {}, emails: {}, resumes: { total: 0, pending_approval: 0 }, vault: 0, user_input: 0 }

export default function Dashboard() {
  const [summary, setSummary] = useState<any>(null)
  const [jobs, setJobs] = useState<any[]>([])
  const [performance, setPerformance] = useState<any>(null)
  const [profile, setProfile] = useState<any>(null)
  const [billing, setBilling] = useState<any>(null)
  // True until the summary's *first* verdict (success OR failure). The loading
  // screen is this flag and nothing else: it is set once, closed once, and a
  // later retry can never re-enter it.
  const [loading, setLoading] = useState(true)
  // Failed reads, keyed by section, each with a human message — empty means everything loaded.
  const [errors, setErrors] = useState<Partial<Record<Section, string>>>({})
  // Sections whose request is in flight right now (initial load or a retry).
  const [pending, setPending] = useState<Section[]>([])
  // True once the summary has made its first verdict (success OR failure):
  // only then does the polling effect below know to arm. A failure arms the
  // poll too so the stats recover without a manual reload.
  const [summaryReady, setSummaryReady] = useState(false)
  const mounted = useRef(true)
  // Coalesce in-flight poll ticks so a slow response cannot stack requests.
  const summaryInFlight = useRef(false)
  // Live layer (v2.2.8): the queue counters below come from the polled
  // snapshot when it is available, so the dashboard's "Running" and "Open
  // queues" numbers move while work is in flight instead of freezing at the
  // value fetched on mount.
  const work = useAIWork()

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const applySection = (key: Section, data: any) => {
    switch (key) {
      case 'summary': setSummary(data); setSummaryReady(true); break
      case 'jobs': setJobs((data || []).slice(0, 6)); break
      case 'performance': setPerformance(data); break
      case 'profile': setProfile(data); break
      case 'billing': setBilling(data); break
    }
  }

  /**
   * Remove `key` from the in-flight set. On the summary's *first* verdict it
   * also closes the loading screen AND arms polling — success and failure
   * both count as a verdict, which is what guarantees no error path can sit
   * on the spinner and a failing first read still gets retried by the poll.
   * (A re-fetch for a retry does not touch `loading`: it is already closed,
   * and the retry shows "Retrying…" on its button instead.)
   */
  const settle = (key: Section) => {
    setPending(p => p.filter(k => k !== key))
    if (key === 'summary') {
      setLoading(false)
      setSummaryReady(true)
    }
  }

  /**
   * Fetch one or more sections (all five on first load). Each request settles
   * independently, so a slow or failed read can neither hold the page hostage
   * nor hide the others: a success clears that section's error, a failure
   * records it. The summary's failure also applies `EMPTY_SUMMARY`, which is
   * what keeps every card below null-safe while the banner explains why.
   */
  const load = (keys?: Section[]) => {
    const targets = (keys && keys.length > 0 ? keys : ALL_SECTIONS).filter(k => !pending.includes(k))
    if (targets.length === 0) return
    setPending(p => [...p, ...targets])
    for (const section of SECTIONS) {
      if (!targets.includes(section.key)) continue
      client.get(section.url)
        .then(r => {
          if (!mounted.current) return
          applySection(section.key, r.data)
          setErrors(prev => { if (!(section.key in prev)) return prev; const next = { ...prev }; delete next[section.key]; return next })
          settle(section.key)
        })
        .catch(e => {
          if (!mounted.current) return
          if (section.key === 'summary') setSummary(EMPTY_SUMMARY)
          setErrors(prev => ({ ...prev, [section.key]: apiError(e) }))
          settle(section.key)
        })
    }
  }

  useEffect(() => { load() }, [])

  /**
   * Live summary: the mount fetch is one-shot, so the "Running / Completed /
   * Failed" counters and the quota lines would go stale. Re-read the summary
   * every 60 s while the tab is *visible* — visibility-aware (same pattern as
   * Funding/Emails/Queues): a hidden tab polls nothing, becoming visible
   * re-reads immediately instead of waiting out the interval, and a failed
   * tick keeps the last read (counters are a refresh, never a reset). In-flight
   * ticks are coalesced so a slow network cannot stack duplicate requests.
   */
  useEffect(() => {
    if (!summaryReady) return
    let timer: ReturnType<typeof setInterval> | null = null
    const tick = async () => {
      if (summaryInFlight.current) return
      summaryInFlight.current = true
      try {
        const r = await client.get('/api/dashboard/summary')
        if (mounted.current) {
          setSummary(r.data)
          setErrors(prev => { if (!('summary' in prev)) return prev; const next = { ...prev }; delete next.summary; return next })
        }
      } catch {
        // Keep the last read: a late tick must not blank a working page.
      } finally {
        summaryInFlight.current = false
      }
    }
    const start = (readNow = false) => {
      if (readNow) void tick()
      if (timer) return
      timer = setInterval(() => void tick(), SUMMARY_POLL_MS)
    }
    const stop = () => { if (timer) clearInterval(timer); timer = null }
    const onVisibility = () => {
      if (typeof document === 'undefined') return
      if (document.visibilityState === 'hidden') stop()
      else start(true)
    }
    // The summary's first read just settled — arm the interval without a
    // duplicate read, and arm nothing at all behind a hidden tab.
    if (typeof document === 'undefined' || document.visibilityState !== 'hidden') start(false)
    document.addEventListener('visibilitychange', onVisibility)
    return () => { stop(); document.removeEventListener('visibilitychange', onVisibility) }
  }, [summaryReady])

  const failedSections = ALL_SECTIONS.filter(k => k in errors)
  const allFailed = failedSections.length === ALL_SECTIONS.length
  const retrying = (keys: Section[]) => keys.some(k => pending.includes(k))

  // The loading screen is one state owned by one flag: `loading` is true only
  // until the summary's first verdict, and both outcomes close it — success
  // stores the data, failure stores `EMPTY_SUMMARY` and falls through to the
  // error card or the page-with-banner below. Every request is bounded by the
  // client's 30 s timeout, so no error path can ever sit on this screen, and
  // because the flag only ever closes, a retry can never send the user back
  // here (it shows "Retrying…" on its own button instead).
  if (loading)
    return <div className="p-8 mono text-sm">Loading dashboard…</div>

  if (allFailed) {
    const busy = retrying(ALL_SECTIONS)
    return (
      <div className="p-8">
        <div role="alert" className="card max-w-lg mx-auto p-8 text-center">
          <AlertTriangle className="w-10 h-10 mx-auto text-red-500" />
          <h2 className="mt-4 text-lg font-semibold">Couldn't load the dashboard</h2>
          <p className="mt-2 text-sm text-zinc-500 dark:text-zinc-400">{errors.summary || 'Something went wrong.'} Every section failed to load.</p>
          <button
            onClick={() => load()}
            disabled={busy}
            className="mt-6 inline-flex items-center gap-2 px-5 py-2.5 rounded-full bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium disabled:opacity-60 focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <RefreshCw className={`w-4 h-4 ${busy ? 'animate-spin' : ''}`} />
            {busy ? 'Retrying…' : 'Retry'}
          </button>
        </div>
      </div>
    )
  }

  const ent = summary.entitlements
  const plan = ent?.plan || 'free'
  const aiUsage = summary.ai_usage || {}
  // The AI-credit ceiling, resolved once so every readout on this page agrees.
  // `0` = UNLIMITED (Pro+): `creditLabel` says so in words and `usagePercent`
  // returns null, so no meter is drawn against a fake 0 (or invented 50000).
  const aiCreditLimit = aiUsage.limit ?? ent?.limits?.ai_credits_per_month
  const aiCreditPercent = usagePercent(aiUsage.credits_used, aiCreditLimit)
  const apps = summary.applications || {}
  const outreach = summary.outreach || {}
  const automation = summary.automation || {}
  // The hero trio is labelled "Pipelines" but `automation.running/completed/
  // failed` are computed from the *application* pipeline only. The same payload
  // carries `automation.queues` (= GET /api/pipelines/stats buckets for every
  // pipeline, incl. paused/dead), so total it here instead of relabelling.
  const buckets: Record<string, Record<string, number>> = automation.queues || {}
  const sumStatus = (...keys: string[]) => Object.values(buckets).reduce((n, b) => n + keys.reduce((m, k) => m + (b?.[k] ?? 0), 0), 0)
  const hasBuckets = Object.keys(buckets).length > 0
  const live = work.snapshot
  const pipeRunning = live ? live.counts.queued + live.counts.processing : hasBuckets ? sumStatus('queued', 'processing') : (automation.running ?? 0)
  const pipeCompleted = hasBuckets ? sumStatus('done') : (automation.completed ?? 0)
  const pipeFailed = hasBuckets ? sumStatus('failed', 'dead') : (automation.failed ?? 0)
  const pipePaused = live ? live.counts.paused : hasBuckets ? sumStatus('paused') : 0
  const needsInputNow = live ? live.counts.needs_input : (automation.needs_input ?? 0)
  const inFlight = live ? live.items.slice(0, 5) : []
  const autoNextIso: string | null = automation.next_run || null
  const autoNextMs = autoNextIso ? new Date(autoNextIso).getTime() : NaN
  // `next_run: null` is "no next run to promise": the switch is on but blocked
  // (`auto_mode_active` false — plan, consent, scheduler) or every workflow's run
  // is in flight right now. Neither is rendered as a date.
  const autoModeLine = !automation.auto_mode
    ? 'off'
    : !autoNextIso || Number.isNaN(autoNextMs)
      ? (automation.auto_mode_active ? 'running now' : 'on — blocked')
      : autoNextMs <= Date.now()
        ? 'due next sweep'
        : fmtRelativeToNow(autoNextIso)
  const autoNextTitle = autoNextIso && !Number.isNaN(autoNextMs)
    ? `Next scheduled auto run no earlier than ${new Date(autoNextMs).toLocaleString([], { hour12: false })} — the sweep that starts it runs every few minutes, so it can begin a little later.`
    : automation.auto_mode && !automation.auto_mode_active
      ? 'Auto mode is on but not queueing right now (plan, consent or the scheduler) — the reason is in Settings → Auto mode'
      : 'Auto-mode schedule, cadence and history: Settings → Auto mode'

  const statCards = [
    {label:'Discovered', value: summary.jobs?.total ?? 0, sub: `${summary.jobs?.last_24h ?? 0} last 24h`, icon: Search, color:'text-blue-600', bg:'bg-blue-50 dark:bg-blue-950'},
    {label:'Applied', value: apps.submitted ?? summary.jobs?.applied ?? 0, sub: `${apps.response_rate ?? 0}% response`, icon: CheckCircle, color:'text-emerald-600', bg:'bg-emerald-50 dark:bg-emerald-950'},
    {label:'Needs Input', value: summary.jobs?.needs_input ?? 0, sub: 'requires action', icon: AlertTriangle, color:'text-amber-600', bg:'bg-amber-50 dark:bg-amber-950'},
    {label:'High Priority', value: summary.jobs?.high_match ?? 0, sub: 'score ≥75', icon: Target, color:'text-violet-600', bg:'bg-violet-50 dark:bg-violet-950'},
  ]

  return (
    <div className="space-y-6">
      {/* Partial failure (P1 #4): everything that loaded renders below; the
          reads that did not are named here, each with its own message and a
          retry that re-fetches exactly those sections — never the whole page. */}
      {failedSections.length > 0 && (
        <div role="alert" className="card p-4 border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/50">
          <div className="flex items-center gap-2 text-sm font-medium text-amber-900 dark:text-amber-100">
            <AlertTriangle className="w-4 h-4 shrink-0" />
            {failedSections.length === 1
              ? `${SECTION_LABEL[failedSections[0]]} failed to load`
              : `${failedSections.length} parts of the dashboard failed to load`}
          </div>
          <ul className="mt-2 space-y-0.5 text-xs mono text-amber-800 dark:text-amber-200">
            {failedSections.map(k => (
              <li key={k}><span className="font-medium">{SECTION_LABEL[k]}:</span> {errors[k]}</li>
            ))}
          </ul>
          <button
            onClick={() => load(failedSections)}
            disabled={retrying(failedSections)}
            className="mt-3 text-xs font-medium text-amber-900 dark:text-amber-100 underline decoration-dotted underline-offset-2 disabled:opacity-60"
          >
            {retrying(failedSections) ? 'Retrying…' : `Retry ${failedSections.length > 1 ? 'these sections' : 'this section'} →`}
          </button>
        </div>
      )}

      {/* hero — command center positioning */}
      <div className="card p-6 bg-gradient-to-br from-zinc-900 to-zinc-800 dark:from-zinc-900 dark:to-black text-white border-zinc-800 overflow-hidden relative">
        <div className="absolute -right-20 -top-20 w-72 h-72 bg-gradient-to-br from-blue-600/20 to-violet-600/20 rounded-full blur-3xl" />
        <div className="relative">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <div className="inline-flex items-center gap-2 text-xs mono bg-white/10 backdrop-blur rounded-full px-3 py-1 border border-white/10"><Sparkles className="w-3.5 h-3.5"/> Your complete AI-powered job hunting command center</div>
              <h1 className="text-2xl lg:text-3xl font-semibold tracking-tight mt-3">Welcome back{profile?.data?.name ? `, ${profile.data.name.split(' ')[0]}` : ''}.</h1>
              <p className="text-zinc-300 mt-2 max-w-2xl text-sm leading-relaxed">Discover → Analyze → Prepare → Apply → Automate → Reach Out → Track → Improve. Your system scans, scores with transparent intelligence, tailors resumes with fact-guard, auto-creates vault credentials, and applies — while you approve.</p>
              <div className="mt-4 flex flex-wrap gap-2">
                <Link to="/jobs" className="px-4 py-2 bg-white text-zinc-900 rounded-full text-sm font-medium inline-flex items-center gap-2">Explore jobs <ArrowRight className="w-4 h-4"/></Link>
                <Link to="/resumes" className="px-4 py-2 bg-white/10 backdrop-blur border border-white/20 text-white rounded-full text-sm">Resume Studio</Link>
                <Link to="/analytics" className="px-4 py-2 bg-white/10 backdrop-blur border border-white/20 text-white rounded-full text-sm">Analytics</Link>
              </div>
              <div className="mt-3 text-[11px] mono text-zinc-400">Plan: {ent?.plan_label || plan} • {ent?.capabilities?.can_use_advanced_matching ? 'Advanced matching' : 'Basic matching'} • {billing ? `${aiUsage.credits_used || 0} tokens used` : ''}</div>
            </div>
            <div className="bg-white/10 backdrop-blur rounded-2xl p-4 border border-white/10 min-w-[280px]">
              <div className="text-xs mono text-zinc-300 flex items-center gap-2"><Activity className="w-3.5 h-3.5"/> Pipelines (FIFO) • Entitlements</div>
              <div className="grid grid-cols-3 gap-3 mt-3 text-center">
                <div><div className="text-lg font-semibold mono">{pipeRunning}</div><div className="text-[11px] mono text-zinc-400">Running</div></div>
                <div><div className="text-lg font-semibold mono">{pipeCompleted}</div><div className="text-[11px] mono text-zinc-400">Completed</div></div>
                <div><div className="text-lg font-semibold mono">{pipeFailed}</div><div className="text-[11px] mono text-zinc-400">Failed</div></div>
              </div>
              {pipePaused > 0 && (
                <Link to="/queues" className="mt-2 block text-[11px] mono text-amber-300 text-center" title="Parked by an AI outage — resumes on its own when the provider is back">
                  {pipePaused} paused — AI outage, will resume automatically
                </Link>
              )}
              <div className="mt-3 space-y-2 text-[11px] mono">
                <div className="flex justify-between"><span>AI credits</span><span>{creditLabel(aiUsage.credits_used, aiCreditLimit)}</span></div>
                {aiCreditPercent !== null && (
                  <div className="w-full bg-white/10 rounded-full h-1.5 overflow-hidden"><div className="bg-blue-500 h-1.5 rounded-full" style={{width: `${aiCreditPercent}%`}} /></div>
                )}
                <div className="flex justify-between"><span>Outreach</span><span>{outreach.sent ?? 0}/{ent?.limits?.outreach_per_month ?? 10}</span></div>
                <div className="flex justify-between"><span>Automation</span><span>{ent?.usage?.automation_runs_per_month?.used ?? 0}/{ent?.limits?.automation_runs_per_month ?? 5}</span></div>
                {/* v2.2 — auto mode. Both fields come from the scheduler's own
                    state (ops dashboard `auto_mode`/`next_run`), never recomputed
                    here: "waiting" means switched on but blocked, or a run of that
                    workflow is already in flight. */}
                <div className="flex justify-between">
                  <span>Auto mode</span>
                  <Link to="/settings" className="underline decoration-dotted" title={autoNextTitle}>
                    {autoModeLine}
                  </Link>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {statCards.map(c=> (
          <div key={c.label} className="card p-4 flex items-center gap-3">
            <div className={`w-10 h-10 rounded-xl flex items-center justify-center ${c.bg}`}><c.icon className={`w-5 h-5 ${c.color}`} /></div>
            <div><div className="text-xl font-semibold mono leading-none">{c.value}</div><div className="text-xs text-zinc-500 dark:text-zinc-400">{c.label}</div><div className="text-[11px] mono text-zinc-400">{c.sub}</div></div>
          </div>
        ))}
      </div>

      <div className="grid lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 space-y-4">
          <div className="card p-5">
            <div className="flex items-center justify-between">
              <h3 className="font-medium flex items-center gap-2"><Briefcase className="w-4 h-4"/> Recent discoveries — with intelligence</h3>
              <Link to="/jobs" className="text-xs text-blue-600 dark:text-blue-400">View all →</Link>
            </div>
            <div className="mt-4 space-y-2">
              {jobs.length===0 ? <div className="text-sm text-zinc-500 py-8 text-center mono">No jobs yet. Trigger discovery in Jobs →</div> : jobs.map(j=> (
                <Link key={j.id} to="/jobs" className="flex items-center gap-3 p-3 rounded-xl border dark:border-zinc-800 hover:bg-zinc-50 dark:hover:bg-zinc-800/50 transition">
                  <div className="w-9 h-9 rounded-lg bg-zinc-900 dark:bg-zinc-800 text-white flex items-center justify-center text-xs font-bold">{(j.company || '??').slice(0,2).toUpperCase()}</div>
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium truncate">{j.title || 'Untitled'} <span className="text-zinc-500 font-normal">• {j.company || ''}</span></div>
                    <div className="text-xs text-zinc-500 mono flex gap-2"><span>{j.source || ''}</span><span>•</span><span>{j.company_size || ''}</span><span>•</span><span>score {j.score ?? 0}</span></div>
                  </div>
                  <div className={`text-[11px] px-2 py-1 rounded-full border mono ${j.score>=75?'bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-800 dark:text-emerald-300': j.score>=60?'bg-amber-50 border-amber-200 text-amber-700 dark:bg-amber-950':'bg-zinc-100 dark:bg-zinc-800'}`}>{j.score} • {j.status}</div>
                </Link>
              ))}
            </div>
          </div>

          {/* Performance */}
          <div className="card p-5">
            <h3 className="font-medium flex items-center gap-2"><BarChart3 className="w-4 h-4"/> Application intelligence</h3>
            {performance ? (
              <div className="mt-3 grid grid-cols-3 gap-3 text-center">
                <div className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3"><div className="text-xl font-semibold mono">{performance.summary?.total_jobs ?? 0}</div><div className="text-xs text-zinc-500">Total</div></div>
                <div className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3"><div className="text-xl font-semibold mono">{performance.summary?.applied ?? 0}</div><div className="text-xs text-zinc-500">Applied</div></div>
                <div className="bg-emerald-50 dark:bg-emerald-950 rounded-xl p-3 border border-emerald-200 dark:border-emerald-800"><div className="text-xl font-semibold mono text-emerald-700 dark:text-emerald-300">{performance.summary?.interview_rate ?? 0}%</div><div className="text-xs text-zinc-500">Interview rate</div></div>
              </div>
            ) : <div className="text-xs mono text-zinc-500 mt-2">No analytics yet — apply to jobs to see performance.</div>}
            <div className="mt-3 text-xs mono text-zinc-500">
              {performance?.summary?.best_role && <div>Best performing: {performance.summary.best_role} ({performance.summary.best_role_rate}% interview rate)</div>}
              {performance?.skills?.strongest?.[0] && <div className="mt-1">Strongest skill: {performance.skills.strongest[0].skill} • Weakest: {performance.skills.weakest?.[0]?.skill || 'N/A'}</div>}
            </div>
            <Link to="/analytics" className="mt-3 inline-flex text-xs px-3 py-1.5 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900">View full analytics →</Link>
          </div>
        </div>

        <div className="space-y-4">
          <div className="card p-5">
            <h3 className="font-medium flex items-center gap-2"><ListChecks className="w-4 h-4"/> Command center</h3>
            <div className="mt-3 grid gap-2">
              <Link to="/queues" className="p-3 min-h-[44px] rounded-xl bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm flex items-center justify-between focus:outline-none focus:ring-2 focus:ring-blue-500">Open queues <span className="text-xs mono bg-white/20 dark:bg-zinc-900/10 px-2 py-0.5 rounded-full" data-testid="dashboard-queue-counter">{pipeRunning} running{pipePaused ? ` • ${pipePaused} paused` : ''}{needsInputNow ? ` • ${needsInputNow} need input` : ''}</span></Link>
              {needsInputNow > 0 && (
                <Link to="/queues#user-input" className="p-3 min-h-[44px] rounded-xl border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950 text-amber-900 dark:text-amber-100 text-sm flex items-center justify-between focus:outline-none focus:ring-2 focus:ring-amber-500">User Input Needed <span className="text-xs mono">{needsInputNow} waiting on you →</span></Link>
              )}
              {inFlight.length > 0 && (
                <div className="rounded-xl border dark:border-zinc-800 p-2 space-y-1" data-testid="dashboard-in-flight">
                  <div className="text-[11px] mono uppercase text-zinc-500 px-1">AI work in flight — live</div>
                  {inFlight.map((row)=>{ const st = deriveState(row); return (
                    <div key={row.id} className={`px-2 py-1.5 rounded-lg border text-[11px] mono flex items-start gap-2 ${toneClasses(st?.tone || 'neutral')}`}>
                      <StateIcon state={st} />
                      <div className="min-w-0 flex-1">
                        <div className="flex justify-between gap-2"><span className="font-medium truncate">#{row.id} {describeRow(row)}</span><span className="shrink-0">{st?.label || row.status}</span></div>
                        {st?.detail && <div className="opacity-80 mt-0.5 line-clamp-1">{st.detail}</div>}
                      </div>
                    </div>
                  )})}
                </div>
              )}
              <Link to="/emails" className="p-3 rounded-xl border dark:border-zinc-800 text-sm flex items-center justify-between">Email bucket <Mail className="w-4 h-4 text-zinc-500"/><span className="text-xs mono">{outreach.sent ?? 0} sent • {outreach.pending_approval ?? 0} pending</span></Link>
              <Link to="/vault" className="p-3 rounded-xl border dark:border-zinc-800 text-sm flex items-center justify-between">Vault <Vault className="w-4 h-4 text-zinc-500"/><span className="text-xs mono">{summary.vault ?? 0} creds</span></Link>
              <Link to="/interview" className="p-3 rounded-xl border dark:border-zinc-800 text-sm flex items-center justify-between">Interview Prep <Brain className="w-4 h-4 text-zinc-500"/></Link>
              <Link to="/notifications" className="p-3 rounded-xl border dark:border-zinc-800 text-sm flex items-center justify-between">Notifications <Bell className="w-4 h-4 text-zinc-500"/><span className="text-xs mono">{summary.notifications?.unread ?? 0} unread</span></Link>
            </div>
          </div>

          <div className="card p-5">
            <h3 className="font-medium flex items-center gap-2"><DollarSign className="w-4 h-4"/> Billing & Usage</h3>
            <div className="mt-2 text-xs mono space-y-1">
              <div className="flex justify-between"><span>Plan</span><span className="font-medium">{ent?.plan_label || 'Free'}</span></div>
              <div className="flex justify-between"><span>AI tokens</span><span>{creditLabel(aiUsage.credits_used, aiCreditLimit)}</span></div>
              <div className="flex justify-between"><span>Cost this month</span><span>${aiUsage.cost_usd ?? 0}</span></div>
              <div className="flex justify-between"><span>Automation</span><span>{ent?.usage?.automation_runs_per_month?.used ?? 0}/{ent?.limits?.automation_runs_per_month ?? 5}</span></div>
            </div>
            <Link to="/billing" className="mt-3 inline-flex items-center gap-2 text-sm px-3 py-2 rounded-full bg-blue-600 text-white">Manage billing <ArrowRight className="w-3.5 h-3.5"/></Link>
            <Link to="/pricing" className="ml-2 mt-3 inline-flex text-xs px-3 py-2 rounded-full border dark:border-zinc-700">View pricing</Link>
          </div>

          <div className="card p-5">
            <h3 className="font-medium flex items-center gap-2"><TrendingUp className="w-4 h-4"/> Funding radar</h3>
            <p className="text-xs text-zinc-500 mt-1">Finds Series A-D raises, checks openings, or cold-emails founders.</p>
            <Link to="/funding" className="mt-3 inline-flex items-center gap-2 text-sm px-3 py-2 rounded-full bg-blue-600 text-white">Explore funding <ArrowRight className="w-3.5 h-3.5"/></Link>
          </div>

          <div className="card p-5">
            <h3 className="font-medium text-sm flex items-center gap-2"><FileText className="w-4 h-4"/> Profile & resume</h3>
            <div className="mt-2 text-xs mono text-zinc-500">
              {profile ? <><div>Skills: {profile.data.skills?.slice(0,6).join(', ')}</div><div className="mt-1">{profile.data.email}</div></> : 'No master resume uploaded yet.'}
            </div>
            <div className="mt-3 flex gap-2">
              <div className="text-xs px-2 py-1 rounded-full bg-zinc-100 dark:bg-zinc-800 mono flex items-center gap-1"><FileText className="w-3 h-3"/>{typeof summary.resumes === 'object' ? (summary.resumes.total ?? 0) : (summary.resumes ?? 0)} resumes</div>
              <div className="text-xs px-2 py-1 rounded-full bg-zinc-100 dark:bg-zinc-800 mono flex items-center gap-1"><Vault className="w-3 h-3"/>{summary.vault ?? 0} vault</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
