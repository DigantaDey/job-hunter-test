/**
 * Auto mode (scheduled work) — the shared vocabulary of the Auto-mode UI.
 *
 * The shapes here are the ``GET /api/automation`` contract, and the states are
 * the *recorded* states of a scheduled run, straight from
 * ``scheduled_runs``. They are deliberately not collapsed into "working / not
 * working": a run skipped because the AI is down, one skipped because the
 * month's automation budget is spent, and a finished run are three different
 * truths, and the user has to be able to tell them apart to trust the feature.
 * The writer is :mod:`app.services.auto_scheduler`; nothing here recomputes a
 * schedule, a due-check or a quota.
 */

export type AutoRunState =
  | 'queued'
  | 'done'
  | 'paused'
  | 'failed'
  | 'needs_input'
  | 'skipped_outage'
  | 'skipped_quota'
  | 'skipped_no_consent'

/** One row of ``overview().last_run[workflow]`` (no workflow key) or ``recent[]`` (with it). */
export interface AutoRunRow {
  workflow?: string
  at: string | null
  state: AutoRunState | string | null
  job_id?: number | null
  queue_id?: number | null
  reason?: string
}

export interface AutoOverview {
  /** The user's switch (`automation.auto_mode`). */
  enabled: boolean
  /** Their plan may have the switch at all (``can_use_scheduled_workflows``). */
  can_use: boolean
  /** Switch on AND runnable: what the scheduler will actually honour. */
  active: boolean
  /** False when the whole scheduler is disabled by config (interval 0). */
  schedulable: boolean
  plan: string
  cadence: Record<string, number>
  /** Workflow → its AI-workflow toggle, so a silent workflow can be explained. */
  toggles: Record<string, boolean>
  consent_ok: boolean
  sweep_interval_seconds: number
  blocking: string[]
  last_run: Record<string, AutoRunRow | null>
  /** Workflow → ISO time, or ``null`` when there is no next run to promise. */
  next_run: Record<string, string | null>
  recent: AutoRunRow[]
  quota: { used: number; limit: number; remaining: number }
}

/** Workflow key → what it does, in the user's words (mirrors the backend's keys). */
export const AUTO_WORKFLOW_LABELS: Record<string, string> = {
  discovery: 'Job discovery',
  funding: 'Funding radar scan',
  application_prep: 'Application prep',
}

/** ``blocking`` codes → why nothing is being scheduled. */
export const AUTO_BLOCKERS: Record<string, string> = {
  scheduler_disabled: 'scheduled work is disabled on this server',
  plan_locked: 'your plan does not include scheduled workflows',
  plan_has_no_schedule: 'your plan has no cadence for scheduled workflows',
  auto_mode_off: 'auto mode is off',
  consent_required: 'the automation disclosure has not been accepted yet',
  workflow_off: "the workflow's AI toggle is off",
}

/**
 * State → chip label + colours + the line the user reads next to it.
 * Colours are literal class strings (Tailwind scans this file), and one tone is
 * used per family: green finished, amber waiting-on-something, red failed,
 * grey skipped.
 */
export const AUTO_STATE_COPY: Record<AutoRunState, { label: string; cls: string; note: string }> = {
  queued: {
    label: 'Queued',
    cls: 'bg-sky-50 border-sky-200 text-sky-700',
    note: 'Waiting for a worker slot.',
  },
  done: {
    label: 'Done',
    cls: 'bg-emerald-50 border-emerald-200 text-emerald-700',
    note: 'The run finished.',
  },
  paused: {
    label: 'Paused',
    cls: 'bg-amber-50 border-amber-200 text-amber-800',
    // v2.2.2: the queue's retry budget counts *failures*, so being parked by an
    // outage (and resumed) costs the run none of its attempts.
    note: 'Paused by an AI outage — it resumes on its own and spends no retry.',
  },
  needs_input: {
    label: 'Needs your input',
    cls: 'bg-amber-50 border-amber-200 text-amber-800',
    note: 'An application is waiting on you in Queues.',
  },
  failed: {
    label: 'Failed',
    cls: 'bg-red-50 border-red-200 text-red-700',
    note: 'It failed — the reason is in Queues.',
  },
  skipped_outage: {
    label: 'Skipped (AI unavailable)',
    cls: 'bg-zinc-100 border-zinc-300 text-zinc-700',
    note: 'AI unavailable — will run next cycle. Nothing was added to the queue.',
  },
  skipped_quota: {
    label: 'Limit reached',
    cls: 'bg-zinc-100 border-zinc-300 text-zinc-700',
    note: "This month's automation runs are used up — the scheduler stopped queueing.",
  },
  skipped_no_consent: {
    label: 'Consent needed',
    cls: 'bg-amber-50 border-amber-200 text-amber-800',
    note: 'The automation disclosure has not been accepted.',
  },
}

/** Cadence seconds → "every 2h" / "every 1d" / "every 30m". */
export function fmtCadence(seconds?: number | null): string {
  if (!seconds || seconds <= 0) return 'not on this plan'
  if (seconds % 86400 === 0) return `every ${seconds / 86400}d`
  if (seconds % 3600 === 0) return `every ${seconds / 3600}h`
  if (seconds % 60 === 0) return `every ${seconds / 60}m`
  return `every ${seconds}s`
}

/** "in 1h 20m" / "35m ago" — never a bare "soon", never a fake countdown. */
export function fmtRelativeToNow(iso?: string | null, now: number = Date.now()): string {
  if (!iso) return '—'
  const at = new Date(iso).getTime()
  if (Number.isNaN(at)) return '—'
  const delta = Math.round((at - now) / 1000)
  const abs = Math.abs(delta)
  const unit = abs >= 86400
    ? `${Math.floor(abs / 86400)}d ${Math.floor((abs % 86400) / 3600)}h`
    : abs >= 3600
      ? `${Math.floor(abs / 3600)}h ${Math.floor((abs % 3600) / 60)}m`
      : abs >= 60
        ? `${Math.floor(abs / 60)}m`
        : `${abs}s`
  return delta >= 0 ? `in ${unit}` : `${unit} ago`
}

/**
 * A recorded moment, in local time, with the relative tail:
 * "9/13/2026, 13:40:07 (in 1h 20m)". The API always sends an explicit UTC
 * offset for these, so the local conversion is correct — which it is *not* for
 * the naive timestamps most other endpoints return.
 */
export function fmtWhen(iso?: string | null, now: number = Date.now()): string {
  if (!iso) return '—'
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return '—'
  return `${at.toLocaleString([], { hour12: false })} (${fmtRelativeToNow(iso, now)})`
}

/** A row's state → chip, or a neutral "never run" when there is no row yet. */
export function autoStateCopy(state?: string | null): { label: string; cls: string; note: string } | null {
  if (!state) return null
  return (AUTO_STATE_COPY as Record<string, { label: string; cls: string; note: string }>)[state] || null
}
