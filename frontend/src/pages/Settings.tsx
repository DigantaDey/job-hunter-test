import { useEffect, useState } from 'react'
import client, { apiError as apiErrorMessage } from '../api/client'
import { Settings as SettingsIcon, Save, Bot, Search, Sliders, Mail, Shield, Zap, TrendingUp, Eye, EyeOff, CheckCircle, AlertTriangle, Globe, Key, Cpu } from 'lucide-react'

const WORKFLOWS = ['parse','keyword_extract','scoring','resume_gen','classify','email_gen','form_detect','funding_scan','tagging','interview','company_intel']

export default function Settings(){
  const [data, setData]=useState<any>(null)
  const [wfConfig, setWfConfig]=useState<any>({})
  const [saving, setSaving]=useState(false)
  const [msg, setMsg]=useState('')
  const [showKey, setShowKey]=useState(false)
  const [aiForm, setAiForm]=useState({base_url:'', model:'', api_key:'', rpm:60})
  const [aiStatus, setAiStatus]=useState<any>(null)
  const [aiSaving, setAiSaving]=useState(false)

  const load=async()=>{
    const {data}=await client.get('/api/settings'); setData(data)
    const {data:wfc}=await client.get('/api/ai/config'); setWfConfig(wfc.overrides||wfc||{})
    const {data:status}=await client.get('/api/settings/ai/status'); setAiStatus(status)
    // Prefill AI form from settings
    if(data?.ai){
      setAiForm({
        base_url: data.ai.base_url || status?.user_config?.base_url || 'https://api.openai.com/v1',
        model: data.ai.model || status?.user_config?.model || 'gpt-4o-mini',
        api_key: '',
        rpm: data.ai.rpm || status?.user_config?.rpm || 60
      })
    }
  }
  useEffect(()=>{ load() },[])

  const updateWf = (wf:string, key:string, value:string)=> setWfConfig({...wfConfig, [wf]: {...(wfConfig[wf]||{}), [key]: value}})
  const saveWf = async(wf:string)=>{
    setSaving(true); setMsg('')
    try{ await client.post('/api/ai/config', {[wf]: wfConfig[wf]||{}}); setMsg(`Saved workflow "${wf}" ✓`); setTimeout(()=>setMsg(''), 3000); load()} catch(e:any){ setMsg(apiErrorMessage(e)) } finally{ setSaving(false)}
  }

  const saveAiDefault = async()=>{
    setAiSaving(true); setMsg('')
    try{
      // Validate OpenAI compatible format
      if(!aiForm.base_url || !aiForm.base_url.startsWith('http')){
        setMsg('base_url must be a valid URL like https://api.openai.com/v1'); return
      }
      if(!aiForm.model || aiForm.model.length < 2){
        setMsg('model must be at least 2 chars like gpt-4o-mini'); return
      }
      const payload:any = {
        ai: {
          base_url: aiForm.base_url,
          model: aiForm.model,
          rpm: Number(aiForm.rpm)||60,
        }
      }
      if(aiForm.api_key && aiForm.api_key.trim()){
        payload.ai.api_key = aiForm.api_key.trim()
      }
      await client.put('/api/settings', payload)
      setMsg(`AI default saved ✓ ${aiForm.api_key?'• API key updated':''} — OpenAI compatible: ${aiForm.base_url} / ${aiForm.model}`)
      setAiForm({...aiForm, api_key:''})
      setTimeout(()=>setMsg(''), 4000)
      load()
    }catch(e:any){ setMsg(apiErrorMessage(e)) }finally{ setAiSaving(false)}
  }

  const save=async()=>{
    setSaving(true); setMsg('')
    try{
      const writable = (data._meta?.writable || {}) as Record<string, string[]>
      const payload: Record<string, Record<string, unknown>> = {}
      Object.entries(writable).forEach(([cat, keys])=>{
        if(cat==='ai') return // handled separately via saveAiDefault
        const values: Record<string, unknown> = {}
        keys.forEach((key: string)=>{ if(data[cat] && key in data[cat]) values[key] = data[cat][key] })
        if(Object.keys(values).length) payload[cat] = values
      })
      if(Object.keys(payload).length){
        await client.put('/api/settings', payload)
        setMsg('Saved ✓'); setTimeout(()=>setMsg(''), 2000)
      } else {
        setMsg('No changes to save (AI settings use separate save button)')
      }
    }catch(e){ setMsg(apiErrorMessage(e)) }finally{ setSaving(false)}
  }
  const update=(cat:string, key:string, value:any)=> setData({...data, [cat]: {...data[cat], [key]: value}})

  if(!data) return <div className="p-8 mono text-sm">Loading settings…</div>

  const isOwner = data.ai?.is_owner || aiStatus?.is_owner

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold flex items-center gap-2"><SettingsIcon className="w-5 h-5"/> Settings <span className="text-xs mono font-normal text-zinc-500">grouped • all editable • OpenAI compatible</span></h1>
        <button onClick={save} disabled={saving} className="px-4 py-2 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm inline-flex items-center gap-2 disabled:opacity-50"><Save className="w-4 h-4"/> {saving?'Saving…':'Save other sections'}</button>
      </div>
      {msg && <div className="text-sm mono p-3 rounded-xl bg-emerald-50 dark:bg-emerald-950 border border-emerald-200 dark:border-emerald-800 text-emerald-700 dark:text-emerald-300">{msg}</div>}

      <div className="grid lg:grid-cols-2 gap-4">
        {/* AI - Owner default OpenAI compatible */}
        <div className="card p-5 lg:col-span-2 border-2 border-blue-200 dark:border-blue-800 bg-gradient-to-br from-blue-50/50 to-violet-50/30 dark:from-blue-950/20 dark:to-violet-950/10">
          <h3 className="font-medium flex items-center gap-2"><Bot className="w-5 h-5"/> AI API — Default (OpenAI Compatible) {isOwner && <span className="text-[11px] px-2 py-0.5 rounded-full bg-amber-500 text-white">Owner • Global Default</span>}</h3>
          <p className="text-xs mono text-zinc-600 dark:text-zinc-400 mt-1">Owner account can add/modify default API details. OpenAI compatible format: works with OpenAI, Groq, Together, Anyscale, OpenRouter, Ollama, LM Studio, vLLM, etc. Per-workflow overrides below fallback to this default.</p>
          
          {aiStatus && (
            <div className={`mt-3 p-3 rounded-xl border text-xs mono flex flex-wrap items-center gap-2 ${aiStatus.online ? 'bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-800 dark:text-emerald-300' : 'bg-amber-50 border-amber-200 text-amber-700 dark:bg-amber-950 dark:border-amber-800 dark:text-amber-300'}`}>
              {aiStatus.online ? <CheckCircle className="w-4 h-4"/> : <AlertTriangle className="w-4 h-4"/>}
              <span>
                {aiStatus.online
                  ? `AI online • ${aiStatus.latency_ms}ms • ${aiStatus.model}`
                  : `AI offline • ${aiStatus.reason || 'no_api_key'}${aiStatus.detail ? ` — ${aiStatus.detail}` : ''}`}
              </span>
              <span className="opacity-70">{aiStatus.online && aiStatus.probe === 'chat_completions' ? '• /models unavailable, chat verified working' : ''}</span>
              <span className="ml-auto flex items-center gap-2">
                {aiStatus.key_preview && <span className="px-2 py-0.5 rounded-full bg-white/60 dark:bg-zinc-800/60" title={`API key currently in use (${aiStatus.key_source || 'env'})`}>key {aiStatus.key_preview} · {aiStatus.key_source || 'env'}</span>}
                <span>Configured: {String(aiStatus.configured)} • RPM {aiStatus.remaining}/{aiStatus.rpm}</span>
              </span>
            </div>
          )}

          {aiStatus?.stored_key_error && (
            <div className="mt-2 p-3 rounded-xl border border-red-300 bg-red-50 dark:bg-red-950 dark:border-red-800 text-red-700 dark:text-red-300 text-xs mono">
              Your saved API key can&apos;t be decrypted on this server (its ENCRYPTION_KEY changed since the key was saved).
              The app is currently using a fallback key — re-enter your key below and click <strong>Save AI Default</strong> to fix it.
            </div>
          )}

          <div className="mt-4 grid md:grid-cols-3 gap-3">
            <div className="md:col-span-2">
              <label className="text-xs mono font-medium flex items-center gap-1"><Globe className="w-3 h-3"/> Base URL — OpenAI compatible endpoint</label>
              <input value={aiForm.base_url} onChange={e=>setAiForm({...aiForm, base_url:e.target.value})} placeholder="https://api.openai.com/v1" className="w-full mt-1 border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700"/>
              <div className="text-[11px] mono text-zinc-500 mt-1">Examples: https://api.openai.com/v1, https://api.groq.com/openai/v1, https://api.together.xyz/v1, http://localhost:11434/v1 (Ollama), https://openrouter.ai/api/v1</div>
            </div>
            <div>
              <label className="text-xs mono font-medium flex items-center gap-1"><Cpu className="w-3 h-3"/> Model</label>
              <input value={aiForm.model} onChange={e=>setAiForm({...aiForm, model:e.target.value})} placeholder="gpt-4o-mini" className="w-full mt-1 border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700"/>
              <div className="text-[11px] mono text-zinc-500 mt-1">e.g. gpt-4o-mini, gpt-4o, gpt-3.5-turbo, llama-3.1-70b, mixtral-8x7b, claude-3-sonnet</div>
            </div>
          </div>

          <div className="mt-3 grid md:grid-cols-[1fr_auto] gap-3 items-end">
            <div>
              <label className="text-xs mono font-medium flex items-center gap-1"><Key className="w-3 h-3"/> API Key — encrypted at rest (per-user HKDF), never logged</label>
              <div className="relative mt-1">
                <input value={aiForm.api_key} onChange={e=>setAiForm({...aiForm, api_key:e.target.value})} type={showKey ? "text" : "password"} placeholder={data.ai.api_key_set ? "•••••••••••••••• (set) — leave blank to keep, enter new to replace" : "sk-... or gsk_... or custom key"} className="w-full border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700 pr-10"/>
                <button onClick={()=>setShowKey(!showKey)} className="absolute right-2 top-1/2 -translate-y-1/2 p-1.5 rounded-full hover:bg-zinc-100 dark:hover:bg-zinc-800">{showKey ? <EyeOff className="w-4 h-4"/> : <Eye className="w-4 h-4"/>}</button>
              </div>
              <div className="text-[11px] mono text-zinc-500 mt-1 flex items-center gap-2"><Shield className="w-3 h-3"/> Stored encrypted with per-user HKDF • {data.ai.api_key_set ? `Key set: ${data.ai.api_key_masked||'***'}` : 'No key set — AI will use heuristics fallback'} • Owner key is global fallback for members without their own key</div>
            </div>
            <div className="space-y-2">
              <div><label className="text-xs mono">RPM</label><input type="number" value={aiForm.rpm} onChange={e=>setAiForm({...aiForm, rpm: Number(e.target.value)})} className="w-full mt-1 border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700"/></div>
              <button onClick={saveAiDefault} disabled={aiSaving} className="w-full px-4 py-2.5 rounded-full bg-blue-600 text-white text-sm font-medium inline-flex items-center justify-center gap-2 disabled:opacity-50"><Save className="w-4 h-4"/> {aiSaving?'Saving…':'Save AI Default'}</button>
            </div>
          </div>

          <div className="mt-4 p-3 bg-white dark:bg-zinc-900 rounded-xl border dark:border-zinc-800">
            <div className="text-xs mono font-medium">How OpenAI compatible works</div>
            <div className="text-[11px] mono text-zinc-500 mt-1 leading-relaxed">
              <div>• Any provider exposing <code className="bg-zinc-100 dark:bg-zinc-800 px-1 rounded">/chat/completions</code> and <code>/models</code> works — OpenAI, Azure OpenAI, Groq, Together, Anyscale, OpenRouter, Perplexity, Ollama, LM Studio, vLLM, LocalAI.</div>
              <div>• Resolution order: per-workflow override → your default (this form) → owner default (global fallback) → env var AI_API_KEY</div>
              <div>• Owner: your key is global default for members without their own key. Members can override with their own key in same UI.</div>
              <div>• Security: key encrypted at rest via Fernet + HKDF per-user, never returned in full, never logged, masked as ***</div>
            </div>
          </div>

          <div className="mt-4">
            <div className="text-xs mono font-medium flex items-center gap-1"><Zap className="w-3.5 h-3.5"/> Per-workflow AI override <span className="text-zinc-400">(optional — falls back to default above)</span></div>
            <div className="mt-2 grid md:grid-cols-2 gap-2 max-h-64 overflow-auto">
              {WORKFLOWS.map(wf=>{
                const cfg = wfConfig[wf] || {}
                const active = !!(cfg.base_url || cfg.model || cfg.api_key)
                return (
                  <div key={wf} className="border dark:border-zinc-700 rounded-lg p-2 bg-white dark:bg-zinc-900">
                    <div className="flex items-center justify-between">
                      <span className="text-[11px] mono font-medium">{wf} {active && <span className="text-emerald-600">• override set</span>}</span>
                      <button onClick={()=>saveWf(wf)} disabled={saving} className="text-[11px] px-2 py-1 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 disabled:opacity-50">Save</button>
                    </div>
                    <div className="mt-1.5 grid grid-cols-3 gap-1.5">
                      <input value={cfg.base_url||''} onChange={e=>updateWf(wf,'base_url',e.target.value)} placeholder="Base URL (inherit if blank)" className="border rounded-lg px-2 py-1 text-[11px] mono bg-white dark:bg-zinc-800 dark:border-zinc-700"/>
                      <input value={cfg.model||''} onChange={e=>updateWf(wf,'model',e.target.value)} placeholder="Model" className="border rounded-lg px-2 py-1 text-[11px] mono bg-white dark:bg-zinc-800 dark:border-zinc-700"/>
                      <input value={cfg.api_key||''} onChange={e=>updateWf(wf,'api_key',e.target.value)} type="password" placeholder="API key" className="border rounded-lg px-2 py-1 text-[11px] mono bg-white dark:bg-zinc-800 dark:border-zinc-700"/>
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        </div>

        <div className="card p-5">
          <h3 className="font-medium flex items-center gap-2"><Search className="w-4 h-4"/> Scraping</h3>
          <div className="mt-3 space-y-3">
            <div><label className="text-xs mono">Keywords (comma-separated, extracted from resume + editable)</label><input value={Array.isArray(data.scraping.keywords) ? data.scraping.keywords.join(', ') : (data.scraping.keywords || '')} onChange={e=>update('scraping','keywords', e.target.value.split(',').map(s=>s.trim()).filter(Boolean))} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"/></div>
            <div><label className="text-xs mono">Job freshness (hours)</label><input type="number" value={data.scraping.freshness_hours} onChange={e=>update('scraping','freshness_hours', Number(e.target.value))} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"/></div>
            <div><label className="text-xs mono">Sources (clubbed)</label><div className="flex flex-wrap gap-1 mt-1">{(data.scraping.sources||[]).map((s:string)=> <span key={s} className="text-xs px-2 py-1 rounded-full bg-zinc-100 dark:bg-zinc-800 mono">{s}</span>)}</div></div>
            <label className="flex items-center gap-2 text-sm pt-1"><input type="checkbox" checked={!!data.scraping.live_enabled} onChange={e=>update('scraping','live_enabled', e.target.checked)}/> Live job sources (Arbeitnow + Remotive, keyless APIs)</label>
            <div className="text-[11px] mono text-zinc-500 leading-relaxed">When on, discovery pulls real postings from public job APIs on top of the curated pool (falls back gracefully offline).</div>
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
          <div className="mt-3 text-[11px] mono text-zinc-500">All settings are in categories — AI (OpenAI compatible default + per-workflow overrides), Scraping, General, Application, Email, Workflows — editable, with rate limiter, keywords, profile details, additional questions etc. Owner sets global default, members inherit or override.</div>
        </div>
      </div>
    </div>
  )
}
