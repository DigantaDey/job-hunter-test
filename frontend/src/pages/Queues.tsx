import { useEffect, useState } from 'react'
import client from '../api/client'
import { Layers, Bot, Search, Send, Clock, CheckCircle, AlertTriangle, Pause, Play, ArrowRight, UserCheck } from 'lucide-react'

export default function Queues(){
  const [stats, setStats] = useState<any>(null)
  const [jobs, setJobs] = useState<any[]>([])
  const [inputQueue, setInputQueue] = useState<any[]>([])
  const [filter, setFilter] = useState('application')

  const load = async()=>{
    const {data}=await client.get('/api/pipelines/stats')
    setStats(data)
    const {data: j}=await client.get('/api/pipelines/jobs', {params:{pipeline: filter}})
    setJobs(j)
    const {data: iq}=await client.get('/api/user-input-queue')
    setInputQueue(iq)
  }
  useEffect(()=>{ load(); const id=setInterval(load, 3000); return ()=>clearInterval(id)},[filter])

  const submitInput = async(item:any, payload:any)=>{
    await client.post(`/api/jobs/${item.job_id}/input`, payload)
    load()
  }

  if(!stats) return <div className="p-8 mono text-sm">Loading queues…</div>

  const pipelines = [
    {id:'discovery', label:'Job Discovery', icon: Search, desc:'Scrapes via keywords + freshness, AI finds structure', color:'bg-blue-600'},
    {id:'application', label:'Job Application', icon: Send, desc:'Autofill profile, credential, submit; external redirect handled', color:'bg-violet-600'},
    {id:'ai', label:'AI API', icon: Bot, desc:'Prioritized FIFO, rate-limited, green/red online dot', color:'bg-emerald-600'},
  ]

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold tracking-tight flex items-center gap-2"><Layers className="w-5 h-5"/> Pipelines & Queues <span className="text-xs mono font-normal text-zinc-500">three in parallel • FIFO</span></h1>

      <div className="grid lg:grid-cols-3 gap-3">
        {pipelines.map(p=> (
          <div key={p.id} className="card p-4">
            <div className="flex items-center gap-3">
              <div className={`w-9 h-9 rounded-xl ${p.color} text-white flex items-center justify-center`}><p.icon className="w-5 h-5"/></div>
              <div><div className="text-sm font-medium">{p.label}</div><div className="text-xs text-zinc-500 leading-tight">{p.desc}</div></div>
            </div>
            <div className="grid grid-cols-5 gap-1 mt-3 text-center">
              {['queued','processing','done','failed','needs_input'].map(k=> (
                <div key={k} className="bg-zinc-50 dark:bg-zinc-800 rounded-lg py-2">
                  <div className="text-sm font-semibold mono">{stats[p.id]?.[k] ?? stats[p.id]?.ai_queue?.[k] ?? 0}</div>
                  <div className="text-[10px] mono uppercase text-zinc-500">{k}</div>
                </div>
              ))}
            </div>
            {p.id==='ai' && stats.ai?.ai_queue?.queue_preview && (
              <div className="mt-3 text-xs mono bg-zinc-50 dark:bg-zinc-800 rounded-xl p-2">
                <div className="font-medium flex items-center gap-1"><Clock className="w-3 h-3"/> Next in AI queue</div>
                {(stats.ai.ai_queue.queue_preview as any[]).slice(0,3).map((q:any)=> <div key={q.id} className="flex justify-between text-[11px] mt-1"><span>{q.workflow} • P{q.priority}</span><span className="text-zinc-500">{q.status}</span></div>)}
                {stats.ai.ai_queue.processing_now && <div className="mt-1 text-emerald-600 dark:text-emerald-400">Processing: {stats.ai.ai_queue.processing_now.workflow}</div>}
                <div className="mt-2 flex gap-2 text-[11px]"><span className="px-2 py-1 rounded-full bg-white dark:bg-zinc-900 border mono">RPM {stats.rate_limiter.remaining}/{stats.rate_limiter.rpm}</span><span className="px-2 py-1 rounded-full bg-white dark:bg-zinc-900 border mono">Throttled {stats.rate_limiter.throttled}</span></div>
              </div>
            )}
          </div>
        ))}
      </div>

      {/* user input needed queue */}
      <div className="card p-5">
        <h3 className="font-medium flex items-center gap-2"><UserCheck className="w-4 h-4"/> User Input Needed Queue <span className="text-xs mono font-normal text-zinc-500">remains until you complete • then re-queued to application</span></h3>
        {inputQueue.length===0 ? <div className="py-6 text-center text-sm mono text-zinc-500">No pending inputs. When autofill hits unknown fields, jobs pause here.</div> :
          <div className="mt-3 grid gap-3">
            {inputQueue.map((req:any)=> (
              <div key={req.id} className="border dark:border-zinc-800 rounded-xl p-4 bg-amber-50/50 dark:bg-amber-950/20">
                <div className="flex items-center justify-between">
                  <div className="text-sm font-medium">Job #{req.job_id} • {req.fields.length} fields required</div>
                  <span className="text-xs mono px-2 py-1 rounded-full bg-amber-500 text-white">pending</span>
                </div>
                <InputForm req={req} onSubmit={(payload)=>submitInput(req,payload)} />
              </div>
            ))}
          </div>
        }
      </div>

      <div className="card overflow-hidden">
        <div className="p-3 border-b dark:border-zinc-800 flex items-center gap-2">
          <span className="text-sm font-medium">Pipeline jobs</span>
          <div className="ml-auto flex gap-1">
            {['discovery','application','ai','email'].map(p=> <button key={p} onClick={()=>setFilter(p)} className={`text-xs px-3 py-1 rounded-full border mono ${filter===p?'bg-zinc-900 text-white border-zinc-900 dark:bg-white dark:text-zinc-900':'bg-white dark:bg-zinc-800'}`}>{p}</button>)}
          </div>
        </div>
        <div className="max-h-[50vh] overflow-auto divide-y dark:divide-zinc-800">
          {jobs.length===0 ? <div className="p-8 text-center mono text-sm text-zinc-500">No jobs in this pipeline</div> :
            jobs.map((j:any)=> (
              <div key={j.id} className="p-3 flex gap-3 text-sm">
                <div className={`w-2 h-2 rounded-full mt-2 shrink-0 ${j.status==='done'?'bg-emerald-500': j.status==='failed'?'bg-red-500': j.status==='processing'?'bg-blue-500': j.status==='needs_input'?'bg-amber-500':'bg-zinc-400'}`} />
                <div className="min-w-0 flex-1">
                  <div className="mono text-xs text-zinc-500">#{j.id} • {j.pipeline} • P{j.priority} • {new Date(j.created_at).toLocaleString()}</div>
                  <div className="font-medium mono text-xs mt-1 truncate">{JSON.stringify(j.payload).slice(0,120)}</div>
                  {j.error && <div className="text-xs text-red-600 dark:text-red-400 mt-1">{j.error}</div>}
                </div>
                <span className="text-xs mono px-2 py-1 rounded-full bg-zinc-100 dark:bg-zinc-800 h-fit">{j.status}</span>
              </div>
            ))}
        </div>
      </div>
    </div>
  )
}

function InputForm({req, onSubmit}: any){
  const [values, setValues] = useState<Record<string,any>>({})
  useEffect(()=>{
    const init:any={}
    req.fields.forEach((f:any)=> init[f.name]=f.value || '')
    setValues(init)
  },[req])
  return (
    <div className="mt-3 space-y-2">
      {req.fields.map((f:any)=> (
        <div key={f.name}>
          <label className="text-xs mono">{f.label} {f.required && <span className="text-red-500">*</span>}</label>
          {f.type==='select' ? <select value={values[f.name]||''} onChange={e=>setValues({...values,[f.name]:e.target.value})} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"><option value="">Select…</option><option>Yes</option><option>No</option></select>
            : <input value={values[f.name]||''} onChange={e=>setValues({...values,[f.name]:e.target.value})} placeholder={f.label} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"/>}
        </div>
      ))}
      <button onClick={()=>onSubmit(values)} className="px-4 py-2 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm font-medium inline-flex items-center gap-2">Submit & re-queue <ArrowRight className="w-4 h-4"/></button>
    </div>
  )
}
