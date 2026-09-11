import { useEffect, useState } from 'react'
import client from '../api/client'
import { BarChart3, TrendingUp, Target, DollarSign } from 'lucide-react'

export default function Analytics() {
  const [perf, setPerf] = useState<any>(null)
  const [funnel, setFunnel] = useState<any>(null)
  const [costs, setCosts] = useState<any>(null)

  useEffect(()=>{
    client.get('/api/analytics/performance').then(r=>setPerf(r.data)).catch(()=>{})
    client.get('/api/analytics/funnel').then(r=>setFunnel(r.data)).catch(()=>{})
    client.get('/api/analytics/costs').then(r=>setCosts(r.data)).catch(()=>{})
  },[])

  if(!perf) return <div className="p-8 mono text-sm">Loading analytics…</div>

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold flex items-center gap-2"><BarChart3 className="w-5 h-5"/> Application Intelligence & Performance</h1>

      <div className="grid md:grid-cols-4 gap-3">
        <div className="card p-4"><div className="text-xs text-zinc-500 mono">Total Jobs</div><div className="text-2xl font-bold mono">{perf.summary.total_jobs}</div></div>
        <div className="card p-4"><div className="text-xs text-zinc-500 mono">Applied</div><div className="text-2xl font-bold mono">{perf.summary.applied}</div></div>
        <div className="card p-4"><div className="text-xs text-zinc-500 mono">Interviews (est)</div><div className="text-2xl font-bold mono">{perf.summary.interviews}</div></div>
        <div className="card p-4 bg-emerald-50 dark:bg-emerald-950 border-emerald-200 dark:border-emerald-800"><div className="text-xs text-zinc-500 mono">Interview Rate</div><div className="text-2xl font-bold mono text-emerald-700 dark:text-emerald-300">{perf.summary.interview_rate}%</div></div>
      </div>

      {perf.summary.best_role && (
        <div className="card p-4 bg-gradient-to-br from-blue-50 to-violet-50 dark:from-blue-950 dark:to-violet-950 border-blue-200 dark:border-blue-800">
          <div className="text-sm font-medium flex items-center gap-2"><TrendingUp className="w-4 h-4"/> Insight: {perf.summary.best_role} applications perform {perf.summary.best_role_rate>perf.summary.interview_rate ? `${(perf.summary.best_role_rate/perf.summary.interview_rate).toFixed(1)}× better` : 'best'} than other roles</div>
          <div className="text-xs text-zinc-600 dark:text-zinc-400 mt-1">Strongest matching skill: {perf.skills.strongest?.[0]?.skill || 'N/A'} • Weakest recurring: {perf.skills.weakest?.[0]?.skill || 'N/A'}</div>
        </div>
      )}

      <div className="grid lg:grid-cols-2 gap-4">
        <div className="card p-5">
          <h3 className="font-medium">Per-role performance</h3>
          <div className="mt-3 space-y-2">
            {perf.roles.map((r:any)=>(
              <div key={r.role} className="flex items-center justify-between p-2 rounded-xl bg-zinc-50 dark:bg-zinc-800">
                <div><div className="text-sm font-medium">{r.role}</div><div className="text-xs mono text-zinc-500">{r.total} total • {r.applied} applied • {r.interviews} interviews</div></div>
                <div className="text-sm mono font-bold">{r.interview_rate}%</div>
              </div>
            ))}
          </div>
        </div>

        <div className="card p-5">
          <h3 className="font-medium flex items-center gap-2"><Target className="w-4 h-4"/> Skills analysis</h3>
          <div className="mt-3 grid grid-cols-2 gap-3">
            <div>
              <div className="text-xs mono font-medium uppercase">Strongest matches</div>
              <div className="mt-2 space-y-1">
                {perf.skills.strongest.map((s:any)=>(
                  <div key={s.skill} className="flex justify-between text-xs p-1.5 rounded bg-emerald-50 dark:bg-emerald-950"><span>{s.skill}</span><span className="mono">{s.count}</span></div>
                ))}
              </div>
            </div>
            <div>
              <div className="text-xs mono font-medium uppercase">Missing / weak</div>
              <div className="mt-2 space-y-1">
                {perf.skills.weakest.map((s:any)=>(
                  <div key={s.skill} className="flex justify-between text-xs p-1.5 rounded bg-red-50 dark:bg-red-950"><span>{s.skill}</span><span className="mono">{s.count}</span></div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="grid lg:grid-cols-2 gap-4">
        <div className="card p-5">
          <h3 className="font-medium">Weekly trend</h3>
          <div className="mt-3 space-y-1">
            {perf.weekly_trend.map((d:any)=>(
              <div key={d.date} className="flex items-center gap-2 text-xs mono"><span className="w-24">{d.date}</span><div className="flex-1 bg-zinc-100 dark:bg-zinc-800 rounded-full h-2 overflow-hidden"><div className="bg-blue-600 h-2" style={{width: `${Math.min(100, d.discovered*10)}%`}}/></div><span>{d.discovered} discovered, {d.applied} applied</span></div>
            ))}
          </div>
        </div>

        <div className="card p-5">
          <h3 className="font-medium flex items-center gap-2"><DollarSign className="w-4 h-4"/> Cost breakdown</h3>
          {costs ? (
            <div className="mt-3 text-xs mono space-y-2">
              <div className="flex justify-between"><span>Total cost</span><span>${costs.total_cost_usd}</span></div>
              <div className="flex justify-between"><span>Total tokens</span><span>{costs.total_tokens}</span></div>
              <div className="flex justify-between"><span>Avg cost / app</span><span>${costs.average_cost_per_application}</span></div>
              <div className="mt-3 space-y-1">
                {Object.entries(costs.by_workflow).map(([k,v]: any)=>(
                  <div key={k} className="flex justify-between p-1.5 rounded bg-zinc-50 dark:bg-zinc-800"><span>{k}</span><span>{v.count} ops • {v.tokens} tokens • ${v.cost_usd}</span></div>
                ))}
              </div>
            </div>
          ) : <div className="text-xs mono text-zinc-500">No cost data.</div>}
        </div>
      </div>

      {funnel && (
        <div className="card p-5">
          <h3 className="font-medium">Funnel</h3>
          <div className="mt-3 flex gap-2 flex-wrap">
            {Object.entries(funnel.funnel).map(([stage,count]: any)=>(
              <div key={stage} className="px-3 py-2 rounded-full bg-zinc-100 dark:bg-zinc-800 text-xs mono">{stage}: {count as number}</div>
            ))}
            <div className="px-3 py-2 rounded-full bg-blue-600 text-white text-xs mono">Conversion: {funnel.conversion_rate}%</div>
          </div>
        </div>
      )}

      {!perf.is_pro && <div className="card p-4 bg-amber-50 dark:bg-amber-950 border-amber-200 dark:border-amber-800 text-xs">Upgrade to Pro for full analytics, role comparison, skill gap, cost insights.</div>}
    </div>
  )
}
