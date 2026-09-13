import { useEffect, useRef, useState } from 'react'
import client, { apiError as apiErrorMessage } from '../api/client'
import { Settings as SettingsIcon, Save, Bot, Search, Sliders, Mail, Shield, Zap, TrendingUp, Eye, EyeOff, CheckCircle, AlertTriangle, Globe, Key, Cpu, CalendarClock } from 'lucide-react'
import { Link } from 'react-router-dom'
import { AIStatusBanner } from '../components/AIBanner'
import { AUTO_BLOCKERS, AUTO_WORKFLOW_LABELS, autoStateCopy, fmtCadence, fmtWhen, type AutoOverview } from '../lib/automation'

const WORKFLOWS = ['parse','keyword_extract','scoring','resume_gen','classify','email_gen','form_detect','funding_scan','tagging','interview','company_intel']

/** How often the Auto-mode card re-reads its schedule/history while it is on screen. */
const AUTOMATION_POLL_MS = 30_000

export default function Settings(){
  const [data, setData]=useState<any>(null)
  const [wfConfig, setWfConfig]=useState<any>({})
  const [saving, setSaving]=useState(false)
  const [msg, setMsg]=useState('')
  const [showKey, setShowKey]=useState(false)
  const [aiForm, setAiForm]=useState({base_url:'', model:'', api_key:'', rpm:60, timeout:300, max_input_tokens:12000, max_output_tokens:16000})
  const [aiStatus, setAiStatus]=useState<any>(null)
  const [aiSaving, setAiSaving]=useState(false)
  const [extracted, setExtracted]=useState<any>(null)
  // Auto mode (v2.2): the schedule is the backend's, never recomputed here.
  const [auto, setAuto]=useState<AutoOverview|null>(null)
  const [autoBusy, setAutoBusy]=useState(false)
  const [autoNote, setAutoNote]=useState('')
  const mounted=useRef(true)

  const loadAuto=async()=>{
    try{
      const {data}=await client.get('/api/automation')
      if(mounted.current) setAuto(data as AutoOverview)
    }catch{
      // A failed poll must not blank the card: keep the last read.
    }
  }

  /**
   * Flipping auto mode goes through the same PUT /api/settings the rest of this
   * page uses — one settings contract, one tier gate. A 403 `upgrade_required`
   * carries a human message, so it is shown as the message (never an error code,
   * never a checkbox that silently snaps back), and the switch is re-read either
   * way so what is on screen is what the server stored.
   */
  const setAutoMode=async(on:boolean)=>{
    setAutoBusy(true); setAutoNote('')
    try{
      await client.put('/api/settings', { automation: { auto_mode: on } })
    }catch(e:any){
      const detail=e?.response?.data?.detail
      setAutoNote(detail && typeof detail==='object' && detail.message ? String(detail.message) : apiErrorMessage(e))
    }
    await load()
    setAutoBusy(false)
  }

  const load=async()=>{
    const {data}=await client.get('/api/settings'); setData(data)
    const {data:wfc}=await client.get('/api/ai/config'); setWfConfig(wfc.overrides||wfc||{})
    const {data:status}=await client.get('/api/settings/ai/status'); setAiStatus(status)
    try{ const {data:ctx}=await client.get('/api/context/keywords'); setExtracted(ctx) }catch{ setExtracted(null) }
    await loadAuto()
    // Prefill AI form from settings
    if(data?.ai){
      setAiForm({
        base_url: data.ai.base_url || status?.user_config?.base_url || 'https://api.openai.com/v1',
        model: data.ai.model || status?.user_config?.model || 'gpt-4o-mini',
        api_key: '',
        rpm: data.ai.rpm || status?.user_config?.rpm || 60,
        timeout: data.ai.timeout || 300,
        max_input_tokens: data.ai.max_input_tokens || 12000,
        max_output_tokens: data.ai.max_output_tokens || 16000
      })
    }
  }
  useEffect(()=>{ load() },[])

  // While this page is open the schedule is live — last/next run, the quota and
  // the AI banner refresh on a 30s timer, cleared on unmount so a background
  // tab never polls (same interval the Jobs banner uses).
  useEffect(()=>{
    mounted.current = true
    const t = setInterval(()=>{
      void loadAuto()
      void client.get('/api/settings/ai/status').then(r=>{ if(mounted.current) setAiStatus(r.data) }).catch(()=>undefined)
    }, AUTOMATION_POLL_MS)
    return ()=>{ mounted.current = false; clearInterval(t) }
  },[])

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
      const timeoutSecs = Number(aiForm.timeout) || 300
      if (timeoutSecs < 5 || timeoutSecs > 1800) {
        setMsg('timeout must be between 5 and 1800 seconds — heavy reasoning models need minutes'); return
      }
      const payload:any = {
        ai: {
          base_url: aiForm.base_url,
          model: aiForm.model,
          rpm: Number(aiForm.rpm)||60,
          timeout: timeoutSecs,
          max_input_tokens: Number(aiForm.max_input_tokens) || 12000,
          max_output_tokens: Number(aiForm.max_output_tokens) || 16000,
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
  // Auto mode: the switch's editability and the cadence table both come from
  // GET /api/settings (`automation.can_use/cadence/locked_reason`) — the same
  // predicates the PUT gate and the sweep use, so the card can never advertise a
  // schedule the server would refuse.
  const canAuto = !!data.automation?.can_use
  const autoCadence = (data.automation?.cadence || {}) as Record<string, number>

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
              <div className="text-[11px] mono text-zinc-500 mt-1 flex items-center gap-2"><Shield className="w-3 h-3"/> Stored encrypted with per-user HKDF • {data.ai.api_key_set ? `Key set: ${data.ai.api_key_masked||'***'}` : 'No key set — AI features are blocked until a key is configured'} • Owner key is the global default for members without their own key</div>
            </div>
            <div className="space-y-2">
              <div className="grid grid-cols-2 gap-2">
                <div><label className="text-xs mono">RPM</label><input type="number" value={aiForm.rpm} onChange={e=>setAiForm({...aiForm, rpm: Number(e.target.value)})} className="w-full mt-1 border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700"/></div>
                <div><label className="text-xs mono" title="How long to wait for the model to finish generating, per request. Heavy reasoning models need minutes.">Timeout (s)</label><input type="number" min={5} max={1800} value={aiForm.timeout} onChange={e=>setAiForm({...aiForm, timeout: Number(e.target.value)})} className="w-full mt-1 border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700"/></div>
              </div>
              <div className="text-[11px] mono text-zinc-500">Timeout: how long to wait for the model per request (5–1800s). Reasoning models on large tasks need minutes — raise this if slow models time out.</div>
              <div className="mt-2 grid grid-cols-2 gap-2">
                <div>
                  <label className="text-xs mono">Max input tokens</label>
                  <input type="number" min={256} max={200000} value={aiForm.max_input_tokens} disabled={!data.ai?.token_limits_editable} onChange={e=>setAiForm({...aiForm, max_input_tokens: Number(e.target.value)})} className="w-full mt-1 border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700 disabled:opacity-50"/>
                  <div className="text-[11px] mono text-zinc-500 mt-1">
                    {data.ai?.token_limits_editable
                      ? 'Cap on prompt length (resume + JD + context) sent to the model, in tokens (256–200000). Larger prompts are clamped and the truncation is flagged in the record.'
                      : 'Locked on the free tier — upgrade to edit. Default 12000.'}
                  </div>
                </div>
                <div>
                  <label className="text-xs mono">Max output tokens</label>
                  <input type="number" min={64} max={16000} value={aiForm.max_output_tokens} disabled={!data.ai?.token_limits_editable} onChange={e=>setAiForm({...aiForm, max_output_tokens: Number(e.target.value)})} className="w-full mt-1 border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700 disabled:opacity-50"/>
                  <div className="text-[11px] mono text-zinc-500 mt-1">
                    {data.ai?.token_limits_editable
                      ? 'Cap on how much the model may generate per request, in tokens (64–16000). A failed attempt that is retried can never exceed this.'
                      : 'Locked on the free tier — upgrade to edit. Default 16000.'}
                  </div>
                </div>
              </div>
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
            <div>
              <label className="text-xs mono font-medium">Extracted from your resume (AI, read-only)</label>
              <div className="mt-1 min-h-[38px] border rounded-xl px-3 py-2 bg-zinc-50 dark:bg-zinc-800 dark:border-zinc-700 flex flex-wrap gap-1">
                {(extracted?.keywords || []).length === 0
                  ? <span className="text-xs text-zinc-500 mono">Nothing yet — upload a master resume and the AI extracts these automatically.</span>
                  : (extracted.keywords || []).map((k:string)=> <span key={k} className="text-[11px] px-2 py-0.5 rounded-full bg-white dark:bg-zinc-900 border dark:border-zinc-700 mono">{k}</span>)}
              </div>
              <div className="text-[11px] mono text-zinc-500 mt-1">
                Source: {extracted?.source ? (extracted.source === 'ai' || String(extracted.source).startsWith('ai') ? 'AI extraction (merged with profile data)' : `${extracted.source} (deterministic profile miner — no AI merge yet)`) : 'unknown'}
                {extracted?.profile_id ? '' : ' • no resume uploaded yet'}
              </div>
            </div>
            <div>
              <label className="text-xs mono">Extra search keywords (comma-separated, optional)</label>
              <input value={Array.isArray(data.scraping.keywords) ? data.scraping.keywords.join(', ') : (data.scraping.keywords || '')} onChange={e=>update('scraping','keywords', e.target.value.split(',').map(s=>s.trim()).filter(Boolean))} placeholder="empty by default — add terms your resume does not cover" className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"/>
              <div className="text-[11px] mono text-zinc-500 mt-1">Added <em>on top of</em> the extracted keywords. This starts empty on purpose — nothing generic is prefilled.</div>
            </div>
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
          <div className="mt-3 text-[11px] mono text-zinc-500">All settings are in categories — AI (OpenAI compatible default + per-workflow overrides), Scraping, General, Application, Email, Workflows, Automation — editable, with rate limiter, keywords and profile details. Owner sets global default, members inherit or override.</div>
        </div>

        {/* Auto mode — scheduled work (v2.2). Switch, cadence, clocks, last runs. */}
        <div className="card p-5 lg:col-span-2">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h3 className="font-medium flex items-center gap-2">
                <CalendarClock className="w-4 h-4" /> Auto mode
                {auto && (
                  <span className={`text-[11px] mono px-2 py-0.5 rounded-full border ${!auto.enabled ? 'bg-zinc-100 border-zinc-300 text-zinc-600' : auto.active ? 'bg-emerald-50 border-emerald-200 text-emerald-700' : 'bg-amber-50 border-amber-200 text-amber-800'}`}>
                    {!auto.enabled ? 'off' : auto.active ? 'scheduled' : 'on — not queueing right now'}
                  </span>
                )}
              </h3>
              <p className="text-xs mono text-zinc-500 mt-1 max-w-3xl leading-relaxed">
                Job discovery, the funding radar and application prep run on a cadence instead of on a click. Auto mode only decides <em>when to queue</em>: the queue, its retries, the AI-outage pause and your plan limits are exactly the ones a manual run goes through — and nothing is ever submitted to an employer without you.
              </p>
            </div>
            <label className={`shrink-0 flex items-center gap-2 text-sm p-3 rounded-xl border ${canAuto ? 'bg-white dark:bg-zinc-900 cursor-pointer' : 'bg-zinc-50 dark:bg-zinc-800'}`} title={canAuto ? 'Starts work on the cadence below' : (data.automation?.locked_reason || 'Locked on your plan')}>
              <input type="checkbox" checked={!!data.automation?.auto_mode} disabled={!canAuto || autoBusy} onChange={e=>void setAutoMode(e.target.checked)} />
              <span className="mono text-xs">{autoBusy ? 'Saving…' : canAuto ? (data.automation?.auto_mode ? 'On' : 'Off') : 'Locked'}</span>
            </label>
          </div>

          {autoNote && (
            <div role="alert" className="mt-3 p-3 rounded-xl border border-red-300 bg-red-50 dark:bg-red-950 dark:border-red-800 text-red-700 dark:text-red-300 text-xs mono">{autoNote}</div>
          )}
          {!canAuto && (
            <div className="mt-2 text-[11px] mono text-zinc-500">
              Locked on the free tier — <Link to="/billing" className="underline font-medium">upgrade to Pro</Link> for scheduled runs (discovery every 12 h, funding radar every 24 h).
            </div>
          )}
          {auto && canAuto && !auto.consent_ok && (
            <div className="mt-2 p-3 rounded-xl border border-amber-200 bg-amber-50 dark:bg-amber-950 dark:border-amber-800 text-amber-800 dark:text-amber-200 text-xs mono">
              The automation disclosure has not been accepted, so nothing is queued even with the switch on. Accept it in <Link to="/account" className="underline font-medium">Account → Consent</Link>.
            </div>
          )}
          <AIStatusBanner status={aiStatus} />
          {auto && auto.enabled && !auto.active && auto.blocking?.length ? (
            <div className="mt-2 text-[11px] mono text-amber-700 dark:text-amber-300">
              Not queueing right now: {auto.blocking.map(code=>AUTO_BLOCKERS[code] || code).join(' · ')}.
            </div>
          ) : null}
          {auto && auto.enabled && !auto.schedulable && (
            <div className="mt-2 text-[11px] mono text-zinc-500">The scheduler is disabled on this server (AUTO_SCHEDULER_INTERVAL_SECONDS=0), so no sweep runs.</div>
          )}

          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-left text-xs mono">
              <thead className="text-zinc-500">
                <tr>
                  <th className="py-1 pr-3 font-medium">Workflow</th>
                  <th className="py-1 pr-3 font-medium">Cadence</th>
                  <th className="py-1 pr-3 font-medium">Last run</th>
                  <th className="py-1 font-medium">Next</th>
                </tr>
              </thead>
              <tbody>
                {Object.keys(AUTO_WORKFLOW_LABELS).map(wf=>{
                  const secs = autoCadence[wf]
                  const last = auto?.last_run?.[wf] || null
                  const next = auto?.next_run?.[wf] ?? null
                  const chip = autoStateCopy(last?.state)
                  const toggleOff = !!auto && wf in (auto.toggles || {}) && auto.toggles[wf] === false
                  return (
                    <tr key={wf} className="border-t dark:border-zinc-800 align-top">
                      <td className="py-1.5 pr-3">
                        {AUTO_WORKFLOW_LABELS[wf]}
                        {toggleOff ? <div className="text-[10px] text-zinc-500">its AI-workflow toggle is off — never scheduled</div> : null}
                      </td>
                      <td className="py-1.5 pr-3">{fmtCadence(secs)}</td>
                      <td className="py-1.5 pr-3">
                        {last ? (
                          <div className="flex flex-col gap-0.5">
                            <span className={`self-start text-[10px] px-1.5 py-0.5 rounded-full border ${chip?.cls || 'bg-zinc-100 border-zinc-300 text-zinc-700'}`}>{chip?.label || String(last.state)}</span>
                            <span className="text-[10px] text-zinc-500">{fmtWhen(last.at)}</span>
                            {last.reason ? <span className="text-[10px] text-zinc-500 break-words">{String(last.reason).slice(0,120)}</span> : null}
                          </div>
                        ) : <span className="text-zinc-500">never run</span>}
                      </td>
                      <td className="py-1.5">
                        {/* `next_run: null` = no next run to promise. Never a date:
                            off → "—", on but blocked → "blocked", on and runnable
                            (so a run is in flight right now) → "running now". */}
                        {next
                          ? (new Date(next).getTime() <= Date.now() ? <span title={fmtWhen(next)}>due — starts at the next sweep</span> : fmtWhen(next))
                          : <span className="text-zinc-500">{!auto?.enabled ? '—' : auto.active ? 'running now — next set when it finishes' : 'blocked — see above'}</span>}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <div className="mt-1 text-[10px] mono text-zinc-500">
            “Next” is the first moment the sweep may start that run — the loop wakes every {Math.max(1, Math.round((auto?.sweep_interval_seconds ?? 300) / 60))} min, so a run can start up to that much later. No time is promised while a run is in flight or auto mode is blocked. This card re-reads the schedule every {AUTOMATION_POLL_MS / 1000}s while open.
          </div>

          {auto?.quota && (
            <div className="mt-3 text-[11px] mono text-zinc-500">
              Automation runs this month: <strong className="text-inherit">{auto.quota.used}{auto.quota.limit ? `/${auto.quota.limit}` : ''}</strong>
              {auto.quota.limit ? ` · ${auto.quota.remaining} left` : ' · no cap on this plan'}
              {' '}— charged when an auto run finishes, and it resets with the calendar month.
            </div>
          )}

          <div className="mt-4">
            <div className="text-xs mono font-medium">Recent scheduled runs</div>
            {!auto || !auto.recent?.length ? (
              <div className="mt-1 text-[11px] mono text-zinc-500">
                Nothing scheduled yet{auto?.enabled ? ' — the next sweep starts within a few minutes and appears here.' : ' — turn auto mode on to start.'}
              </div>
            ) : (
              <ul className="mt-1 space-y-1">
                {auto.recent.slice(0,5).map((row, i)=>{
                  const chip = autoStateCopy(row.state)
                  return (
                    <li key={`${row.workflow}-${row.at}-${i}`} className="flex flex-wrap items-center gap-2 text-[11px] mono p-2 rounded-lg border dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-800/50">
                      <span className={`px-1.5 py-0.5 rounded-full border ${chip?.cls || 'bg-zinc-100 border-zinc-300 text-zinc-700'}`}>{chip?.label || String(row.state)}</span>
                      <span>{AUTO_WORKFLOW_LABELS[row.workflow || ''] || row.workflow || 'Auto run'}</span>
                      <span className="text-zinc-500">{fmtWhen(row.at)}</span>
                      {row.queue_id ? <Link to="/queues" className="underline text-zinc-600 dark:text-zinc-300">queue #{row.queue_id}</Link> : null}
                      {row.job_id ? <Link to="/jobs" className="underline text-zinc-600 dark:text-zinc-300">job #{row.job_id}</Link> : null}
                      {row.state !== 'done' && chip?.note ? <span className="text-zinc-500">{chip.note}</span> : null}
                    </li>
                  )
                })}
              </ul>
            )}
            <div className="mt-2 text-[11px] mono text-zinc-500 leading-relaxed">
              Skipped cycles are listed rather than hidden, and a skipped cycle does not reset the cadence — the work happens as soon as the reason clears. A run that failed or needs input is in <Link to="/queues" className="underline font-medium">Queues</Link>, where you can retry it like any other job.
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
