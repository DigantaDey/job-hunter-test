import { useEffect, useState } from 'react'
import client, { apiError, aiOutage, AI_REQUEST_TIMEOUT_MS, type AIOutage } from '../api/client'
import { AIOutageBanner } from '../components/AIBanner'
import { EmptyBoardBanner } from '../components/EmptyBoardBanner'
import { useDiscoveryLastRun } from '../hooks/useDiscoveryLastRun'
import { Search, Sparkles, ExternalLink, Award, Building2, Clock, Filter, Loader2, Wand2, CheckCircle, AlertCircle, Eye, Brain, Target, MapPin, DollarSign, GraduationCap, Zap, TrendingUp, FileText } from 'lucide-react'

/**
 * Where a score came from. The AI verdict and a keyword-overlap estimate are
 * not the same claim, so the label travels with the number.
 */
function SourceBadge({ source }: { source?: string }) {
  const map: Record<string, { label: string; cls: string; title: string }> = {
    ai: { label: 'AI verified', cls: 'bg-emerald-600 text-white', title: 'Guardrail-checked model score' },
    preliminary: { label: 'estimate', cls: 'bg-zinc-200 dark:bg-zinc-700 text-zinc-700 dark:text-zinc-200', title: 'Keyword-overlap pre-rank — not an AI verdict' },
    pending: { label: 'AI pending', cls: 'bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300', title: 'The model could not be reached' },
    rejected: { label: 'rejected', cls: 'bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300', title: 'The model answered but failed the accuracy guardrail' },
    insufficient_data: { label: 'no data', cls: 'bg-zinc-200 dark:bg-zinc-700 text-zinc-600 dark:text-zinc-300', title: 'Not enough text to score' },
    funding_context: { label: 'funding context', cls: 'bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-300', title: 'Tracked from a funding provider’s own open-position data — there is no job description to score' },
  }
  const info = map[source || ''] || { label: source || 'unknown', cls: 'bg-zinc-200 dark:bg-zinc-700 text-zinc-600 dark:text-zinc-300', title: '' }
  return <span title={info.title} className={`text-[10px] px-2 py-0.5 rounded-full font-normal mono ${info.cls}`}>{info.label}</span>
}

