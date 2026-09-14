import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Activity, ChevronDown, Loader2, PauseCircle, UserCheck, AlertCircle, CheckCircle, ArrowRight } from 'lucide-react'
import { useAIWork } from '../context/AIWorkContext'
import { describeRow, deriveState, toneClasses, type DerivedState } from '../lib/aiWork'
import type { QueueItem } from '../hooks/useAIQueue'

/**
 * The persistent header chip (v2.2.8): `N queued • M processing • K paused`.
 *
 * Rendered by `Layout` above every route, so a run started on Jobs is still
 * visible on Funding Radar — and after a hard refresh, because it reads the
 * durable rows, not component state. Click/Enter opens a list of the in-flight
 * runs with their derived phase and a link to Queues (→ User Input Needed when
 * a run is parked on the user). No hover-only affordances: the toggle is a
 * real button, 44px tall, with a visible focus ring.
 */
export function AIWorkChip() {
  const { snapshot, counts, active, stale } = useAIWork()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onKey) }
  }, [open])

  if (!active) return null

  const items: QueueItem[] = snapshot?.items || []
  const busy = counts.processing > 0
  const parts: string[] = []
  if (counts.queued) parts.push(`${counts.queued} queued`)
  if (counts.processing) parts.push(`${counts.processing} processing`)
  if (counts.paused) parts.push(`${counts.paused} paused`)
  if (counts.needs_input) parts.push(`${counts.needs_input} need${counts.needs_input === 1 ? 's' : ''} input`)
  const summary = parts.join(' • ') || 'queued…'
  const tone = counts.needs_input ? 'warn' : counts.paused && !busy ? 'warn' : busy ? 'busy' : 'info'

  return (
    <div ref={ref} className="relative" data-testid="ai-work-chip">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="true"
        aria-label={`AI work: ${summary}`}
        title={stale ? 'Last read failed — showing the last known state' : 'Live AI work — updates every few seconds'}
        className={`min-h-[44px] inline-flex items-center gap-2 px-3 rounded-full border text-xs mono font-medium focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-1 dark:focus:ring-offset-zinc-900 ${toneClasses(tone)}`}
      >
        {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden /> : counts.paused ? <PauseCircle className="w-3.5 h-3.5" aria-hidden /> : <Activity className="w-3.5 h-3.5" aria-hidden />}
        <span data-testid="ai-work-summary">{summary}</span>
        {counts.paused > 0 && <span className="hidden sm:inline opacity-80">— AI paused, will resume</span>}
        {stale && <span className="w-1.5 h-1.5 rounded-full bg-red-500" aria-label="stale" />}
        <ChevronDown className={`w-3.5 h-3.5 transition-transform ${open ? 'rotate-180' : ''}`} aria-hidden />
      </button>
      {open && (
        <div role="menu" className="absolute right-0 mt-2 w-[min(92vw,26rem)] z-40 rounded-xl border dark:border-zinc-700 bg-white dark:bg-zinc-900 shadow-lg p-2 space-y-1">
          <div className="px-2 py-1 text-[11px] mono uppercase text-zinc-500 flex items-center justify-between">
            <span>In flight</span>
            <span>{items.length} item{items.length === 1 ? '' : 's'}</span>
          </div>
          {items.length === 0 && <div className="px-2 py-3 text-xs mono text-zinc-500">Waiting for the first poll…</div>}
          {items.slice(0, 8).map((row) => <ChipRow key={row.id} row={row} />)}
          {items.length > 8 && <div className="px-2 py-1 text-[11px] mono text-zinc-500">+{items.length - 8} more in Queues</div>}
          <div className="border-t dark:border-zinc-800 pt-1 mt-1 flex gap-1">
            <Link role="menuitem" to="/queues" onClick={() => setOpen(false)} className="flex-1 min-h-[44px] inline-flex items-center justify-center gap-1 rounded-lg text-xs mono font-medium bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 focus:outline-none focus:ring-2 focus:ring-blue-500">
              Open Queues <ArrowRight className="w-3.5 h-3.5" />
            </Link>
            {counts.needs_input > 0 && (
              <Link role="menuitem" to="/queues#user-input" onClick={() => setOpen(false)} className="flex-1 min-h-[44px] inline-flex items-center justify-center gap-1 rounded-lg text-xs mono font-medium bg-amber-500 text-white focus:outline-none focus:ring-2 focus:ring-amber-600">
                <UserCheck className="w-3.5 h-3.5" /> User Input Needed
              </Link>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function ChipRow({ row }: { row: QueueItem }) {
  const state = deriveState(row)
  return (
    <div className={`px-2 py-1.5 rounded-lg border text-[11px] mono flex items-start gap-2 ${toneClasses(state?.tone || 'neutral')}`}>
      <StateIcon state={state} />
      <div className="min-w-0 flex-1">
        <div className="flex justify-between gap-2">
          <span className="font-medium truncate">#{row.id} {describeRow(row)}</span>
          <span className="shrink-0">{state?.label || row.status}</span>
        </div>
        {state?.detail && <div className="opacity-80 mt-0.5 line-clamp-2">{state.detail}</div>}
      </div>
    </div>
  )
}

export function StateIcon({ state, className = 'w-3.5 h-3.5 shrink-0 mt-0.5' }: { state: DerivedState | null; className?: string }) {
  if (!state) return <Activity className={className} aria-hidden />
  if (state.live && state.tone === 'busy') return <Loader2 className={`${className} animate-spin`} aria-hidden />
  if (state.phase === 'paused') return <PauseCircle className={className} aria-hidden />
  if (state.needsInput) return <UserCheck className={className} aria-hidden />
  if (state.tone === 'bad') return <AlertCircle className={className} aria-hidden />
  if (state.tone === 'ok') return <CheckCircle className={className} aria-hidden />
  return <Activity className={className} aria-hidden />
}

/**
 * The per-page status line: `Queued → Preparing → Applying → Applied` etc.,
 * derived from a queue row. Renders nothing without a row. Includes the
 * `Queues → User Input Needed` link when the run is parked on the user.
 */
export function WorkStatusLine({ row, prefix, onDismiss, testId }: { row: QueueItem | null; prefix?: string; onDismiss?: () => void; testId?: string }) {
  const state = deriveState(row)
  if (!row || !state) return null
  return (
    <div data-testid={testId} role="status" aria-live="polite" className={`text-xs mono p-2 rounded-lg border flex gap-2 items-start ${toneClasses(state.tone)}`}>
      <StateIcon state={state} />
      <div className="min-w-0 flex-1">
        <span className="font-medium">{prefix ? `${prefix} — ` : ''}{state.label}</span>
        <span className="opacity-60"> • #{row.id}</span>
        {state.detail && <div className="mt-0.5 opacity-90">{state.detail}</div>}
        {state.link && (
          <Link to={state.link} className="inline-flex items-center gap-1 mt-1 underline font-medium min-h-[32px] focus:outline-none focus:ring-2 focus:ring-blue-500 rounded px-1">
            {state.linkLabel || 'Open'} <ArrowRight className="w-3 h-3" />
          </Link>
        )}
      </div>
      {onDismiss && !state.live && (
        <button type="button" onClick={onDismiss} aria-label="Dismiss" className="shrink-0 px-2 min-h-[32px] rounded focus:outline-none focus:ring-2 focus:ring-blue-500 opacity-70 hover:opacity-100">×</button>
      )}
    </div>
  )
}

export default AIWorkChip
