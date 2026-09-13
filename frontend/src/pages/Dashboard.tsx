import { useEffect, useState } from 'react'
import client from '../api/client'
import { Briefcase, CheckCircle, AlertTriangle, Clock, Mail, FileText, Vault, Sparkles, TrendingUp, ArrowRight, Activity, Search, ListChecks, Zap, BarChart3, Target, Brain, DollarSign, Bell } from 'lucide-react'
import { Link } from 'react-router-dom'
import { fmtRelativeToNow } from '../lib/automation'

export default function Dashboard() {
  const [summary, setSummary] = useState<any>(null)
  const [jobs, setJobs] = useState<any[]>([])
  const [performance, setPerformance] = useState<any>(null)
  const [profile, setProfile] = useState<any>(null)
  const [billing, setBilling] = useState<any>(null)

  useEffect(()=>{
    let cancelled = false
    client.get('/api/dashboard/summary')
      .then(r=>{ if (!cancelled) setSummary(r.data) })
      .catch(()=>{ if (!cancelled) setSummary({ jobs: {}, emails: {}, resumes: {total:0,pending_approval:0}, vault: 0, user_input: 0 }) })
    client.get('/api/jobs?limit=6')
      .then(r=>{ if (!cancelled) setJobs((r.data || []).slice(0, 6)) })
      .catch(()=>{ if (!cancelled) setJobs([]) })
    client.get('/api/analytics/performance')
      .then(r=>{ if (!cancelled) setPerformance(r.data) })
      .catch(()=>{ if (!cancelled) setPerformance(null) })
    client.get('/api/profile/current')
      .then(r=>{ if (!cancelled) setProfile(r.data) })
      .catch(()=>{ if (!cancelled) setProfile(null) })
    client.get('/api/billing/subscription')
      .then(r=>{ if (!cancelled) setBilling(r.data) })
      .catch(()=>{ if (!cancelled) setBilling(null) })
    return ()=>{ cancelled = true }
  },[])

  if(!summary) return <div className="p-8 mono text-sm">Loading dashboard…</div>

  const ent = summary.entitlements
  const plan = ent?.plan || 'free'
  const aiUsage = summary.ai_usage || {}
  const apps = summary.applications || {}
  const outreach = summary.outreach || {}
  const automation = summary.automation || {}
  const autoNextIso: string | null = automation.next_run || null
  const autoNextMs = autoNextIso ? new Date(autoNextIso).getTime() : NaN
  const autoModeLine = !automation.auto_mode
    ? 'off'
    : !autoNextIso || Number.isNaN(autoNextMs)
      ? 'waiting'
      : autoNextMs <= Date.now()
        ? 'due next sweep'
        : fmtRelativeToNow(autoNextIso)
  const autoNextTitle = autoNextIso && !Number.isNaN(autoNextMs)
    ? `Next scheduled auto run no earlier than ${new Date(autoNextMs).toLocaleString([], { hour12: false })} — the sweep that starts it runs every few minutes, so it can begin a little later.`
    : 'Auto-mode schedule, cadence and history: Settings → Auto mode'

  const statCards = [
    {label:'Discovered', value: summary.jobs?.total ?? 0, sub: `${summary.jobs?.last_24h ?? 0} last 24h`, icon: Search, color:'text-blue-600', bg:'bg-blue-50 dark:bg-blue-950'},
    {label:'Applied', value: apps.submitted ?? summary.jobs?.applied ?? 0, sub: `${apps.response_rate ?? 0}% response`, icon: CheckCircle, color:'text-emerald-600', bg:'bg-emerald-50 dark:bg-emerald-950'},
    {label:'Needs Input', value: summary.jobs?.needs_input ?? 0, sub: 'requires action', icon: AlertTriangle, color:'text-amber-600', bg:'bg-amber-50 dark:bg-amber-950'},
    {label:'High Priority', value: summary.jobs?.high_match ?? 0, sub: 'score ≥75', icon: Target, color:'text-violet-600', bg:'bg-violet-50 dark:bg-violet-950'},
  ]

  return (
    <div className="space-y-6">
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
                <div><div className="text-lg font-semibold mono">{automation.running ?? 0}</div><div className="text-[11px] mono text-zinc-400">Running</div></div>
                <div><div className="text-lg font-semibold mono">{automation.completed ?? 0}</div><div className="text-[11px] mono text-zinc-400">Completed</div></div>
                <div><div className="text-lg font-semibold mono">{automation.failed ?? 0}</div><div className="text-[11px] mono text-zinc-400">Failed</div></div>
              </div>
              <div className="mt-3 space-y-2 text-[11px] mono">
                <div className="flex justify-between"><span>AI credits</span><span>{aiUsage.credits_used ?? 0}/{aiUsage.limit ?? ent?.limits?.ai_credits_per_month ?? 50000}</span></div>
                <div className="w-full bg-white/10 rounded-full h-1.5 overflow-hidden"><div className="bg-blue-500 h-1.5 rounded-full" style={{width: `${Math.min(100, ((aiUsage.credits_used||0)/(aiUsage.limit||50000))*100)}%`}} /></div>
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
              <Link to="/queues" className="p-3 rounded-xl bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm flex items-center justify-between">Open queues <span className="text-xs mono bg-white/20 px-2 py-0.5 rounded-full">{automation.running ?? 0} running</span></Link>
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
              <div className="flex justify-between"><span>AI tokens</span><span>{aiUsage.credits_used ?? 0}/{aiUsage.limit ?? 50000}</span></div>
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
