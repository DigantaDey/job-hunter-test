import { useEffect, useState } from 'react'
import client, { apiError } from '../api/client'
import { useAIWork } from '../context/AIWorkContext'
import { AI_QUEUE_POLL_MS } from '../hooks/useAIQueue'
import { StateIcon } from '../components/AIWorkChip'
import { describeRow, deriveState, toneClasses } from '../lib/aiWork'
import { Layers, Bot, Search, Send, Clock, CheckCircle, AlertTriangle, Pause, Play, ArrowRight, UserCheck } from 'lucide-react'

/**
 * Cadence for this page's three reads — `/api/pipelines/stats`,
 * `/api/pipelines/jobs` and `/api/user-input-queue`.
 *
 * It was 3 s: 60 requests a minute from a single open tab, 8× the 25 s the rest
 * of the app polls at, and all of it for numbers that move slowly (per-pipeline
 * counters, a job list). The one thing on this page that genuinely *is* live —
 * the "Processing now" card — reads the global `useAIWork()` layer, which polls
 * `/api/queues/ai` on its own 4 s cadence (`useAIQueue`), not this one. So
 * slowing these three reads down to 20 s changes what a user sees by nothing
 * they were watching for, and takes the page from 60 req/min to 9.
 *
 * Same shape as `POLL_INTERVAL_MS` on Emails (25 s) and `AUTOMATION_POLL_MS` on
 * Settings (30 s): a named constant, armed only while the tab is visible.
 */
export const QUEUES_POLL_MS = 20_000

/** The seven statuses `queue_stats` returns per pipeline — rendered in this order. */
const STATUS_CHIPS = ['queued','processing','done','failed','needs_input','paused','dead'] as const
type ChipStatus = typeof STATUS_CHIPS[number]

/**
 * Hover copy per status.
 *
 * v2.2.2 — "attempts" means *failure* attempts everywhere in this file: the
 * backend counts a handler failure, never a claim. Being picked up by a worker
 * (or parked by an AI outage and resumed) costs an item nothing, so the hints
 * say "failed attempts" instead of implying every run spends the budget.
 */
const STATUS_HINTS: Record<ChipStatus, string> = {
  queued: 'Waiting for a worker slot',
  processing: 'Running right now',
  done: 'Finished',
  failed: 'Failed — retried until its failure attempts run out, then marked dead',
  needs_input: 'Waiting on your answers (User Input Needed queue below)',
  paused: 'AI outage — parked, resumes automatically when the provider is back (parking costs no failure attempt)',
  dead: 'Out of failure attempts, or out of AI-outage pauses — nothing more will happen without a manual retry',
}

/**
 * Why a `dead` row gave up — the two budgets are separate.
 *
 * `attempts` counts *handler failures* against `max_attempts`; an item can also
 * be dead-lettered by the AI-outage pause cap (`payload.paused_count`, 12) with
 * zero failures. v2.2.2: the old "gave up after N attempts" read the claim
 * counter, so an outage-parked job reported attempts it had never failed and a
 * pause-capped job read "gave up after 0 attempts".
 */
function deadReason(j: any): string {
  const failures = Number(j?.attempts ?? 0)
  const cap = Number(j?.max_attempts ?? 0)
  const pauses = Number(j?.payload?.paused_count ?? 0)
  if (failures === 0 && pauses > 0) return `gave up after ${pauses} AI-outage pauses — it never failed`
  const spent = cap > 0 ? `${failures} of ${cap} failed attempts` : `${failures} failed attempt${failures === 1 ? '' : 's'}`
  return pauses > 0 ? `gave up after ${spent} and ${pauses} AI-outage pause${pauses === 1 ? '' : 's'}` : `gave up after ${spent}`
}

