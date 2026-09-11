import { useEffect, useState, useCallback } from 'react'
import client, { apiError } from '../api/client'
import { TrendingUp, ExternalLink, Mail, Briefcase, Loader2, Sparkles, RefreshCw, Calendar, Target, Building2, CheckCircle2 } from 'lucide-react'
import { Link } from 'react-router-dom'

type Company = {
  id: number
  name: string
  stage: string
  website: string
  industry: string
  raised_at: string | null
  days_ago: number | null
  has_open_positions: boolean
  keywords_matched: string[]
  summary: string
  source: string
}

type Context = {
  keywords?: string[]
  roles?: string[]
  industries?: string[]
  funding_focus?: string[]
  source?: string
  profile_id?: number | null
}

const STAGES = ['Seed', 'Series A', 'Series B', 'Series C', 'Series D']

export default function Funding() {
  const [companies, setCompanies] = useState<Company[]>([])
  const [context, setContext] = useState<Context | null>(null)
  const [refreshedAt, setRefreshedAt] = useState<string>('')
  const [stageFilter, setStageFilter] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [scanning, setScanning] = useState(false)
  const [busy, setBusy] = useState<string>('')
  const [result, setResult] = useState<{ action: string; message: string; job_id?: number; email_id?: number } | null>(null)
  const [contextInput, setContextInput] = useState('')
  const [showContextInput, setShowContextInput] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async (refresh = false) => {
    setError('')
    if (refresh) setScanning(true)
    try {
      const { data } = await client.get('/api/funding/companies', { params: { refresh } })
      setCompanies(data.companies || [])
      setContext(data.context || null)
      setRefreshedAt(data.refreshed_at || '')
    } catch (e: any) {
      setError(apiError(e, 'Failed to load funding radar'))
    } finally {
      setLoading(false)
      setScanning(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const rescan = async () => {
    setScanning(true); setResult(null); setError('')
    try {
      const body: Record<string, unknown> = {}
      if (contextInput.trim()) body.context = contextInput.trim()
      // /funding/refresh only *queues* a scan — it returns a receipt
      // ({queued, pipeline_job_id, duplicate}), not companies. Treating the
      // receipt as a result used to blank the whole radar. Re-read the list
      // instead so the user keeps seeing their current companies.
      const { data } = await client.post('/api/funding/refresh', body)
      setNotice(
        data.duplicate
          ? 'A scan is already running — results will appear here shortly.'
          : 'Scan queued — results appear as the worker finishes.'
      )
      await load(false)
      setRefreshedAt(new Date().toISOString())
    } catch (e: any) {
      setError(apiError(e, 'Re-scan failed'))
    } finally { setScanning(false) }
  }

  const process = async (c: Company) => {
    setBusy(c.name); setResult(null); setError('')
    try {
      const { data } = await client.post(`/api/funding/${encodeURIComponent(c.name)}/process`)
      setResult(data)
      await load(false)
    } catch (e: any) {
      setError(apiError(e, 'Action failed'))
    } finally { setBusy('') }
  }

  const toggleStage = (s: string) =>
    setStageFilter(prev => prev.includes(s) ? prev.filter(x => x !== s) : [...prev, s])

  const visible = stageFilter.length ? companies.filter(c => stageFilter.includes(c.stage)) : companies

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold tracking-tight flex items-center gap-2">
          <TrendingUp className="w-5 h-5" /> Funding Radar
          <span className="text-xs mono font-normal text-zinc-500">Seed → Series D • fresh ≤45d • AI-matched to your profile</span>
        </h1>
        <div className="flex gap-2">
          <button onClick={() => setShowContextInput(v => !v)} className="px-3 py-2 rounded-full border dark:border-zinc-700 text-xs inline-flex items-center gap-1.5 bg-white dark:bg-zinc-800">
            <Target className="w-3.5 h-3.5" /> Add focus
          </button>
          <button onClick={rescan} disabled={scanning} className="px-4 py-2 rounded-full bg-blue-600 text-white text-sm font-medium inline-flex items-center gap-2 disabled:opacity-50">
            {scanning ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />} AI re-scan
          </button>
        </div>
      </div>

      {/* AI context panel */}
      <div className="card p-4 bg-gradient-to-br from-blue-50 to-violet-50 dark:from-blue-950/30 dark:to-violet-950/30 border-blue-200 dark:border-blue-900">
        <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
          <Sparkles className="w-4 h-4 text-blue-600" /> Search context auto-extracted by AI
          {context?.source && (
            <span className="text-[11px] px-2 py-0.5 rounded-full bg-white dark:bg-zinc-900 border mono text-zinc-600 dark:text-zinc-300">
              {context.source === 'ai' ? 'AI (profile + resume + context)' : 'heuristic fallback (no AI key)'}
            </span>
          )}
          {refreshedAt && <span className="text-[11px] mono text-zinc-500 ml-auto">updated {new Date(refreshedAt).toLocaleTimeString()}</span>}
        </div>
        {!context?.profile_id && (
          <div className="mt-2 text-xs text-amber-700 dark:text-amber-300 mono">No resume uploaded yet — upload one in Resume Studio for personalized matches.</div>
        )}
        <div className="mt-2 flex flex-wrap gap-1.5">
          {(context?.funding_focus || []).map((f: string) => (
            <span key={'f-' + f} className="text-[11px] px-2 py-1 rounded-full bg-blue-600 text-white mono inline-flex items-center gap-1"><Target className="w-3 h-3" />{f}</span>
          ))}
          {(context?.keywords || []).slice(0, 10).map((k: string) => (
            <span key={k} className="text-[11px] px-2 py-1 rounded-full bg-white dark:bg-zinc-900 border dark:border-zinc-700 mono text-zinc-600 dark:text-zinc-300">{k}</span>
          ))}
        </div>
        {showContextInput && (
          <div className="mt-3 flex gap-2">
            <input
              value={contextInput}
              onChange={e => setContextInput(e.target.value)}
              placeholder="Extra focus beyond your resume — e.g. 'AI infra, fintech in India'"
              className="flex-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"
            />
            <button onClick={rescan} disabled={scanning} className="px-4 py-2 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm font-medium disabled:opacity-50">
              {scanning ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Apply & re-scan'}
            </button>
          </div>
        )}
      </div>

      {/* stage filters + count */}
      <div className="card p-3 flex flex-wrap items-center gap-2">
        <span className="text-xs mono text-zinc-500">{visible.length} companies</span>
        <div className="ml-auto flex flex-wrap gap-1">
          <button onClick={() => setStageFilter([])} className={`text-xs px-3 py-1 rounded-full border mono ${stageFilter.length === 0 ? 'bg-zinc-900 text-white border-zinc-900 dark:bg-white dark:text-zinc-900' : 'bg-white dark:bg-zinc-800'}`}>All stages</button>
          {STAGES.map(s => (
            <button key={s} onClick={() => toggleStage(s)} className={`text-xs px-3 py-1 rounded-full border mono ${stageFilter.includes(s) ? 'bg-zinc-900 text-white border-zinc-900 dark:bg-white dark:text-zinc-900' : 'bg-white dark:bg-zinc-800'}`}>{s}</button>
          ))}
        </div>
      </div>

      {error && <div className="p-3 rounded-xl bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 text-sm mono text-red-700 dark:text-red-300">{error}</div>}
      {notice && <div className="p-3 rounded-xl bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-800 text-sm mono text-blue-700 dark:text-blue-300">{notice}</div>}
      {result && (
        <div className="p-3 rounded-xl bg-emerald-50 dark:bg-emerald-950 border border-emerald-200 dark:border-emerald-800 text-sm text-emerald-800 dark:text-emerald-200 flex flex-wrap items-center gap-2">
          <CheckCircle2 className="w-4 h-4" />
          <span className="mono">{result.message}</span>
          {result.job_id && <Link to="/jobs" className="underline font-medium">Go to job #{result.job_id} →</Link>}
          {result.email_id && <Link to="/emails" className="underline font-medium">Review email draft →</Link>}
        </div>
      )}

      {loading ? (
        <div className="grid md:grid-cols-2 gap-3">
          {[...Array(6)].map((_, i) => <div key={i} className="card p-4 h-28 animate-pulse bg-zinc-100 dark:bg-zinc-800" />)}
        </div>
      ) : visible.length === 0 ? (
        <div className="card p-12 text-center">
          <Building2 className="w-8 h-8 mx-auto text-zinc-300 dark:text-zinc-700" />
          <div className="mono text-sm text-zinc-500 mt-3">No companies match this filter.</div>
          <button onClick={() => load(true)} className="mt-3 px-4 py-2 rounded-full bg-blue-600 text-white text-sm inline-flex items-center gap-2"><RefreshCw className="w-4 h-4" /> Run an AI re-scan</button>
        </div>
      ) : (
        <div className="grid md:grid-cols-2 gap-3">
          {visible.map(c => (
            <div key={c.id} className="card p-4 flex flex-col">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-sm font-semibold truncate">{c.name}</div>
                  <div className="text-xs mono text-zinc-500 flex flex-wrap items-center gap-x-2">
                    <span className="inline-flex items-center gap-1"><Calendar className="w-3 h-3" /> raised {c.days_ago === 0 ? 'today' : c.days_ago === 1 ? 'yesterday' : `${c.days_ago}d ago`}</span>
                    <span>•</span>
                    <a href={`https://${c.website}`} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-blue-600 dark:text-blue-400">{c.website} <ExternalLink className="w-3 h-3" /></a>
                  </div>
                </div>
                <span className="text-xs px-2 py-1 rounded-full mono border bg-violet-50 border-violet-200 text-violet-700 dark:bg-violet-950 dark:border-violet-800 dark:text-violet-300 whitespace-nowrap">{c.stage}</span>
              </div>
              {c.summary && <p className="text-xs text-zinc-600 dark:text-zinc-400 mt-2 line-clamp-2 leading-relaxed">{c.summary}</p>}
              <div className="mt-2 flex flex-wrap gap-1.5">
                {c.industry && <span className="text-[11px] px-2 py-0.5 rounded-full bg-zinc-100 dark:bg-zinc-800 mono">{c.industry}</span>}
                {(c.keywords_matched || []).map(k => (
                  <span key={k} className="text-[11px] px-2 py-0.5 rounded-full bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300 border border-blue-100 dark:border-blue-900 mono">↳ {k}</span>
                ))}
              </div>
              <div className="mt-3 flex items-center gap-2">
                <span className={`text-xs px-2 py-1 rounded-full mono border ${c.has_open_positions ? 'bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-800' : 'bg-amber-50 border-amber-200 text-amber-700 dark:bg-amber-950 dark:border-amber-800'}`}>{c.has_open_positions ? 'open roles' : 'no opening'}</span>
                <button onClick={() => process(c)} disabled={!!busy} className="ml-auto flex-1 py-2 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-xs font-medium inline-flex items-center justify-center gap-2 disabled:opacity-50">
                  {busy === c.name ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : c.has_open_positions ? <Briefcase className="w-3.5 h-3.5" /> : <Mail className="w-3.5 h-3.5" />} {c.has_open_positions ? 'Apply usual flow' : 'Cold email founder'}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
