import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useEffect, useState } from 'react'
import { LayoutDashboard, Briefcase, FileText, Mail, Vault, Settings, ScrollText, Layers, TrendingUp, Moon, Sun, Monitor, Circle, LogOut, UserCog, CreditCard, BarChart3, Brain, Bell, DollarSign, Users, BadgeCheck } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import client from '../api/client'
import { useAuth } from '../context/AuthContext'
import { creditLabel } from '../lib/credits'
import { ErrorBoundary } from './ErrorBoundary'
import { AIStatusBanner } from './AIBanner'
import { AIWorkProvider } from '../context/AIWorkContext'
import { AIWorkChip } from './AIWorkChip'

const nav = [
  {to:'/', label:'Dashboard', icon: LayoutDashboard},
  {to:'/jobs', label:'Jobs', icon: Briefcase},
  {to:'/queues', label:'Queues', icon: Layers},
  {to:'/personas', label:'Personas', icon: Users},
  {to:'/packets', label:'Packets', icon: FileText},
  {to:'/resumes', label:'Resume Studio', icon: FileText},
  {to:'/profile-review', label:'Profile Review', icon: BadgeCheck},
  {to:'/emails', label:'Email Bucket', icon: Mail},
  {to:'/funding', label:'Funding Radar', icon: TrendingUp},
  {to:'/vault', label:'Vault', icon: Vault},
  {to:'/interview', label:'Interview Prep', icon: Brain},
  {to:'/analytics', label:'Analytics', icon: BarChart3},
  {to:'/notifications', label:'Notifications', icon: Bell},
  {to:'/billing', label:'Billing', icon: CreditCard},
  {to:'/pricing', label:'Pricing', icon: DollarSign},
  {to:'/settings', label:'Settings', icon: Settings},
  {to:'/account', label:'Account', icon: UserCog},
  {to:'/logs', label:'Error Logs', icon: ScrollText},
]

/**
 * The sidebar plan banner — the readout in the bug report
 * ("Pro+ • 222757/2000000 credits • Score 57 preliminary + AI pending").
 *
 * Exported so the unlimited reading is tested directly: a Pro+ account has
 * `ai_credits_per_month: 0`, and `0` is *unlimited*, not a spent quota. The
 * ceiling claim comes from `creditLabel` — the one place that decides.
 */
export function PlanCreditBanner({ ent }: { ent: any }) {
  if (!ent) return null
  const used = ent.usage?.ai_credits_per_month?.used ?? 0
  return (
    <div className="mt-2 text-[11px] mono bg-gradient-to-r from-blue-600 to-violet-600 text-white rounded-full px-3 py-1 text-center">
      {ent.plan_label} • {creditLabel(used, ent.limits?.ai_credits_per_month)}
    </div>
  )
}