/** Amber for a non-zero paused count, grey for dead, neutral otherwise. */
function chipTone(k: ChipStatus, n: number): string {
  if (k==='paused' && n>0) return 'bg-amber-50 dark:bg-amber-950 text-amber-800 dark:text-amber-200 border border-amber-200 dark:border-amber-800'
  if (k==='dead') return 'bg-zinc-200/70 dark:bg-zinc-700 text-zinc-700 dark:text-zinc-200'
  return 'bg-zinc-50 dark:bg-zinc-800 text-zinc-500'
}

export default function Queues(){
  const [stats, setStats] = useState<any>(null)
  const [jobs, setJobs] = useState<any[]>([])
  const [inputQueue, setInputQueue] = useState<any[]>([])
  const [filter, setFilter] = useState('application')
  const [inputError, setInputError] = useState<string | null>(null)

  const [loadError, setLoadError] = useState<string | null>(null)
  const [lastPollAt, setLastPollAt] = useState<number | null>(null)
  // The global live layer (v2.2.8): `processing_now`/counts across *every*
  // pipeline, shared with the header chip — so this page and the chip can
  // never disagree about what is running.
  const work = useAIWork()

  const load = async()=>{
    try{
      const {data}=await client.get('/api/pipelines/stats')
      setStats(data)
      const {data: j}=await client.get('/api/pipelines/jobs', {params:{pipeline: filter}})
      setJobs(j)
      const {data: iq}=await client.get('/api/user-input-queue')
      setInputQueue(iq)
      setLoadError(null)
      setLastPollAt(Date.now())
    }catch(e:any){
      // Keep the last good view; say the read failed instead of blanking.
      setLoadError(apiError(e, 'Live update failed'))
    }
  }
  // Live (v2.2.8): poll every QUEUES_POLL_MS (20 s) while the tab is visible;
  // pause when hidden and re-read immediately when it becomes visible again.
  // What is running *right now* is not this poll's job — the "Processing now"
  // card below reads the global `useAIWork()` layer, which polls on its own
  // `AI_QUEUE_POLL_MS` cadence, so this interval only has to keep the counters
  // and the job list roughly current.
  useEffect(()=>{
    let id: ReturnType<typeof setInterval> | null = null
    const start = ()=>{ if(id) return; load(); id = setInterval(load, QUEUES_POLL_MS) }
    const stop = ()=>{ if(id) clearInterval(id); id = null }
    const onVis = ()=>{ document.visibilityState === 'hidden' ? stop() : start() }
    if(document.visibilityState !== 'hidden') start()
    document.addEventListener('visibilitychange', onVis)
    return ()=>{ stop(); document.removeEventListener('visibilitychange', onVis) }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  },[filter])

  // Deep link from the chip/dashboard: /queues#user-input scrolls to the section.
  useEffect(()=>{
    if(window.location.hash === '#user-input'){
      const el = document.getElementById('user-input')
      if(el){ el.scrollIntoView({behavior:'smooth', block:'start'}); (el as HTMLElement).focus?.() }
    }
  },[stats === null])

  const submitInput = async(item:any, values:Record<string, any>)=>{
    // The endpoint expects InputPayload = {answers: {...}}. Posting the bare
    // values object returned 200 while silently discarding every answer, so the
    // job was re-queued with the same missing fields and failed again.
    setInputError(null)
    try{
      await client.post(`/api/jobs/${item.job_id}/input`, { answers: values })
      load()
    }catch(e:any){ setInputError(apiError(e, 'Could not submit your answers')) }
  }

  if(!stats) return <div className="p-8 mono text-sm">Loading queues…</div>

  const pipelines = [
    {id:'discovery', label:'Job Discovery', icon: Search, desc:'Scrapes via keywords + freshness, AI finds structure', color:'bg-blue-600'},
    {id:'application', label:'Job Application', icon: Send, desc:'Autofill profile, credential, submit; external redirect handled', color:'bg-violet-600'},
    {id:'ai', label:'AI API', icon: Bot, desc:'Durable priority queue, rate-limited; pauses on an AI outage and resumes on its own', color:'bg-emerald-600'},
  ]

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold tracking-tight flex items-center gap-2 flex-wrap"><Layers className="w-5 h-5"/> Pipelines & Queues <span className="text-xs mono font-normal text-zinc-500">three in parallel • FIFO</span>
        {/* The cadence is honest but slow, so say so on hover (the wording is
            unchanged — the timestamp already tells the truth): these three reads
            come every QUEUES_POLL_MS, while "Processing now" is the global 4 s
            live layer. Same footnote Settings puts under its auto-mode card. */}
        <span
          className="ml-auto text-[11px] mono font-normal text-zinc-500 inline-flex items-center gap-1"
          aria-live="polite"
          title={`Counters and the job list re-read every ${QUEUES_POLL_MS / 1000}s while this tab is visible — polling pauses in a background tab and catches up the moment you come back. “Processing now” is the global live read, every ${AI_QUEUE_POLL_MS / 1000}s.`}
        >
          <span className={`w-2 h-2 rounded-full ${loadError ? 'bg-red-500' : 'bg-emerald-500 animate-pulse'}`} aria-hidden />
          {loadError ? `live update failed — ${loadError}` : `live • updated ${lastPollAt ? new Date(lastPollAt).toLocaleTimeString() : '…'}`}
        </span>
      </h1>

      {/* Processing now — across every pipeline, from the same poll as the header chip. */}
      <div className="card p-4" data-testid="processing-now">
        <div className="flex items-center gap-2 text-sm font-medium"><Play className="w-4 h-4"/> Processing now
          <span className="text-xs mono font-normal text-zinc-500">{work.counts.queued} queued • {work.counts.processing} processing • {work.counts.paused} paused{work.counts.needs_input ? ` • ${work.counts.needs_input} need input` : ''}</span>
        </div>
        {work.snapshot?.processing_now ? (()=>{ const row = work.snapshot!.processing_now!; const st = deriveState(row); return (
          <div className={`mt-2 px-3 py-2 rounded-xl border text-xs mono flex items-start gap-2 ${toneClasses(st?.tone || 'busy')}`}>
            <StateIcon state={st} />
            <div className="min-w-0 flex-1">
              <div className="flex justify-between gap-2"><span className="font-medium">#{row.id} {describeRow(row)} • {row.pipeline}</span><span>{st?.label || row.status}</span></div>
              {st?.detail && <div className="opacity-80 mt-0.5">{st.detail}</div>}
            </div>
          </div>
        )})() : (
          <div className="mt-2 text-xs mono text-zinc-500">{work.snapshot ? 'idle — no worker is executing anything right now' : 'reading…'}</div>
        )}
        {(work.snapshot?.items || []).filter(r=>r.status!=='processing').slice(0,6).map(row=>{ const st = deriveState(row); return (
          <div key={row.id} className={`mt-1 px-3 py-1.5 rounded-lg border text-[11px] mono flex items-start gap-2 ${toneClasses(st?.tone || 'neutral')}`}>
            <StateIcon state={st} />
            <div className="min-w-0 flex-1 flex justify-between gap-2"><span className="truncate">#{row.id} {describeRow(row)}</span><span className="shrink-0">{st?.label || row.status}</span></div>
          </div>
        )})}
      </div>

      <div className="grid lg:grid-cols-3 gap-3">
        {pipelines.map(p=> (
          <div key={p.id} className="card p-4">
            <div className="flex items-center gap-3">
              <div className={`w-9 h-9 rounded-xl ${p.color} text-white flex items-center justify-center`}><p.icon className="w-5 h-5"/></div>
              <div><div className="text-sm font-medium">{p.label}</div><div className="text-xs text-zinc-500 leading-tight">{p.desc}</div></div>
            </div>
            {/* All seven durable statuses the backend counts (queue_stats). `paused`
                is the v2.1 headline state: an AI outage parks the work and it
                resumes on its own (parking costs no failure attempt — v2.2.2);
                `dead` is a job that ran out of *failure* attempts, or out of
                AI-outage pauses. */}
            <div className="grid grid-cols-4 sm:grid-cols-7 gap-1 mt-3 text-center">
              {STATUS_CHIPS.map(k=> {
                const n = stats[p.id]?.[k] ?? 0
                return (
                  <div key={k} className={`rounded-lg py-2 ${chipTone(k, n)}`} title={STATUS_HINTS[k]}>
                    <div className="text-sm font-semibold mono">{n}</div>
                    <div className="text-[10px] mono uppercase leading-tight">{k.replace('_',' ')}</div>
                  </div>
                )
              })}
            </div>
            {/* GET /api/pipelines/stats puts `ai_queue` and `rate_limiter` at the
                TOP level of the payload (siblings of the per-pipeline buckets),
                not under `stats.ai` — reading `stats.ai.ai_queue` meant this
                panel never rendered. */}
            {p.id==='ai' && stats.ai_queue && (
              <div className="mt-3 text-xs mono bg-zinc-50 dark:bg-zinc-800 rounded-xl p-2">
                <div className="font-medium flex items-center gap-1"><Clock className="w-3 h-3"/> Next in AI queue</div>
                {(stats.ai_queue.queue_preview as any[] || []).length===0 && !stats.ai_queue.processing_now && <div className="text-[11px] mt-1 text-zinc-500">idle — nothing waiting</div>}
                {(stats.ai_queue.queue_preview as any[] || []).slice(0,3).map((q:any)=> (
                  <div key={q.id} className="flex justify-between gap-2 text-[11px] mt-1">
                    <span>#{q.id} {q.workflow} • P{q.priority}</span>
                    <span className={q.status==='paused'?'text-amber-700 dark:text-amber-300':'text-zinc-500'}>{q.status}{q.status==='paused'?' — resumes automatically':''}</span>
                  </div>
                ))}
                {stats.ai_queue.processing_now && <div className="mt-1 text-emerald-600 dark:text-emerald-400">Processing: #{stats.ai_queue.processing_now.id} {stats.ai_queue.processing_now.workflow}</div>}
                {stats.rate_limiter && <div className="mt-2 flex gap-2 text-[11px]"><span className="px-2 py-1 rounded-full bg-white dark:bg-zinc-900 border mono">RPM {stats.rate_limiter.remaining}/{stats.rate_limiter.rpm}</span><span className="px-2 py-1 rounded-full bg-white dark:bg-zinc-900 border mono">Throttled {stats.rate_limiter.throttled}</span></div>}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* user input needed queue */}
      <div className="card p-5" id="user-input" tabIndex={-1}>
        <h3 className="font-medium flex items-center gap-2"><UserCheck className="w-4 h-4"/> User Input Needed Queue <span className="text-xs mono font-normal text-zinc-500">remains until you complete • then re-queued to application</span></h3>
        {inputError && <div className="mt-2 text-xs mono p-2 rounded-lg bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 text-red-700 dark:text-red-300">{inputError}</div>}
        {inputQueue.length===0 ? <div className="py-6 text-center text-sm mono text-zinc-500">No pending inputs. When autofill hits unknown fields, jobs pause here.</div> :
          <div className="mt-3 grid gap-3">
            {inputQueue.map((req:any)=> (
              <div key={req.id} className="border dark:border-zinc-800 rounded-xl p-4 bg-amber-50/50 dark:bg-amber-950/20">
                <div className="flex items-center justify-between">
                  <div className="text-sm font-medium">Job #{req.job_id} • {req.fields.length} fields required</div>
                  <span className="text-xs mono px-2 py-1 rounded-full bg-amber-500 text-white">pending</span>
                </div>
                <InputForm req={req} onSubmit={(payload: any)=>submitInput(req,payload)} />
              </div>
            ))}
          </div>
        }
      </div>

      <div className="card overflow-hidden">
        <div className="p-3 border-b dark:border-zinc-800 flex items-center gap-2">
          <span className="text-sm font-medium">Pipeline jobs</span>
          <div className="ml-auto flex gap-1">
            {['discovery','application','ai','email','funding'].map(p=> <button key={p} onClick={()=>setFilter(p)} aria-pressed={filter===p} className={`text-xs px-3 min-h-[44px] rounded-full border mono focus:outline-none focus:ring-2 focus:ring-blue-500 ${filter===p?'bg-zinc-900 text-white border-zinc-900 dark:bg-white dark:text-zinc-900':'bg-white dark:bg-zinc-800'}`}>{p}</button>)}
          </div>
        </div>
        <div className="max-h-[50vh] overflow-auto divide-y dark:divide-zinc-800">
          {jobs.length===0 ? <div className="p-8 text-center mono text-sm text-zinc-500">No jobs in this pipeline</div> :
            jobs.map((j:any)=> (
              <div key={j.id} className="p-3 flex gap-3 text-sm">
                <div className={`w-2 h-2 rounded-full mt-2 shrink-0 ${j.status==='done'?'bg-emerald-500': j.status==='failed'?'bg-red-500': j.status==='processing'?'bg-blue-500': j.status==='paused'?'bg-amber-400': j.status==='needs_input'?'bg-amber-500':'bg-zinc-400'}`} />
                <div className="min-w-0 flex-1">
                  <div className="mono text-xs text-zinc-500">#{j.id} • {j.pipeline} • P{j.priority} • {new Date(j.created_at).toLocaleString()}</div>
                  <div className="font-medium mono text-xs mt-1 truncate">{JSON.stringify(j.payload).slice(0,120)}</div>
                  {j.error && <div className="text-xs text-red-600 dark:text-red-400 mt-1">{j.error}</div>}
                  {(()=>{ const st = deriveState(j); return st && st.phase !== j.status ? <div className="text-[11px] mono text-zinc-500 mt-1">{st.label}{st.detail ? ` — ${st.detail}` : ''}</div> : null })()}
                </div>
                <div className="flex flex-col items-end gap-1 shrink-0">
                  <span className={`text-xs mono px-2 py-1 rounded-full h-fit ${j.status==='paused'?'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200': j.status==='dead'?'bg-zinc-200 text-zinc-700 dark:bg-zinc-700 dark:text-zinc-200':'bg-zinc-100 dark:bg-zinc-800'}`}>{j.status}</span>
                  {/* v2.1: amber must never be a mystery — say what happens next. */}
                  {j.status==='paused' && <span className="text-[11px] mono text-amber-700 dark:text-amber-300 text-right">AI outage — will resume automatically</span>}
                  {j.status==='dead' && <span className="text-[11px] mono text-zinc-500 text-right" title="Attempts counts handler failures only — being claimed, or parked by an AI outage, costs nothing.">{deadReason(j)}</span>}
                </div>
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
          {f.type==='select' ? <select value={values[f.name]||''} onChange={e=>setValues({...values,[f.name]:e.target.value})} className="w-full mt-1 border rounded-xl px-3 py-2 min-h-[44px] text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700 focus:outline-none focus:ring-2 focus:ring-blue-500"><option value="">Select…</option><option>Yes</option><option>No</option></select>
            : <input value={values[f.name]||''} onChange={e=>setValues({...values,[f.name]:e.target.value})} placeholder={f.label} className="w-full mt-1 border rounded-xl px-3 py-2 min-h-[44px] text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700 focus:outline-none focus:ring-2 focus:ring-blue-500"/>}
        </div>
      ))}
      <button onClick={()=>onSubmit(values)} className="px-4 py-2 min-h-[44px] rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm font-medium inline-flex items-center gap-2 focus:outline-none focus:ring-2 focus:ring-blue-500">Submit & re-queue <ArrowRight className="w-4 h-4"/></button>
    </div>
  )
}
