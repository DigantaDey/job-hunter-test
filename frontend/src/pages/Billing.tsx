import { useEffect, useState } from 'react'
import client, { apiError } from '../api/client'
import { Crown, Zap, Sparkles, BarChart3, CreditCard, AlertTriangle, Check } from 'lucide-react'

export default function Billing() {
  const [sub, setSub] = useState<any>(null)
  const [usage, setUsage] = useState<any>(null)
  const [credits, setCredits] = useState<any>(null)
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  const load = ()=>{
    client.get('/api/billing/subscription').then(r=>setSub(r.data)).catch(()=>{})
    client.get('/api/billing/usage').then(r=>setUsage(r.data)).catch(()=>{})
    client.get('/api/billing/credits?limit=30').then(r=>setCredits(r.data)).catch(()=>{})
  }
  useEffect(()=>{ load() },[])

  const upgrade = async (plan: string)=>{
    setBusy(true); setMsg('')
    try{
      const {data} = await client.post('/api/billing/upgrade', {plan})
      setMsg(data.message || `Upgraded to ${plan}`)
      load()
    }catch(e:any){ setMsg(apiError(e)) }
    finally{ setBusy(false) }
  }

  if(!sub) return <div className="p-8 mono text-sm">Loading billing…</div>

  const plan = sub.plan || 'free'
  const ent = sub

  return (
    <div className="space-y-6 max-w-5xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold flex items-center gap-2"><CreditCard className="w-5 h-5"/> Billing & Subscription</h1>
        <div className="text-xs mono px-3 py-1 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900">{ent.plan_label} • {ent.subscription?.status || 'active'}</div>
      </div>

      {msg && <div className="p-3 rounded-xl bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 text-xs mono">{msg}</div>}

      <div className="grid md:grid-cols-3 gap-4">
        <div className="card p-5">
          <h3 className="font-medium flex items-center gap-2"><Crown className="w-4 h-4"/> Current Plan</h3>
          <div className="mt-2 text-2xl font-bold mono">{ent.plan_label}</div>
          <div className="text-xs text-zinc-500 mt-1">{ent.plan_config?.description}</div>
          <div className="mt-3 text-xs mono space-y-1">
            <div>Status: {ent.subscription?.status}</div>
            <div>Period: {ent.period}</div>
            {ent.subscription?.trial_end && <div>Trial ends: {new Date(ent.subscription.trial_end).toLocaleDateString()}</div>}
            {ent.subscription?.grace_until && <div className="text-amber-600">Grace until: {new Date(ent.subscription.grace_until).toLocaleDateString()}</div>}
          </div>
          <div className="mt-4 flex gap-2">
            <button disabled={busy} onClick={()=>upgrade('free')} className="px-3 py-1.5 rounded-full border text-xs">Downgrade to Free</button>
            {plan==='free' && <button disabled={busy} onClick={()=>upgrade('pro')} className="px-3 py-1.5 rounded-full bg-blue-600 text-white text-xs">Upgrade to Pro</button>}
            {plan!=='pro_plus' && <button disabled={busy} onClick={()=>upgrade('pro_plus')} className="px-3 py-1.5 rounded-full bg-violet-600 text-white text-xs">Upgrade to Pro+</button>}
          </div>
        </div>

        <div className="card p-5 md:col-span-2">
          <h3 className="font-medium flex items-center gap-2"><BarChart3 className="w-4 h-4"/> Usage this month ({ent.period})</h3>
          <div className="mt-3 grid grid-cols-2 gap-3">
            {ent.usage && Object.entries(ent.usage).slice(0,12).map(([k,v]: any)=>(
              <div key={k} className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3">
                <div className="text-[11px] mono uppercase text-zinc-500">{k.replace(/_/g,' ')}</div>
                <div className="mt-1 flex items-center justify-between">
                  <span className="text-sm font-medium mono">{v.limit > 0 ? `${v.used}/${v.limit}` : `${v.used} used`}</span>
                  {/* 0 (or an explicit unlimited flag) = no ceiling on this plan. */}
                  {v.limit > 0
                    ? <span className="text-[11px] mono text-zinc-500">{v.remaining ?? 0} left</span>
                    : <span className="text-[11px] mono font-medium text-violet-600 dark:text-violet-300 px-2 py-0.5 rounded-full bg-violet-100 dark:bg-violet-950" title="No ceiling on this plan">Unlimited</span>}
                </div>
                {v.limit>0 && <div className="mt-2 w-full bg-zinc-200 dark:bg-zinc-700 rounded-full h-1"><div className="bg-blue-600 h-1 rounded-full" style={{width: `${Math.min(100, (v.used/v.limit)*100)}%`}}/></div>}
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="grid md:grid-cols-2 gap-4">
        <div className="card p-5">
          <h3 className="font-medium">AI Credits & Cost</h3>
          {credits ? (
            <div className="mt-3 space-y-2 text-xs mono">
              <div className="flex justify-between"><span>Monthly tokens</span><span>{credits.monthly_tokens}</span></div>
              <div className="flex justify-between"><span>Monthly cost</span><span>${credits.monthly_cost_usd}</span></div>
              <div className="flex justify-between"><span>Avg cost/app</span><span>${usage?.recent_ai_ops ? (credits.monthly_cost_usd/Math.max(1,usage?.usage?.applications_per_month?.used||1)).toFixed(4) : '0'}</span></div>
              <div className="mt-3 max-h-72 overflow-auto divide-y dark:divide-zinc-800 border rounded-xl">
                {credits.ledger.map((r:any)=>(
                  <div key={r.id} className={`p-2 ${!r.success ? 'bg-red-50/50 dark:bg-red-950/20' : ''}`}>
                    <div className="flex justify-between gap-2">
                      <span className="truncate">{r.workflow} • {r.model || 'unknown model'}</span>
                      <span>{r.total_tokens} tokens • ${Number(r.cost_usd||0).toFixed(4)}</span>
                      <span className={r.success?'text-emerald-600':'text-red-600 font-medium'}>{r.success?'ok':'fail'}</span>
                    </div>
                    <div className="flex justify-between text-[11px] opacity-60">
                      <span>{r.created_at ? new Date(r.created_at).toLocaleString() : ''}{r.latency_ms ? ` • ${r.latency_ms}ms` : ''}</span>
                      <span>prompt {r.prompt_tokens} • completion {r.completion_tokens}</span>
                    </div>
                    {!r.success && r.error && <div className="text-[11px] mt-1 p-1 rounded bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-300 break-all line-clamp-2">{String(r.error).slice(0,200)}</div>}
                    {r.success && r.total_tokens===0 && <div className="text-[11px] text-amber-600">⚠ 0 tokens — provider omitted usage, estimated from text length</div>}
                  </div>
                ))}
                {credits.ledger.length===0 && <div className="p-4 text-center text-zinc-500">No AI operations yet — upload a resume to generate the first parse entry.</div>}
              </div>
              <div className="mt-2 text-[11px] mono text-zinc-500">Tip: “fail” rows are AI calls that reached the provider but returned invalid JSON/guardrail failure — check error. “ok” rows include token counts (estimated when provider omits usage).</div>
            </div>
          ) : <div className="text-xs mono text-zinc-500">No AI usage yet.</div>}
        </div>

        <div className="card p-5">
          <h3 className="font-medium">Capabilities</h3>
          <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
            {ent.capabilities && Object.entries(ent.capabilities).map(([k,v]: any)=>(
              <div key={k} className={`p-2 rounded-xl border flex items-center gap-2 ${v?'bg-emerald-50 border-emerald-200 dark:bg-emerald-950 dark:border-emerald-800':'bg-zinc-50 dark:bg-zinc-800'}`}><Check className={`w-3.5 h-3.5 ${v?'text-emerald-600':'text-zinc-400'}`}/>{k.replace('can_','').replace(/_/g,' ')}</div>
            ))}
          </div>
          {!ent.capabilities?.can_use_advanced_matching && <div className="mt-3 text-xs p-2 rounded-xl bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 flex gap-2"><AlertTriangle className="w-4 h-4 text-amber-600"/>Upgrade to Pro for advanced matching, analytics, autofill, scheduled workflows, API keys.</div>}
        </div>
      </div>

      <div className="card p-5">
        <h3 className="font-medium">How billing works</h3>
        <div className="mt-2 text-xs text-zinc-600 dark:text-zinc-400 leading-relaxed">
          <p>Manual provider is active for self-hosted. In production, set BILLING_PROVIDER=stripe or razorpay and configure keys. Webhooks are idempotent via BillingEvent dedupe (provider + event_id unique). Supports trial, upgrade, downgrade, cancellation, renewal, failed payment, grace period (3 days), webhook processing.</p>
          <p className="mt-2">Free: complete product with limits. Pro: $19/mo (₹999) higher limits + advanced intelligence. Pro+: $49/mo (₹2499) highest limits + premium automation. All plans include audit trail, vault encryption, compliance gates.</p>
        </div>
      </div>
    </div>
  )
}
