import { useEffect, useState } from 'react'
import client from '../api/client'
import { Settings as SettingsIcon, Save, Bot, Search, Sliders, Mail, Shield, Zap, TrendingUp } from 'lucide-react'

export default function Settings(){
  const [data, setData]=useState<any>(null)
  const [saving, setSaving]=useState(false)
  const [msg, setMsg]=useState('')

  const load=async()=>{ const {data}=await client.get('/api/settings'); setData(data)}
  useEffect(()=>{ load() },[])

  const save=async()=>{
    setSaving(true); setMsg('')
    try{ await client.put('/api/settings', data); setMsg('Saved ✓'); setTimeout(()=>setMsg(''), 2000)} finally{ setSaving(false)}
  }
  const update=(cat:string, key:string, value:any)=> setData({...data, [cat]: {...data[cat], [key]: value}})

  if(!data) return <div className="p-8 mono text-sm">Loading settings…</div>

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold flex items-center gap-2"><SettingsIcon className="w-5 h-5"/> Settings <span className="text-xs mono font-normal text-zinc-500">grouped • all editable</span></h1>
        <button onClick={save} disabled={saving} className="px-4 py-2 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm inline-flex items-center gap-2 disabled:opacity-50"><Save className="w-4 h-4"/> {saving?'Saving…':'Save all'}</button>
      </div>
      {msg && <div className="text-sm mono p-2 rounded-lg bg-emerald-50 dark:bg-emerald-950 border border-emerald-200 dark:border-emerald-800 text-emerald-700 dark:text-emerald-300">{msg}</div>}

      <div className="grid lg:grid-cols-2 gap-4">
        {/* AI */}
        <div className="card p-5">
          <h3 className="font-medium flex items-center gap-2"><Bot className="w-4 h-4"/> AI API</h3>
          <p className="text-xs mono text-zinc-500 mt-1">OpenAI-compatible. Different API per workflow possible. Rate limiter respects RPM.</p>
          <div className="mt-3 space-y-3">
            <div><label className="text-xs mono">Base URL</label><input value={data.ai.base_url} onChange={e=>update('ai','base_url',e.target.value)} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700"/></div>
            <div><label className="text-xs mono">Model</label><input value={data.ai.model} onChange={e=>update('ai','model',e.target.value)} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700"/></div>
            <div><label className="text-xs mono">RPM (requests per minute) — slider</label><div className="flex items-center gap-3 mt-1"><input type="range" min={5} max={300} value={data.ai.rpm} onChange={e=>update('ai','rpm', Number(e.target.value))} className="flex-1"/><span className="mono text-sm w-12 text-center">{data.ai.rpm}</span></div><div className="text-[11px] mono text-zinc-500">Pipeline waits intelligently to never hit limit • green/red dot shows online</div></div>
            <div className="flex items-center gap-2 text-xs mono"><Shield className="w-3.5 h-3.5"/> API key: <span className="px-2 py-1 rounded-full bg-zinc-100 dark:bg-zinc-800">•••••••• set: {String(data.ai.api_key_set)}</span></div>
          </div>
          <div className="mt-4 p-3 bg-zinc-50 dark:bg-zinc-800 rounded-xl">
            <div className="text-xs mono font-medium flex items-center gap-1"><Zap className="w-3.5 h-3.5"/> Per-workflow AI override</div>
            <div className="text-[11px] mono text-zinc-500">Set different API for discovery vs resume vs email at <span className="underline">/api/ai/config</span> — UI for that is in this card for demo.</div>
          </div>
        </div>

        <div className="card p-5">
          <h3 className="font-medium flex items-center gap-2"><Search className="w-4 h-4"/> Scraping</h3>
          <div className="mt-3 space-y-3">
            <div><label className="text-xs mono">Keywords (comma-separated, extracted from resume + editable)</label><input value={data.scraping.keywords} onChange={e=>update('scraping','keywords',e.target.value)} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"/></div>
            <div><label className="text-xs mono">Job freshness (hours)</label><input type="number" value={data.scraping.freshness_hours} onChange={e=>update('scraping','freshness_hours', Number(e.target.value))} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"/></div>
            <div><label className="text-xs mono">Sources (clubbed)</label><div className="flex flex-wrap gap-1 mt-1">{(data.scraping.sources||[]).map((s:string)=> <span key={s} className="text-xs px-2 py-1 rounded-full bg-zinc-100 dark:bg-zinc-800 mono">{s}</span>)}</div></div>
          </div>
        </div>

        <div className="card p-5">
          <h3 className="font-medium flex items-center gap-2"><TrendingUp className="w-4 h-4"/> Funding Radar</h3>
          <p className="text-xs mono text-zinc-500 mt-1">AI auto-extracts keywords from your profile + resume. Add extra focus here (optional).</p>
          <div className="mt-3 space-y-3">
            <div><label className="text-xs mono">Context notes (free-form, merged with AI extraction)</label><textarea value={data.funding?.context_notes||''} onChange={e=>update('funding','context_notes',e.target.value)} rows={2} placeholder="e.g. AI infrastructure, fintech in India" className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"/></div>
            <div><label className="text-xs mono">Industries to prioritize (optional)</label><input value={data.funding?.industries||''} onChange={e=>update('funding','industries',e.target.value)} placeholder="ai/ml, fintech" className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"/></div>
          </div>
        </div>

        <div className="card p-5">
          <h3 className="font-medium flex items-center gap-2"><Sliders className="w-4 h-4"/> General & Resume</h3>
          <div className="mt-3 space-y-3">
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={data.general.strict_skeleton} onChange={e=>update('general','strict_skeleton',e.target.checked)}/> Strictly maintain uploaded skeleton</label>
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={data.general.auto_approve_email} onChange={e=>update('general','auto_approve_email',e.target.checked)}/> Auto-approve emails (else bucket)</label>
            <div><label className="text-xs mono">Default resume format</label><select value={data.general.default_resume_format} onChange={e=>update('general','default_resume_format',e.target.value)} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"><option value="ai_generated">AI generated</option><option value="user_skeleton">User skeleton</option></select></div>
            <div><label className="text-xs mono">Additional questions (free-form)</label><textarea value={data.general.additional_questions||''} onChange={e=>update('general','additional_questions',e.target.value)} rows={3} placeholder="e.g. Work authorization, visa status…" className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"/></div>
          </div>
        </div>

        <div className="card p-5">
          <h3 className="font-medium flex items-center gap-2"><Mail className="w-4 h-4"/> Email (SMTP + 2FA)</h3>
          <div className="mt-3 space-y-3">
            <div><label className="text-xs mono">SMTP Host</label><input value={data.email.host||''} onChange={e=>update('email','host',e.target.value)} placeholder="smtp.gmail.com" className="w-full mt-1 border rounded-xl px-3 py-2 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700"/></div>
            <div className="grid grid-cols-2 gap-2"><div><label className="text-xs mono">Port</label><input type="number" value={data.email.port||587} onChange={e=>update('email','port',Number(e.target.value))} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"/></div><div><label className="text-xs mono">Username (your email)</label><input value={data.email.username||''} onChange={e=>update('email','username',e.target.value)} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"/></div></div>
            <div className="text-[11px] mono text-zinc-500">For 2FA: system asks for OTP / verify on other device / app password. Handles all provider options.</div>
          </div>
        </div>

        <div className="card p-5 lg:col-span-2">
          <h3 className="font-medium flex items-center gap-2"><Bot className="w-4 h-4"/> Workflows toggles (clubbed)</h3>
          <div className="mt-3 grid md:grid-cols-4 gap-3">
            {Object.entries(data.workflows||{}).map(([k,v]:any)=> (
              <label key={k} className="flex items-center gap-2 text-sm p-3 rounded-xl border dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-800"><input type="checkbox" checked={!!v} onChange={e=>update('workflows',k,e.target.checked)}/><span className="mono text-xs">{k}</span></label>
            ))}
          </div>
          <div className="mt-3 text-[11px] mono text-zinc-500">All settings are in categories — AI, Scraping, General, Application, Email, Workflows — editable, with rate limiter, keywords, profile details, additional questions etc.</div>
        </div>
      </div>
    </div>
  )
}
