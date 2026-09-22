import type { AIQueueSnapshot, QueueItem } from '../hooks/useAIQueue'

/**
 * Derived state for AI work (v2.2.8) — pure functions over `pipeline_jobs` rows.
 *
 * The pages used to keep a private `applyMsg`/`generateMsg` string that died
 * with the component. Now every label is *derived* from the durable row, on
 * every render, so the same run reads the same on Jobs, Funding, Resume Studio
 * and the Dashboard, and reappears after a refresh. Nothing here talks to the
 * network; nothing here is stored.
 */

// ----------------------------------------------------------------------------
// sessionStorage — the optimistic id between the click and the first poll
// ----------------------------------------------------------------------------

/** One remembered trigger: enough to find the row again after an F5. */
export interface TrackedWork {
  /** Queue row id from the trigger's `{pipeline_job_id}` receipt. */
  id: number
  /** `application` | `discovery` | `ai` | `email` | `funding` */
  pipeline: string
  /** Scope key so a page can find *its* run (`apply:12`, `resume:12`, `discover`, `funding`, `email:12`). */
  key: string
  /** When the user clicked — used to expire forgotten entries. */
  at: number
}

export const TRACKED_KEY = 'jh.aiwork.tracked'
/** Forget an entry after this long — the server's `recent` window is 30 min. */
export const TRACKED_TTL_MS = 6 * 60 * 60 * 1000

function storage(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.sessionStorage : null
  } catch {
    return null
  }
}

export function readTracked(): TrackedWork[] {
  const store = storage()
  if (!store) return []
  try {
    const raw = store.getItem(TRACKED_KEY)
    const list = raw ? (JSON.parse(raw) as TrackedWork[]) : []
    const cutoff = Date.now() - TRACKED_TTL_MS
    return Array.isArray(list) ? list.filter((t) => t && typeof t.id === 'number' && t.at > cutoff) : []
  } catch {
    return []
  }
}

export function writeTracked(list: TrackedWork[]): void {
  const store = storage()
  if (!store) return
  try {
    store.setItem(TRACKED_KEY, JSON.stringify(list.slice(-50)))
  } catch {
    /* quota / private mode — the poll still finds the row, we just lose the optimistic hint */
  }
}

/** Remember a trigger receipt (replaces an older entry with the same key). */
export function track(entry: Omit<TrackedWork, 'at'>): TrackedWork[] {
  const next = readTracked().filter((t) => t.key !== entry.key)
  next.push({ ...entry, at: Date.now() })
  writeTracked(next)
  return next
}

export function untrack(key: string): TrackedWork[] {
  const next = readTracked().filter((t) => t.key !== key)
  writeTracked(next)
  return next
}

// ----------------------------------------------------------------------------
// Finding rows
// ----------------------------------------------------------------------------

/** Every row the snapshot knows about, in-flight first, then recent outcomes. */
export function allRows(snapshot: AIQueueSnapshot | null): QueueItem[] {
  if (!snapshot) return []
  return [...snapshot.items, ...snapshot.recent]
}

export function rowById(snapshot: AIQueueSnapshot | null, id: number | null | undefined): QueueItem | null {
  if (id == null) return null
  return allRows(snapshot).find((r) => r.id === id) || null
}

/**
 * The newest row for a (pipeline, job) pair — how a page finds the application
 * run for job #A without having stored the id (another tab, another device).
 */
export function latestRowFor(
  snapshot: AIQueueSnapshot | null,
  pipeline: string,
  jobId?: number | null,
  task?: string | null,
): QueueItem | null {
  const rows = allRows(snapshot).filter(
    (r) =>
      r.pipeline === pipeline &&
      (jobId == null || r.job_id === jobId || r.payload?.job_id === jobId) &&
      (task == null || r.task === task || r.payload?.task === task),
  )
  if (rows.length === 0) return null
  // In-flight rows come first; among equals prefer the newest id.
  const live = rows.filter((r) => isLive(r.status))
  const pool = live.length ? live : rows
  return pool.reduce((best, r) => (r.id > best.id ? r : best), pool[0])
}

export function isLive(status: string): boolean {
  return status === 'queued' || status === 'processing' || status === 'paused' || status === 'needs_input'
}

// ----------------------------------------------------------------------------
// Derived phases
// ----------------------------------------------------------------------------

