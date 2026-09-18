import { useEffect, useState } from 'react'
import client, { apiError } from '../api/client'
import { ShieldCheck, AlertTriangle, Check, X, Edit2, History, FileText, BadgeCheck, Clock } from 'lucide-react'

type Field = {
  provenance_id: number
  path: string
  value_masked: boolean
  value_preview: string | null
  value_hash: string
  origin: string
  sensitivity: string
  confidence: number | null
  confidence_band: string
  evidence: { kind: string; quote: string; locator: any }[]
  review_status: string
  review_required: boolean
  ambiguity: string
  needs_answer_for: string[]
  candidates: { value: string; source: string; confidence: number }[]
  used_by: any[]
}

type ReviewDoc = {
  profile_id: number
  version: number
  state: string
  is_current: boolean
  document: any
  completeness: { percent: number; required_filled: number; required_total: number; missing: string[]; uncertain: string[]; breakdown: any }
  review: { required: number; resolved: number; deferred: number; completed_at: string | null }
  fields: Field[]
  server_time: string
}

type CompletionReq = {
  field: string
  label: string
  type: string
  required: boolean
  status: string
  confidence: number
  band: string
  evidence: any[]
  candidates: any[]
  message: string
}

function bandColor(band: string) {
  switch (band) {
    case 'high': return 'bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:border-emerald-800'
    case 'medium': return 'bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:border-amber-800'
    case 'low': return 'bg-orange-50 text-orange-700 border-orange-200 dark:bg-orange-950 dark:text-orange-300 dark:border-orange-800'
    default: return 'bg-zinc-100 text-zinc-600 border-zinc-200 dark:bg-zinc-800 dark:text-zinc-400 dark:border-zinc-700'
  }
}

function statusColor(status: string) {
  switch (status) {
    case 'confirmed': case 'auto_accepted': case 'corrected': return 'bg-emerald-600 text-white'
    case 'needs_review': return 'bg-amber-500 text-white'
    case 'missing': return 'bg-zinc-400 text-white'
    case 'conflicting': return 'bg-red-500 text-white'
    case 'uncertain': return 'bg-orange-400 text-white'
    default: return 'bg-zinc-200 text-zinc-700'
  }
}

function originLabel(origin: string) {
  switch (origin) {
    case 'resume_extraction': return 'Resume'
    case 'user_confirmed': return 'User confirmed'
    case 'user_corrected': return 'User corrected'
    case 'ai_inferred': return 'AI inferred'
    case 'heuristic': return 'Missing / heuristic'
    default: return origin
  }
}

