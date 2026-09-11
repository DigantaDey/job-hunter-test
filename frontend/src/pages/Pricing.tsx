import { useEffect, useState } from 'react'
import client from '../api/client'
import { Check, Sparkles, Zap, Crown, ArrowRight } from 'lucide-react'
import { Link } from 'react-router-dom'

export default function Pricing() {
  const [plans, setPlans] = useState<any>(null)
  const [billingCycle, setBillingCycle] = useState<'monthly'|'yearly'>('monthly')
  const [region, setRegion] = useState<'usd'|'inr'>('usd')

  useEffect(()=>{
    client.get('/api/billing/plans').then(r=>setPlans(r.data)).catch(()=>setPlans(null))
  },[])

  if (!plans) return <div className="p-8 mono text-sm">Loading pricing…</div>

  const planKeys = ['free','pro','pro_plus']
  const planIcons: any = {free: Sparkles, pro: Zap, pro_plus: Crown}
  const planColors: any = {free: 'border-zinc-200 dark:border-zinc-800', pro: 'border-blue-500 shadow-lg shadow-blue-500/10', pro_plus: 'border-violet-500 shadow-lg shadow-violet-500/10'}

  return (
    <div className="space-y-8 max-w-6xl mx-auto">
      <div className="text-center py-8">
        <div className="inline-flex items-center gap-2 text-xs mono bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 rounded-full px-3 py-1"><Sparkles className="w-3 h-3"/> Your complete AI-powered job hunting command center</div>
        <h1 className="text-3xl lg:text-4xl font-semibold tracking-tight mt-4">Discover → Analyze → Prepare → Apply → Automate → Reach Out → Track → Improve</h1>
        <p className="text-zinc-600 dark:text-zinc-400 mt-3 max-w-2xl mx-auto text-sm">JobHunter is not just a job board. It's your autonomous career command center. Full feature set in every plan — limits differentiate, not artificial removal.</p>
        <div className="mt-6 flex items-center justify-center gap-2">
          <div className="bg-zinc-100 dark:bg-zinc-800 rounded-full p-1 flex">
            <button onClick={()=>setBillingCycle('monthly')} className={`px-4 py-1.5 rounded-full text-xs ${billingCycle==='monthly'?'bg-white dark:bg-zinc-700 shadow font-medium':'text-zinc-500'}`}>Monthly</button>
            <button onClick={()=>setBillingCycle('yearly')} className={`px-4 py-1.5 rounded-full text-xs ${billingCycle==='yearly'?'bg-white dark:bg-zinc-700 shadow font-medium':'text-zinc-500'}`}>Yearly (save 20%)</button>
          </div>
          <div className="bg-zinc-100 dark:bg-zinc-800 rounded-full p-1 flex ml-2">
            <button onClick={()=>setRegion('usd')} className={`px-3 py-1.5 rounded-full text-xs ${region==='usd'?'bg-white dark:bg-zinc-700 shadow':'text-zinc-500'}`}>$ USD</button>
            <button onClick={()=>setRegion('inr')} className={`px-3 py-1.5 rounded-full text-xs ${region==='inr'?'bg-white dark:bg-zinc-700 shadow':'text-zinc-500'}`}>₹ INR</button>
          </div>
        </div>
      </div>

      <div className="grid md:grid-cols-3 gap-6">
        {planKeys.map(key=>{
          const cfg = plans.plans[key]
          const Icon = planIcons[key]
          const price = region==='usd' ? (billingCycle==='monthly' ? cfg.price_monthly_usd : cfg.price_yearly_usd) : (billingCycle==='monthly' ? cfg.price_monthly_inr : cfg.price_yearly_inr)
          const isFree = key==='free'
          return (
            <div key={key} className={`card p-6 border-2 ${planColors[key]} relative`}>
              {key==='pro' && <div className="absolute -top-3 left-1/2 -translate-x-1/2 bg-blue-600 text-white text-[11px] mono px-3 py-1 rounded-full">Most Popular</div>}
              {key==='pro_plus' && <div className="absolute -top-3 left-1/2 -translate-x-1/2 bg-violet-600 text-white text-[11px] mono px-3 py-1 rounded-full">Power Users</div>}
              <div className="flex items-center gap-2"><Icon className="w-5 h-5"/><h3 className="font-semibold">{cfg.label}</h3></div>
              <div className="mt-3"><span className="text-3xl font-bold mono">{region==='usd'?'$':'₹'}{price}</span><span className="text-xs text-zinc-500">/{billingCycle==='monthly'?'mo':'yr'}</span>{isFree && <span className="ml-2 text-xs bg-zinc-100 dark:bg-zinc-800 px-2 py-1 rounded-full">Free forever</span>}</div>
              <p className="text-xs text-zinc-500 mt-2">{cfg.description}</p>
              <div className="mt-4 space-y-2 text-xs">
                <div className="font-medium mono text-[11px] uppercase tracking-wide">Limits</div>
                <div className="flex justify-between"><span>Jobs/month</span><span className="mono font-medium">{cfg.limits.jobs_discovered_per_month}</span></div>
                <div className="flex justify-between"><span>AI ops/month</span><span className="mono font-medium">{cfg.limits.ai_operations_per_month}</span></div>
                <div className="flex justify-between"><span>Tailored resumes</span><span className="mono font-medium">{cfg.limits.tailored_resumes_per_month}</span></div>
                <div className="flex justify-between"><span>Automation runs</span><span className="mono font-medium">{cfg.limits.automation_runs_per_month}</span></div>
                <div className="flex justify-between"><span>Outreach/month</span><span className="mono font-medium">{cfg.limits.outreach_per_month}</span></div>
                <div className="flex justify-between"><span>Interview sessions</span><span className="mono font-medium">{cfg.limits.interview_sessions_per_month}</span></div>
              </div>
              <div className="mt-4 space-y-1.5">
                {Object.entries(cfg.capabilities).filter(([,v])=>v).slice(0,6).map(([k])=>(
                  <div key={k} className="flex items-center gap-2 text-xs"><Check className="w-3.5 h-3.5 text-emerald-600"/>{k.replace('can_','').replace(/_/g,' ')}</div>
                ))}
                {key==='free' && <div className="text-[11px] text-zinc-500 mt-2">+ Advanced features locked — upgrade to unlock detailed intelligence, analytics, autofill</div>}
              </div>
              <div className="mt-6">
                {isFree ? <Link to="/" className="w-full py-2.5 rounded-full border dark:border-zinc-700 text-sm flex items-center justify-center gap-2">Start free</Link> :
                  <Link to="/billing" className={`w-full py-2.5 rounded-full text-sm font-medium flex items-center justify-center gap-2 ${key==='pro'?'bg-blue-600 text-white':'bg-violet-600 text-white'}`}>Upgrade to {cfg.label} <ArrowRight className="w-4 h-4"/></Link>}
              </div>
            </div>
          )
        })}
      </div>

      <div className="card p-6">
        <h3 className="font-medium">Free users understand the value. Paid users remove the limits.</h3>
        <p className="text-xs text-zinc-500 mt-2">All existing features remain available in Free tier. We monetize genuine value: higher search limits, advanced AI analysis, unlimited tailoring, more automation runs, higher outreach, advanced analytics, interview prep, company intelligence, scheduled workflows.</p>
        <div className="mt-4 grid md:grid-cols-3 gap-4 text-xs">
          <div><div className="font-medium">Job Discovery</div><div className="text-zinc-500">Higher search limits</div></div>
          <div><div className="font-medium">AI Matching</div><div className="text-zinc-500">Advanced analysis with breakdown</div></div>
          <div><div className="font-medium">Resume</div><div className="text-zinc-500">Unlimited tailoring + cover letters</div></div>
          <div><div className="font-medium">Automation</div><div className="text-zinc-500">More runs + autofill + scheduled</div></div>
          <div><div className="font-medium">Outreach</div><div className="text-zinc-500">Higher limits + integrations</div></div>
          <div><div className="font-medium">Analytics</div><div className="text-zinc-500">Performance insights + cost tracking</div></div>
        </div>
      </div>

      <div className="text-center text-[11px] mono text-zinc-500">Pricing: Free $0, Pro $19/mo (₹999), Pro+ $49/mo (₹2499). Yearly saves 20%. Razorpay for India, Stripe for international.</div>
    </div>
  )
}