export type Tone = 'neutral' | 'info' | 'busy' | 'warn' | 'ok' | 'bad'

export interface DerivedState {
  /** Short machine phase, e.g. `queued`, `preparing`, `applying`, `applied`, `needs_input`, `paused`, `dead`. */
  phase: string
  /** Human label for the chip/line. */
  label: string
  /** One sentence of detail — the resume decision, the missing fields, the error… */
  detail: string
  tone: Tone
  /** Still moving — keep the spinner. */
  live: boolean
  /** Send the user to Queues → User Input Needed. */
  needsInput: boolean
  /** Route to open for more (`/queues`, `/resumes`, `/emails`, `/funding`). */
  link?: string
  linkLabel?: string
}

function pausedDetail(row: QueueItem): string {
  const n = row.paused_count || 0
  return `AI paused${n ? ` (${n} outage${n === 1 ? '' : 's'})` : ''} — will resume automatically when the provider is back.`
}

function commonQueueStates(row: QueueItem, verb: string): DerivedState | null {
  switch (row.status) {
    case 'queued':
      return { phase: 'queued', label: 'Queued', detail: `Waiting for a worker slot — ${verb} starts on its own.`, tone: 'info', live: true, needsInput: false }
    case 'paused':
      return { phase: 'paused', label: 'AI paused', detail: pausedDetail(row), tone: 'warn', live: true, needsInput: false, link: '/queues', linkLabel: 'Queues' }
    case 'dead':
      return { phase: 'dead', label: 'Gave up', detail: row.error || 'Out of attempts — retry from Queues.', tone: 'bad', live: false, needsInput: false, link: '/queues', linkLabel: 'Queues' }
    case 'failed':
      return { phase: 'failed', label: 'Failed', detail: row.error || 'The run failed.', tone: 'bad', live: false, needsInput: false, link: '/queues', linkLabel: 'Queues' }
    default:
      return null
  }
}

/** Queued → Preparing → Applying → Applied / Ready to apply / Needs input. */
export function deriveApplyState(row: QueueItem | null): DerivedState | null {
  if (!row) return null
  const common = commonQueueStates(row, 'preparing')
  if (common) return common
  const result = row.result || {}
  const decision = result.resume_decision ? ` • resume: ${result.resume_decision}` : ''
  const vault = result.credential_created || result.vault_created ? ' • vault credential created' : ''
  if (row.status === 'processing') {
    const applying = (row.payload?.mode || 'prepare') === 'execute'
    return {
      phase: applying ? 'applying' : 'preparing',
      label: applying ? 'Applying' : 'Preparing',
      detail: applying
        ? 'Resume chosen, form mapped — the browser is filling the application now.'
        : 'Choosing the resume, detecting the form and mapping your profile to it…',
      tone: 'busy', live: true, needsInput: false,
    }
  }
  if (row.status === 'needs_input') {
    const fields = (result.missing_fields || []) as { label?: string; name?: string }[]
    const names = fields.slice(0, 4).map((f) => f.label || f.name).filter(Boolean).join(', ')
    return {
      phase: 'needs_input', label: 'Needs your input',
      detail: `${fields.length || 'Some'} required field${fields.length === 1 ? '' : 's'} need${fields.length === 1 ? 's' : ''} an answer${names ? `: ${names}` : ''}.`,
      tone: 'warn', live: false, needsInput: true, link: '/queues', linkLabel: 'Queues → User Input Needed',
    }
  }
  // done
  const status = String(result.status || 'done')
  if (status === 'applied') {
    return { phase: 'applied', label: 'Applied', detail: `Application submitted${decision}${vault}.`, tone: 'ok', live: false, needsInput: false }
  }
  if (status === 'ready_to_apply') {
    const prepareOnly = (row.payload?.mode || 'prepare') === 'prepare'
    if (prepareOnly) {
      return {
        phase: 'ready_to_apply', label: 'Preparation complete',
        detail: `Your resume and form plan are ready${decision}${vault}. No browser submission was run — continue in Assisted Apply.`,
        tone: 'ok', live: false, needsInput: false,
        link: `/assist?job=${row.job_id}`, linkLabel: 'Continue in Assisted Apply',
      }
    }
    return { phase: 'ready_to_apply', label: 'Ready to apply', detail: `Prefilled in dry-run — review and submit${decision}.`, tone: 'ok', live: false, needsInput: false }
  }
  if (status === 'preparing' || status === 'prepared') {
    const mapped = result.fields_total != null ? ` • ${result.fields_mapped ?? 0}/${result.fields_total} fields mapped` : ''
    return { phase: 'prepared', label: 'Prepared', detail: `Preparation completed${decision}${vault}${mapped}. Continue in Assisted Apply to fill and review the application.`, tone: 'ok', live: false, needsInput: false,
      link: `/assist?job=${row.job_id}`, linkLabel: 'Continue in Assisted Apply' }
  }
  if (status === 'failed') {
    return { phase: 'failed', label: 'Failed', detail: result.message || row.error || 'Autofill could not run.', tone: 'bad', live: false, needsInput: false, link: '/queues', linkLabel: 'Queues' }
  }
  return { phase: status, label: status.replace(/_/g, ' '), detail: `${decision}${vault}`.replace(/^ • /, ''), tone: 'ok', live: false, needsInput: false }
}

