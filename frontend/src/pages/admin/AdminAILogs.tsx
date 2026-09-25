import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowLeft, Cpu, FileJson, RefreshCw, ScrollText, ShieldCheck } from 'lucide-react'
import client, { apiError } from '../../api/client'
import { EmptyState, ErrorState, LoadingBlock, SectionCard } from '../../components/states'

/**
 * Owner-only AI log (v2.3) — the two questions the console could not answer:
 *
 *  • **"What exactly did we send?"** — every logged AI call, with the outbound
 *    request body per attempt (so an escalation or a JSON fix-up is visible as
 *    one), the endpoint, tokens/cost, latency, and a bounded excerpt of the
 *    answer. The list is metadata; the bodies come from the detail route.
 *  • **"How is Laya doing?"** — the local decision engine's status mix per task
 *    (`ok` / `low_confidence` / `timeout` / `error` / `parked`), its latency and
 *    its calibrated confidence against the configured floor.
 *
 * Both are server-enforced owner-only: `/api/admin/ai/*` and `/api/admin/laya/*`
 * 403 a member token, the route is mounted behind `<RequireOwner>`, and the
 * whole nav entry is hidden from members. Nothing here is a user-facing surface.
 */

type Call = {
  id: number
  workflow: string
  status: string
  reason: string
  model: string
  provider: string
  base_url: string
  http_status: number | null
  attempts: number
  latency_ms: number
  total_tokens: number
  estimated_cost_usd: number
  preview: string
  error: string
  created_at: string | null
}

type CallDetail = Call & {
  request_url: string
  request_params: Record<string, unknown>
  request: { attempts?: { attempt: number; body: unknown }[] }
  response: string
}

type LayaTask = { task: string; total: number; statuses: Record<string, number>; avg_latency_ms: number }

const LAYA_STATUS_ORDER = ['ok', 'low_confidence', 'timeout', 'error', 'parked'] as const

export function statusTone(status: string): string {
  if (status === 'ok') return 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300'
  if (status === 'low_confidence') return 'bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300'
  return 'bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300'
}

function shortTime(value: string | null): string {
  if (!value) return '—'
  return value.replace('T', ' ').slice(0, 19)
}

