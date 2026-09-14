import { useEffect, useRef, useState } from 'react'
import client, { apiError, aiOutage, guardrailFailure, downloadResume, AI_REQUEST_TIMEOUT_MS, type AIOutage } from '../api/client'
import { AIOutageBanner } from '../components/AIBanner'
import { useAIWork } from '../context/AIWorkContext'
import { WorkStatusLine } from '../components/AIWorkChip'
import { Upload, FileText, Wand2, Tag, Download, Brain, ShieldCheck, Sparkles, Layers, Check, Loader2, AlertTriangle, BadgeCheck, XCircle, Clock, Activity, Timer } from 'lucide-react'

type Guardrail = {
  passed?: boolean
  score?: number
  issues?: { code: string; field?: string; message?: string; severity?: string }[]
  repaired?: string[]
  checks?: string[]
  source?: string
}

type Progress = {
  step: string
  detail?: string
  progress?: number
  updated_at?: string
  error?: string
  resume_id?: number
  model?: string
  ai_meta?: any
}

const STEP_LABELS: Record<string, {label: string, desc: string}> = {
  idle: {label: 'Ready', desc: 'No upload in progress'},
  validating: {label: 'Validating', desc: 'Checking file type and magic bytes'},
  uploaded: {label: 'Uploaded', desc: 'File saved to server, checking size'},
  extracting: {label: 'Extracting', desc: 'Pulling text from PDF/DOCX (pdfminer + pypdf)'},
  layout: {label: 'Layout', desc: 'Detecting margins, fonts, bullets, colors'},
  ai_parsing: {label: 'AI parsing', desc: 'Calling AI to extract structured profile (this is the slow step)'},
  ai_done: {label: 'Validated', desc: 'AI JSON validated against schema + anti-hallucination guardrail'},
  saving: {label: 'Saving', desc: 'Writing resume + profile + search context'},
  context: {label: 'Context', desc: 'Building keyword context and persona'},
  done: {label: 'Done', desc: 'Profile ready'},
  failed: {label: 'Failed', desc: 'Something went wrong'},
  uploading: {label: 'Uploading', desc: 'Sending file to server'},
}

