import { useEffect, useState } from 'react'
import client from '../api/client'
import { BarChart3, TrendingUp, Target, DollarSign, History } from 'lucide-react'
import { conversionText, groupLabel, rateText, type TrackingTally } from '../lib/tracking'
import { useAuth } from '../context/AuthContext'
import { isOwner } from '../lib/role'

/**
 * Interviews on this page are *recorded*, never estimated.
 *
 * It used to render "Interviews (est)" over a number the backend invented by
 * counting applied jobs whose match score was >= 75 — a scorer output dressed as
 * an outcome, and the interview rate moved when the scorer moved. The tracking
 * layer (docs/contracts/07 §11) is now the only source: a row exists because the
 * user pressed "I got an interview", because our own machinery submitted the
 * application and saw the receipt, or because an integration the user connected
 * reported it. With no recorded outcomes the number is 0, the rate is `—` when
 * there is nothing to divide by, and the note says where to fix it.
 *
 * The cost breakdown is owner-level (v2.3): `GET /api/analytics/costs` 403s a
 * member, so the page only fetches — and renders — it for the workspace owner.
 * A member's success metrics here are applications and interviews.
 */

export default function Analytics() {
  const { user } = useAuth()
  const owner = isOwner(user)
  const [perf, setPerf] = useState<any>(null)
  const [funnel, setFunnel] = useState<any>(null)
  const [costs, setCosts] = useState<any>(null)

  useEffect(()=>{
    client.get('/api/analytics/performance').then(r=>setPerf(r.data)).catch(()=>{})
    client.get('/api/analytics/funnel').then(r=>setFunnel(r.data)).catch(()=>{})
    if (isOwner(user)) {
      client.get('/api/analytics/costs').then(r=>setCosts(r.data)).catch(()=>{})
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  },[])

  if(!perf) return <div className="p-8 mono text-sm">Loading analytics…</div>

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold flex items-center gap-2"><BarChart3 className="w-5 h-5"/> Application Intelligence & Performance</h1>

      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        <div className="card p-4"><div className="text-xs text-zinc-500 mono">Total Jobs</div><div className="text-2xl font-bold mono">{perf.summary.total_jobs}</div></div>
        <div className="card p-4"><div className="text-xs text-zinc-500 mono">Applied</div><div className="text-2xl font-bold mono">{perf.summary.applied}</div></div>
        <div className="card p-4" title={perf.summary.interview_note}>
          <div className="text-xs text-zinc-500 mono">Interviews recorded</div>
          <div className="text-2xl font-bold mono" data-testid="interviews">{perf.summary.interviews}</div>
          <div className="text-[10px] mono text-zinc-400">
            {perf.summary.interview_data === 'application_tracking' ? 'from your timeline' : 'nothing recorded yet'}
          </div>
        </div>
        <div className="card p-4 bg-emerald-50 dark:bg-emerald-950 border-emerald-200 dark:border-emerald-800"
             title="Applications that reached an interview, out of applications recorded">
          <div className="text-xs text-zinc-500 mono">Application → interview</div>
          <div className="text-2xl font-bold mono text-emerald-700 dark:text-emerald-300" data-testid="interview-rate">
            {rateText(perf.summary.interview_rate)}
          </div>
        </div>
        <div className="card p-4"><div className="text-xs text-zinc-500 mono">Offers</div><div className="text-2xl font-bold mono">{perf.summary.offers ?? 0}</div></div>
        <div className="card p-4"><div className="text-xs text-zinc-500 mono">Rejections</div><div className="text-2xl font-bold mono">{perf.summary.rejections ?? 0}</div></div>
      </div>

      <div className="card p-4 flex flex-wrap items-center gap-3" data-testid="interview-note">
        <History className="w-4 h-4 text-zinc-400" />
        <div className="text-xs text-zinc-600 dark:text-zinc-400 flex-1 min-w-[240px]">{perf.summary.interview_note}</div>
        <a href="/tracking" className="text-xs mono text-blue-600 dark:text-blue-400 underline min-h-[32px] inline-flex items-center">
          Open the tracking board
        </a>
      </div>

      {perf.summary.best_role && (
        <div className="card p-4 bg-gradient-to-br from-blue-50 to-violet-50 dark:from-blue-950 dark:to-violet-950 border-blue-200 dark:border-blue-800">
          {/* The multiplier is only claimed when both rates are real numbers:
              dividing by a `null` rate would print "Infinity× better". */}
          <div className="text-sm font-medium flex items-center gap-2"><TrendingUp className="w-4 h-4"/> Insight: {perf.summary.best_role} applications perform {typeof perf.summary.best_role_rate === 'number' && typeof perf.summary.interview_rate === 'number' && perf.summary.interview_rate > 0 && perf.summary.best_role_rate > perf.summary.interview_rate ? `${(perf.summary.best_role_rate/perf.summary.interview_rate).toFixed(1)}× better` : 'best'} than other roles</div>
          <div className="text-xs text-zinc-600 dark:text-zinc-400 mt-1">Strongest matching skill: {perf.skills.strongest?.[0]?.skill || 'N/A'} • Weakest recurring: {perf.skills.weakest?.[0]?.skill || 'N/A'}</div>
        </div>
      )}

      <div className="grid lg:grid-cols-2 gap-4">
        <div className="card p-5">
          <h3 className="font-medium">Per-role performance</h3>
          <div className="mt-3 space-y-2">
            {perf.roles.map((r:any)=>(
              <div key={r.role} data-testid={`role-${r.role.replace(/[^a-z0-9]+/gi, '-').toLowerCase()}`} className="flex items-center justify-between p-2 rounded-xl bg-zinc-50 dark:bg-zinc-800">
                <div><div className="text-sm font-medium">{r.role}</div><div className="text-xs mono text-zinc-500">{r.total} total • {r.applied} applied • {r.interviews} interviews</div></div>
                <div className="text-sm mono font-bold" title={r.applied ? 'Interviews recorded for this role family ÷ applications' : 'No applications recorded for this role family'}>{rateText(r.interview_rate)}</div>
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

        {owner && (
        <div className="card p-5" data-testid="cost-breakdown">
          <h3 className="font-medium flex items-center gap-2"><DollarSign className="w-4 h-4"/> Cost breakdown</h3>
          {costs ? (
            <div className="mt-3 text-xs mono space-y-2">
              <div className="flex justify-between"><span>Total cost</span><span>${costs.total_cost_usd}</span></div>
              <div className="flex justify-between"><span>Total tokens</span><span>{costs.total_tokens}</span></div>
              <div className="flex justify-between"><span>Avg cost / app</span><span>${costs.average_cost_per_application}</span></div>
              <div className="mt-3 space-y-1">
                {/* A partial cost payload must not blank the whole page. */}
                {Object.entries(costs.by_workflow || {}).map(([k,v]: any)=>(
                  <div key={k} className="flex justify-between p-1.5 rounded bg-zinc-50 dark:bg-zinc-800"><span>{k}</span><span>{v.count} ops • {v.tokens} tokens • ${v.cost_usd}</span></div>
                ))}
              </div>
            </div>
          ) : <div className="text-xs mono text-zinc-500">No cost data.</div>}
        </div>
        )}
      </div>

      {perf.conversion && (
        <div className="card p-5">
          <h3 className="font-medium flex items-center gap-2"><History className="w-4 h-4"/> Application → interview, by source</h3>
          <div className="text-xs text-zinc-600 dark:text-zinc-400 mt-1" data-testid="conversion-summary">
            {conversionText(perf.conversion.totals as TrackingTally)}
          </div>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-xs mono">
              <thead className="text-zinc-500 text-left">
                <tr>
                  <th className="py-1 pr-3 font-medium">Source</th>
                  <th className="py-1 pr-3 font-medium">Applied</th>
                  <th className="py-1 pr-3 font-medium">Interviews</th>
                  <th className="py-1 pr-3 font-medium">Conversion</th>
                  <th className="py-1 pr-3 font-medium">Recorded by</th>
                </tr>
              </thead>
              <tbody>
                {(perf.conversion.groups || []).map((g: any) => (
                  <tr key={g.key} data-testid={`conversion-row-${g.key}`} className="border-t dark:border-zinc-800">
                    <td className="py-1.5 pr-3">{groupLabel(g.key, 'source')}</td>
                    <td className="py-1.5 pr-3">{g.applied}</td>
                    <td className="py-1.5 pr-3">{g.interviews}</td>
                    <td className="py-1.5 pr-3">{rateText(g.application_to_interview_rate)}</td>
                    <td className="py-1.5 pr-3 text-zinc-500">
                      {Object.entries(g.by_origin || {}).map(([origin, count]) => `${origin}: ${count}`).join(' • ') || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="text-[11px] mono text-zinc-500 mt-2">{perf.disclaimer}</div>
        </div>
      )}

      {funnel && (
        <div className="card p-5">
          <h3 className="font-medium">Funnel</h3>
          <div className="mt-3 flex gap-2 flex-wrap">
            {/* Same rule as the cost block: a partial payload cannot blank the page. */}
            {Object.entries(funnel.funnel || {}).map(([stage,count]: any)=>(
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