export default function ProfileReview() {
  const [doc, setDoc] = useState<ReviewDoc | null>(null)
  const [completeness, setCompleteness] = useState<any>(null)
  const [requirements, setRequirements] = useState<CompletionReq[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [editing, setEditing] = useState<Record<string, string>>({})
  const [history, setHistory] = useState<any[]>([])
  const [showHistory, setShowHistory] = useState<string | null>(null)
  const [notice, setNotice] = useState('')

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const { data } = await client.get('/api/profile/review')
      setDoc(data)
      try {
        const c = await client.get('/api/profile/completeness', { params: { profile_id: data.profile_id } })
        setCompleteness(c.data.completeness)
        setRequirements(c.data.completion_requirements || [])
      } catch {}
    } catch (e: any) {
      setError(apiError(e, 'Failed to load profile review'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const resolve = async (path: string, action: 'confirm' | 'correct' | 'reject' | 'defer', value?: any) => {
    if (!doc) return
    setNotice('')
    try {
      const { data } = await client.post('/api/profile/review/resolve', {
        profile_id: doc.profile_id,
        path,
        action,
        value,
        reason: action === 'correct' ? 'user correction via UI' : undefined
      })
      setDoc(data)
      setNotice(`✓ ${path} ${action}ed`)
      // refresh completeness
      try {
        const c = await client.get('/api/profile/completeness', { params: { profile_id: data.profile_id } })
        setCompleteness(c.data.completeness)
        setRequirements(c.data.completion_requirements || [])
      } catch {}
    } catch (e: any) {
      setNotice(apiError(e, 'Resolve failed'))
    }
  }

  const loadHistory = async (path: string) => {
    if (!doc) return
    try {
      const { data } = await client.get('/api/profile/history', { params: { profile_id: doc.profile_id, path } })
      setHistory(data.history || [])
      setShowHistory(path)
    } catch (e: any) {
      setNotice(apiError(e, 'Failed to load history'))
    }
  }

  if (loading) return <div className="p-6 mono text-sm text-zinc-500">Loading profile review…</div>
  if (error) return <div className="p-6"><div className="p-3 rounded-xl bg-red-50 border border-red-200 text-sm text-red-700">{error}</div><button onClick={load} className="mt-3 px-4 py-2 rounded-full bg-zinc-900 text-white text-sm">Retry</button></div>
  if (!doc) return <div className="p-6 mono text-sm">No profile. Upload a resume first.</div>

  const needsReview = doc.fields.filter(f => f.review_required)
  const confirmed = doc.fields.filter(f => !f.review_required)

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold tracking-tight flex items-center gap-2"><FileText className="w-5 h-5"/> Candidate Profile Review</h1>

      {notice && <div className={`p-3 rounded-xl border text-sm mono ${notice.startsWith('✓') ? 'bg-emerald-50 border-emerald-200 text-emerald-700' : 'bg-red-50 border-red-200 text-red-700'}`}>{notice}</div>}

      <div className="grid md:grid-cols-3 gap-4">
        <div className="card p-4">
          <h3 className="font-medium text-sm flex items-center gap-2"><BadgeCheck className="w-4 h-4"/> Completeness</h3>
          <div className="mt-3">
            <div className="text-3xl font-bold">{completeness?.percent ?? doc.completeness?.percent ?? 0}%</div>
            <div className="w-full bg-zinc-200 dark:bg-zinc-800 rounded-full h-2 mt-2"><div className="bg-blue-600 h-2 rounded-full" style={{width: `${completeness?.percent ?? doc.completeness?.percent ?? 0}%`}} /></div>
            <div className="text-xs mono mt-2 text-zinc-500">
              {completeness?.required_filled ?? doc.completeness?.required_filled ?? 0} / {completeness?.required_total ?? doc.completeness?.required_total ?? 0} required fields filled
            </div>
            <div className="text-xs mono mt-1">State: <span className="font-medium">{doc.state}</span> • Version {doc.version}</div>
          </div>
          <div className="mt-4 space-y-1 text-[11px] mono">
            <div className="font-medium">Breakdown</div>
            {Object.entries((completeness?.breakdown || doc.completeness?.breakdown || {}) as any).slice(0,12).map(([k, v]: any) => (
              <div key={k} className="flex justify-between"><span>{k}</span><span className={`px-1.5 rounded ${v.status==='confirmed'?'text-emerald-600': v.status==='missing'?'text-zinc-400':'text-amber-600'}`}>{v.status} {v.confidence?.toFixed ? v.confidence.toFixed(2) : ''}</span></div>
            ))}
          </div>
        </div>

        <div className="md:col-span-2 card p-4">
          <h3 className="font-medium text-sm flex items-center gap-2"><AlertTriangle className="w-4 h-4"/> Completion Requirements</h3>
          {requirements.length === 0 ? <div className="mt-3 text-sm mono text-emerald-600">All required fields complete ✓</div> : (
            <div className="mt-3 space-y-2">
              {requirements.map(r => (
                <div key={r.field} className={`p-2.5 rounded-xl border text-xs mono ${r.required ? 'bg-amber-50 border-amber-200 dark:bg-amber-950 dark:border-amber-800' : 'bg-zinc-50 border-zinc-200 dark:bg-zinc-800 dark:border-zinc-700'}`}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium">{r.label} {r.required && <span className="text-red-600">*</span>}</span>
                    <span className={`px-2 py-0.5 rounded-full text-[10px] border ${bandColor(r.band)}`}>{r.status} • {r.band} {r.confidence.toFixed(2)}</span>
                  </div>
                  <div className="mt-1 opacity-80">{r.message}</div>
                  {r.candidates.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {r.candidates.map((c:any, i:number) => <span key={i} className="px-2 py-0.5 rounded-full bg-white border text-[10px]">{c.value.slice(0,60)}</span>)}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="card p-4">
        <h3 className="font-medium text-sm flex items-center gap-2"><ShieldCheck className="w-4 h-4"/> Fields needing review ({needsReview.length})</h3>
        <div className="mt-3 grid md:grid-cols-2 gap-3">
          {needsReview.map(f => (
            <div key={f.path} className="border dark:border-zinc-700 rounded-xl p-3 bg-amber-50/50 dark:bg-amber-950/20">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium mono">{f.path}</span>
                <span className={`text-[10px] px-2 py-0.5 rounded-full ${statusColor(f.review_status)}`}>{f.review_status}</span>
              </div>
              <div className="mt-1 flex items-center gap-2 text-[11px] mono">
                <span className={`px-2 py-0.5 rounded-full border ${bandColor(f.confidence_band)}`}>{f.confidence_band} {f.confidence?.toFixed(2) ?? '—'}</span>
                <span className="px-2 py-0.5 rounded-full bg-white border">{originLabel(f.origin)}</span>
                <span className={`px-2 py-0.5 rounded-full border ${f.sensitivity==='sensitive' || f.sensitivity==='restricted' ? 'bg-red-50 border-red-200 text-red-700' : 'bg-zinc-100 border-zinc-200'}`}>{f.sensitivity}</span>
              </div>
              <div className="mt-2 text-xs mono">
                {f.value_masked ? <span className="text-zinc-500">*** masked (sensitive) ***</span> : <span className="break-all">{f.value_preview || '(missing)'}</span>}
              </div>
              {f.evidence.length > 0 && (
                <div className="mt-2 space-y-1">
                  <div className="text-[11px] mono font-medium">Evidence ({f.evidence.length})</div>
                  {f.evidence.map((ev, i) => (
                    <div key={i} className="text-[11px] mono p-1.5 rounded bg-white dark:bg-zinc-900 border dark:border-zinc-700">"{ev.quote}" <span className="opacity-60">[{ev.kind}] {ev.locator?.start != null ? `at ${ev.locator.start}-${ev.locator.end}` : ''}</span></div>
                  ))}
                </div>
              )}
              {f.candidates.length > 0 && (
                <div className="mt-2">
                  <div className="text-[11px] mono">Candidates</div>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {f.candidates.map((c, i) => <button key={i} onClick={() => resolve(f.path, 'correct', c.value)} className="px-2 py-1 rounded-full bg-white border text-[11px] hover:bg-zinc-100">{c.value.slice(0,80)}</button>)}
                  </div>
                </div>
              )}
              <div className="mt-3 flex flex-wrap gap-2">
                <button onClick={() => resolve(f.path, 'confirm')} className="px-3 py-1.5 rounded-full bg-emerald-600 text-white text-xs inline-flex items-center gap-1"><Check className="w-3 h-3"/> Confirm</button>
                <button onClick={() => setEditing({...editing, [f.path]: editing[f.path] ?? (f.value_preview || '')})} className="px-3 py-1.5 rounded-full bg-zinc-900 text-white text-xs inline-flex items-center gap-1"><Edit2 className="w-3 h-3"/> Correct</button>
                <button onClick={() => resolve(f.path, 'reject')} className="px-3 py-1.5 rounded-full bg-red-600 text-white text-xs inline-flex items-center gap-1"><X className="w-3 h-3"/> Reject</button>
                <button onClick={() => loadHistory(f.path)} className="px-3 py-1.5 rounded-full bg-white border text-xs inline-flex items-center gap-1"><History className="w-3 h-3"/> History</button>
              </div>
              {editing[f.path] !== undefined && (
                <div className="mt-2 flex gap-2">
                  <input value={editing[f.path]} onChange={e => setEditing({...editing, [f.path]: e.target.value})} className="flex-1 border rounded-full px-3 py-1.5 text-xs mono bg-white dark:bg-zinc-900" placeholder="Corrected value"/>
                  <button onClick={() => { resolve(f.path, 'correct', editing[f.path]); const n={...editing}; delete n[f.path]; setEditing(n)}} className="px-3 py-1.5 rounded-full bg-blue-600 text-white text-xs">Save</button>
                  <button onClick={() => { const n={...editing}; delete n[f.path]; setEditing(n)}} className="px-3 py-1.5 rounded-full bg-zinc-200 text-xs">Cancel</button>
                </div>
              )}
            </div>
          ))}
        </div>
        {needsReview.length === 0 && <div className="mt-3 text-sm mono text-emerald-600">No fields need review — profile is active ✓</div>}
      </div>

      <div className="card p-4">
        <h3 className="font-medium text-sm">All fields ({doc.fields.length})</h3>
        <div className="mt-3 grid md:grid-cols-2 gap-3">
          {doc.fields.map(f => (
            <div key={f.path} className={`border rounded-xl p-3 ${f.review_required ? 'bg-white dark:bg-zinc-900' : 'bg-emerald-50/30 dark:bg-emerald-950/10'} dark:border-zinc-700`}>
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs mono font-medium">{f.path}</span>
                <span className={`text-[10px] px-2 py-0.5 rounded-full ${statusColor(f.review_status)}`}>{f.review_status}</span>
              </div>
              <div className="mt-1 text-[11px] mono flex gap-1">
                <span className={`px-1.5 py-0.5 rounded border ${bandColor(f.confidence_band)}`}>{f.confidence_band}</span>
                <span>{originLabel(f.origin)}</span>
              </div>
              <div className="mt-1 text-xs mono truncate">{f.value_masked ? '***' : (f.value_preview?.slice(0,200) || '(empty)')}</div>
            </div>
          ))}
        </div>
      </div>

      {showHistory && (
        <div className="card p-4">
          <h3 className="font-medium text-sm flex items-center gap-2"><Clock className="w-4 h-4"/> History for {showHistory} <button onClick={() => setShowHistory(null)} className="ml-auto text-xs px-2 py-1 rounded-full bg-zinc-200">Close</button></h3>
          <div className="mt-3 space-y-2">
            {history.map(h => (
              <div key={h.event_id} className="border rounded-xl p-2 text-[11px] mono bg-zinc-50 dark:bg-zinc-800 dark:border-zinc-700">
                <div className="flex justify-between"><span>{h.actor_type} #{h.actor_id} • {h.origin_before} → {h.origin_after}</span><span>{h.occurred_at}</span></div>
                <div className="mt-1">{h.review_status_before} → {h.review_status_after} • {h.reason}</div>
                <div className="mt-1 flex gap-2"><span className="truncate">prev: {h.previous_value_preview?.slice(0,120)}</span><span className="truncate">new: {h.new_value_preview?.slice(0,120)}</span></div>
              </div>
            ))}
            {history.length === 0 && <div className="text-xs mono text-zinc-500">No history yet.</div>}
          </div>
        </div>
      )}
    </div>
  )
}