function pretty(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

export default function AdminAILogs() {
  const [calls, setCalls] = useState<Call[]>([])
  const [stats, setStats] = useState<any>(null)
  const [laya, setLaya] = useState<any>(null)
  const [detail, setDetail] = useState<CallDetail | null>(null)
  const [detailBusy, setDetailBusy] = useState<number | null>(null)
  const [status, setStatus] = useState('')
  const [workflow, setWorkflow] = useState('')
  const [windowHours, setWindowHours] = useState(24)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const mounted = useRef(true)

  const params = () => {
    const query: Record<string, any> = { limit: 25 }
    if (status) query.status = status
    if (workflow.trim()) query.workflow = workflow.trim()
    return query
  }

  const load = async (hours = windowHours) => {
    const [callsResult, statsResult, layaResult] = await Promise.allSettled([
      client.get('/api/admin/ai/calls', { params: params() }),
      client.get('/api/admin/ai/stats', { params: { window_hours: hours } }),
      client.get('/api/admin/laya/stats', { params: { window_hours: hours } }),
    ])
    if (!mounted.current) return
    if (callsResult.status === 'fulfilled') setCalls(callsResult.value.data.items || [])
    if (statsResult.status === 'fulfilled') setStats(statsResult.value.data)
    else setError(apiError(statsResult.reason, 'The AI call log could not be loaded.'))
    if (layaResult.status === 'fulfilled') setLaya(layaResult.value.data)
    if (callsResult.status === 'fulfilled' && statsResult.status === 'fulfilled') setError(null)
    setLoading(false)
  }

  useEffect(() => {
    mounted.current = true
    void load()
    return () => { mounted.current = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const openCall = async (id: number) => {
    setDetailBusy(id)
    try {
      const { data } = await client.get(`/api/admin/ai/calls/${id}`)
      if (mounted.current) setDetail(data)
    } catch (e) {
      if (mounted.current) setError(apiError(e, 'That call could not be loaded.'))
    } finally {
      if (mounted.current) setDetailBusy(null)
    }
  }

  const totals = stats?.totals ?? {}
  const layaTotals = laya?.totals ?? {}
  const tasks: LayaTask[] = laya?.by_task ?? []

  return (
    <div className="space-y-6" data-testid="admin-ai-logs">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold flex items-center gap-2">
            <ShieldCheck className="w-5 h-5" /> AI log
          </h1>
          <p className="text-sm text-zinc-500 dark:text-zinc-400 mt-0.5">
            The exact request sent for every AI call, and how the local decision engine is doing —
            owner-only.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Link to="/admin" className="text-xs px-3 py-2 rounded-full border dark:border-zinc-700 inline-flex items-center gap-1">
            <ArrowLeft className="w-3 h-3" /> Admin console
          </Link>
          <button onClick={() => void load(windowHours)} data-testid="ai-log-refresh"
                  className="text-xs px-3 py-2 rounded-full border dark:border-zinc-700 inline-flex items-center gap-1">
            <RefreshCw className="w-3 h-3" /> Refresh
          </button>
        </div>
      </div>

      {error && <ErrorState message={error} onRetry={() => void load()} />}
      {loading && <LoadingBlock label="Loading the AI log…" />}

      {/* ── How Laya is doing ─────────────────────────────────────────────── */}
      <SectionCard title="Local decision engine (Laya) health" icon={Cpu}
                   aside={<span className="text-[11px] mono text-zinc-500">last {windowHours}h</span>}>
        {laya?.enabled === false ? (
          <EmptyState title="Decision logging disabled"
                      hint="AI_LOG_ENABLED=false — calls and decisions are not being recorded." />
        ) : (
          <div data-testid="laya-health">
            <div className="grid grid-cols-2 md:grid-cols-6 gap-2 text-center">
              {[
                ['decisions', layaTotals.decisions ?? 0],
                ['ok', layaTotals.ok ?? 0],
                ['low confidence', layaTotals.low_confidence ?? 0],
                ['timeouts', layaTotals.timeout ?? 0],
                ['errors', layaTotals.error ?? 0],
                ['parked', layaTotals.parked ?? 0],
              ].map(([label, value]) => (
                <div key={String(label)} className="p-3 rounded-xl bg-zinc-50 dark:bg-zinc-800/60">
                  <div className="text-lg font-semibold mono" data-testid={`laya-${String(label).replace(' ', '-')}`}>{String(value)}</div>
                  <div className="text-[10px] text-zinc-500">{label}</div>
                </div>
              ))}
            </div>
            <div className="mt-2 text-[11px] mono text-zinc-500 dark:text-zinc-400">
              avg latency {layaTotals.avg_latency_ms ?? 0}ms • avg confidence{' '}
              {layaTotals.avg_confidence ?? 0} • retained {laya?.retention_days ?? 7}d
            </div>
            {tasks.length === 0 ? (
              <div className="mt-3 text-xs text-zinc-500 dark:text-zinc-400">
                No decision engine passes in this window.
              </div>
            ) : (
              <div className="mt-3 space-y-2" data-testid="laya-tasks">
                {tasks.map(task => (
                  <div key={task.task} className="p-3 rounded-xl border dark:border-zinc-800">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="text-sm font-medium mono">{task.task}</span>
                      <span className="text-[11px] mono text-zinc-500">
                        {task.total} pass(es) • {task.avg_latency_ms}ms avg
                      </span>
                    </div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {LAYA_STATUS_ORDER.filter(choice => task.statuses?.[choice]).map(choice => (
                        <span key={choice} className={`text-[10px] mono px-2 py-0.5 rounded-full ${statusTone(choice)}`}>
                          {choice.replace('_', ' ')} {task.statuses[choice]}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </SectionCard>

      {/* ── What exactly did we send? ─────────────────────────────────────── */}
      <SectionCard title="AI calls" icon={ScrollText}
                   aside={
                     <span className="text-[11px] mono text-zinc-500">
                       {totals.calls ?? 0} call(s) • {totals.errors ?? 0} failed •{' '}
                       {totals.tokens ?? 0} tokens • ${(totals.cost_usd ?? 0).toFixed(4)}
                     </span>
                   }>
        {stats?.enabled === false ? (
          <EmptyState title="Logging disabled"
                      hint="AI_LOG_ENABLED=false — nothing is being recorded. Turn it on to see the exact requests." />
        ) : (
          <div data-testid="ai-calls">
            <div className="flex flex-wrap items-end gap-3">
              <label className="text-xs mono">
                Status
                <select value={status} data-testid="ai-log-status"
                        onChange={e => { setStatus(e.target.value); void load() }}
                        className="ml-2 border rounded-lg px-2 py-1.5 text-xs mono bg-white dark:bg-zinc-900 dark:border-zinc-700">
                  <option value="">all</option>
                  <option value="ok">ok</option>
                  <option value="error">error</option>
                </select>
              </label>
              <label className="text-xs mono">
                Workflow
                <input value={workflow} data-testid="ai-log-workflow" placeholder="scoring"
                       onChange={e => setWorkflow(e.target.value)}
                       className="ml-2 w-32 border rounded-lg px-2 py-1.5 text-xs mono bg-white dark:bg-zinc-900 dark:border-zinc-700" />
              </label>
              <button onClick={() => void load()} data-testid="ai-log-apply"
                      className="text-xs px-3 py-1.5 rounded-full border dark:border-zinc-700">Apply</button>
              <label className="text-xs mono ml-auto">
                Window
                <select value={windowHours} data-testid="ai-log-window"
                        onChange={e => { const next = Number(e.target.value); setWindowHours(next); void load(next) }}
                        className="ml-2 border rounded-lg px-2 py-1.5 text-xs mono bg-white dark:bg-zinc-900 dark:border-zinc-700">
                  <option value={24}>24h</option>
                  <option value={168}>7d</option>
                </select>
              </label>
            </div>

            {calls.length === 0 ? (
              <EmptyState title="No AI calls logged"
                          hint="Either nothing ran in this window, or the filters exclude everything." />
            ) : (
              <ul className="mt-3 space-y-2">
                {calls.map(call => (
                  <li key={call.id} data-testid={`ai-call-${call.id}`}
                      className="p-3 rounded-xl border dark:border-zinc-800">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`text-[10px] mono px-2 py-0.5 rounded-full ${statusTone(call.status)}`}>{call.status}</span>
                      <span className="text-sm font-medium mono">{call.workflow}</span>
                      <span className="text-[11px] mono text-zinc-500">{call.model || '—'}</span>
                      <span className="text-[11px] mono text-zinc-500">
                        {call.total_tokens} tok • {call.latency_ms}ms • {call.attempts} attempt(s)
                      </span>
                      {call.reason && <span className="text-[11px] mono text-red-600 dark:text-red-400">{call.reason}</span>}
                      <span className="text-[11px] mono text-zinc-400 ml-auto">{shortTime(call.created_at)}</span>
                      <button onClick={() => void openCall(call.id)} disabled={detailBusy === call.id}
                              data-testid={`ai-call-open-${call.id}`}
                              className="text-[11px] px-2 py-1 rounded-full border dark:border-zinc-700 inline-flex items-center gap-1 disabled:opacity-50">
                        <FileJson className="w-3 h-3" /> {detailBusy === call.id ? 'Loading…' : 'Exact request'}
                      </button>
                    </div>
                    {call.preview && (
                      <div className="mt-1 text-[11px] mono text-zinc-500 dark:text-zinc-400 break-words">
                        {call.preview}
                      </div>
                    )}
                    {call.error && (
                      <div className="mt-1 text-[11px] mono text-red-600 dark:text-red-400 break-words">{call.error}</div>
                    )}
                  </li>
                ))}
              </ul>
            )}

            {detail && (
              <div className="mt-4 p-3 rounded-xl border-2 border-blue-200 dark:border-blue-900" data-testid="ai-call-detail">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="text-sm font-medium mono">
                    Call #{detail.id} • {detail.workflow} • {detail.status}
                  </div>
                  <button onClick={() => setDetail(null)} className="text-[11px] px-2 py-1 rounded-full border dark:border-zinc-700">
                    Close
                  </button>
                </div>
                <div className="mt-1 text-[11px] mono text-zinc-500 break-all">
                  {detail.request_url || detail.base_url}
                  {Object.keys(detail.request_params || {}).length > 0 && (
                    <> • {pretty(detail.request_params)}</>
                  )}
                </div>
                {(detail.request?.attempts || []).map(attempt => (
                  <div key={attempt.attempt} className="mt-2">
                    <div className="text-[11px] mono font-medium text-zinc-500">
                      attempt {attempt.attempt} — body sent
                    </div>
                    <pre data-testid={`ai-call-attempt-${attempt.attempt}`}
                         className="mt-1 p-2 rounded-lg bg-zinc-50 dark:bg-zinc-900 text-[11px] mono overflow-x-auto whitespace-pre-wrap break-words">
                      {pretty(attempt.body)}
                    </pre>
                  </div>
                ))}
                {detail.response && (
                  <div className="mt-2">
                    <div className="text-[11px] mono font-medium text-zinc-500">answer excerpt</div>
                    <pre data-testid="ai-call-response"
                         className="mt-1 p-2 rounded-lg bg-zinc-50 dark:bg-zinc-900 text-[11px] mono overflow-x-auto whitespace-pre-wrap break-words">
                      {detail.response}
                    </pre>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </SectionCard>
    </div>
  )
}
