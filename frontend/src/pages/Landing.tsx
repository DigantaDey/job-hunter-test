import { Link } from 'react-router-dom'
import { Sparkles, Search, Brain, FileText, CheckCircle, Zap, Mail, BarChart3, TrendingUp, ArrowRight, Shield, Clock, Vault } from 'lucide-react'

export default function Landing(){
  return (
    <div className="min-h-screen bg-white dark:bg-zinc-950">
      <div className="max-w-6xl mx-auto px-6 py-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3"><div className="w-8 h-8 rounded-xl bg-gradient-to-br from-blue-600 to-violet-600 flex items-center justify-center text-white font-bold text-xs">JH</div><span className="font-semibold">JobHunter</span><span className="text-[11px] mono bg-zinc-100 dark:bg-zinc-800 px-2 py-0.5 rounded-full">v2.1</span></div>
          <div className="flex gap-2"><Link to="/login" className="px-4 py-2 rounded-full border text-sm">Sign in</Link><Link to="/login" className="px-4 py-2 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm">Start free</Link></div>
        </div>

        <div className="mt-20 text-center">
          <div className="inline-flex items-center gap-2 text-xs mono bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 rounded-full px-4 py-1.5"><Sparkles className="w-3 h-3"/> Your complete AI-powered job hunting command center</div>
          <h1 className="text-4xl lg:text-6xl font-bold tracking-tight mt-6 leading-[1.05]">Discover → Analyze → Prepare → Apply → Automate → Reach Out → Track → Improve</h1>
          <p className="text-zinc-600 dark:text-zinc-400 mt-6 max-w-2xl mx-auto">JobHunter isn't a job board. It's your autonomous career system: multi-source discovery, transparent AI matching (Overall 87/100 + breakdown), resume fact-guard, vault credential autofill, approval-gated automation, outreach with compliance, analytics that tells you what's working.</p>
          <div className="mt-8 flex items-center justify-center gap-3"><Link to="/login" className="px-6 py-3 rounded-full bg-blue-600 text-white text-sm font-medium inline-flex items-center gap-2">Start free — full features <ArrowRight className="w-4 h-4"/></Link><Link to="/pricing" className="px-6 py-3 rounded-full border text-sm">View pricing</Link></div>
          <div className="mt-4 text-xs mono text-zinc-500">Free forever with limits • Pro $19/mo • Pro+ $49/mo • Razorpay + Stripe</div>
        </div>

        <div className="mt-20 grid md:grid-cols-3 lg:grid-cols-4 gap-4">
          {[
            {icon: Search, title:'Discover', desc:'ATS-native adapters (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday) + dedup, normalization, 24h freshness'},
            {icon: Brain, title:'Analyze', desc:'Transparent breakdown: Overall 87/100 + Skills/Experience/Seniority/Location/Salary/Education + strong/missing + HIGH PRIORITY'},
            {icon: FileText, title:'Prepare', desc:'Resume studio: storage, parsing, extraction, tailoring with JD fact-guard, bullets, skills alignment, cover letters, no fabrication'},
            {icon: CheckCircle, title:'Apply', desc:'Prepare→Review→Confirm→Execute. User-controlled, transparent, rate-limited, auditable, cancellable, fault-tolerant'},
            {icon: Zap, title:'Automate', desc:'FIFO pipelines, autofill, vault credentials, browser-assisted, never bypass CAPTCHA/ToS, safe alternatives'},
            {icon: Mail, title:'Reach Out', desc:'Templates, consent, suppression, unsubscribe, daily/monthly limits, Gmail/Outlook/SMTP, recruiter discovery'},
            {icon: BarChart3, title:'Track', desc:'Dashboard: new/high-priority/saved, applications prepared/submitted/interviews, automation running/completed/failed'},
            {icon: TrendingUp, title:'Improve', desc:'Interview rate, role performance, strongest/weakest skills, AI credit tracking, cost per application'},
          ].map(c=>(
            <div key={c.title} className="card p-5"><c.icon className="w-5 h-5"/><div className="font-medium mt-2">{c.title}</div><div className="text-xs text-zinc-500 mt-1 leading-relaxed">{c.desc}</div></div>
          ))}
        </div>

        <div className="mt-20 grid md:grid-cols-3 gap-6">
          <div className="card p-6"><Shield className="w-5 h-5"/><div className="font-medium mt-2">Security-first</div><div className="text-xs text-zinc-500 mt-1">JWT, refresh rotation, bcrypt, per-user HKDF vault encryption at rest, audit trail, IDOR/SQLi/XSS/CSRF/SSRF/path traversal hardening, no secret logging</div></div>
          <div className="card p-6"><Clock className="w-5 h-5"/><div className="font-medium mt-2">Reliability</div><div className="text-xs text-zinc-500 mt-1">Worker crashes, DB/queue failures, AI timeouts, rate limits, browser failures, malformed resumes, duplicate jobs/webhooks — retryable, observable, idempotent</div></div>
          <div className="card p-6"><Vault className="w-5 h-5"/><div className="font-medium mt-2">Monetized without removing features</div><div className="text-xs text-zinc-500 mt-1">Free users understand value, paid removes limits. Central entitlements system, AI credit ledger, subscription lifecycle, Razorpay India + Stripe international</div></div>
        </div>

        <div className="mt-20 text-center py-10 border-t dark:border-zinc-800">
          <div className="text-sm font-medium">Ready to automate your job hunt — without losing control?</div>
          <Link to="/login" className="mt-4 inline-flex px-6 py-3 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm">Start free → command center</Link>
        </div>
      </div>
    </div>
  )
}
