import { AlertTriangle, PauseCircle, ShieldAlert, X } from 'lucide-react'
import { Link } from 'react-router-dom'
import type { AIOutage } from '../api/client'
import { useAuth } from '../context/AuthContext'
import { isOwner } from '../lib/role'

function fmtHint(seconds?: number | null): string | null {
  if (!seconds || seconds <= 0) return null
  const s = Math.round(seconds)
  if (s < 90) return `~${s}s`
  if (s < 5400) return `~${Math.round(s / 60)}min`
  return `~${Math.round(s / 3600)}h`
}

/**
 * Per-page banner for a 503 that came back with the AI outage shape.
 *
 * v2.1 renders three honest variants — the old single red box is kept for
 * responses that predate the state field:
 *
 * * `ai_paused`  (transient_outage)  — "AI paused — will resume automatically";
 *   nothing is degraded, the work will be re-run when the provider is back.
 * * `ai_blocked` (blocked_needs_action) — "AI blocked — action needed" with
 *   the fix and a link to Settings → AI API (retrying is pointless).
 * * legacy (no state) — the plain "AI unavailable" diagnosis.
 */
export function AIOutageBanner({ outage, what = 'nothing was generated', onDismiss }: {
  outage: AIOutage
  what?: string
  onDismiss?: () => void
}) {
  const { user } = useAuth()
  const owner = isOwner(user)
  const paused = outage.status === 'ai_paused' || outage.state === 'transient_outage'
  const blocked = outage.status === 'ai_blocked' || outage.state === 'blocked_needs_action'
  const tone = paused
    ? 'bg-amber-50 dark:bg-amber-950 border-amber-200 dark:border-amber-800'
    : 'bg-red-50 dark:bg-red-950 border-red-200 dark:border-red-800'
  const textTone = paused ? 'text-amber-800 dark:text-amber-200' : 'text-red-800 dark:text-red-200'
  const subTone = paused ? 'text-amber-700 dark:text-amber-300' : 'text-red-700 dark:text-red-300'
  const icon = paused ? PauseCircle : blocked ? ShieldAlert : AlertTriangle
  const Icon = icon

  return (
    <div className={`p-3 rounded-xl border ${tone} text-sm`}>
      <div className="flex items-start gap-2">
        <Icon className={`w-4 h-4 shrink-0 mt-0.5 ${paused ? 'text-amber-600 dark:text-amber-400' : 'text-red-600 dark:text-red-400'}`} />
        <div className="min-w-0 flex-1">
          <div className={`font-medium ${textTone}`}>
            {paused
              ? 'AI paused — will resume automatically'
              : blocked
                ? 'AI blocked — action needed'
                : `AI unavailable — ${what}`}
          </div>
          <div className={`text-xs mono mt-1 ${subTone}`}>
            {outage.message || 'The AI provider could not be reached.'}
            {outage.reason ? <span className="opacity-70"> ({outage.reason}{outage.workflow ? ` • ${outage.workflow}` : ''})</span> : null}
          </div>
          {paused && (
            <div className={`text-xs mono mt-1 ${subTone}`}>
              No degraded result was produced{outage.retry_after_hint ? ` — retrying in ${fmtHint(outage.retry_after_hint)}` : ''}.
              Queued work resumes on its own when the provider is back.
            </div>
          )}
          {blocked && (
            <div className={`text-xs mono mt-1 ${subTone}`}>
              Retrying won't fix this until the key/config is corrected.
            </div>
          )}
          {outage.fix && (
            <div className={`text-xs mono mt-1 ${subTone}`}>
              <strong>Fix:</strong> {outage.fix}{' '}
              {(blocked || !paused) && owner && (
                <Link to="/admin" className="underline font-medium">Admin console → AI providers</Link>
              )}
              {(blocked || !paused) && !owner && (
                <span className="underline font-medium">ask the workspace owner to check the AI connection</span>
              )}
            </div>
          )}
          {outage.detail && <div className={`text-[11px] mono mt-1 break-all ${subTone} opacity-80`}>{outage.detail}</div>}
          {(outage.base_url || outage.model) && (
            <div className={`text-[11px] mono mt-1 ${subTone} opacity-80`}>
              endpoint {outage.base_url || '—'} • model {outage.model || '—'}
            </div>
          )}
        </div>
        {onDismiss && (
          <button onClick={onDismiss} className={`p-1 rounded hover:bg-black/5 dark:hover:bg-white/10 ${subTone}`} title="Dismiss">
            <X className="w-3.5 h-3.5" />
          </button>
        )}
      </div>
    </div>
  )
}

