import { useEffect, useState } from 'react'
import client, { apiError } from '../api/client'
import { Loader2, CheckCircle, AlertTriangle, FileText, Edit3, ShieldCheck, Clock, History, Eye, Send, RefreshCw } from 'lucide-react'

type Packet = {
  id: number
  job_id: number
  version: number
  is_current: boolean
  status: string
  approval_state: string
  jd_hash: string
  jd_version: number
  is_stale: boolean
  job_title: string
  company: string
  master_profile_snapshot: any
  tailored_resume: any
  cover_note: string
  short_answers: any[]
  outreach_draft: any
  checklist: any[]
  summary: string
  evidence: any[]
  emphasized_facts: any[]
  guardrail_report: any
  token_usage: any
  generated_at: string
  created_at: string
}

export default function Packets() {
  const [jobs, setJobs] = useState<any[]>([])
  const [selectedJob, setSelectedJob] = useState<any | null>(null)
  const [packets, setPackets] = useState<Packet[]>([])
  const [current, setCurrent] = useState<Packet | null>(null)
  const [loading, setLoading] = useState(false)
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const [editing, setEditing] = useState(false)
  const [editCover, setEditCover] = useState('')
  const [editSummary, setEditSummary] = useState('')
  const [editOutreachSubject, setEditOutreachSubject] = useState('')
  const [editOutreachBody, setEditOutreachBody] = useState('')
  const [editTailored, setEditTailored] = useState('')
  const [editAnswers, setEditAnswers] = useState('')

  const loadJobs = async () => {
    const r = await client.get('/api/jobs', { params: { limit: 50 } })
    setJobs(r.data.jobs || r.data || [])
  }
  const loadPackets = async (jobId: number) => {
    const r = await client.get(`/api/packets`, { params: { job_id: jobId } })
    const list: Packet[] = r.data.packets || r.data || []
    setPackets(list)
    const cur = list.find(p => p.is_current) || list[0] || null
    setCurrent(cur)
    if (cur) {
      setEditCover(cur.cover_note || '')
      setEditSummary(cur.summary || '')
      setEditOutreachSubject(cur.outreach_draft?.subject || '')
      setEditOutreachBody(cur.outreach_draft?.body || '')
      setEditTailored(JSON.stringify(cur.tailored_resume || {}, null, 2))
      setEditAnswers(JSON.stringify(cur.short_answers || [], null, 2))
    }
  }

  useEffect(() => { loadJobs() }, [])
  useEffect(() => { if (selectedJob) loadPackets(selectedJob.id) }, [selectedJob])

  const prepare = async () => {
    if (!selectedJob) return
    setLoading(true); setMsg(''); setErr('')
    try {
      const r = await client.post(`/api/packets/prepare`, { job_id: selectedJob.id })
      setMsg(`Packet v${r.data.version} prepared — pending approval`)
      await loadPackets(selectedJob.id)
    } catch (e: any) {
      const detail = e?.response?.data?.detail
      if (detail?.code === 'guardrail_failed') setErr(`Guardrail rejected: ${JSON.stringify(detail.issues || detail)}`)
      else setErr(apiError(e) || 'Prepare failed')
    } finally { setLoading(false) }
  }

  const saveEdits = async () => {
    if (!current) return
    setLoading(true); setErr(''); setMsg('')
    try {
      let tailored: any = undefined
      let answers: any = undefined
      try { tailored = editTailored ? JSON.parse(editTailored) : undefined } catch { setErr('Tailored resume is not valid JSON'); setLoading(false); return }
      try { answers = editAnswers ? JSON.parse(editAnswers) : undefined } catch { setErr('Short answers is not valid JSON'); setLoading(false); return }
      const body: any = {
        cover_note: editCover,
        summary: editSummary,
        outreach_draft: { subject: editOutreachSubject, body: editOutreachBody },
      }
      if (tailored !== undefined) body.tailored_resume = tailored
      if (answers !== undefined) body.short_answers = answers
      const r = await client.put(`/api/packets/${current.id}`, body)
      setCurrent(r.data)
      setMsg('Edits saved — still pending approval')
      setEditing(false)
      await loadPackets(selectedJob.id)
    } catch (e: any) { setErr(apiError(e)) } finally { setLoading(false) }
  }

  const approve = async () => {
    if (!current) return
    setLoading(true); setErr(''); setMsg('')
    try {
      const r = await client.post(`/api/packets/${current.id}/approve`)
      setCurrent(r.data)
      setMsg('Packet approved — ready to use')
      await loadPackets(selectedJob!.id)
    } catch (e: any) { setErr(apiError(e)) } finally { setLoading(false) }
  }
  const reject = async () => {
    if (!current) return
    setLoading(true); setErr(''); setMsg('')
    try {
      const r = await client.post(`/api/packets/${current.id}/reject`, { reason: 'User rejected packet' })
      setCurrent(r.data); await loadPackets(selectedJob!.id)
    } catch (e: any) { setErr(apiError(e)) } finally { setLoading(false) }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2"><FileText className="w-6 h-6" /> Application Packets</h1>
        <div className="text-xs mono text-zinc-500">Preparation only — no auto-submit. Review every artifact before use.</div>
      </div>

      {msg && <div className="bg-emerald-50 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-300 px-4 py-2 rounded-lg text-sm flex items-center gap-2"><CheckCircle className="w-4 h-4" /> {msg}</div>}
      {err && <div className="bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-300 px-4 py-2 rounded-lg text-sm flex items-center gap-2"><AlertTriangle className="w-4 h-4" /> {err}</div>}

      <div className="grid lg:grid-cols-3 gap-6">
        <div className="lg:col-span-1 space-y-4">
          <div className="bg-white dark:bg-zinc-900 rounded-xl border dark:border-zinc-800 p-4">
            <h2 className="font-medium text-sm mb-3">Select job</h2>
            <div className="space-y-2 max-h-[420px] overflow-auto pr-1">
              {jobs.map(j => (
                <button key={j.id} onClick={() => setSelectedJob(j)} className={`w-full text-left p-3 rounded-lg border text-sm ${selectedJob?.id===j.id ? 'bg-zinc-900 text-white dark:bg-white dark:text-zinc-900' : 'bg-white dark:bg-zinc-900 hover:bg-zinc-50 dark:hover:bg-zinc-800 border-zinc-200 dark:border-zinc-800'}`}>
                  <div className="font-medium truncate">{j.title}</div>
                  <div className="text-xs opacity-70 truncate">{j.company} • {j.status}</div>
                  <div className="text-[11px] mono opacity-60 truncate">{j.id}</div>
                </button>
              ))}
              {jobs.length===0 && <div className="text-sm text-zinc-500">No jobs — run discovery first.</div>}
            </div>
            {selectedJob && (
              <button onClick={prepare} disabled={loading} className="mt-4 w-full inline-flex items-center justify-center gap-2 bg-blue-600 text-white rounded-lg px-4 py-2 text-sm hover:bg-blue-700 disabled:opacity-50">
                {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />} Prepare packet
              </button>
            )}
          </div>

          {packets.length>0 && (
            <div className="bg-white dark:bg-zinc-900 rounded-xl border dark:border-zinc-800 p-4">
              <h3 className="font-medium text-sm mb-2 flex items-center gap-2"><History className="w-4 h-4" /> Version history</h3>
              <div className="space-y-2">
                {packets.map(p => (
                  <div key={p.id} onClick={() => { setCurrent(p); setEditCover(p.cover_note||''); setEditSummary(p.summary||''); setEditOutreachSubject(p.outreach_draft?.subject||''); setEditOutreachBody(p.outreach_draft?.body||''); setEditTailored(JSON.stringify(p.tailored_resume||{},null,2)); setEditAnswers(JSON.stringify(p.short_answers||[],null,2)) }} className={`p-3 rounded-lg border cursor-pointer ${current?.id===p.id ? 'border-blue-500 bg-blue-50 dark:bg-blue-950' : 'border-zinc-200 dark:border-zinc-800'}`}>
                    <div className="flex items-center justify-between text-xs">
                      <span className="font-medium">v{p.version} {p.is_current ? '• current' : ''}</span>
                      <span className={`px-2 py-0.5 rounded-full text-[11px] ${p.status==='approved' ? 'bg-emerald-100 text-emerald-700' : p.status==='pending_approval' ? 'bg-amber-100 text-amber-700' : 'bg-zinc-100 text-zinc-600'}`}>{p.status}</span>
                    </div>
                    <div className="text-[11px] mono text-zinc-500">{new Date(p.generated_at).toLocaleString()} • JD {p.jd_hash?.slice(0,8)}</div>
                    {p.is_stale && <div className="text-[11px] text-amber-600 flex items-center gap-1"><AlertTriangle className="w-3 h-3" /> JD changed — regenerate</div>}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="lg:col-span-2 space-y-4">
          {!current && <div className="bg-white dark:bg-zinc-900 rounded-xl border dark:border-zinc-800 p-8 text-center text-sm text-zinc-500"><Eye className="w-8 h-8 mx-auto mb-2 opacity-40" /> Select a job and prepare a packet to review artifacts.</div>}
          {current && (
            <>
              <div className="bg-white dark:bg-zinc-900 rounded-xl border dark:border-zinc-800 p-4">
                <div className="flex items-center justify-between flex-wrap gap-2">
                  <div>
                    <div className="font-medium">{current.job_title} at {current.company} — v{current.version}</div>
                    <div className="text-xs mono text-zinc-500">Generated {current.generated_at ? new Date(current.generated_at).toLocaleString() : ''} • JD version {current.jd_version} • hash {current.jd_hash?.slice(0,12)} • {current.is_stale ? 'stale — JD changed' : 'fresh'}</div>
                    <div className="text-xs mt-1 flex items-center gap-2">
                      <span className={`px-2 py-0.5 rounded-full text-xs ${current.status==='approved' ? 'bg-emerald-600 text-white' : 'bg-amber-100 text-amber-800'}`}>{current.approval_state || current.status}</span>
                      {current.is_stale && <span className="text-amber-600 inline-flex items-center gap-1"><AlertTriangle className="w-3 h-3" /> Stale packet — regenerate after JD edit</span>}
                      <span className="inline-flex items-center gap-1 text-zinc-500"><ShieldCheck className="w-3 h-3" /> Guardrail {current.guardrail_report?.score ?? ''} • attempts {current.guardrail_report?.attempts ?? current.token_usage?.guardrail_attempts ?? 1}</span>
                    </div>
                  </div>
                  <div className="flex gap-2">
                    <button onClick={() => setEditing(!editing)} className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-sm"><Edit3 className="w-4 h-4" /> {editing ? 'Cancel edit' : 'Edit before use'}</button>
                    {current.status !== 'approved' && <button onClick={approve} disabled={loading} className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-600 text-white text-sm disabled:opacity-50"><CheckCircle className="w-4 h-4" /> Approve</button>}
                    {current.status !== 'rejected' && <button onClick={reject} disabled={loading} className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-sm">Reject</button>}
                  </div>
                </div>
                <div className="grid grid-cols-3 gap-3 mt-4 text-xs">
                  <div className="bg-zinc-50 dark:bg-zinc-800 rounded-lg p-3"><div className="font-medium flex items-center gap-1"><Clock className="w-3 h-3" /> Token accounting</div><div className="mono mt-1">prompt {current.token_usage?.prompt_tokens ?? 0} • completion {current.token_usage?.completion_tokens ?? 0} • total {current.token_usage?.total_tokens ?? 0}</div><div className="mono text-zinc-500">model {current.token_usage?.model ?? '—'} • ${current.token_usage?.estimated_cost_usd ?? 0}</div></div>
                  <div className="bg-zinc-50 dark:bg-zinc-800 rounded-lg p-3"><div className="font-medium">Checklist</div><div className="mono mt-1">{current.checklist?.length ?? 0} items • {current.checklist?.filter((c:any)=>c.needs_review).length ?? 0} need review</div><div className="text-zinc-500">Ambiguous fields routed to you, not auto-filled</div></div>
                  <div className="bg-zinc-50 dark:bg-zinc-800 rounded-lg p-3"><div className="font-medium">Evidence</div><div className="mono mt-1">{current.evidence?.length ?? 0} facts • {current.emphasized_facts?.length ?? 0} emphasized</div><div className="text-zinc-500">Source locators preserved</div></div>
                </div>
              </div>

              <div className="bg-white dark:bg-zinc-900 rounded-xl border dark:border-zinc-800 p-4">
                <h3 className="font-medium text-sm mb-2">Application summary</h3>
                <p className="text-sm whitespace-pre-wrap">{current.summary}</p>
              </div>

              <div className="bg-white dark:bg-zinc-900 rounded-xl border dark:border-zinc-800 p-4">
                <h3 className="font-medium text-sm mb-2">Tailored resume (reviewable, versioned)</h3>
                {!editing ? (
                  <pre className="text-xs mono bg-zinc-50 dark:bg-zinc-800 rounded-lg p-3 overflow-auto max-h-[320px] whitespace-pre-wrap">{JSON.stringify(current.tailored_resume, null, 2)}</pre>
                ) : (
                  <textarea value={editTailored} onChange={e=>setEditTailored(e.target.value)} rows={14} className="w-full text-xs mono border rounded-lg p-3 bg-white dark:bg-zinc-900" />
                )}
                <div className="text-[11px] mono text-zinc-500 mt-1">Master profile preserved separately • tailored is a reordered subset — regenerate after profile correction.</div>
              </div>

              <div className="bg-white dark:bg-zinc-900 rounded-xl border dark:border-zinc-800 p-4">
                <h3 className="font-medium text-sm mb-2">Cover note</h3>
                {!editing ? <p className="text-sm whitespace-pre-wrap">{current.cover_note}</p> : <textarea value={editCover} onChange={e=>setEditCover(e.target.value)} rows={6} className="w-full text-sm border rounded-lg p-3 bg-white dark:bg-zinc-900" />}
              </div>

              <div className="bg-white dark:bg-zinc-900 rounded-xl border dark:border-zinc-800 p-4">
                <h3 className="font-medium text-sm mb-2">Short answers</h3>
                {!editing ? (
                  <div className="space-y-2">
                    {current.short_answers?.map((a:any,i:number)=>(
                      <div key={i} className={`p-3 rounded-lg border text-sm ${a.needs_review ? 'border-amber-300 bg-amber-50 dark:bg-amber-950' : 'border-zinc-200 dark:border-zinc-800'}`}>
                        <div className="font-medium">{a.question} {a.needs_review && <span className="text-amber-700 text-xs">• needs review</span>}</div>
                        <div className="mt-1 whitespace-pre-wrap">{a.answer || <span className="text-zinc-400 italic">No answer — requires your input</span>}</div>
                        <div className="text-[11px] mono text-zinc-500 mt-1">source {a.source_field || '—'} • confidence {a.confidence}</div>
                      </div>
                    ))}
                    {(!current.short_answers || current.short_answers.length===0) && <div className="text-sm text-zinc-500">No short answers.</div>}
                  </div>
                ) : (
                  <textarea value={editAnswers} onChange={e=>setEditAnswers(e.target.value)} rows={10} className="w-full text-xs mono border rounded-lg p-3 bg-white dark:bg-zinc-900" />
                )}
              </div>

              <div className="bg-white dark:bg-zinc-900 rounded-xl border dark:border-zinc-800 p-4">
                <h3 className="font-medium text-sm mb-2 flex items-center gap-2"><Send className="w-4 h-4" /> Outreach draft</h3>
                {!editing ? (
                  <>
                    <div className="text-sm font-medium">Subject: {current.outreach_draft?.subject || '—'}</div>
                    <p className="text-sm whitespace-pre-wrap mt-2">{current.outreach_draft?.body || '—'}</p>
                  </>
                ) : (
                  <>
                    <input value={editOutreachSubject} onChange={e=>setEditOutreachSubject(e.target.value)} placeholder="Subject" className="w-full text-sm border rounded-lg p-2 mb-2 bg-white dark:bg-zinc-900" />
                    <textarea value={editOutreachBody} onChange={e=>setEditOutreachBody(e.target.value)} rows={6} className="w-full text-sm border rounded-lg p-3 bg-white dark:bg-zinc-900" />
                  </>
                )}
              </div>

              <div className="bg-white dark:bg-zinc-900 rounded-xl border dark:border-zinc-800 p-4">
                <h3 className="font-medium text-sm mb-2">Missing-information checklist</h3>
                {current.checklist?.length ? (
                  <div className="space-y-2">
                    {current.checklist.map((c:any,i:number)=>(
                      <div key={i} className={`p-3 rounded-lg border text-sm ${c.needs_review ? 'border-amber-300 bg-amber-50 dark:bg-amber-950' : 'border-zinc-200'}`}>
                        <div className="flex items-center justify-between"><span className="font-medium">{c.label || c.field}</span><span className={`text-[11px] px-2 py-0.5 rounded-full ${c.severity==='error' ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-700'}`}>{c.severity}</span></div>
                        <div className="text-xs text-zinc-600 dark:text-zinc-400">{c.reason}</div>
                        <div className="text-[11px] mono text-zinc-500">path {c.path} • sensitivity {c.sensitivity} {c.supported===false ? '• unsupported — clearly marked' : ''}</div>
                      </div>
                    ))}
                  </div>
                ) : <div className="text-sm text-zinc-500">No missing fields — profile is complete for this job.</div>}
              </div>

              <div className="bg-white dark:bg-zinc-900 rounded-xl border dark:border-zinc-800 p-4">
                <h3 className="font-medium text-sm mb-2">Source evidence & emphasized facts</h3>
                <div className="grid md:grid-cols-2 gap-4">
                  <div>
                    <div className="text-xs font-medium mb-1">Evidence (locators)</div>
                    <div className="space-y-1 max-h-[260px] overflow-auto pr-1">
                      {current.evidence?.map((ev:any,i:number)=>(
                        <div key={i} className="text-xs p-2 bg-zinc-50 dark:bg-zinc-800 rounded-lg">
                          <div className="font-medium truncate">{ev.field || ev.fact}</div>
                          <div className="mono text-zinc-500 truncate">{ev.locator || (ev.evidence?.[0]?.locator) || '—'}</div>
                          {ev.value_preview && <div className="truncate text-zinc-600 dark:text-zinc-400">{ev.value_preview.slice(0,120)}</div>}
                        </div>
                      ))}
                    </div>
                  </div>
                  <div>
                    <div className="text-xs font-medium mb-1">Emphasized facts for this JD</div>
                    <div className="space-y-1">
                      {current.emphasized_facts?.map((f:any,i:number)=>(
                        <div key={i} className="text-xs p-2 bg-blue-50 dark:bg-blue-950 rounded-lg">
                          <div className="font-medium">{typeof f==='string' ? f : f.fact}</div>
                          {typeof f!=='string' && <div className="mono text-zinc-500">{f.reason} • keyword {f.jd_keyword}</div>}
                        </div>
                      ))}
                      {(!current.emphasized_facts || current.emphasized_facts.length===0) && <div className="text-xs text-zinc-500">No emphasized facts.</div>}
                    </div>
                  </div>
                </div>
              </div>

              {editing && <button onClick={saveEdits} disabled={loading} className="w-full inline-flex items-center justify-center gap-2 bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 rounded-lg px-4 py-2 text-sm"> {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Edit3 className="w-4 h-4" />} Save edits (still pending approval)</button>}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