/** Queued → Generating (fact guard) → Generated / Rejected. */
export function deriveResumeState(row: QueueItem | null): DerivedState | null {
  if (!row) return null
  const common = commonQueueStates(row, 'generation')
  if (common) return common
  const result = row.result || {}
  if (row.status === 'processing') {
    return { phase: 'generating', label: 'Generating', detail: 'Tailoring your profile to the JD — the fact guard checks every employer, title, school and date before anything is saved.', tone: 'busy', live: true, needsInput: false }
  }
  if (result.status === 'rejected') {
    const issues = (result.issues || result.violations || []) as any[]
    return {
      phase: 'rejected', label: 'Rejected by the guardrail',
      detail: result.message || 'The draft contained facts not in your profile — nothing was saved.',
      tone: 'bad', live: false, needsInput: false,
      link: issues.length ? '/resumes' : undefined, linkLabel: issues.length ? `Resume Studio (${issues.length} issue${issues.length === 1 ? '' : 's'})` : undefined,
    }
  }
  if (result.resume_id) {
    return { phase: 'generated', label: 'Tailored resume ready', detail: `${result.display_name || 'Generated'} — pending your approval${(result.tags || []).length ? ` • ${result.tags.slice(0, 4).join(', ')}` : ''}.`, tone: 'ok', live: false, needsInput: false, link: '/resumes', linkLabel: 'Resume Studio' }
  }
  return { phase: 'done', label: 'Done', detail: '', tone: 'ok', live: false, needsInput: false }
}

/** Queued → Scanning → N companies / paused / blocked. */
export function deriveFundingState(row: QueueItem | null): DerivedState | null {
  if (!row) return null
  const common = commonQueueStates(row, 'the scan')
  if (common) return common
  const result = row.result || {}
  if (row.status === 'processing') {
    return { phase: 'scanning', label: 'Scanning', detail: 'Searching funding sources and ranking companies against your profile…', tone: 'busy', live: true, needsInput: false }
  }
  const scanned = Number(result.scanned ?? 0)
  const status = String(result.scan_status || 'ok')
  if (status !== 'ok') {
    return { phase: status, label: status === 'paused' ? 'Scan paused' : 'Scan blocked', detail: result.reason || 'The radar could not be refreshed.', tone: status === 'paused' ? 'warn' : 'bad', live: false, needsInput: false, link: '/funding', linkLabel: 'Funding Radar' }
  }
  return { phase: 'scanned', label: 'Scan finished', detail: `${scanned} compan${scanned === 1 ? 'y' : 'ies'} verified${result.added != null ? ` • ${result.added} new` : ''}.`, tone: 'ok', live: false, needsInput: false, link: '/funding', linkLabel: 'Funding Radar' }
}

/** Queued → Searching → N jobs found. */
export function deriveDiscoveryState(row: QueueItem | null): DerivedState | null {
  if (!row) return null
  const common = commonQueueStates(row, 'the search')
  if (common) return common
  const result = row.result || {}
  if (row.status === 'processing') {
    return { phase: 'searching', label: 'Discovering', detail: 'Fetching boards and scoring postings…', tone: 'busy', live: true, needsInput: false }
  }
  const added = Number(result.added ?? (result.jobs || []).length ?? 0)
  const fetched = result.fetched != null ? ` of ${result.fetched} fetched` : ''
  return { phase: 'discovered', label: 'Discovery finished', detail: `${added} new job${added === 1 ? '' : 's'}${fetched}${result.why_empty ? ` — ${String(result.why_empty).replace(/_/g, ' ')}` : ''}.`, tone: 'ok', live: false, needsInput: false, link: '/jobs', linkLabel: 'Jobs' }
}

