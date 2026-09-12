import { useEffect, useState } from 'react'
import client, { apiError, AI_REQUEST_TIMEOUT_MS } from '../api/client'
import { Brain, Plus, Trash2, CheckCircle } from 'lucide-react'

export default function Interview() {
  const [sessions, setSessions] = useState<any[]>([])
  const [selected, setSelected] = useState<any>(null)
  const [form, setForm] = useState({job_title:'', company:'', job_description:'', count:10})
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const [answer, setAnswer] = useState<{[key:number]:string}>({})

  const load = ()=>{
    client.get('/api/interview').then(r=>setSessions(r.data)).catch(()=>{})
  }
  useEffect(()=>{ load() },[])

  const generate = async ()=>{
    setBusy(true); setMsg('')
    try{
      const {data} = await client.post('/api/interview/generate', form, { timeout: AI_REQUEST_TIMEOUT_MS })
      setMsg(`Generated ${data.questions.length} questions for ${data.job_title}`)
      load()
      setSelected(data)
    }catch(e:any){ setMsg(apiError(e)) }
    finally{ setBusy(false) }
  }

  const submitAnswer = async (idx: number)=>{
    const ans = answer[idx]
    if(!ans) return
    setBusy(true)
    try{
      const {data} = await client.post(`/api/interview/${selected.id}/answer`, {question_index: idx, answer: ans}, { timeout: AI_REQUEST_TIMEOUT_MS })
      setMsg(`Feedback: score ${data.feedback.score}/10`)
      // refresh
      const {data: refreshed} = await client.get(`/api/interview/${selected.id}`)
      setSelected(refreshed)
    }catch(e:any){ setMsg(apiError(e)) }
    finally{ setBusy(false) }
  }

  return (
    <div className="space-y-6 max-w-6xl">
      <h1 className="text-xl font-semibold flex items-center gap-2"><Brain className="w-5 h-5"/> Interview Preparation</h1>
      <p className="text-xs text-zinc-500">AI-generated questions grounded in your resume + JD. Never fabricates experience. Pro: 20 sessions/mo, Pro+: 100.</p>
      {msg && <div className="p-2 rounded-xl bg-zinc-100 dark:bg-zinc-800 text-xs mono">{msg}</div>}

      <div className="grid lg:grid-cols-[0.9fr_1.1fr] gap-4">
        <div className="space-y-4">
          <div className="card p-5">
            <h3 className="font-medium flex items-center gap-2"><Plus className="w-4 h-4"/> New session</h3>
            <div className="mt-3 space-y-2">
              <input value={form.job_title} onChange={e=>setForm({...form, job_title:e.target.value})} placeholder="Job title (e.g. Backend Engineer)" className="w-full px-3 py-2 rounded-full border text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"/>
              <input value={form.company} onChange={e=>setForm({...form, company:e.target.value})} placeholder="Company" className="w-full px-3 py-2 rounded-full border text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"/>
              <textarea value={form.job_description} onChange={e=>setForm({...form, job_description:e.target.value})} placeholder="Job description (optional if you have job_id)" className="w-full px-3 py-2 rounded-xl border text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700" rows={4}/>
              <div className="flex gap-2 items-center"><input type="number" value={form.count} onChange={e=>setForm({...form, count: parseInt(e.target.value)||10})} className="w-20 px-3 py-2 rounded-full border text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"/><span className="text-xs mono">questions</span></div>
              <button disabled={busy} onClick={generate} className="w-full py-2.5 rounded-full bg-blue-600 text-white text-sm disabled:opacity-50">{busy?'Generating…':'Generate interview prep'}</button>
            </div>
          </div>

          <div className="card p-5">
            <h3 className="font-medium">Previous sessions</h3>
            <div className="mt-3 space-y-2 max-h-[60vh] overflow-auto">
              {sessions.map(s=>(
                <div key={s.id} className={`p-3 rounded-xl border dark:border-zinc-800 cursor-pointer hover:bg-zinc-50 dark:hover:bg-zinc-800/50 ${selected?.id===s.id?'bg-blue-50 dark:bg-blue-950/30 border-blue-200 dark:border-blue-800':''}`} onClick={async()=>{ const {data}=await client.get(`/api/interview/${s.id}`); setSelected(data)}}>
                  <div className="text-sm font-medium">{s.job_title} • {s.company}</div>
                  <div className="text-xs mono text-zinc-500">{s.questions?.length} Qs • {s.status} • {new Date(s.created_at).toLocaleDateString()}</div>
                </div>
              ))}
              {sessions.length===0 && <div className="text-xs mono text-zinc-500">No sessions yet.</div>}
            </div>
          </div>
        </div>

        <div className="card p-5 h-fit lg:sticky lg:top-6">
          {!selected ? <div className="py-16 text-center text-sm mono text-zinc-500">Select a session or generate new one</div> : (
            <div className="space-y-4">
              <div className="flex items-center justify-between"><div><div className="font-semibold">{selected.job_title}</div><div className="text-xs text-zinc-500">{selected.company} • {selected.status}</div></div><button onClick={async()=>{ if(confirm('Delete?')){ await client.delete(`/api/interview/${selected.id}`); setSelected(null); load() }}} className="p-2 rounded-full hover:bg-zinc-100 dark:hover:bg-zinc-800"><Trash2 className="w-4 h-4 text-zinc-500"/></button></div>
              <div className="space-y-4">
                {selected.questions?.map((q:any, idx:number)=>(
                  <div key={idx} className="border dark:border-zinc-800 rounded-xl p-3">
                    <div className="text-xs mono text-zinc-500 flex gap-2"><span className="px-2 py-0.5 rounded-full bg-zinc-100 dark:bg-zinc-800">{q.category}</span><span className="px-2 py-0.5 rounded-full bg-zinc-100 dark:bg-zinc-800">{q.difficulty}</span></div>
                    <div className="text-sm font-medium mt-2">{idx+1}. {q.question}</div>
                    {q.hint && <div className="text-xs text-zinc-500 mt-1">Hint: {q.hint}</div>}
                    {selected.answers?.[idx] ? (
                      <div className="mt-2 space-y-2">
                        <div className="text-xs p-2 rounded-lg bg-zinc-50 dark:bg-zinc-800">Your answer: {selected.answers[idx]}</div>
                        {selected.feedback?.[idx] && (
                          <div className="text-xs p-2 rounded-lg bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-800 space-y-1">
                            <div className="flex items-center gap-2"><CheckCircle className="w-3.5 h-3.5 text-emerald-600"/>Score: {selected.feedback[idx].score}/10</div>
                            <div><strong>Strengths:</strong> {selected.feedback[idx].strengths?.join(', ')}</div>
                            <div><strong>Improve:</strong> {selected.feedback[idx].improvements?.join(', ')}</div>
                            {selected.feedback[idx].suggested_answer && <div><strong>Suggested:</strong> {selected.feedback[idx].suggested_answer}</div>}
                          </div>
                        )}
                      </div>
                    ) : (
                      <div className="mt-2 flex gap-2">
                        <input value={answer[idx]||''} onChange={e=>setAnswer({...answer, [idx]: e.target.value})} placeholder="Your answer…" className="flex-1 px-3 py-1.5 rounded-full border text-xs bg-white dark:bg-zinc-900 dark:border-zinc-700"/>
                        <button disabled={busy} onClick={()=>submitAnswer(idx)} className="px-3 py-1.5 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-xs">Submit</button>
                      </div>
                    )}
                  </div>
                ))}
              </div>
              <button onClick={async()=>{ await client.post(`/api/interview/${selected.id}/complete`); const {data}=await client.get(`/api/interview/${selected.id}`); setSelected(data)}} className="w-full py-2 rounded-full border text-xs">Mark completed</button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
