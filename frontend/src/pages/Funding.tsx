import { useEffect, useState, useCallback, useRef } from 'react'
import client, { apiError, aiOutage, AI_REQUEST_TIMEOUT_MS, type AIOutage } from '../api/client'
import { useAIWork } from '../context/AIWorkContext'
import { WorkStatusLine } from '../components/AIWorkChip'
import { AIOutageBanner } from '../components/AIBanner'
import { TrendingUp, ExternalLink, Mail, Briefcase, Loader2, Sparkles, RefreshCw, Calendar, Target, Building2, CheckCircle2, AlertTriangle, ShieldAlert, PauseCircle, Globe } from 'lucide-react'
import { Link } from 'react-router-dom'

type OpenPosition = { title: string; url?: string }

type Company = {
  id: number
  name: string
  stage: string
  website: string
  industry: string
  raised_at: string | null
  days_ago: number | null
  /** The provider reported the event but not a usable date — never render "today". */
  raised_at_estimated?: boolean
  keywords_matched: string[]
  summary: string
  source: string
  verified?: boolean
  url?: string
  /** The AI's own one-sentence reason this event matched the candidate's focus. */
  why?: string
  rank?: number | null
  /** Open positions the *provider* reported (never inferred). */
  open_positions?: OpenPosition[]
}

type ScanReport = {
  scan_status?: string
  reason?: string | null
  providers?: string[]
  counts?: Record<string, number>
  errors?: Record<string, string>
  scanned?: number | null
  returned?: number | null
  scanned_at?: string | null
  attempted_at?: string | null
  ai?: {
    state?: string
    status?: string
    message?: string
    fix?: string
    detail?: string
    retry_after_hint?: number | null
  } | null
}

type Context = {
  keywords?: string[]
  roles?: string[]
  industries?: string[]
  funding_focus?: string[]
  source?: string
  profile_id?: number | null
  persona?: { id: number; name: string; target_role: string } | null
}

type ResumeInfo = {
  id?: number | null
  profile_id?: number | null
  name?: string
  current_title?: string
  extraction_source?: string
}

/** Search-provider status from GET /api/funding/providers (secret-free). */
type SearchProviderInfo = {
  provider: string
  configured: boolean
  hint?: string
}

const STAGES = ['Seed', 'Series A', 'Series B', 'Series C', 'Series D']

const SOURCE_LABELS: Record<string, string> = {
  sec_edgar: 'SEC EDGAR Form D',
  search: 'web search',
  crunchbase: 'Crunchbase',
  tracxn: 'Tracxn',
  imported: 'imported dataset',
  demo: 'synthetic demo',
  unknown: 'unknown source',
}

/** Why an honest scan returned nothing (backend `report.reason`). */
const EMPTY_COPY: Record<string, string> = {
  no_matching_events: 'events were scanned and none matched your focus',
  no_events_fetched: 'the providers returned no funding events in this window',
  provider_errors: 'every provider failed — nothing could be scanned',
}