/** Queued → Drafting → In the bucket / rejected. */
export function deriveEmailState(row: QueueItem | null): DerivedState | null {
  if (!row) return null
  const common = commonQueueStates(row, 'drafting')
  if (common) return common
  const result = row.result || {}
  if (row.status === 'processing') {
    return { phase: 'drafting', label: 'Drafting', detail: 'Finding a decision maker and writing the email from your profile and the JD…', tone: 'busy', live: true, needsInput: false }
  }
  if (result.status === 'rejected') {
    return { phase: 'rejected', label: 'Not drafted', detail: result.message || result.code || 'The draft could not be written.', tone: 'bad', live: false, needsInput: false }
  }
  if (result.email_id) {
    return { phase: 'drafted', label: result.status === 'sent' ? 'Sent' : 'Email drafted', detail: `To ${result.to || 'a contact'} — ${result.status === 'sent' ? 'sent' : 'waiting in the approval bucket'}.`, tone: 'ok', live: false, needsInput: false, link: '/emails', linkLabel: 'Email Bucket' }
  }
  return { phase: 'done', label: 'Done', detail: '', tone: 'ok', live: false, needsInput: false }
}

/** Route a row to the right deriver by pipeline/task. */
export function deriveState(row: QueueItem | null): DerivedState | null {
  if (!row) return null
  switch (row.pipeline) {
    case 'application':
      return deriveApplyState(row)
    case 'discovery':
      return deriveDiscoveryState(row)
    case 'funding':
      return deriveFundingState(row)
    case 'email':
      return deriveEmailState(row)
    case 'ai':
      if ((row.task || row.payload?.task) === 'generate_resume') return deriveResumeState(row)
      return commonQueueStates(row, 'the task') || {
        phase: row.status === 'processing' ? 'working' : 'done',
        label: row.status === 'processing' ? 'Working' : 'Done',
        detail: String(row.task || row.payload?.task || 'ai').replace(/_/g, ' '),
        tone: row.status === 'processing' ? 'busy' : 'ok', live: row.status === 'processing', needsInput: false,
      }
    default:
      return commonQueueStates(row, 'the task')
  }
}

/** A short human name for a row in the chip's dropdown. */
export function describeRow(row: QueueItem): string {
  const job = row.job_id ? ` job #${row.job_id}` : ''
  switch (row.pipeline) {
    case 'application': return `Auto-apply${job}`
    case 'discovery': return 'Discover jobs'
    case 'funding': return 'Funding re-scan'
    case 'email': return `Cold email${job}`
    case 'ai':
      return (row.task || row.payload?.task) === 'generate_resume' ? `Tailored resume${job}` : `AI • ${String(row.task || row.payload?.task || 'task').replace(/_/g, ' ')}`
    default: return row.pipeline
  }
}

/** Tailwind classes for a tone (background/text/border), shared by every chip. */
export function toneClasses(tone: Tone): string {
  switch (tone) {
    case 'busy': return 'bg-blue-50 dark:bg-blue-950 border-blue-200 dark:border-blue-800 text-blue-800 dark:text-blue-200'
    case 'info': return 'bg-zinc-50 dark:bg-zinc-800 border-zinc-200 dark:border-zinc-700 text-zinc-700 dark:text-zinc-200'
    case 'warn': return 'bg-amber-50 dark:bg-amber-950 border-amber-200 dark:border-amber-800 text-amber-800 dark:text-amber-200'
    case 'ok': return 'bg-emerald-50 dark:bg-emerald-950 border-emerald-200 dark:border-emerald-800 text-emerald-800 dark:text-emerald-200'
    case 'bad': return 'bg-red-50 dark:bg-red-950 border-red-200 dark:border-red-800 text-red-800 dark:text-red-200'
    default: return 'bg-zinc-50 dark:bg-zinc-800 border-zinc-200 dark:border-zinc-700 text-zinc-600 dark:text-zinc-300'
  }
}
