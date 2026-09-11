import { useEffect, useState } from 'react'
import client, { apiError } from '../api/client'
import { Search, Sparkles, ExternalLink, Award, Building2, Clock, Filter, Loader2, Wand2, CheckCircle, AlertCircle, Eye, Brain } from 'lucide-react'

export default function Jobs(){
  const [jobs, setJobs] = useState<any[]>([])
  const [filter, setFilter] = useState({status:'', source:'', q:''})
  const [selected, setSelected] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [discoverBusy, setDiscoverBusy] = useState(false)
  const [resumeChoice, setResumeChoice] = useState('auto')
  const [applyMsg, setApplyMsg] = useState('')
  const [discoverMsg, setDiscoverMsg] = useState('')
  const [generating, setGenerating] = useState(false)
  const [generateMsg, setGenerateMsg] = useState('')
  const [generatedLinks, setGeneratedLinks] = useState<any>(null)

  const load = ()=>{
    const p:any={}
    if(filter.status) p.status=filter.status
    if(filter.source) p.source=filter.source
    if(filter.q) p.q=filter.q
    client.get('/api/jobs',{params:p}).then(r=>setJobs(r.data))
    if(selected) client.get(`/api/jobs/${selected.id}`).then(r=>setSelected(r.data))
  }
  useEffect(()=>{ load() },[filter.status, filter.source])
  // debounced search
  useEffect(()=>{
    const id=setTimeout(load, 400)
    return ()=>clearTimeout(id)
  },[filter.q])

  const discover = async()=>{
    setDiscoverBusy(true)
    try{
      // DiscoveryRequest is a JSON body — every field is optional, but the
      // body itself is required, so `{}` is the correct "use my defaults" call.
      const {data} = await client.post('/api/jobs/discover', {})
      setDiscoverMsg(`${(data.keywords || []).slice(0, 8).join(' • ')} — extracted from your ${data.context_source === 'ai' ? 'profile by AI' : 'profile (heuristic)'}`)
      setTimeout(load, 2000)
      setTimeout(load, 4500)
    }finally{ setDiscoverBusy(false)}
  }
  const generateResume = async()=>{
    if(!selected) return
    setGenerating(true)
    try{
      const {data} = await client.post('/api/resumes/generate', null, {params:{job_id: selected.id}})
      setGenerateMsg('Tailored resume ready')
      setGeneratedLinks(data.files)
    }catch(e:any){ setGenerateMsg(apiError(e, 'Generation failed')) }
    finally{ setGenerating(false) }
  }
  const apply = async()=>{
    if(!selected) return
    setBusy(true); setApplyMsg('')
    try{
      // ApplyRequest is a JSON body, not query params.
      const {data}=await client.post(`/api/jobs/${selected.id}/apply`, {resume_choice: resumeChoice})
      if(data.status==='needs_input'){
        setApplyMsg('Needs your input — check Queues → User Input Needed')
      } else {
        const decision = data.resume_decision ? `• resume: ${data.resume_decision}` : ''
        setApplyMsg(`Status: ${data.status} ${decision} ${data.vault_created ? '• vault credential created' : ''}`)
      }
      load()
      const fresh = await client.get(`/api/jobs/${selected.id}`); setSelected(fresh.data)
    }catch(e:any){ setApplyMsg(apiError(e, 'Apply failed')) }
    finally{ setBusy(false)}
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-3 items-center justify-between">
        <h1 className="text-xl font-semibold tracking-tight flex items-center gap-2"><Building2 className="w-5 h-5"/> Job discovery</h1>
        <div className="flex gap-2">
          <button onClick={discover} disabled={discoverBusy} className="px-4 py-2 rounded-full bg-blue-600 text-white text-sm font-medium inline-flex items-center gap-2 disabled:opacity-50">
            {discoverBusy ? <Loader2 className="w-4 h-4 animate-spin"/> : <Search className="w-4 h-4"/>} Discover jobs
          </button>
        </div>
      </div>
      {discoverMsg && <div className="text-xs mono p-2 rounded-lg bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-800 text-blue-700 dark:text-blue-300">AI keywords: {discoverMsg}</div>}

      {/* filters */}
      <div className="card p-3 flex flex-wrap gap-2 items-center">
        <div className="flex items-center gap-2 text-xs mono text-zinc-500"><Filter className="w-3.5 h-3.5"/> Filter:</div>
        <select value={filter.status} onChange={e=>setFilter({...filter, status:e.target.value})} className="text-sm border rounded-full px-3 py-1.5 bg-white dark:bg-zinc-900 dark:border-zinc-700">
          <option value="">All status</option>
          <option value="discovered">Discovered</option>
          <option value="queued">Queued</option>
          <option value="needs_input">Needs Input</option>
          <option value="applying">Applying</option>
          <option value="applied">Applied</option>
          <option value="failed">Failed</option>
        </select>
        <select value={filter.source} onChange={e=>setFilter({...filter, source:e.target.value})} className="text-sm border rounded-full px-3 py-1.5 bg-white dark:bg-zinc-900 dark:border-zinc-700">
          <option value="">All sources</option>
          <option value="linkedin">LinkedIn</option>
          <option value="naukri">Naukri</option>
          <option value="indeed">Indeed</option>
          <option value="lever">Lever</option>
          <option value="greenhouse">Greenhouse</option>
          <option value="workday">Workday</option>
          <option value="instahyre">Instahyre</option>
        </select>
        <div className="relative ml-auto w-full sm:w-64">
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-zinc-400"/>
          <input value={filter.q} onChange={e=>setFilter({...filter,q:e.target.value})} placeholder="Search title, company…" className="w-full pl-9 pr-3 py-1.5 rounded-full border text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"/>
        </div>
      </div>

      <div className="grid lg:grid-cols-[1.1fr_0.9fr] gap-4">
        <div className="card overflow-hidden">
          <div className="p-3 border-b dark:border-zinc-800 flex items-center justify-between">
            <span className="text-sm font-medium mono">{jobs.length} jobs</span>
            <span className="text-xs mono text-zinc-500">Click any job → details</span>
          </div>
          <div className="max-h-[70vh] overflow-auto divide-y dark:divide-zinc-800">
            {jobs.length===0 ? <div className="p-8 text-center text-sm mono text-zinc-500">No jobs. Try “Discover jobs” — it uses extracted keywords + freshness, with AI form detection.</div> :
              jobs.map(j=> (
              <button key={j.id} onClick={async()=>{ const {data}=await client.get(`/api/jobs/${j.id}`); setSelected(data)}} className={`w-full text-left p-4 hover:bg-zinc-50 dark:hover:bg-zinc-800/50 flex gap-3 ${selected?.id===j.id ? 'bg-blue-50 dark:bg-blue-950/30' : ''}`}>
                <div className="w-10 h-10 rounded-xl bg-zinc-900 dark:bg-zinc-800 text-white flex items-center justify-center text-xs font-bold shrink-0">{j.company.slice(0,2).toUpperCase()}</div>
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium flex items-center gap-2 truncate">{j.title} <span className="text-[11px] px-1.5 py-0.5 rounded bg-zinc-100 dark:bg-zinc-800 mono">{j.source}</span></div>
                  <div className="text-xs text-zinc-500 truncate">{j.company} • {j.location} • <span className="mono">{j.company_size}</span></div>
                  <div className="text-xs mono text-zinc-500 mt-1 line-clamp-1">{j.url}</div>
                </div>
                <div className="shrink-0 flex flex-col items-end gap-1">
                  <span className={`text-[11px] px-2 py-1 rounded-full mono border ${j.score>=75?'bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-800 dark:text-emerald-300': j.score>=60?'bg-amber-50 border-amber-200 text-amber-700 dark:bg-amber-950':'bg-zinc-100 dark:bg-zinc-800'}`}>{j.score} score</span>
                  <span className={`text-[11px] px-2 py-1 rounded-full mono ${j.status==='applied'?'bg-emerald-600 text-white': j.status==='needs_input'?'bg-amber-500 text-white': j.status==='failed'?'bg-red-500 text-white':'bg-zinc-900 text-white dark:bg-zinc-700'}`}>{j.status}</span>
                </div>
              </button>
            ))}
          </div>
        </div>

        <div className="card p-5 lg:sticky lg:top-6 h-fit">
          {!selected ? <div className="py-16 text-center">
            <Eye className="w-8 h-8 mx-auto text-zinc-300 dark:text-zinc-700"/>
            <div className="mono text-sm text-zinc-500 mt-2">Select a job to see all possible details & states</div>
            <div className="text-xs text-zinc-400 mt-1 mono">AI-scored, company size, forms, vault, resume options, application state</div>
          </div> : (
            <div className="space-y-4">
              <div>
                <div className="text-lg font-semibold leading-tight">{selected.title}</div>
                <div className="text-sm text-zinc-600 dark:text-zinc-400">{selected.company} • {selected.location}</div>
                <a href={selected.url} target="_blank" className="inline-flex items-center gap-1 text-xs text-blue-600 dark:text-blue-400 mt-1">Open original <ExternalLink className="w-3 h-3"/></a>
              </div>
              <div className="flex flex-wrap gap-2">
                <span className="text-xs mono px-2 py-1 rounded-full bg-zinc-100 dark:bg-zinc-800">{selected.source}</span>
                <span className="text-xs mono px-2 py-1 rounded-full bg-zinc-100 dark:bg-zinc-800">{selected.company_size}</span>
                <span className={`text-xs mono px-2 py-1 rounded-full ${selected.status==='applied'?'bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-300':'bg-zinc-100 dark:bg-zinc-800'}`}>{selected.status}</span>
                <span className="text-xs mono px-2 py-1 rounded-full bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300 inline-flex items-center gap-1"><Award className="w-3 h-3"/>Score {selected.score}</span>
              </div>
              <div className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3">
                <div className="text-xs mono font-medium flex items-center gap-1"><Brain className="w-3.5 h-3.5"/> Scoring reason</div>
                <div className="text-xs text-zinc-600 dark:text-zinc-400 mt-1 leading-relaxed">{selected.score_reason || 'Heuristic + AI hybrid'}</div>
                {selected.company_info?.forms && <div className="mt-2 text-xs mono text-zinc-500">Forms detected: {selected.company_info.forms.fields?.length} fields • requires_login: {String(selected.company_info.forms.requires_login)} • confidence {selected.company_info.forms.ai_confidence}</div>}
              </div>
              <div>
                <div className="text-xs mono font-medium">Job description</div>
                <div className="mt-1 text-sm leading-relaxed text-zinc-700 dark:text-zinc-300 bg-white dark:bg-zinc-900 border dark:border-zinc-800 rounded-xl p-3 max-h-48 overflow-auto">{selected.description}</div>
              </div>

              <div className="border-t dark:border-zinc-800 pt-4 space-y-3">
                <div className="text-xs mono font-medium flex items-center gap-2"><Wand2 className="w-3.5 h-3.5"/> Apply with resume</div>
                <div className="grid grid-cols-3 gap-2">
                  <label className={`text-xs p-2 rounded-xl border cursor-pointer text-center ${resumeChoice==='auto'?'bg-zinc-900 text-white border-zinc-900 dark:bg-white dark:text-zinc-900':'bg-white dark:bg-zinc-900 dark:border-zinc-700'}`}><input type="radio" name="resumeChoice" value="auto" checked={resumeChoice==='auto'} onChange={()=>setResumeChoice('auto')} className="hidden"/>Auto (AI decides)</label>
                  <label className={`text-xs p-2 rounded-xl border cursor-pointer text-center ${resumeChoice==='master'?'bg-zinc-900 text-white border-zinc-900 dark:bg-white dark:text-zinc-900':'bg-white dark:bg-zinc-900 dark:border-zinc-700'}`}><input type="radio" name="resumeChoice" value="master" checked={resumeChoice==='master'} onChange={()=>setResumeChoice('master')} className="hidden"/>Master</label>
                  <label className={`text-xs p-2 rounded-xl border cursor-pointer text-center ${resumeChoice==='generated'?'bg-zinc-900 text-white border-zinc-900 dark:bg-white dark:text-zinc-900':'bg-white dark:bg-zinc-900 dark:border-zinc-700'}`}><input type="radio" name="resumeChoice" value="generated" checked={resumeChoice==='generated'} onChange={()=>setResumeChoice('generated')} className="hidden"/>Generated</label>
                </div>
                <button onClick={apply} disabled={busy} className="w-full py-2.5 rounded-full bg-blue-600 text-white text-sm font-medium inline-flex items-center justify-center gap-2 disabled:opacity-50">
                  {busy ? <Loader2 className="w-4 h-4 animate-spin"/> : <Sparkles className="w-4 h-4"/>} {selected.status==='needs_input' ? 'Re-queue application' : 'Auto-apply (vault + autofill)'}
                </button>
                {applyMsg && <div className="text-xs mono p-2 rounded-lg bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 text-amber-800 dark:text-amber-200 flex gap-2"><AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5"/>{applyMsg}</div>}
                <div className="text-[11px] mono text-zinc-500 leading-relaxed">
                  For Workday/Lever etc: auto-creates sign-in credential → vault (Chrome/Apple export). For LinkedIn/Naukri/Indeed: auto-submits; if redirects to external company page, automatically switches to credential+profile workflow. Unknown fields → User Input Needed queue.
                </div>
              </div>

              {selected.error && <div className="text-xs mono p-2 rounded-lg bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 text-red-700 dark:text-red-300">{selected.error}</div>}
              <div className="space-y-2 text-xs mono">
                <button onClick={generateResume} disabled={generating} className="w-full py-2 rounded-full border dark:border-zinc-700 flex items-center justify-center gap-1 disabled:opacity-50">{generating ? <Loader2 className="w-3.5 h-3.5 animate-spin"/> : <Wand2 className="w-3.5 h-3.5"/>} Generate tailored resume (JD fact guard)</button>
                {generateMsg && <div className="text-[11px] text-zinc-500">{generateMsg}</div>}
                {generatedLinks && (
                  <div className="flex gap-2">
                    <a href={generatedLinks.docx} className="flex-1 py-1.5 rounded-full border dark:border-zinc-700 text-center">Download .docx</a>
                    <a href={generatedLinks.pdf} className="flex-1 py-1.5 rounded-full border dark:border-zinc-700 text-center">Download .pdf</a>
                  </div>
                )}
                <button onClick={async()=>{
                  setApplyMsg('')
                  try{
                    // JSON body — see EmailGenerate on the backend.
                    await client.post('/api/emails/generate', {company: selected.company, job_id: selected.id})
                    setApplyMsg('Email drafted → check the Email Bucket')
                  }catch(e:any){ setApplyMsg(apiError(e, 'Could not draft the email')) }
                }} className="w-full py-2 rounded-full border dark:border-zinc-700">Cold email</button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