/**
 * Global (Layout) banner driven by the polled three-state availability
 * signal. Hidden when the signal is green or unknown (e.g. not signed in).
 *
 * * transient_outage — amber: the product is paused and will resume
 *   automatically; offers one-click resume for the impatient.
 * * blocked_needs_action — red: retrying is pointless until the user acts.
 */
export function AIStatusBanner({ status, resuming, onResume, owner }: {
  status: { state?: string, reason?: string | null, hint?: string | null, paused_count?: number, retry_after_hint?: number | null } | null
  resuming?: boolean
  onResume?: () => void
  owner?: boolean
}) {
  const { user } = useAuth()
  const isOwnerUser = owner ?? isOwner(user)
  const state = status?.state
  if (state !== 'transient_outage' && state !== 'blocked_needs_action') return null
  const paused = state === 'transient_outage'
  const pausedCount = Math.max(0, status?.paused_count || 0)
  const hint = status?.hint || ''

  return (
    <div className={`p-3 mb-4 rounded-xl border text-sm flex items-start gap-2 ${
      paused
        ? 'bg-amber-50 dark:bg-amber-950 border-amber-200 dark:border-amber-800'
        : 'bg-red-50 dark:bg-red-950 border-red-200 dark:border-red-800'
    }`}>
      {paused ? <PauseCircle className={`w-4 h-4 shrink-0 mt-0.5 ${'text-amber-600 dark:text-amber-400'}`} />
        : <ShieldAlert className="w-4 h-4 shrink-0 mt-0.5 text-red-600 dark:text-red-400" />}
      <div className="min-w-0 flex-1">
        <span className={`font-medium ${paused ? 'text-amber-800 dark:text-amber-200' : 'text-red-800 dark:text-red-200'}`}>
          {paused ? 'AI paused — will resume automatically' : 'AI blocked — action needed'}
        </span>
        <span className={`ml-2 text-xs mono ${paused ? 'text-amber-700 dark:text-amber-300' : 'text-red-700 dark:text-red-300'}`}>
          {paused
            ? <>
                {pausedCount > 0 && <>{pausedCount} queued item{pausedCount === 1 ? '' : 's'} paused. </>}
                The watchdog re-checks the provider and resumes the work on its own
                {status?.retry_after_hint ? ` (next check ~${fmtHint(status.retry_after_hint)})` : ''}.
                {hint && <span className="opacity-80"> — {hint}</span>}
              </>
            : <>
                {status?.reason ? <span className="opacity-80">({status.reason})</span> : null}
                {hint && <> — {hint}</>}
              </>}
        </span>
      </div>
      {paused && pausedCount > 0 && onResume && (
        <button
          onClick={onResume}
          disabled={resuming}
          className="text-xs mono font-medium px-2.5 py-1.5 rounded-lg bg-amber-600 hover:bg-amber-700 disabled:opacity-50 text-white shrink-0"
        >
          {resuming ? 'Resuming…' : 'Resume now'}
        </button>
      )}
      {!paused && isOwnerUser && (
        <Link to="/admin" className="text-xs mono font-medium px-2.5 py-1.5 rounded-lg bg-red-600 hover:bg-red-700 text-white shrink-0">
          Fix in admin console
        </Link>
      )}
      {!paused && !isOwnerUser && (
        <span className="text-xs mono text-red-700 dark:text-red-300 shrink-0">
          Ask the workspace owner to check the connection
        </span>
      )}
    </div>
  )
}