export default function Jobs(){
  const [jobs, setJobs] = useState<any[]>([])
  const [filter, setFilter] = useState({status:'', source:'', q:''})
  const [selected, setSelected] = useState<any>(null)
  const [intelligence, setIntelligence] = useState<any>(null)
  const [companyIntel, setCompanyIntel] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [discoverBusy, setDiscoverBusy] = useState(false)
  const [outage, setOutage] = useState<AIOutage | null>(null)
  const [resumeChoice, setResumeChoice] = useState('auto')
  const [applyMsg, setApplyMsg] = useState('')
  const [discoverMsg, setDiscoverMsg] = useState('')
  const [generating, setGenerating] = useState(false)
  const [generateMsg, setGenerateMsg] = useState('')
  const [generatedLinks, setGeneratedLinks] = useState<any>(null)

  /**
   * The board is not filtered by a track today, so there is no persona to scope
   * the read to — this is the slot where a persona-scoped board would put its
   * selection. The hook forwards it as `?persona_id=`, so the verdict is about
   * *that* track's last run rather than the newest run of any track.
   */
  const personaFilter: number | null = null
  // Read only while it can be shown — an empty board is the only place the
  // "why" belongs, and a full board must not pay for the call.
  const { run: lastRun, loading: lastRunLoading, reload: reloadLastRun } = useDiscoveryLastRun(jobs.length === 0, personaFilter)

  const load = ()=>{
    const p:any={}
    if(filter.status) p.status=filter.status
    if(filter.source) p.source=filter.source
    if(filter.q) p.q=filter.q
    client.get('/api/jobs',{params:p}).then(r=>setJobs(r.data))
  }
  useEffect(()=>{ load() },[filter.status, filter.source])
  useEffect(()=>{
    const id=setTimeout(load, 400)
    return ()=>clearTimeout(id)
  },[filter.q])

  const selectJob = async (id:number)=>{
    const {data}=await client.get(`/api/jobs/${id}`)
    setSelected(data)
    setIntelligence(null)
    setCompanyIntel(null)
    // Fetch intelligence breakdown
    client.get(`/api/jobs/${id}/intelligence`, { timeout: AI_REQUEST_TIMEOUT_MS }).then(r=>setIntelligence(r.data)).catch(()=>{})
    // Fetch company intel
    client.get(`/api/company/${encodeURIComponent(data.company)}/intel`, { timeout: AI_REQUEST_TIMEOUT_MS }).then(r=>setCompanyIntel(r.data)).catch(()=>{})
  }

  const discover = async()=>{
    setDiscoverBusy(true)
    try{
      const {data} = await client.post('/api/jobs/discover', {})
      setDiscoverMsg(`${(data.keywords || []).slice(0, 8).join(' • ')}`)
      setTimeout(load, 2000)
      setTimeout(load, 4500)
      // The queued run's own verdict is what the empty-board banner shows, so it
      // is re-read once the polls above have had time to pick the jobs up.
      setTimeout(reloadLastRun, 5000)
    }catch(e:any){ setDiscoverMsg(apiError(e)) }
    finally{ setDiscoverBusy(false)}
  }
  const generateResume = async()=>{
    if(!selected) return
    setGenerating(true)
    try{
      const {data} = await client.post('/api/resumes/generate', null, {params:{job_id: selected.id}, timeout: AI_REQUEST_TIMEOUT_MS})
      setGenerateMsg('Tailored resume ready')
      setGeneratedLinks(data.files)
    }catch(e:any){
      const o = aiOutage(e)
      if (o) setOutage(o)
      else setGenerateMsg(apiError(e, 'Generation failed'))
    }
    finally{ setGenerating(false) }
  }
  const apply = async()=>{
    if(!selected) return
    setBusy(true); setApplyMsg('')
    try{
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
        <h1 className="text-xl font-semibold tracking-tight flex items-center gap-2"><Building2 className="w-5 h-5"/> Job discovery — with intelligence</h1>
        <div className="flex gap-2">
          <button onClick={discover} disabled={discoverBusy} className="px-4 py-2 rounded-full bg-blue-600 text-white text-sm font-medium inline-flex items-center gap-2 disabled:opacity-50">
            {discoverBusy ? <Loader2 className="w-4 h-4 animate-spin"/> : <Search className="w-4 h-4"/>} Discover jobs
          </button>
        </div>
      </div>
      {discoverMsg && <div className="text-xs mono p-2 rounded-lg bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-800 text-blue-700 dark:text-blue-300">Keywords: {discoverMsg}</div>}
      {outage && <AIOutageBanner outage={outage} what="no tailored resume was created" onDismiss={()=>setOutage(null)} />}

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
          <option value="greenhouse">Greenhouse</option>
          <option value="lever">Lever</option>
          <option value="ashby">Ashby</option>
          <option value="workable">Workable</option>
          <option value="workday">Workday</option>
          <option value="smartrecruiters">SmartRecruiters</option>
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
            <span className="text-xs mono text-zinc-500">Intelligence → click job</span>
          </div>
          <div className="max-h-[75vh] overflow-auto divide-y dark:divide-zinc-800">
            {jobs.length===0 ? <EmptyBoardBanner lastRun={lastRunLoading ? undefined : lastRun} busy={discoverBusy} onDiscover={discover}/> :
              jobs.map(j=> (
              <button key={j.id} onClick={()=>selectJob(j.id)} className={`w-full text-left p-4 hover:bg-zinc-50 dark:hover:bg-zinc-800/50 flex gap-3 ${selected?.id===j.id ? 'bg-blue-50 dark:bg-blue-950/30' : ''}`}>
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

        <div className="card p-5 lg:sticky lg:top-6 h-fit max-h-[85vh] overflow-auto">
          {!selected ? <div className="py-16 text-center">
            <Eye className="w-8 h-8 mx-auto text-zinc-300 dark:text-zinc-700"/>
            <div className="mono text-sm text-zinc-500 mt-2">Select a job to see intelligence breakdown</div>
            <div className="text-xs text-zinc-400 mt-1 mono">Transparent scoring • Company intel • Automation</div>
          </div> : (
            <div className="space-y-4">
              <div>
                <div className="text-lg font-semibold leading-tight">{selected.title}</div>
                <div className="text-sm text-zinc-600 dark:text-zinc-400 flex items-center gap-1"><Building2 className="w-3 h-3"/>{selected.company} • {selected.location}</div>
                <a href={selected.url} target="_blank" className="inline-flex items-center gap-1 text-xs text-blue-600 dark:text-blue-400 mt-1">Open original <ExternalLink className="w-3 h-3"/></a>
              </div>

              <div className="flex flex-wrap gap-2">
                <span className="text-xs mono px-2 py-1 rounded-full bg-zinc-100 dark:bg-zinc-800">{selected.source}</span>
                <span className="text-xs mono px-2 py-1 rounded-full bg-zinc-100 dark:bg-zinc-800">{selected.company_size}</span>
                <span className={`text-xs mono px-2 py-1 rounded-full ${selected.status==='applied'?'bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-300':'bg-zinc-100 dark:bg-zinc-800'}`}>{selected.status}</span>
                <span className="text-xs mono px-2 py-1 rounded-full bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300 inline-flex items-center gap-1"><Award className="w-3 h-3"/>Score {selected.score}</span>
              </div>

              {/* Intelligence Breakdown */}
              {intelligence ? (
                <div className="bg-gradient-to-br from-zinc-50 to-blue-50/30 dark:from-zinc-800 dark:to-blue-950/20 rounded-xl p-4 border dark:border-zinc-700 space-y-3">
                  <div className="flex items-center justify-between">
                    <div className="text-sm font-semibold flex items-center gap-2"><Target className="w-4 h-4"/> Overall Match <span className="text-lg mono">{intelligence.overall || intelligence.score}/100</span>
                      <SourceBadge source={intelligence.score_source} />
                    </div>
                    <span className={`text-[11px] px-2 py-1 rounded-full font-bold ${intelligence.recommendation?.includes('HIGH')?'bg-emerald-600 text-white': intelligence.recommendation?.includes('GOOD')?'bg-blue-600 text-white':'bg-amber-500 text-white'}`}>{intelligence.recommendation}</span>
                  </div>
                  <div className="text-xs text-zinc-600 dark:text-zinc-400">{intelligence.recommendation_reason}</div>
                  
                  {intelligence.breakdown && (
                    <div className="grid grid-cols-3 gap-2 mt-2">
                      <div className="bg-white dark:bg-zinc-900 rounded-lg p-2 text-center"><div className="text-[10px] mono uppercase text-zinc-500 flex items-center justify-center gap-1"><Zap className="w-3 h-3"/>Skills</div><div className="font-bold mono text-sm">{intelligence.breakdown.skills}</div></div>
                      <div className="bg-white dark:bg-zinc-900 rounded-lg p-2 text-center"><div className="text-[10px] mono uppercase text-zinc-500 flex items-center justify-center gap-1"><Clock className="w-3 h-3"/>Exp</div><div className="font-bold mono text-sm">{intelligence.breakdown.experience}</div></div>
                      <div className="bg-white dark:bg-zinc-900 rounded-lg p-2 text-center"><div className="text-[10px] mono uppercase text-zinc-500">Seniority</div><div className="font-bold mono text-sm">{intelligence.breakdown.seniority}</div></div>
                      <div className="bg-white dark:bg-zinc-900 rounded-lg p-2 text-center"><div className="text-[10px] mono uppercase text-zinc-500 flex items-center justify-center gap-1"><MapPin className="w-3 h-3"/>Location</div><div className="font-bold mono text-sm">{intelligence.breakdown.location}</div></div>
                      <div className="bg-white dark:bg-zinc-900 rounded-lg p-2 text-center"><div className="text-[10px] mono uppercase text-zinc-500 flex items-center justify-center gap-1"><DollarSign className="w-3 h-3"/>Domain</div><div className="font-bold mono text-sm">{intelligence.breakdown.domain}</div></div>
                      <div className="bg-white dark:bg-zinc-900 rounded-lg p-2 text-center"><div className="text-[10px] mono uppercase text-zinc-500 flex items-center justify-center gap-1"><GraduationCap className="w-3 h-3"/>Edu</div><div className="font-bold mono text-sm">{intelligence.breakdown.education}</div></div>
                    </div>
                  )}

                  {intelligence.strong_matches?.length>0 && (
                    <div><div className="text-[11px] mono font-medium uppercase text-emerald-700 dark:text-emerald-300">Strong matches</div><div className="flex flex-wrap gap-1 mt-1">{intelligence.strong_matches.map((s:string)=><span key={s} className="text-[11px] px-2 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-900 text-emerald-700 dark:text-emerald-300">{s}</span>)}</div></div>
                  )}
                  {intelligence.missing_weak?.length>0 && (
                    <div><div className="text-[11px] mono font-medium uppercase text-amber-700 dark:text-amber-300">Missing / weak areas</div><div className="flex flex-wrap gap-1 mt-1">{intelligence.missing_weak.map((s:string)=><span key={s} className="text-[11px] px-2 py-0.5 rounded-full bg-amber-100 dark:bg-amber-900 text-amber-700 dark:text-amber-300">{s}</span>)}</div></div>
                  )}
                  {intelligence.ai_error && (
                    <div className="text-[11px] p-2 rounded-lg bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 mono">
                      <div className="font-medium text-red-800 dark:text-red-200">AI scoring unavailable — this is not an AI verdict</div>
                      <div className="text-red-700 dark:text-red-300 mt-0.5">{intelligence.ai_error.message} ({intelligence.ai_error.reason})</div>
                      {intelligence.ai_error.fix && <div className="text-red-700 dark:text-red-300 mt-0.5"><strong>Fix:</strong> {intelligence.ai_error.fix}</div>}
                      {intelligence.preliminary_score != null && <div className="text-red-600 dark:text-red-400 mt-0.5">Keyword-overlap estimate for reference: {intelligence.preliminary_score}/100</div>}
                    </div>
                  )}
                  {intelligence.evidence?.length > 0 && (
                    <div><div className="text-[11px] mono font-medium uppercase text-zinc-500">Evidence the score rests on</div>
                      <ul className="mt-1 space-y-0.5">{intelligence.evidence.slice(0,4).map((e:string,i:number)=><li key={i} className="text-[11px] text-zinc-600 dark:text-zinc-400">“{e}”</li>)}</ul></div>
                  )}
                  {intelligence.upgrade_hint && <div className="text-[11px] p-2 rounded-lg bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800">{intelligence.upgrade_hint}</div>}
                </div>
              ) : (
                <div className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3">
                  <div className="text-xs mono font-medium flex items-center gap-1"><Brain className="w-3.5 h-3.5"/> Scoring reason</div>
                  <div className="text-xs text-zinc-600 dark:text-zinc-400 mt-1 leading-relaxed">{selected.score_reason || 'Preliminary keyword-overlap score'}</div>
                </div>
              )}

              {/* Company Intel */}
              {companyIntel && (
                <div className="bg-zinc-50 dark:bg-zinc-800 rounded-xl p-3">
                  <div className="text-xs mono font-medium flex items-center gap-1"><TrendingUp className="w-3.5 h-3.5"/> Company Intelligence {companyIntel.cached && <span className="text-[10px] bg-zinc-200 dark:bg-zinc-700 px-1.5 py-0.5 rounded-full">cached</span>}</div>
                  <div className="text-xs mt-2 space-y-1">
                    <div><span className="text-zinc-500">Industry:</span> {companyIntel.industry}</div>
                    <div><span className="text-zinc-500">Size:</span> {companyIntel.size} • <span className="text-zinc-500">Funding:</span> {companyIntel.funding_stage}</div>
                    {companyIntel.tech_stack?.length>0 && <div><span className="text-zinc-500">Tech:</span> {companyIntel.tech_stack.slice(0,5).join(', ')}</div>}
                    {companyIntel.summary && <div className="text-zinc-600 dark:text-zinc-400 mt-1">{companyIntel.summary}</div>}
                  </div>
                </div>
              )}

              <div>
                <div className="text-xs mono font-medium">Job description</div>
                <div className="mt-1 text-sm leading-relaxed text-zinc-700 dark:text-zinc-300 bg-white dark:bg-zinc-900 border dark:border-zinc-800 rounded-xl p-3 max-h-48 overflow-auto">{selected.description}</div>
              </div>

              <div className="border-t dark:border-zinc-800 pt-4 space-y-3">
                <div className="text-xs mono font-medium flex items-center gap-2"><Wand2 className="w-3.5 h-3.5"/> Application — Prepare → Review → Confirm → Execute</div>
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
                  Prepare → Review → Confirm → Execute. For Workday/Lever: auto-creates vault credential. Unknown fields → User Input Needed queue. Never silently fails.
                </div>
              </div>

              {selected.error && <div className="text-xs mono p-2 rounded-lg bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 text-red-700 dark:text-red-300">{selected.error}</div>}
              <div className="space-y-2 text-xs mono">
                <button onClick={generateResume} disabled={generating} className="w-full py-2 rounded-full border dark:border-zinc-700 flex items-center justify-center gap-1 disabled:opacity-50">{generating ? <Loader2 className="w-3.5 h-3.5 animate-spin"/> : <Wand2 className="w-3.5 h-3.5"/>} Generate tailored resume (fact guard)</button>
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
                    await client.post('/api/emails/generate', {company: selected.company, job_id: selected.id}, { timeout: AI_REQUEST_TIMEOUT_MS })
                    setApplyMsg('Email drafted → check the Email Bucket')
                  }catch(e:any){ setApplyMsg(apiError(e, 'Could not draft the email')) }
                }} className="w-full py-2 rounded-full border dark:border-zinc-700">Cold email</button>
                <button onClick={async()=>{
                  try{
                    const {data} = await client.post('/api/interview/generate', {job_id: selected.id, count: 8}, { timeout: AI_REQUEST_TIMEOUT_MS })
                    setApplyMsg(`Interview prep generated: ${data.questions.length} questions → Interview Prep page`)
                  }catch(e:any){ setApplyMsg(apiError(e)) }
                }} className="w-full py-2 rounded-full border dark:border-zinc-700 flex items-center justify-center gap-1"><FileText className="w-3.5 h-3.5"/> Interview prep</button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