function GuardrailPanel({ report }: { report: Guardrail }) {
  if (!report || !report.checks) return null
  const issues = report.issues || []
  const errors = issues.filter(i => (i.severity || 'error') === 'error')
  return (
    <div className={`mt-2 rounded-xl border p-2.5 text-[11px] mono ${report.passed ? 'bg-emerald-50 dark:bg-emerald-950 border-emerald-200 dark:border-emerald-800 text-emerald-800 dark:text-emerald-200' : 'bg-red-50 dark:bg-red-950 border-red-200 dark:border-red-800 text-red-700 dark:text-red-300'}`}>
      <div className="flex items-center gap-1.5 font-medium">
        {report.passed ? <BadgeCheck className="w-3.5 h-3.5" /> : <AlertTriangle className="w-3.5 h-3.5" />}
        Guardrail {report.passed ? 'passed' : 'FAILED'} • score {report.score ?? '—'} • {report.source || 'ai'}
        {errors.length > 0 && <span>• {errors.length} violation(s)</span>}
      </div>
      <div className="mt-1 opacity-80">checks: {(report.checks || []).join(', ') || 'none'}</div>
      {issues.length > 0 && (
        <ul className="mt-1 space-y-0.5">
          {issues.slice(0, 6).map((i, idx) => (
            <li key={idx} className="truncate">• [{i.code}] {i.message}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

function ProgressCard({ progress, uploading, elapsed, ledger }: { progress: Progress | null, uploading: boolean, elapsed: number, ledger: any[] }) {
  if (!progress || progress.step === 'idle') return null
  const info = STEP_LABELS[progress.step] || {label: progress.step, desc: progress.detail || ''}
  const pct = Math.max(0, Math.min(100, progress.progress ?? (uploading ? 50 : 100)))
  const isFailed = progress.step === 'failed'
  const isDone = progress.step === 'done'
  return (
    <div className={`p-3 rounded-xl border text-sm ${isFailed ? 'bg-red-50 dark:bg-red-950 border-red-200 dark:border-red-800' : isDone ? 'bg-emerald-50 dark:bg-emerald-950 border-emerald-200 dark:border-emerald-800' : 'bg-blue-50 dark:bg-blue-950 border-blue-200 dark:border-blue-800'}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {uploading && !isFailed && !isDone ? <Loader2 className="w-4 h-4 animate-spin text-blue-600" /> : isDone ? <Check className="w-4 h-4 text-emerald-600" /> : isFailed ? <XCircle className="w-4 h-4 text-red-600" /> : <Activity className="w-4 h-4 text-blue-600" />}
          <span className={`font-medium mono ${isFailed ? 'text-red-800 dark:text-red-200' : isDone ? 'text-emerald-800 dark:text-emerald-200' : 'text-blue-800 dark:text-blue-200'}`}>{info.label}</span>
          <span className="text-xs mono opacity-60 flex items-center gap-1"><Timer className="w-3 h-3" />{elapsed}s</span>
        </div>
        <span className="text-[11px] mono opacity-70">{pct}%</span>
      </div>
      <div className="mt-1 text-xs mono opacity-80">{progress.detail || info.desc}</div>
      {progress.error && <div className="mt-1 text-[11px] mono text-red-600 break-all">{progress.error}</div>}
      <div className="mt-2 w-full bg-black/10 dark:bg-white/10 rounded-full h-1.5 overflow-hidden">
        <div className={`h-1.5 rounded-full transition-all duration-500 ${isFailed ? 'bg-red-500' : isDone ? 'bg-emerald-500' : 'bg-blue-500'}`} style={{width: `${pct}%`}} />
      </div>
      <div className="mt-2 flex flex-wrap gap-1 text-[10px] mono">
        {Object.keys(STEP_LABELS).filter(k=>k!=='idle').map(k=> (
          <span key={k} className={`px-1.5 py-0.5 rounded-full border ${progress.step===k ? (isFailed?'bg-red-500 text-white border-red-500': isDone && k==='done'?'bg-emerald-500 text-white border-emerald-500':'bg-blue-600 text-white border-blue-600') : pct>0 && (Object.keys(STEP_LABELS).indexOf(k) < Object.keys(STEP_LABELS).indexOf(progress.step)) ? 'bg-zinc-800 text-white dark:bg-white dark:text-zinc-900 border-zinc-800' : 'bg-white dark:bg-zinc-800 border-zinc-200 dark:border-zinc-700 opacity-60'}`}>{k}</span>
        ))}
      </div>
      {ledger.length > 0 && (
        <div className="mt-2 text-[11px] mono border-t dark:border-white/10 pt-2">
          <div className="font-medium flex items-center gap-1"><Clock className="w-3 h-3" /> Recent parse attempts</div>
          {ledger.slice(0,2).map((r:any)=> (
            <div key={r.id} className={`flex justify-between mt-1 p-1 rounded ${r.success ? 'bg-white dark:bg-zinc-900' : 'bg-red-100 dark:bg-red-900'}`}>
              <span>{r.workflow} • {r.model || '—'}</span>
              <span>{r.total_tokens} tokens • ${Number(r.cost_usd||0).toFixed(4)} • {r.success ? <span className="text-emerald-600">ok</span> : <span className="text-red-600">fail</span>}</span>
            </div>
          ))}
          {!ledger[0]?.success && ledger[0]?.error && <div className="mt-1 text-[10px] break-all opacity-70">{ledger[0].error.slice(0,200)}</div>}
        </div>
      )}
    </div>
  )
}

export default function Resumes(){
  const [resumes, setResumes] = useState<any[]>([])
  const [profile, setProfile] = useState<any>(null)
  const [layout, setLayout] = useState<any>(null)
  const [jobs, setJobs] = useState<any[]>([])
  const [personas, setPersonas] = useState<any[]>([])
  const [personaId, setPersonaId] = useState<number|''>('')
  const [selectedJob, setSelectedJob] = useState('')
  const [strict, setStrict] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [preview, setPreview] = useState<any>(null)
  const [editTags, setEditTags] = useState<Record<number,string>>({})
  const [outage, setOutage] = useState<AIOutage|null>(null)
  const [guardrailErr, setGuardrailErr] = useState<any>(null)
  const [notice, setNotice] = useState('')
  const [downloading, setDownloading] = useState('')
  const [uploading, setUploading] = useState(false)
  const [progress, setProgress] = useState<Progress|null>(null)
  const [ledger, setLedger] = useState<any[]>([])
  const [elapsed, setElapsed] = useState(0)
  const elapsedRef = useRef<number>(0)
  const progressTimerRef = useRef<any>(null)
  const elapsedTimerRef = useRef<any>(null)

  const load = async()=>{
    const {data}=await client.get('/api/resumes')
    setResumes(data)
    try{ const p=await client.get('/api/profile/current'); setProfile(p.data); setLayout(p.data.layout)}catch{}
    const j=await client.get('/api/jobs'); setJobs(j.data)
    try{
      const p=await client.get('/api/personas')
      setPersonas(p.data.personas||[])
      if(!personaId && p.data.active_id) setPersonaId(p.data.active_id)
    }catch{}
    // also refresh ledger for status strip
    try{
      const r = await client.get('/api/resume/recent-ledger', {params:{limit: 3}})
      setLedger(r.data || [])
    }catch{}
    try{
      const r = await client.get('/api/billing/credits', {params:{limit: 5}})
      // keep ledger in sync for parse view — billing ledger is the same source
    }catch{}
  }
  useEffect(()=>{ load() },[])

  /**
   * The live layer (v2.2.8): generation is queued, and the preview below the
   * button is *derived* from the queue row's result — resume id, tags,
   * guardrail report, or the fact-guard rejection — so it survives navigation
   * and F5 and the header chip shows "generating" on every route. Any
   * generate_resume row for the picked job is shown (same tab or not).
   */
  const work = useAIWork()
  const jobIdNum = selectedJob ? Number(selectedJob) : null
  const genRow = jobIdNum ? work.findRow({ key: `resume:${jobIdNum}`, pipeline: 'ai', jobId: jobIdNum, task: 'generate_resume' }) : null
  const genLive = !!genRow && ['queued','processing','paused'].includes(String(genRow.status))
  const genResult = genRow?.status === 'done' ? genRow.result : null
  const derivedPreview = genResult
    ? (genResult.status === 'rejected'
        ? { failed: true, error: genResult.message, guardrail: { passed: false, issues: genResult.issues || [], checks: genResult.checks || ['guardrail'], source: 'ai' } }
        : genResult)
    : null
  const shownPreview = derivedPreview || preview
  const genFinished = genRow && !genLive ? `${genRow.id}:${genRow.status}` : ''
  useEffect(()=>{ if(genFinished) load() // the new pending row must appear in the list
  // eslint-disable-next-line react-hooks/exhaustive-deps
  },[genFinished])

  // elapsed timer while uploading
  useEffect(()=>{
    if(!uploading){
      setElapsed(0)
      elapsedRef.current = 0
      if(elapsedTimerRef.current) clearInterval(elapsedTimerRef.current)
      return
    }
    const start = Date.now()
    elapsedTimerRef.current = setInterval(()=>{
      elapsedRef.current = Math.floor((Date.now()-start)/1000)
      setElapsed(elapsedRef.current)
    }, 500)
    return ()=> clearInterval(elapsedTimerRef.current)
  },[uploading])

  /** Resume downloads go through a signed URL — a plain <a href> sends no token. */
  const download = async(id:number, format:'pdf'|'docx')=>{
    setDownloading(`${id}:${format}`); setNotice(''); setOutage(null); setGuardrailErr(null)
    try{ await downloadResume(id, format) }
    catch(e:any){ setNotice(apiError(e, 'Download failed — sign in again and retry')) }
    finally{ setDownloading('') }
  }

  const upload = async(e:any)=>{
    const file=e.target.files[0]
    if(!file) return
    setOutage(null); setGuardrailErr(null); setNotice(''); setProgress({step:'uploading', detail:`Uploading ${file.name} (${(file.size/1024).toFixed(1)}KB)…`, progress: 2}); setUploading(true); setLedger([])
    // start polling progress + ledger
    const pollProgress = async()=>{
      try{
        const {data} = await client.get('/api/resume/progress')
        if(data && data.step && data.step!=='idle') setProgress(data)
      }catch{}
      try{
        const {data} = await client.get('/api/resume/recent-ledger', {params:{limit: 2}})
        if(Array.isArray(data)) setLedger(data)
      }catch{}
    }
    progressTimerRef.current = setInterval(pollProgress, 700)
    const fd=new FormData(); fd.append('file', file)
    try{
      const {data} = await client.post('/api/resume/upload', fd, {
        headers: {'Content-Type': 'multipart/form-data'},
        onUploadProgress: (evt:any)=>{
          if(evt.total){
            const pct = Math.round((evt.loaded/evt.total)*18) // upload is ~0-18%
            setProgress(prev=> ({...(prev||{step:'uploading'}), step:'uploading', detail:`Uploading ${file.name} — ${Math.round(evt.loaded/1024)}KB / ${Math.round(evt.total/1024)}KB`, progress: Math.max(prev?.progress||0, pct)} as Progress))
          }
        },
        timeout: AI_REQUEST_TIMEOUT_MS
      })
      // final progress will be polled, but set immediately to done
      setProgress({step:'done', detail:`Extracted ${data.profile?.name || 'profile'} • ${(data.profile?.skills||[]).length} skills • ${(data.profile?.experience||[]).length} roles`, progress:100, resume_id: data.resume?.id})
      setNotice(`✓ Extracted ${data.profile?.name || 'profile'} • ${(data.profile?.skills||[]).length} skills • ${(data.profile?.experience||[]).length} roles — resume #${data.resume?.id} saved. ${data.rescored_jobs ? `${data.rescored_jobs} jobs rescored.` : ''}`)
      await load()
      // keep done visible for 3s then idle
      setTimeout(()=> setProgress({step:'idle', progress:0}), 4000)
    }catch(err:any){
      const o = aiOutage(err)
      const g = guardrailFailure(err)
      // also check for 422 guardrail shape that may not be flagged as outage
      if(o){
        setOutage(o)
        setProgress({step:'failed', detail: o.message || 'AI unavailable', progress: 0, error: o.detail || o.fix || ''})
      } else if(g){
        setGuardrailErr(g)
        setProgress({step:'failed', detail: g.message || 'Guardrail rejected the extraction', progress:0, error: (g.issues||[]).map((i:any)=>i.message).join(' | ').slice(0,400)})
      } else {
        // Try to parse detail from 422/503 body directly
        const data = err?.response?.data
        if(data?.issues){
          setGuardrailErr({issues: data.issues, message: data.message || data.detail})
          setProgress({step:'failed', detail: data.message || 'Extraction failed guardrail', progress:0, error: JSON.stringify(data.issues).slice(0,300)})
        } else {
          const msg = apiError(err, 'Upload failed')
          setNotice(msg)
          setProgress({step:'failed', detail: msg, progress:0, error: data?.detail ? JSON.stringify(data.detail).slice(0,300) : ''})
        }
        // also fetch server progress which has richer detail
        try{
          const {data: prog} = await client.get('/api/resume/progress')
          if(prog && prog.step==='failed') setProgress(prog)
        }catch{}
      }
    }
    finally{
      clearInterval(progressTimerRef.current)
      setUploading(false)
      e.target.value = ''
      // final ledger refresh after short delay (ledger commit)
      setTimeout(async()=>{
        try{
          const {data} = await client.get('/api/resume/recent-ledger', {params:{limit: 3}})
          setLedger(data||[])
        }catch{}
      }, 800)
    }
  }

  const generate = async()=>{
    if(!selectedJob) return setNotice('Pick a job')
    setGenerating(true); setOutage(null); setGuardrailErr(null); setNotice(''); setPreview(null)
    try{
      const {data}=await client.post('/api/resumes/generate', null, {params:{job_id: selectedJob, strict_skeleton: strict, persona_id: personaId || undefined}})
      // Queued: keep the receipt, the row carries the outcome (fact guard included).
      if (data.pipeline_job_id) work.track({ id: data.pipeline_job_id, pipeline: 'ai', key: `resume:${selectedJob}` })
      if (data.duplicate) setNotice('Already generating for this job — showing its live status.')
    }catch(e:any){
      const o = aiOutage(e)
      const g = guardrailFailure(e)
      if(o) setOutage(o)
      else if(g) { setGuardrailErr(g); setPreview({guardrail: {passed:false, issues:g.issues, checks:['guardrail']}, failed:true, error: g.message}) }
      else {
        const data = (e as any)?.response?.data
        if(data?.issues) { setGuardrailErr({issues: data.issues, message: data.message}); setPreview({failed:true, guardrail:{passed:false, issues:data.issues}}) }
        else setNotice(apiError(e, 'Generation failed'))
      }
    }finally{ setGenerating(false)}
  }
  const saveTags = async(id:number)=>{
    await client.put(`/api/resumes/${id}/tags`, JSON.parse(editTags[id] || '[]'))
    load()
  }
  const approve = async(id:number)=>{
    await client.post(`/api/resumes/${id}/approve`)
    load()
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold tracking-tight flex items-center gap-2"><FileText className="w-5 h-5"/> Resume Studio</h1>

      {outage && <AIOutageBanner outage={outage} />}
      {guardrailErr && (
        <div className="p-3 rounded-xl bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 text-sm">
          <div className="flex gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
            <div className="min-w-0">
              <div className="font-medium text-amber-800 dark:text-amber-200">Guardrail rejected the AI result — nothing was saved</div>
              <div className="text-xs mono mt-1 text-amber-700 dark:text-amber-300">{guardrailErr.message || 'The model answered but violated accuracy checks.'}</div>
              {(guardrailErr.issues||[]).slice(0,4).map((i:any, idx:number)=> <div key={idx} className="text-[11px] mono mt-1 text-amber-700 dark:text-amber-300">• [{i.code}] {i.message}</div>)}
            </div>
          </div>
        </div>
      )}
      {notice && <div className={`p-3 rounded-xl border text-sm mono ${notice.startsWith('✓') ? 'bg-emerald-50 dark:bg-emerald-950 border-emerald-200 dark:border-emerald-800 text-emerald-700 dark:text-emerald-300' : 'bg-blue-50 dark:bg-blue-950 border-blue-200 dark:border-blue-800 text-blue-700 dark:text-blue-300'}`}>{notice}</div>}

      {(uploading || (progress && progress.step!=='idle')) && (
        <ProgressCard progress={progress} uploading={uploading} elapsed={elapsed} ledger={ledger} />
      )}

      <div className="grid lg:grid-cols-3 gap-4">
        <div className="card p-5 space-y-4">
          <h3 className="font-medium flex items-center gap-2"><Upload className="w-4 h-4"/> Master resume</h3>
          <label className={`block border-2 border-dashed rounded-xl p-6 text-center cursor-pointer hover:bg-zinc-50 dark:hover:bg-zinc-800/50 ${uploading ? 'opacity-50 pointer-events-none dark:border-zinc-700' : 'dark:border-zinc-700 border-zinc-300'}`}>
            {uploading ? <Loader2 className="w-6 h-6 mx-auto text-blue-500 animate-spin"/> : <Upload className="w-6 h-6 mx-auto text-zinc-400"/>}
            <div className="text-sm mono mt-2">{uploading ? 'Processing — see live status above' : 'Upload master resume (PDF/DOCX)'}</div>
            <div className="text-xs text-zinc-500">{uploading ? 'Heavy reasoning models can take several minutes — the live status above tracks progress. Don’t close the tab.' : 'AI extracts profile + layout (margins, bullets, colors, hyperlinks…)'}</div>
            <input type="file" accept=".pdf,.docx,.doc" onChange={upload} disabled={uploading} className="hidden"/>
          </label>
          <div className="text-[11px] mono text-zinc-500 flex items-start gap-1">
            <ShieldCheck className="w-3.5 h-3.5 mt-0.5 shrink-0"/>
            Extraction is AI-only and guardrail-checked. If the model is offline you get the exact reason
            above — the system never falls back to a regex-guessed profile.
          </div>
          {ledger.length>0 && !uploading && (
            <div className="text-[11px] mono p-2 rounded-lg bg-zinc-50 dark:bg-zinc-800 border dark:border-zinc-700">
              <div className="font-medium flex items-center gap-1"><Activity className="w-3 h-3"/> Last AI parse</div>
              <div className="flex justify-between mt-1"><span>{ledger[0].workflow} • {ledger[0].model}</span><span className={ledger[0].success?'text-emerald-600':'text-red-600'}>{ledger[0].success?'ok':'fail'} • {ledger[0].total_tokens} tokens • ${Number(ledger[0].cost_usd||0).toFixed(4)} • {ledger[0].latency_ms}ms</span></div>
              {!ledger[0].success && ledger[0].error && <div className="mt-1 break-all opacity-70">{ledger[0].error.slice(0,180)}</div>}
            </div>
          )}
          {profile && (
            <div className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3 space-y-2">
              <div className="text-sm font-medium">{profile.data.name} • {profile.data.email}</div>
              <div className="text-xs mono text-zinc-600 dark:text-zinc-400 line-clamp-3">{profile.data.summary?.slice(0,220)}</div>
              <div className="flex flex-wrap gap-1">{profile.data.skills?.slice(0,8).map((s:string)=> <span key={s} className="text-[11px] px-2 py-1 rounded-full bg-white dark:bg-zinc-900 border mono">{s}</span>)}</div>
              <details className="text-xs mono">
                <summary className="cursor-pointer text-zinc-500">Layout JSON (margins, bullets, colors…)</summary>
                <pre className="mt-2 bg-white dark:bg-zinc-900 border dark:border-zinc-700 rounded-lg p-2 overflow-auto max-h-40 text-[11px]">{JSON.stringify(layout, null, 2)}</pre>
              </details>
            </div>
          )}
          <div className="pt-2 border-t dark:border-zinc-800">
            <h4 className="text-sm font-medium flex items-center gap-2"><Wand2 className="w-4 h-4"/> Generate tailored resume</h4>
            <div className="mt-2 space-y-2">
              <select value={selectedJob} onChange={e=>setSelectedJob(e.target.value)} aria-label="Job to tailor the resume for" className="w-full border rounded-xl px-3 py-2 min-h-[44px] text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700 focus:outline-none focus:ring-2 focus:ring-blue-500">
                <option value="">Pick a job…</option>
                {jobs.map(j=> <option key={j.id} value={j.id}>{j.title} • {j.company} (score {j.score})</option>)}
              </select>
              {personas.length > 0 && (
                <select value={personaId} onChange={e=>setPersonaId(e.target.value ? Number(e.target.value) : '')} className="w-full border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700">
                  <option value="">Default track (persona)</option>
                  {personas.map((p:any)=> <option key={p.id} value={p.id}>{p.name} — {p.target_role || 'track'}</option>)}
                </select>
              )}
              <label className="flex items-center gap-2 text-xs mono"><input type="checkbox" checked={strict} onChange={e=>setStrict(e.target.checked)}/> Strictly maintain uploaded skeleton</label>
              <label className="flex items-center gap-2 text-xs mono"><input type="checkbox" checked={!strict} onChange={e=>setStrict(!e.target.checked)}/> AI-generated ATS-friendly format</label>
              <button onClick={generate} disabled={generating || genLive} className="w-full min-h-[44px] py-2 rounded-full bg-blue-600 text-white text-sm font-medium inline-flex items-center justify-center gap-2 disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-1">
                {generating || genLive ? <Loader2 className="w-4 h-4 animate-spin"/> : <Sparkles className="w-4 h-4"/>} {genLive ? 'Generating (fact guard)…' : 'Generate with guardrails'}
              </button>
              <WorkStatusLine row={genRow} prefix="Tailored resume" testId="resume-generate-status" onDismiss={()=> jobIdNum && work.untrack(`resume:${jobIdNum}`)} />
              <div className="text-[11px] mono text-zinc-500 flex items-start gap-1"><ShieldCheck className="w-3.5 h-3.5 mt-0.5 shrink-0"/> Schema + fact ledger + ATS quality checks. A draft that invents an employer, date or skill is sent back for repair, then rejected — never saved.</div>
              {shownPreview && (
                <div className={`rounded-xl p-3 border ${shownPreview.failed ? 'bg-red-50 dark:bg-red-950 border-red-200 dark:border-red-800' : 'bg-emerald-50 dark:bg-emerald-950 border-emerald-200 dark:border-emerald-800'}`}>
                  <div className={`text-xs mono font-medium ${shownPreview.failed ? 'text-red-800 dark:text-red-200' : 'text-emerald-800 dark:text-emerald-200'}`}>
                    {shownPreview.failed ? 'Rejected by the guardrail' : `Generated ${shownPreview.display_name || ''}`}
                  </div>
                  {shownPreview.error && <div className="text-[11px] mono mt-1 text-red-700 dark:text-red-300">{shownPreview.error}</div>}
                  {shownPreview.tags && <div className="text-xs mono mt-1">Tags: {shownPreview.tags.join(', ')}</div>}
                  {shownPreview.reasoning && <div className="text-[11px] mono mt-1 opacity-80">{shownPreview.reasoning}</div>}
                  <GuardrailPanel report={shownPreview.guardrail || {}} />
                  {!shownPreview.failed && shownPreview.resume_id && (
                    <div className="mt-2 flex gap-2">
                      <button onClick={()=>download(shownPreview.resume_id,'docx')} className="text-xs px-3 min-h-[44px] rounded-full bg-white dark:bg-zinc-900 border inline-flex items-center gap-1 focus:outline-none focus:ring-2 focus:ring-blue-500"><Download className="w-3 h-3"/> DOCX</button>
                      <button onClick={()=>download(shownPreview.resume_id,'pdf')} className="text-xs px-3 min-h-[44px] rounded-full bg-white dark:bg-zinc-900 border inline-flex items-center gap-1 focus:outline-none focus:ring-2 focus:ring-blue-500"><Download className="w-3 h-3"/> PDF</button>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="lg:col-span-2 card p-5">
          <div className="flex items-center justify-between">
            <h3 className="font-medium flex items-center gap-2"><Layers className="w-4 h-4"/> All resumes • auto-tagged</h3>
            <span className="text-xs mono text-zinc-500">{resumes.length} files</span>
          </div>
          <div className="mt-3 grid md:grid-cols-2 gap-3">
            {resumes.map(r=> (
              <div key={r.id} className="border dark:border-zinc-800 rounded-xl p-3 bg-white dark:bg-zinc-900">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="text-sm font-medium truncate flex items-center gap-2" title={r.display_name || r.filename}>
                      <FileText className="w-4 h-4 text-zinc-400"/>{r.display_name || r.filename}
                    </div>
                    <div className="text-xs mono text-zinc-500">{r.type} • {new Date(r.created_at).toLocaleDateString()} • {r.tags?.join(', ') || 'no tags'}</div>
                  </div>
                  <span className={`text-[11px] px-2 py-1 rounded-full mono border ${r.type==='master'?'bg-zinc-900 text-white dark:bg-white dark:text-zinc-900': 'bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300'}`}>{r.type}</span>
                  {r.type==='generated' && (
                    <span className={`text-[11px] px-2 py-1 rounded-full mono border ${r.status==='approved'?'bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-800':'bg-amber-50 border-amber-200 text-amber-700 dark:bg-amber-950 dark:border-amber-800'}`}>
                      {r.status==='approved' ? 'approved' : 'needs approval'}
                    </span>
                  )}
                </div>
                <div className="mt-2 flex flex-wrap gap-1">
                  {(r.tags||[]).map((t:string)=> <span key={t} className="text-[11px] px-2 py-1 rounded-full bg-zinc-100 dark:bg-zinc-800 mono inline-flex items-center gap-1"><Tag className="w-3 h-3"/>{t}</span>)}
                </div>
                {r.type==='generated' && r.status!=='approved' && (
                  <button onClick={()=>approve(r.id)} className="mt-3 w-full py-1.5 rounded-full bg-emerald-600 text-white text-xs font-medium inline-flex items-center justify-center gap-1"><Check className="w-3 h-3"/> Approve for use in applications</button>
                )}
                <div className="mt-3 flex gap-2">
                  <button onClick={()=>download(r.id,'docx')} disabled={downloading===`${r.id}:docx`} className="flex-1 py-1.5 rounded-full border dark:border-zinc-700 text-xs text-center inline-flex items-center justify-center gap-1 disabled:opacity-50">
                    {downloading===`${r.id}:docx` ? <Loader2 className="w-3 h-3 animate-spin"/> : <Download className="w-3 h-3"/>} DOCX
                  </button>
                  <button onClick={()=>download(r.id,'pdf')} disabled={downloading===`${r.id}:pdf`} className="flex-1 py-1.5 rounded-full border dark:border-zinc-700 text-xs text-center inline-flex items-center justify-center gap-1 disabled:opacity-50">
                    {downloading===`${r.id}:pdf` ? <Loader2 className="w-3 h-3 animate-spin"/> : <Download className="w-3 h-3"/>} PDF
                  </button>
                </div>
                <div className="mt-2 flex gap-1">
                  <input placeholder='["backend","python"]' value={editTags[r.id] ?? JSON.stringify(r.tags||[])} onChange={e=>setEditTags({...editTags,[r.id]:e.target.value})} className="flex-1 border rounded-full px-2 py-1 text-xs mono bg-white dark:bg-zinc-800 dark:border-zinc-700"/>
                  <button onClick={()=>saveTags(r.id)} className="px-2 py-1 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-xs"><Check className="w-3 h-3"/></button>
                </div>
                <div className="mt-2 text-[11px] mono text-zinc-500">Downloads are authorised with a short-lived signed link and saved under a professional file name.</div>
              </div>
            ))}
          </div>
          {resumes.length===0 && <div className="py-12 text-center mono text-sm text-zinc-500">No resumes yet. Upload master to start.</div>}
        </div>
      </div>

      <div className="card p-5">
        <h3 className="font-medium flex items-center gap-2"><Brain className="w-4 h-4"/> How resume decisions work</h3>
        <div className="mt-3 grid md:grid-cols-3 gap-3 text-sm">
          <div className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3"><div className="font-medium mono text-xs">Scoring</div><div className="text-xs text-zinc-600 dark:text-zinc-400 mt-1">AI scores against a weighted rubric (skills 34%, experience 24%, seniority 14%, domain 12%, location 8%, education 8%) and every claimed strength must exist in your profile. Bulk discovery shows a labelled preliminary estimate until the AI verdict lands.</div></div>
          <div className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3"><div className="font-medium mono text-xs">Guardrail</div><div className="text-xs text-zinc-600 dark:text-zinc-400 mt-1">Schema + fact ledger + ATS quality checks run after the model answers. Violations are sent back for one repair pass; a resume that still invents employers, dates or skills is rejected, never saved.</div></div>
          <div className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3"><div className="font-medium mono text-xs">Approval</div><div className="text-xs text-zinc-600 dark:text-zinc-400 mt-1">Before upload, you see preview → approve / download / upload polished version → system uses that. All generated resumes auto-tagged and bound to a persona.</div></div>
        </div>
      </div>
    </div>
  )
}
