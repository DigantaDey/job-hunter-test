import { useEffect, useState } from 'react'
import client from '../api/client'
import { Briefcase, CheckCircle, AlertTriangle, Clock, Mail, FileText, Vault, Sparkles, TrendingUp, ArrowRight, Activity, Search, ListChecks } from 'lucide-react'
import { Link } from 'react-router-dom'

export default function Dashboard() {
  const [summary, setSummary] = useState<any>(null)
  const [jobs, setJobs] = useState<any[]>([])
  const [pipeline, setPipeline] = useState<any>(null)
  const [profile, setProfile] = useState<any>(null)

  useEffect(()=>{
    let cancelled = false
    client.get('/api/dashboard/summary')
      .then(r=>{ if (!cancelled) setSummary(r.data) })
      .catch(()=>{ if (!cancelled) setSummary({ jobs: {}, emails: {}, resumes: {total:0,pending_approval:0}, vault: 0, user_input: 0 }) })
    client.get('/api/jobs')
      .then(r=>{ if (!cancelled) setJobs((r.data || []).slice(0, 5)) })
      .catch(()=>{ if (!cancelled) setJobs([]) })
    client.get('/api/pipelines/stats')
      .then(r=>{ if (!cancelled) setPipeline(r.data) })
      .catch(()=>{ if (!cancelled) setPipeline(null) })
    client.get('/api/profile/current')
      .then(r=>{ if (!cancelled) setProfile(r.data) })
      .catch(()=>{ if (!cancelled) setProfile(null) })
    return ()=>{ cancelled = true }
  },[])

  if(!summary) return <div className="p-8 mono text-sm">Loading dashboard…</div>

  const statCards = [
    {label:'Discovered', value: summary.jobs?.discovered ?? 0, icon: Search, color:'text-blue-600', bg:'bg-blue-50 dark:bg-blue-950'},
    {label:'Applied', value: summary.jobs?.applied ?? 0, icon: CheckCircle, color:'text-emerald-600', bg:'bg-emerald-50 dark:bg-emerald-950'},
    {label:'Needs Input', value: summary.jobs?.needs_input ?? 0, icon: AlertTriangle, color:'text-amber-600', bg:'bg-amber-50 dark:bg-amber-950'},
    {label:'Failed', value: summary.jobs?.failed ?? 0, icon: Clock, color:'text-red-600', bg:'bg-red-50 dark:bg-red-950'},
  ]

  return (
    <div className="space-y-6">
      {/* hero */}
      <div className="card p-6 bg-gradient-to-br from-zinc-900 to-zinc-800 dark:from-zinc-900 dark:to-black text-white border-zinc-800 overflow-hidden relative">
        <div className="absolute -right-20 -top-20 w-72 h-72 bg-gradient-to-br from-blue-600/20 to-violet-600/20 rounded-full blur-3xl" />
        <div className="relative">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <div className="inline-flex items-center gap-2 text-xs mono bg-white/10 backdrop-blur rounded-full px-3 py-1 border border-white/10"><Sparkles className="w-3.5 h-3.5"/> Autonomous job hunting • 3 FIFO pipelines in parallel</div>
              <h1 className="text-2xl lg:text-3xl font-semibold tracking-tight mt-3">Welcome back{profile?.data?.name ? `, ${profile.data.name.split(' ')[0]}` : ''}.</h1>
              <p className="text-zinc-300 mt-2 max-w-2xl text-sm leading-relaxed">Your system scans, scores, tailors resumes with JD fact-guard, auto-creates vault credentials, and applies — while you approve. Built for accuracy, because it’s your career.</p>
              <div className="mt-4 flex flex-wrap gap-2">
                <Link to="/jobs" className="px-4 py-2 bg-white text-zinc-900 rounded-full text-sm font-medium inline-flex items-center gap-2">Explore jobs <ArrowRight className="w-4 h-4"/></Link>
                <Link to="/resumes" className="px-4 py-2 bg-white/10 backdrop-blur border border-white/20 text-white rounded-full text-sm">Resume Studio</Link>
              </div>
            </div>
            <div className="bg-white/10 backdrop-blur rounded-2xl p-4 border border-white/10 min-w-[240px]">
              <div className="text-xs mono text-zinc-300 flex items-center gap-2"><Activity className="w-3.5 h-3.5"/> Pipelines (FIFO)</div>
              <div className="grid grid-cols-3 gap-3 mt-3 text-center">
                <div><div className="text-lg font-semibold mono">{pipeline?.discovery?.queued ?? 0}</div><div className="text-[11px] mono text-zinc-400">Discovery</div></div>
                <div><div className="text-lg font-semibold mono">{pipeline?.application?.queued ?? 0}</div><div className="text-[11px] mono text-zinc-400">Application</div></div>
                <div><div className="text-lg font-semibold mono">{pipeline?.ai?.ai_queue?.queued ?? 0}</div><div className="text-[11px] mono text-zinc-400">AI API</div></div>
              </div>
              <div className="mt-3 text-[11px] mono text-zinc-400">All three run in parallel • rate-limited • never hits RPM</div>
            </div>
          </div>
        </div>
      </div>

      {/* stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {statCards.map(c=> (
          <div key={c.label} className="card p-4 flex items-center gap-3">
            <div className={`w-10 h-10 rounded-xl flex items-center justify-center ${c.bg}`}><c.icon className={`w-5 h-5 ${c.color}`} /></div>
            <div><div className="text-xl font-semibold mono leading-none">{c.value}</div><div className="text-xs text-zinc-500 dark:text-zinc-400">{c.label}</div></div>
          </div>
        ))}
      </div>

      <div className="grid lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 card p-5">
          <div className="flex items-center justify-between">
            <h3 className="font-medium flex items-center gap-2"><Briefcase className="w-4 h-4"/> Recent discoveries</h3>
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
                <div className={`text-[11px] px-2 py-1 rounded-full border mono ${j.status==='applied'?'bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-800 dark:text-emerald-300': j.status==='needs_input'?'bg-amber-50 border-amber-200 text-amber-700 dark:bg-amber-950': 'bg-zinc-50 dark:bg-zinc-800'}`}>{j.status || ''}</div>
              </Link>
            ))}
          </div>
        </div>
        <div className="space-y-4">
          <div className="card p-5">
            <h3 className="font-medium flex items-center gap-2"><ListChecks className="w-4 h-4"/> Quick actions</h3>
            <div className="mt-3 grid gap-2">
              <Link to="/queues" className="p-3 rounded-xl bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm flex items-center justify-between">Open queues <ArrowRight className="w-4 h-4"/></Link>
              <Link to="/emails" className="p-3 rounded-xl border dark:border-zinc-800 text-sm flex items-center justify-between">Email bucket <Mail className="w-4 h-4 text-zinc-500"/></Link>
              <Link to="/vault" className="p-3 rounded-xl border dark:border-zinc-800 text-sm flex items-center justify-between">Vault <Vault className="w-4 h-4 text-zinc-500"/></Link>
            </div>
          </div>
          <div className="card p-5">
            <h3 className="font-medium flex items-center gap-2"><TrendingUp className="w-4 h-4"/> Funding radar</h3>
            <p className="text-xs text-zinc-500 mt-1">Finds Series A-D raises, checks openings, or cold-emails founders.</p>
            <Link to="/funding" className="mt-3 inline-flex items-center gap-2 text-sm px-3 py-2 rounded-full bg-blue-600 text-white">Explore funding <ArrowRight className="w-3.5 h-3.5"/></Link>
          </div>
          <div className="card p-5">
            <h3 className="font-medium text-sm">Profile & resume</h3>
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