export default function Layout() {
  const {theme, setTheme} = useTheme()
  const {user, logout} = useAuth()
  const [ai, setAi] = useState<{online:boolean, rpm:number, remaining:number, latency_ms?:number, state?:string, reason?:string|null, hint?:string|null, paused_count?:number, retry_after_hint?:number|null} | null>(null)
  const [ent, setEnt] = useState<any>(null)
  const [resuming, setResuming] = useState(false)
  const loc = useLocation()
  const refreshAi = () => client.get('/api/settings/ai/status').then(r=>setAi(r.data)).catch(()=>{})
  useEffect(()=>{
    let cancelled = false
    const poll = async ()=>{
      try {
        const {data} = await client.get('/api/settings/ai/status')
        if (!cancelled) setAi(data)
      } catch {
        // Network down — do not flip the banner: the last known signal stands.
        if (!cancelled) setAi(prev => prev ? {...prev, online:false} : {online:false, rpm:60, remaining:0})
      }
    }
    let id = setInterval(poll, 15000)
    poll()
    client.get('/api/billing/subscription').then(r=>{ if(!cancelled) setEnt(r.data) }).catch(()=>{})
    return ()=> { cancelled = true; clearInterval(id) }
  }, [])
  /** One-click resume: force-drain the user's paused AI work (watchdog does it
      automatically on a green probe; this is the impatient path). */
  const resumeNow = async ()=>{
    setResuming(true)
    try {
      await client.post('/api/settings/ai/resume')
      await refreshAi()
    } finally { setResuming(false) }
  }
  return (
    <AIWorkProvider>
    <div className="min-h-screen bg-zinc-50 dark:bg-zinc-950 flex">
      {/* sidebar */}
      <aside className="w-[260px] shrink-0 hidden lg:flex flex-col border-r bg-white dark:bg-zinc-900 dark:border-zinc-800 sticky top-0 h-screen">
        <div className="p-5 border-b dark:border-zinc-800">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-blue-600 to-violet-600 flex items-center justify-center text-white font-bold text-sm">JH</div>
            <div>
              <div className="font-semibold tracking-tight leading-none">JobHunter</div>
              <div className="text-[11px] text-zinc-500 dark:text-zinc-400 mono">AI • Command Center</div>
            </div>
            <div className="ml-auto flex items-center gap-1">
              <Circle className={`w-3 h-3 ${ai?.online ? 'fill-emerald-500 text-emerald-500' : 'fill-red-500 text-red-500'} animate-pulse`} />
              <span className={`text-[11px] font-medium ${ai?.online ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-500'}`}>{ai?.online ? 'AI online' : 'AI offline'}</span>
            </div>
          </div>
          {ai && <div className="mt-3 text-[11px] mono text-zinc-500 dark:text-zinc-400 bg-zinc-50 dark:bg-zinc-800 rounded-lg px-2 py-1 flex justify-between">
            <span>RPM {ai.remaining}/{ai.rpm}</span><span>{ai.latency_ms ? `${ai.latency_ms}ms` : ''}</span>
          </div>}
          <PlanCreditBanner ent={ent} />
        </div>
        <nav className="p-3 space-y-1 flex-1 overflow-auto scrollbar-thin">
          {nav.map(n=> (
            <NavLink key={n.to} to={n.to} className={({isActive})=> `flex items-center gap-3 px-3 py-2 rounded-lg text-sm ${isActive ? 'bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 font-medium' : 'text-zinc-600 dark:text-zinc-400 hover:bg-zinc-100 dark:hover:bg-zinc-800'}`}>
              <n.icon className="w-[18px] h-[18px]" /> {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="p-3 border-t dark:border-zinc-800">
          <div className="flex items-center gap-1 bg-zinc-100 dark:bg-zinc-800 rounded-full p-1">
            <button onClick={()=>setTheme('light')} className={`flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded-full text-xs ${theme==='light'?'bg-white shadow text-zinc-900':'text-zinc-500'}`}><Sun className="w-3.5 h-3.5"/> Light</button>
            <button onClick={()=>setTheme('dark')} className={`flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded-full text-xs ${theme==='dark'?'bg-zinc-900 shadow text-white':'text-zinc-500'}`}><Moon className="w-3.5 h-3.5"/> Dark</button>
            <button onClick={()=>setTheme('system')} className={`flex-1 flex items-center justify-center gap-1 py-1.5 rounded-full text-xs ${theme==='system'?'bg-white dark:bg-zinc-700 shadow':'text-zinc-500'}`}><Monitor className="w-3.5 h-3.5"/> Auto</button>
          </div>
          {user && (
            <div className="mt-3 flex items-center gap-2 text-[11px] mono text-zinc-500 dark:text-zinc-400">
              <span className="truncate flex-1" title={user.email}>{user.email}</span>
              <button onClick={()=>void logout()} title="Sign out" aria-label="Sign out" className="p-2 min-h-[44px] min-w-[44px] inline-flex items-center justify-center rounded hover:bg-zinc-100 dark:hover:bg-zinc-800 focus:outline-none focus:ring-2 focus:ring-blue-500">
                <LogOut className="w-3.5 h-3.5"/>
              </button>
            </div>
          )}
          <div className="mt-2 text-[11px] text-zinc-500 dark:text-zinc-400 mono text-center">v2.1 • SaaS • monetized • command center</div>
        </div>
      </aside>

      {/* main */}
      <div className="flex-1 min-w-0 flex flex-col">
        {/* mobile top bar */}
        <div className="lg:hidden sticky top-0 z-30 bg-white/80 dark:bg-zinc-900/80 backdrop-blur border-b dark:border-zinc-800 px-4 py-3 flex items-center gap-3">
          <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-blue-600 to-violet-600 flex items-center justify-center text-white font-bold text-xs">JH</div>
          <div className="font-semibold text-sm">JobHunter</div>
          <div className="ml-auto flex items-center gap-2">
            <AIWorkChip />
            <Circle className={`w-2.5 h-2.5 ${ai?.online ? 'fill-emerald-500 text-emerald-500' : 'fill-red-500 text-red-500'}`} />
            <span className="text-xs">{ai?.online ? 'AI online' : 'AI offline'}</span>
          </div>
        </div>
        {/* mobile nav */}
        <div className="lg:hidden border-b dark:border-zinc-800 bg-white dark:bg-zinc-900 px-2 py-2 flex gap-1 overflow-auto">
          {nav.map(n=> <NavLink key={n.to} to={n.to} className={({isActive})=> `px-3 py-1.5 rounded-full text-xs whitespace-nowrap border ${isActive?'bg-zinc-900 text-white border-zinc-900 dark:bg-white dark:text-zinc-900':'bg-white dark:bg-zinc-800 text-zinc-600 dark:text-zinc-300'}`}>{n.label}</NavLink>)}
        </div>
        {/* Live AI work header (v2.2.8): the chip is above every route so a run
            started on Jobs is still visible on Funding Radar — and after F5,
            because it reads the durable queue rows, never component state. */}
        <div className="hidden lg:flex sticky top-0 z-30 bg-zinc-50/80 dark:bg-zinc-950/80 backdrop-blur px-6 py-2 items-center justify-end gap-2 min-h-[52px]">
          <AIWorkChip />
        </div>
        <main className="flex-1 p-4 lg:p-6 lg:pt-2 max-w-[1600px] w-full mx-auto">
          <AIStatusBanner status={ai} resuming={resuming} onResume={()=>void resumeNow()} />
          <ErrorBoundary scope="route" resetKey={loc.pathname}>
            <Outlet />
          </ErrorBoundary>
        </main>
        <footer className="px-6 py-4 text-[11px] mono text-zinc-500 dark:text-zinc-500 text-center border-t dark:border-zinc-800 bg-white/50 dark:bg-zinc-900/50">
          JobHunter AI v2.1 — Your complete AI-powered job hunting command center • Discover → Analyze → Prepare → Apply → Automate → Reach Out → Track → Improve • per-user encrypted vault • fact guard • monetized
        </footer>
      </div>
    </div>
    </AIWorkProvider>
  )
}