export default function Funding() {
  const [companies, setCompanies] = useState<Company[]>([])
  const [context, setContext] = useState<Context | null>(null)
  const [resume, setResume] = useState<ResumeInfo | null>(null)
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
  // v2.1.2 scan contract: ok | scan_failed | paused | blocked, plus the report
  // behind it, so "nothing matched" is never confused with "the scan failed".
  const [scanStatus, setScanStatus] = useState<string>('ok')
  const [reason, setReason] = useState<string>('')
  const [scanned, setScanned] = useState<number | null>(null)
  const [lastReport, setLastReport] = useState<ScanReport | null>(null)
  const [windowDays, setWindowDays] = useState<number>(45)
  const [outage, setOutage] = useState<AIOutage | null>(null)
  const [history, setHistory] = useState<any[]>([])
  const [historyCollapsed, setHistoryCollapsed] = useState<boolean>(false)
  // v2.2.6 search-provider badge: how this radar scans (web search API vs the
  // direct — robots-checked — EDGAR path). Fetched once, secret-free.
  const [searchInfo, setSearchInfo] = useState<SearchProviderInfo | null>(null)

  // Refs for visibility-aware polling and request coalescing
  const POLL_INTERVAL_MS = 25_000
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const inFlight = useRef<Promise<void> | null>(null)
  const alive = useRef(true)

  const load = useCallback(async (refresh = false) => {
    // Coalesce: if a request is already in flight, share it rather than firing another
    if (inFlight.current) return inFlight.current
    inFlight.current = (async () => {
      setError('')
      if (refresh) setScanning(true)
      try {
        const { data } = await client.get('/api/funding/companies', { params: { refresh }, timeout: AI_REQUEST_TIMEOUT_MS })
        if (alive.current) {
          setCompanies(data.companies || [])
          setContext(data.context || null)
          setResume(data.resume || null)
          setRefreshedAt(data.refreshed_at || '')
          setScanStatus(data.scan_status || 'ok')
          setReason(data.reason || '')
          setScanned(typeof data.scanned === 'number' ? data.scanned : null)
          setLastReport(data.last_report || null)
          setWindowDays(data.window_days || 45)
          if (refresh) setOutage(null)
        }
        // load recent scans (funding history)
        try { 
          const h = await client.get('/api/funding/history', { params:{limit:5}})
          if (alive.current) setHistory(h.data.history||h.data||[])
        } catch {}

      } catch (e: any) {
        // A refresh the model could not rank comes back as the v2.1 outage shape:
        // show the diagnosis (and keep the rows already on the radar).
        const o = aiOutage(e)
        if (o) {
          if (alive.current) {
            setOutage(o)
            setScanStatus(o.pausable ? 'paused' : 'blocked')
          }
        } else {
          if (alive.current) setError(apiError(e, 'Failed to load funding radar'))
        }
      } finally {
        if (alive.current) {
          setLoading(false)
          setScanning(false)
        }
      }
    })().finally(() => { inFlight.current = null })
    return inFlight.current
  }, [])

  useEffect(() => { load() }, [load])

  /**
   * The live layer (v2.2.8): the queued scan's state is derived from its queue
   * row (through the global context), so "Scan queued → Scanning → N
   * companies verified" survives navigation and F5, and the header chip shows
   * it on every route. When the row finishes the radar list is re-read once.
   */
  const work = useAIWork()
  const scanRow = work.findRow({ key: 'funding', pipeline: 'funding' })
  const scanLive = !!scanRow && ['queued', 'processing', 'paused'].includes(String(scanRow.status))
  const scanFinished = scanRow && !scanLive ? `${scanRow.id}:${scanRow.status}` : ''
  useEffect(() => {
    if (!scanFinished) return
    load(false).then(() => { if (alive.current) setRefreshedAt(new Date().toISOString()) })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scanFinished])

  // Visibility-aware polling: re-read companies every 25-30s while tab is visible
  // (matching Emails/Queues pattern). Stops when hidden, resumes with immediate
  // refresh when visible again. Uses load(false) — the non-refresh variant — which
  // is cheap and already exists.

  useEffect(() => {
    alive.current = true
    const start = () => {
      if (pollRef.current) return
      void load(false)
      pollRef.current = setInterval(() => void load(false), POLL_INTERVAL_MS)
    }
    const stop = () => {
      if (pollRef.current) clearInterval(pollRef.current)
      pollRef.current = null
    }
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') stop()
      else start()
    }
    if (document.visibilityState !== 'hidden') start()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      alive.current = false
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [load])

  // Search-provider status (badge): a non-critical read — a failure just
  // leaves the badge unrendered, never the page.
  useEffect(() => {
    client.get('/api/funding/providers')
      .then(({ data }) => { if (alive.current) setSearchInfo(data?.search || null) })
      .catch(() => {})
  }, [])

  const rescan = async () => {
    setScanning(true); setResult(null); setError(''); setOutage(null)
    try {
      const body: Record<string, unknown> = {}
      if (contextInput.trim()) body.context = contextInput.trim()
      // /funding/refresh only *queues* a scan — it returns a receipt
      // ({queued, pipeline_job_id, duplicate}), not companies. Treating the
      // receipt as a result used to blank the whole radar. Re-read the list
      // instead so the user keeps seeing their current companies.
      const { data } = await client.post('/api/funding/refresh', body)
      // The receipt's id is tracked (sessionStorage) so the status line and
      // the header chip re-attach to this exact scan after a refresh.
      if (data.pipeline_job_id) work.track({ id: data.pipeline_job_id, pipeline: 'funding', key: 'funding' })
      setNotice(
        data.duplicate
          ? 'A scan is already running — its live status is shown below.'
          : `Scan queued${data.focus_applied ? ` with focus "${data.focus_applied}" (saved to your settings)` : ''} — the status below updates live.`
      )
      setContext(data.context || null)
      setShowContextInput(false)
      await load(false)
      setRefreshedAt(new Date().toISOString())
    } catch (e: any) {
      const o = aiOutage(e)
      if (o) { setOutage(o); setScanStatus(o.pausable ? 'paused' : 'blocked') }
      else setError(apiError(e, 'Re-scan failed'))
    } finally { setScanning(false) }
  }

  const process = async (c: Company) => {
    setBusy(c.name); setResult(null); setError(''); setOutage(null)
    try {
      // The company travels in the body (v2.2.9): a name with spaces, unicode,
      // quotes or a slash is data, not a URL path segment.
      const { data } = await client.post('/api/funding/companies/process',
        { company_id: c.id, company: c.name }, { timeout: AI_REQUEST_TIMEOUT_MS })
      setResult(data)
      await load(false)
    } catch (e: any) {
      const o = aiOutage(e)
      if (o) setOutage(o)
      else setError(apiError(e, 'Action failed'))
    } finally { setBusy('') }
  }

  const toggleStage = (s: string) =>
    setStageFilter(prev => prev.includes(s) ? prev.filter(x => x !== s) : [...prev, s])

  const visible = stageFilter.length ? companies.filter(c => stageFilter.includes(c.stage)) : companies
  const providerErrors = Object.entries(lastReport?.errors || {})
  const raisedLabel = (c: Company) => {
    if (c.raised_at_estimated || c.days_ago === null || c.days_ago === undefined) return 'date not disclosed'
    return `raised ${c.days_ago === 0 ? 'today' : c.days_ago === 1 ? 'yesterday' : `${c.days_ago}d ago`}`
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col sm:flex-row flex-wrap items-start sm:items-center justify-between gap-3">
        <h1 className="text-xl font-semibold tracking-tight flex flex-col sm:flex-row items-start sm:items-center gap-2">
          <span className="flex items-center gap-2"><TrendingUp className="w-5 h-5" /> Funding Radar</span>
          <span className="text-xs mono font-normal text-zinc-500">Seed → Series D • fresh ≤{windowDays}d • AI-ranked</span>
        </h1>
        <div className="flex flex-wrap gap-2 chips-wrap">
          <button onClick={() => setShowContextInput(v => !v)} aria-expanded={showContextInput} className="px-3 py-2 rounded-full border dark:border-zinc-700 text-xs inline-flex items-center gap-1.5 bg-white dark:bg-zinc-800 min-h-[44px] focus:outline-none focus:ring-2 focus:ring-blue-500">
            <Target className="w-3.5 h-3.5" /> Add focus
          </button>
          <button onClick={rescan} disabled={scanning || scanLive} className="px-4 py-2 rounded-full bg-blue-600 text-white text-sm font-medium inline-flex items-center gap-2 disabled:opacity-50 min-h-[44px] focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-1">
            {scanning || scanLive ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />} {scanLive ? 'Scanning…' : 'AI re-scan'}
          </button>
        </div>
      </div>
      <WorkStatusLine row={scanRow} prefix="Funding re-scan" testId="funding-scan-status" onDismiss={() => work.untrack('funding')} />

      {/* AI context panel */}
      <div className="card p-4 bg-gradient-to-br from-blue-50 to-violet-50 dark:from-blue-950/30 dark:to-violet-950/30 border-blue-200 dark:border-blue-900">
        <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
          <Sparkles className="w-4 h-4 text-blue-600" /> Search context auto-extracted by AI
          {searchInfo && (searchInfo.configured ? (
            <span
              className="text-[11px] px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700 border border-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:border-emerald-800 mono inline-flex items-center gap-1"
              title={`Funding scans run through the ${searchInfo.provider} search API — efts.sec.gov is never contacted directly.`}
            >
              <Globe className="w-3 h-3" /> search: {searchInfo.provider}
            </span>
          ) : (
            <span
              className="text-[11px] px-2 py-0.5 rounded-full bg-amber-100 text-amber-700 border border-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:border-amber-800 mono inline-flex items-center gap-1"
              title={searchInfo.hint || 'No search provider configured — scans use direct SEC EDGAR (robots-checked; may fail with scan_failed).'}
            >
              <Globe className="w-3 h-3" /> direct EDGAR only
            </span>
          ))}
          {context?.source && (
            <span className="text-[11px] px-2 py-0.5 rounded-full bg-white dark:bg-zinc-900 border mono text-zinc-600 dark:text-zinc-300">
              {/* The backend tags the persona merge as "ai+persona" / "mined+persona" —
                  match on the prefix so the label never leaks the raw tag. */}
              {context.source.startsWith('ai') ? 'AI (profile + resume + context)' : context.source.startsWith('mined') ? 'mined from your data (deterministic — nothing for the model to read yet)' : context.source}
            </span>
          )}
          {refreshedAt && <span className="text-[11px] mono text-zinc-500 ml-auto">updated {new Date(refreshedAt).toLocaleTimeString()}</span>}
        </div>
        {resume?.profile_id ? (
          <div className="mt-2 text-xs text-emerald-700 dark:text-emerald-300 mono">
            Matched to {resume.name || 'your resume'}{resume.current_title ? ` • ${resume.current_title}` : ''}
            {resume.extraction_source && resume.extraction_source !== 'ai' ? ` • profile source: ${resume.extraction_source}` : ''}
          </div>
        ) : (
          <div className="mt-2 text-xs text-amber-700 dark:text-amber-300 mono">
            No resume uploaded yet — <Link to="/resumes" className="underline">upload one in Resume Studio</Link> for personalized matches.
          </div>
        )}
        {context?.persona && (
          <div className="mt-1 text-[11px] mono text-blue-700 dark:text-blue-300">
            Track: {context.persona.name}{context.persona.target_role ? ` — ${context.persona.target_role}` : ''}
          </div>
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
              className="flex-1 border rounded-xl px-3 py-2 min-h-[44px] text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <button onClick={rescan} disabled={scanning || scanLive} className="px-4 py-2 min-h-[44px] rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm font-medium disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-blue-500">
              {scanning || scanLive ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Apply & re-scan'}
            </button>
          </div>
        )}
      </div>

      {/* stage filters + count */}
      <div className="card p-3 flex flex-col sm:flex-row flex-wrap items-start sm:items-center gap-2 chips-wrap">
        <span className="text-xs mono text-zinc-500">{visible.length} companies</span>
        {typeof scanned === 'number' && scanStatus === 'ok' && (
          <span className="text-[11px] mono text-zinc-400">{scanned} events scanned by AI</span>
        )}
        <div className="ml-auto flex flex-wrap gap-1 chips-wrap">
          <button onClick={() => setStageFilter([])} className={`text-xs px-3 py-2 rounded-full border mono min-h-[44px] ${stageFilter.length === 0 ? 'bg-zinc-900 text-white border-zinc-900 dark:bg-white dark:text-zinc-900' : 'bg-white dark:bg-zinc-800'}`}>All stages</button>
          {STAGES.map(s => (
            <button key={s} onClick={() => toggleStage(s)} className={`text-xs px-3 py-2 rounded-full border mono min-h-[44px] ${stageFilter.includes(s) ? 'bg-zinc-900 text-white border-zinc-900 dark:bg-white dark:text-zinc-900' : 'bg-white dark:bg-zinc-800'}`}>{s}</button>
          ))}
        </div>
      </div>

      {outage && <AIOutageBanner outage={outage} what="the funding radar was not re-ranked" onDismiss={() => setOutage(null)} />}
      {error && <div className="p-3 rounded-xl bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 text-sm mono text-red-700 dark:text-red-300">{error}</div>}
      {notice && <div className="p-3 rounded-xl bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-800 text-sm mono text-blue-700 dark:text-blue-300">{notice}</div>}

      {/* Persisted scan state — the radar says why it looks the way it does. */}
      {!outage && scanStatus === 'scan_failed' && (
        <div className="p-3 rounded-xl bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 text-sm">
          <div className="flex items-center gap-2 font-medium text-red-800 dark:text-red-200">
            <AlertTriangle className="w-4 h-4 text-red-600 dark:text-red-400" /> Scan failed — no provider returned data
          </div>
          <div className="text-xs mono mt-1 text-red-700 dark:text-red-300">
            The companies below are from the last successful scan; nothing was deleted.
          </div>
          {providerErrors.length > 0 && (
            <ul className="mt-2 space-y-1">
              {providerErrors.map(([provider, message]) => (
                <li key={provider} className="text-[11px] mono text-red-700 dark:text-red-300 break-all">
                  <span className="font-semibold">{provider}</span>: {message}
                </li>
              ))}
            </ul>
          )}
          <button onClick={() => load(true)} disabled={scanning} className="mt-3 px-3 py-1.5 rounded-full bg-red-600 text-white text-xs mono inline-flex items-center gap-2 disabled:opacity-50">
            {scanning ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />} Try the scan again
          </button>
        </div>
      )}
      {!outage && scanStatus === 'paused' && (
        <div className="p-3 rounded-xl bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 text-sm">
          <div className="flex items-center gap-2 font-medium text-amber-800 dark:text-amber-200">
            <PauseCircle className="w-4 h-4 text-amber-600 dark:text-amber-400" /> AI paused — the re-scan will resume automatically
          </div>
          <div className="text-xs mono mt-1 text-amber-700 dark:text-amber-300">
            {lastReport?.ai?.message || 'The AI provider could not be reached.'}
            {lastReport?.ai?.retry_after_hint ? ` Retrying in ~${Math.round(lastReport.ai.retry_after_hint)}s.` : ''}
            {' '}No unranked list is shown as a result{companies.length ? ' — the companies below are from the last successful scan' : ''}.
          </div>
        </div>
      )}
      {!outage && scanStatus === 'blocked' && (
        <div className="p-3 rounded-xl bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 text-sm">
          <div className="flex items-center gap-2 font-medium text-red-800 dark:text-red-200">
            <ShieldAlert className="w-4 h-4 text-red-600 dark:text-red-400" /> AI blocked — the radar cannot be ranked
          </div>
          <div className="text-xs mono mt-1 text-red-700 dark:text-red-300">
            {lastReport?.ai?.message || 'The AI model is not available for this account.'}
            {lastReport?.ai?.fix ? <><br /><strong>Fix:</strong> {lastReport.ai.fix}</> : null}
            {companies.length ? <><br />The companies below are from the last successful scan; nothing was deleted.</> : null}
          </div>
          <Link to="/settings#ai" className="mt-2 inline-block px-3 py-1.5 rounded-full bg-red-600 text-white text-xs mono">Open Settings → AI API</Link>
        </div>
      )}
      {!outage && scanStatus === 'ok' && providerErrors.length > 0 && (
        <div className="p-3 rounded-xl bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 text-xs mono text-amber-800 dark:text-amber-200">
          <span className="font-medium">Partial scan:</span> {providerErrors.map(([provider, message]) => `${provider} (${message})`).join('; ')}
        </div>
      )}

      {result && (
        <div className="p-3 rounded-xl bg-emerald-50 dark:bg-emerald-950 border border-emerald-200 dark:border-emerald-800 text-sm text-emerald-800 dark:text-emerald-200 flex flex-wrap items-center gap-2">
          <CheckCircle2 className="w-4 h-4" />
          <span className="mono">{result.message}</span>
          {result.job_id && <Link to="/jobs" className="underline font-medium">Go to job #{result.job_id} →</Link>}
          {result.email_id && <Link to="/emails" className="underline font-medium">Review email draft →</Link>}
        </div>
      )}

      {/* Recent scans — funding history (mobile snap/collapse) */}
      <div className="card p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium mono flex items-center gap-2"><Calendar className="w-4 h-4"/> Recent scans</h3>
          <button onClick={()=>setHistoryCollapsed(v=>!v)} className="text-xs px-3 py-2 rounded-full border mono min-h-[44px] sm:hidden">{historyCollapsed ? 'Expand' : 'Collapse'}</button>
          <span className="hidden sm:inline text-xs mono text-zinc-500">{history.length} scans</span>
        </div>
        {!historyCollapsed && (
          <div className="mt-3 flex gap-3 overflow-x-auto history-snap pb-2 snap-x snap-mandatory sm:grid sm:grid-cols-2 sm:overflow-visible">
            {history.length===0 ? <div className="text-xs mono text-zinc-500">No scans yet</div> :
              history.slice(0,5).map((h:any, idx:number)=>(
                <div key={h.id||idx} className="min-w-[280px] sm:min-w-0 card p-3 border bg-zinc-50 dark:bg-zinc-800 snap-start flex-shrink-0 sm:flex-shrink">
                  <div className="text-xs mono font-medium flex items-center gap-2">
                    <span className={`px-2 py-0.5 rounded-full text-[11px] ${h.status==='ok'?'bg-emerald-100 text-emerald-700':'bg-amber-100 text-amber-700'}`}>{h.status||h.scan_status}</span>
                    <span className="text-zinc-500">{h.scanned_at ? new Date(h.scanned_at).toLocaleDateString() : ''}</span>
                  </div>
                  <div className="text-xs mono text-zinc-600 dark:text-zinc-400 mt-1">{h.companies_found ?? h.counts?.total ?? 0} companies • {h.events_seen ?? h.scanned ?? 0} events</div>
                  {h.reason && <div className="text-[11px] mono text-zinc-500 mt-1 line-clamp-2">{h.reason}</div>}
                </div>
              ))}
          </div>
        )}
      </div>

      {loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {[...Array(6)].map((_, i) => <div key={i} className="card p-4 h-28 animate-pulse bg-zinc-100 dark:bg-zinc-800" />)}
        </div>
      ) : visible.length === 0 ? (
        <div className="card p-12 text-center">
          <Building2 className="w-8 h-8 mx-auto text-zinc-300 dark:text-zinc-700" />
          <div className="mono text-sm text-zinc-500 mt-3">
            {stageFilter.length
              ? 'No companies match this stage filter.'
              : scanStatus === 'ok' && reason
                ? `Scanned ${typeof scanned === 'number' ? scanned : 0} ${EMPTY_COPY[reason] || reason}.`
                : scanStatus === 'ok'
                  ? 'No companies on the radar yet.'
                  : 'The radar could not be refreshed — see the notice above.'}
          </div>
          {scanStatus === 'ok' && (
            <button onClick={() => load(true)} disabled={scanning} className="mt-3 px-4 py-2 rounded-full bg-blue-600 text-white text-sm inline-flex items-center gap-2 disabled:opacity-50">
              {scanning ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />} Run an AI re-scan
            </button>
          )}
          {scanStatus !== 'ok' && scanStatus !== 'scan_failed' && (
            <Link to="/settings#ai" className="mt-3 inline-flex px-4 py-2 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm items-center gap-2">Fix AI settings</Link>
          )}
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {visible.map(c => (
            <div key={c.id} className="card p-4 flex flex-col">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-sm font-semibold truncate flex items-center gap-2">{c.name} <a href={`/jobs?company=${encodeURIComponent(c.name)}`} className="text-[10px] px-1.5 py-0.5 rounded bg-blue-50 text-blue-700 border border-blue-200 mono no-underline min-h-[44px] inline-flex items-center">jobs →</a></div>
                  <div className="text-xs mono text-zinc-500 flex flex-wrap items-center gap-x-2">
                    <span className="inline-flex items-center gap-1"><Calendar className="w-3 h-3" /> {raisedLabel(c)}</span>
                    {c.website && (
                      <>
                        <span>•</span>
                        <a href={`https://${c.website}`} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-blue-600 dark:text-blue-400">{c.website} <ExternalLink className="w-3 h-3" /></a>
                      </>
                    )}
                  </div>
                </div>
                <span className="text-xs px-2 py-1 rounded-full mono border bg-violet-50 border-violet-200 text-violet-700 dark:bg-violet-950 dark:border-violet-800 dark:text-violet-300 whitespace-nowrap">{c.stage}</span>
              </div>

              {/* Provenance: which provider said so, and whether it is verifiable. */}
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <span className={`text-[11px] px-2 py-0.5 rounded-full mono border ${c.verified ? 'bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-800 dark:text-emerald-300' : 'bg-zinc-100 border-zinc-200 text-zinc-600 dark:bg-zinc-800 dark:border-zinc-700 dark:text-zinc-300'}`}>
                  {SOURCE_LABELS[c.source] || c.source}{c.verified ? ' • verified' : c.source === 'demo' ? ' • synthetic demo data' : ' • unverified'}
                </span>
                {(c.open_positions || []).length > 0 && (
                  <span className="text-[11px] px-2 py-0.5 rounded-full mono border bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-800 dark:text-emerald-300 inline-flex items-center gap-1">
                    <Briefcase className="w-3 h-3" /> {c.open_positions!.length} open role{c.open_positions!.length === 1 ? '' : 's'} (provider)
                  </span>
                )}
                {c.url && (
                  <a href={c.url} target="_blank" rel="noreferrer" className="text-[11px] px-2 py-0.5 rounded-full mono border bg-white dark:bg-zinc-900 dark:border-zinc-700 text-blue-600 dark:text-blue-400 inline-flex items-center gap-1">
                    {c.source === 'search' ? 'source' : 'filing'} <ExternalLink className="w-3 h-3" />
                  </a>
                )}
              </div>

              {/* The AI's own reason — the radar is ranked, not keyword-filtered. */}
              {c.why && (
                <p className="text-xs text-blue-700 dark:text-blue-300 mt-2 mono leading-relaxed flex items-start gap-1.5">
                  <Sparkles className="w-3 h-3 shrink-0 mt-0.5" /> {c.why}
                </p>
              )}
              {c.summary && <p className="text-xs text-zinc-600 dark:text-zinc-400 mt-2 line-clamp-2 leading-relaxed">{c.summary}</p>}
              <div className="mt-2 flex flex-wrap gap-1.5">
                {c.industry && <span className="text-[11px] px-2 py-0.5 rounded-full bg-zinc-100 dark:bg-zinc-800 mono">{c.industry}</span>}
                {(c.keywords_matched || []).map(k => (
                  <span key={k} className="text-[11px] px-2 py-0.5 rounded-full bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300 border border-blue-100 dark:border-blue-900 mono">↳ {k}</span>
                ))}
              </div>
              {(c.open_positions || []).length > 0 && (
                <ul className="mt-2 space-y-0.5">
                  {c.open_positions!.slice(0, 3).map((p, i) => (
                    <li key={`${c.id}-pos-${i}`} className="text-[11px] mono text-zinc-600 dark:text-zinc-400 truncate">
                      {p.url
                        ? <a href={p.url} target="_blank" rel="noreferrer" className="text-blue-600 dark:text-blue-400 underline">{p.title}</a>
                        : p.title}
                    </li>
                  ))}
                </ul>
              )}
              <div className="mt-3 flex items-center gap-2">
                {/* The backend decides from provider facts: a real posting is
                    tracked as a job, otherwise a founder draft needs approval. */}
                <button onClick={() => process(c)} disabled={!!busy} className="ml-auto flex-1 py-2 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-xs font-medium inline-flex items-center justify-center gap-2 disabled:opacity-50 min-h-[44px] focus:ring-2 focus:ring-blue-500">
                  {busy === c.name
                    ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    : (c.open_positions || []).length > 0 ? <Briefcase className="w-3.5 h-3.5" /> : <Mail className="w-3.5 h-3.5" />}
                  {(c.open_positions || []).length > 0 ? 'Track the open role' : 'Draft founder outreach'}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
