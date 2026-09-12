import { useEffect, useState } from 'react'
import client, { apiError, aiOutage, guardrailFailure, downloadResume, type AIOutage } from '../api/client'
import { Upload, FileText, Wand2, Tag, Download, Brain, ShieldCheck, Sparkles, Layers, Check, Loader2, AlertTriangle, BadgeCheck, XCircle } from 'lucide-react'

type Guardrail = {
  passed?: boolean
  score?: number
  issues?: { code: string; field?: string; message?: string; severity?: string }[]
  repaired?: string[]
  checks?: string[]
  source?: string
}

/** Renders the AI-offline diagnosis the backend returns instead of a degraded result. */
function AIOutageBanner({ outage }: { outage: AIOutage }) {
  return (
    <div className="p-3 rounded-xl bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 text-sm">
      <div className="flex items-start gap-2">
        <XCircle className="w-4 h-4 text-red-600 dark:text-red-400 shrink-0 mt-0.5" />
        <div className="min-w-0">
          <div className="font-medium text-red-800 dark:text-red-200">AI unavailable — nothing was generated</div>
          <div className="text-xs mono mt-1 text-red-700 dark:text-red-300">
            {outage.message || 'The AI provider could not be reached.'}
            {outage.reason ? <span className="opacity-70"> ({outage.reason}{outage.workflow ? ` • ${outage.workflow}` : ''})</span> : null}
          </div>
          {outage.fix && <div className="text-xs mono mt-1 text-red-700 dark:text-red-300"><strong>Fix:</strong> {outage.fix}</div>}
          {outage.detail && <div className="text-[11px] mono mt-1 text-red-600 dark:text-red-400 break-all">{outage.detail}</div>}
          {(outage.base_url || outage.model) && (
            <div className="text-[11px] mono mt-1 text-red-600 dark:text-red-400">
              endpoint {outage.base_url || '—'} • model {outage.model || '—'}
            </div>
          )}
        </div>
      </div>
    </div>
  )
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
  const [notice, setNotice] = useState('')
  const [downloading, setDownloading] = useState('')

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
  }
  useEffect(()=>{ load() },[])

  /** Resume downloads go through a signed URL — a plain <a href> sends no token. */
  const download = async(id:number, format:'pdf'|'docx')=>{
    setDownloading(`${id}:${format}`); setNotice(''); setOutage(null)
    try{ await downloadResume(id, format) }
    catch(e:any){ setNotice(apiError(e, 'Download failed — sign in again and retry')) }
    finally{ setDownloading('') }
  }

  const upload = async(e:any)=>{
    const file=e.target.files[0]
    if(!file) return
    setOutage(null); setNotice('')
    const fd=new FormData(); fd.append('file', file)
    try{
      const {data} = await client.post('/api/resume/upload', fd)
      setNotice(`Extracted ${data.profile?.name || 'profile'} • ${(data.profile?.skills||[]).length} skills • ${(data.profile?.experience||[]).length} roles`)
      load()
    }catch(err:any){
      const o = aiOutage(err)
      if(o) setOutage(o); else setNotice(apiError(err, 'Upload failed'))
    }
    finally{ e.target.value = '' }
  }

  const generate = async()=>{
    if(!selectedJob) return setNotice('Pick a job')
    setGenerating(true); setOutage(null); setNotice(''); setPreview(null)
    try{
      const {data}=await client.post('/api/resumes/generate', null, {params:{job_id: selectedJob, strict_skeleton: strict, persona_id: personaId || undefined}})
      setPreview(data)
      load()
    }catch(e:any){
      const o = aiOutage(e)
      const g = guardrailFailure(e)
      if(o) setOutage(o)
      else if(g) setPreview({guardrail: {passed:false, issues:g.issues, checks:['guardrail']}, failed:true})
      else setNotice(apiError(e, 'Generation failed'))
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
      {notice && <div className="p-3 rounded-xl bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-800 text-sm mono text-blue-700 dark:text-blue-300">{notice}</div>}

      <div className="grid lg:grid-cols-3 gap-4">
        <div className="card p-5 space-y-4">
          <h3 className="font-medium flex items-center gap-2"><Upload className="w-4 h-4"/> Master resume</h3>
          <label className="block border-2 border-dashed dark:border-zinc-700 rounded-xl p-6 text-center cursor-pointer hover:bg-zinc-50 dark:hover:bg-zinc-800/50">
            <Upload className="w-6 h-6 mx-auto text-zinc-400"/>
            <div className="text-sm mono mt-2">Upload master resume (PDF/DOCX)</div>
            <div className="text-xs text-zinc-500">AI extracts profile + layout (margins, bullets, colors, hyperlinks…)</div>
            <input type="file" accept=".pdf,.docx,.doc" onChange={upload} className="hidden"/>
          </label>
          <div className="text-[11px] mono text-zinc-500 flex items-start gap-1">
            <ShieldCheck className="w-3.5 h-3.5 mt-0.5 shrink-0"/>
            Extraction is AI-only and guardrail-checked. If the model is offline you get the exact reason
            above — the system never falls back to a regex-guessed profile.
          </div>
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
              <select value={selectedJob} onChange={e=>setSelectedJob(e.target.value)} className="w-full border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700">
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
              <button onClick={generate} disabled={generating} className="w-full py-2 rounded-full bg-blue-600 text-white text-sm font-medium inline-flex items-center justify-center gap-2 disabled:opacity-50">
                {generating ? <Loader2 className="w-4 h-4 animate-spin"/> : <Sparkles className="w-4 h-4"/>} Generate with guardrails
              </button>
              <div className="text-[11px] mono text-zinc-500 flex items-start gap-1"><ShieldCheck className="w-3.5 h-3.5 mt-0.5 shrink-0"/> Schema + fact ledger + ATS quality checks. A draft that invents an employer, date or skill is sent back for repair, then rejected — never saved.</div>
              {preview && (
                <div className={`rounded-xl p-3 border ${preview.failed ? 'bg-red-50 dark:bg-red-950 border-red-200 dark:border-red-800' : 'bg-emerald-50 dark:bg-emerald-950 border-emerald-200 dark:border-emerald-800'}`}>
                  <div className="text-xs mono font-medium text-emerald-800 dark:text-emerald-200">
                    {preview.failed ? 'Rejected by the guardrail' : `Generated ${preview.display_name || ''}`}
                  </div>
                  {preview.tags && <div className="text-xs mono mt-1">Tags: {preview.tags.join(', ')}</div>}
                  {preview.reasoning && <div className="text-[11px] mono mt-1 opacity-80">{preview.reasoning}</div>}
                  <GuardrailPanel report={preview.guardrail || {}} />
                  {!preview.failed && (
                    <div className="mt-2 flex gap-2">
                      <button onClick={()=>download(preview.resume_id,'docx')} className="text-xs px-3 py-1 rounded-full bg-white dark:bg-zinc-900 border inline-flex items-center gap-1"><Download className="w-3 h-3"/> DOCX</button>
                      <button onClick={()=>download(preview.resume_id,'pdf')} className="text-xs px-3 py-1 rounded-full bg-white dark:bg-zinc-900 border inline-flex items-center gap-1"><Download className="w-3 h-3"/> PDF</button>
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
