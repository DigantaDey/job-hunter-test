/**
 * Assisted Apply — the human side of a browser-assisted application session.
 *
 * The backend drives an isolated browser per job and stops at the first moment a
 * person is required. This page is where that person is: it shows the actionable
 * queue (sign-in, MFA, bot check, an unmapped field, a legal/EEO question, an
 * expired session) and gives each item the one action that closes it.
 *
 * Three rules the UI states rather than hides:
 *
 * * a password, an MFA code or a CAPTCHA is entered in the browser, never here —
 *   "I finished this step" only records *that* it happened, and the API refuses a
 *   payload that carries a secret-looking field (`422 restricted_field`);
 * * a resume is checkpoint-validated (job, URL, employer, application identity,
 *   TTL) before it runs, so a stale tab cannot type into the wrong application;
 * * fields already filled — by the assistant or by the user in the browser —
 *   are never typed again, which is why resuming twice is safe.
 *
 * Polling follows the app-wide convention (`QUEUES_POLL_MS` on Queues, 25 s on
 * Emails): one visibility-aware interval, no reads in a hidden tab.
 */
import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import client, { apiError } from '../api/client'
import {
  AlertTriangle, CheckCircle, Clock, ExternalLink, Hand, Loader2,
  LogIn, Pause, Play, RefreshCw, ShieldCheck, XCircle,
} from 'lucide-react'

/** Same cadence as the Queues page's reads: this list moves only when a human acts. */
export const ASSIST_POLL_MS = 20_000

type Action = {
  id: number
  session_id: number | null
  job_id: number | null
  kind: string
  status: string
  title: string
  instructions: string
  reason: string
  fields: any[]
  handoff: any
  occurrences: number
  created_at: string
}
type BrowserRuntime = {
  available: boolean
  reason?: string
  /** The mode the next launch would use — the user is told whether they see a window. */
  mode?: 'headed' | 'headless' | string
  mode_reason?: string
  engine?: string
}

/** One recorded step of a run: hosts, control labels, field *names* — never a value. */
type FlowStep = {
  event?: string
  step?: number
  host?: string
  url?: string
  title?: string
  reason?: string
  kind?: string
  control?: string
  fields?: string[]
  fields_total?: number
  filled_total?: number
  mode?: string
}

type Session = {
  id: number
  job_id: number
  job: { title: string; company: string; url: string }
  state: string
  phase: string
  state_reason: string
  pause: { kind: string; reason: string }
  progress: Record<string, any>
  checkpoint: { fields: Record<string, any>; completed_fields: string[]; answer_fields: string[] }
  /** What the run actually did, in order — the recording behind "future reference". */
  flow?: { journal: FlowStep[]; progress: Record<string, any> }
  /** The session's visible browser window, if one is still open for you to work in. */
  window?: { open?: boolean; idle_seconds?: number; age_seconds?: number }
  plan?: {
    must_pause: boolean
    autofillable: string[]
    asking: any[]
    handoffs: any[]
    already_completed: string[]
  } | null
  expires_at: string | null
}

/** Kinds where the human acts *in the browser* — never by typing a value here. */
const HANDOFF_KINDS = ['login', 'mfa', 'captcha', 'session_expired']

/** Kinds that hand the *flow itself* over: the assistant stopped on purpose. */
const TAKEOVER_KINDS = ['review_required']

/** True when the way to answer an item is to act in the browser window. */
const actsInBrowser = (kind: string) => HANDOFF_KINDS.includes(kind) || TAKEOVER_KINDS.includes(kind)

/** Why a pass stopped, in the user's words. */
const STOP_LABEL: Record<string, string> = {
  login: 'the portal wants a sign-in',
  login_detected: 'the portal wants a sign-in',
  mfa: 'a verification code is needed',
  captcha: 'a bot check needs a human',
  unknown_field: 'a field we do not recognise',
  ambiguous_field: 'a field with two readings',
  sensitive_field: 'a question only you can answer',
  legal_question: 'a legal or eligibility question',
  session_expired: 'the session expired — sign in again',
  review_required: 'it needs you to finish the flow',
  waiting_for_you_to_submit: 'the form is filled; submitting is yours to do',
  submit_requires_reservation: 'the Submit button is yours to press',
  flow_cannot_continue_automatically: 'the next step is not one it can take alone',
  flow_leaves_the_application: 'the flow left the application site',
  flow_page_budget_reached: 'it reached the page budget for one pass',
  flow_needs_a_human: 'it needs a human to continue',
  no_field_could_be_filled: 'nothing on the page could be filled safely',
  advance_unconfirmed: 'the page did not react to the last click',
  page_not_readable: 'the page could not be read',
  no_progress: 'two passes made no progress',
}

const EVENT_LABEL: Record<string, string> = {
  page: 'Looked at',
  fill: 'Typed into',
  advance: 'Clicked through',
  pause: 'Stopped for you',
  blocked: 'Could not continue',
  confirmed: 'Portal confirmed',
  resumed: 'Resumed',
  submit: 'Submission',
}

/** What a pass actually did — never "done" unless the portal itself said so. */
function passSummary(result: any): string {
  if (!result || typeof result !== 'object') return 'Pass finished.'
  const pages = Number(result.pages || 0)
  const filled = (result.filled || []).length
  const parts = [pages ? `Looked at ${pages} page(s)` : 'Looked at the page',
                 filled ? `typed ${filled} field(s)` : 'typed nothing']
  if ((result.advanced || []).length) {
    parts.push(`moved on with ${(result.advanced as string[]).map(a => `“${a}”`).join(', ')}`)
  }
  if (result.confirmed) {
    return `${parts.join(' • ')} • the portal confirmed the application (${result.confirmed}).`
  }
  const why = result.next_step?.reason || result.pause_reason || result.pause || ''
  const stopped = STOP_LABEL[why] || why
  return `${parts.join(' • ')}${stopped ? ` • stopped because ${stopped}.` : '.'}`
}

/** One recorded step, as a line a human can read. */
function flowLine(entry: FlowStep): string {
  const where = entry.host || entry.url || ''
  switch (entry.event) {
    case 'page':
      return `${entry.title || where}${entry.reason ? ` (step ${entry.reason})` : ''}`
    case 'fill':
      return (entry.fields || []).join(', ') || 'fields'
    case 'advance':
      return `${entry.control || ''}${entry.kind ? ` — ${entry.kind.replace(/_/g, ' ')}` : ''}`
    case 'pause':
      return STOP_LABEL[entry.kind || ''] || entry.kind || entry.reason || ''
    case 'blocked':
      return STOP_LABEL[entry.reason || ''] || entry.reason || ''
    case 'confirmed':
      return entry.reason || 'submitted'
    default:
      return entry.reason || entry.kind || ''
  }
}

const KIND_LABEL: Record<string, string> = {
  login: 'Sign in',
  mfa: 'Verification code',
  captcha: 'Bot check',
  unknown_field: 'A field we do not recognise',
  ambiguous_field: 'A field with two readings',
  sensitive_field: 'Personal question',
  legal_question: 'Legal / eligibility question',
  session_expired: 'Session expired',
  review_required: 'Needs your review',
}

const STATE_TONE: Record<string, string> = {
  awaiting_user: 'text-amber-700 dark:text-amber-300',
  paused: 'text-amber-700 dark:text-amber-300',
  expired: 'text-red-700 dark:text-red-300',
  failed: 'text-red-700 dark:text-red-300',
  completed: 'text-emerald-700 dark:text-emerald-300',
  active: 'text-blue-700 dark:text-blue-300',
}

const isLive = (s: Session) => !['completed', 'expired', 'failed', 'cancelled'].includes(s.state)

export default function Assist() {
  const [searchParams] = useSearchParams()
  const [sessions, setSessions] = useState<Session[]>([])
  const [actions, setActions] = useState<Action[]>([])
  const [jobs, setJobs] = useState<any[]>([])
  const [jobId, setJobId] = useState(() => searchParams.get('job') || '')
  const [browserRuntime, setBrowserRuntime] = useState<BrowserRuntime | null>(null)
  const [answers, setAnswers] = useState<Record<number, string>>({})
  const [descriptors, setDescriptors] = useState<Record<number, any>>({})
  const [busy, setBusy] = useState<number | 'start' | null>(null)
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')

  const load = async () => {
    try {
      const [s, a] = await Promise.all([
        client.get('/api/application-sessions', { params: { include_closed: true } }),
        client.get('/api/application-sessions/actions'),
      ])
      setSessions(s.data.sessions || s.data || [])
      setActions(a.data.actions || a.data || [])
      setErr('')
    } catch (e: any) {
      setErr(apiError(e, 'Could not read your assisted sessions'))
    }
  }

  useEffect(() => {
    let id: ReturnType<typeof setInterval> | null = null
    const start = () => { if (!id) { load(); id = setInterval(load, ASSIST_POLL_MS) } }
    const stop = () => { if (id) { clearInterval(id); id = null } }
    const onVis = () => { document.visibilityState === 'hidden' ? stop() : start() }
    if (document.visibilityState !== 'hidden') start()
    document.addEventListener('visibilitychange', onVis)
    client.get('/api/jobs', { params: { limit: 50 } })
      .then(r => setJobs(r.data.jobs || r.data || []))
      .catch(() => setJobs([]))
    // This operational read is separate from Auto-apply: Assisted Apply can
    // remain available while unattended autofill is purposefully disabled.
    client.get('/api/account/runtime')
      .then(r => setBrowserRuntime(r.data?.assisted_apply_runtime || null))
      .catch(() => setBrowserRuntime(null))
    return () => { stop(); document.removeEventListener('visibilitychange', onVis) }
  }, [])

  useEffect(() => {
    const requested = searchParams.get('job')
    if (requested) setJobId(requested)
  }, [searchParams])

  const startSession = async () => {
    if (!jobId) return
    setBusy('start'); setMsg(''); setErr('')
    try {
      const r = await client.post('/api/application-sessions', { job_id: Number(jobId) })
      setMsg(r.data.created ? 'Session started — open the browser to begin.' : 'You already have a live session for this job.')
      await load()
    } catch (e: any) {
      setErr(apiError(e, 'Could not start a session'))
    } finally {
      setBusy(null)
    }
  }

  const act = async (session: Session, what: 'pause' | 'resume' | 'cancel' | 'reauthenticate' | 'pass') => {
    setBusy(session.id); setMsg(''); setErr('')
    try {
      if (what === 'cancel') {
        await client.post(`/api/application-sessions/${session.id}/cancel`)
      } else if (what === 'pause') {
        await client.post(`/api/application-sessions/${session.id}/pause`)
      } else if (what === 'resume') {
        const r = await client.post(`/api/application-sessions/${session.id}/resume`, {})
        setMsg(r.data.continuing_pass ? 'Resumed — continuing from the checkpoint.' : 'Resumed.')
      } else if (what === 'reauthenticate') {
        await client.post(`/api/application-sessions/${session.id}/reauthenticate`)
        setMsg('A fresh sign-in window was issued. Nothing from the old session is reused.')
      } else {
        const r = await client.post(`/api/application-sessions/${session.id}/pass`, {})
        let summary = passSummary(r.data?.pass)
        if (r.data?.window?.open) {
          summary += ' The browser window is still open — take over in it when it hands you a step.'
        }
        setMsg(summary)
      }
      await load()
    } catch (e: any) {
      setErr(apiError(e, 'That action was refused'))
    } finally {
      setBusy(null)
    }
  }

  const openHandoff = async (action: Action) => {
    if (action.session_id === null) return
    setBusy(action.id); setErr('')
    try {
      const r = await client.post(
        `/api/application-sessions/${action.session_id}/actions/${action.id}/handoff`, {})
      const opened = Boolean(r.data?.window_opened)
      setDescriptors(prev => ({ ...prev, [action.id]: r.data }))
      setMsg(opened
        ? 'A visible browser window just opened on the machine running the app — do this step there, '
          + 'then come back and click “I finished this step in the browser”. What you type there '
          + '(except passwords and codes) is saved for this and future sessions.'
        : `No window could be opened here (${r.data?.reason || r.data?.window_reason || 'headless host'}). `
          + 'Use the “open the posting” link below to continue in your own browser tab.')
    } catch (e: any) {
      setErr(apiError(e, 'Could not open a handoff window'))
    } finally {
      setBusy(null)
    }
  }

  /** Close an item. A handoff item records only *that* the human finished it. */
  const complete = async (action: Action) => {
    if (action.session_id === null) return
    const text = (answers[action.id] || '').trim()
    setBusy(action.id); setMsg(''); setErr('')
    try {
      await client.post(
        `/api/application-sessions/${action.session_id}/actions/${action.id}/complete`,
        text ? { answers: { [action.fields[0]?.name || 'answer']: text } } : { note: 'done in the browser' },
      )
      setAnswers(prev => ({ ...prev, [action.id]: '' }))
      setMsg('Step recorded — the run continues from the checkpoint, never from the top.')
      await load()
    } catch (e: any) {
      setErr(apiError(e, 'Could not close that item'))
    } finally {
      setBusy(null)
    }
  }

  const live = sessions.filter(isLive)
  const closed = sessions.filter(s => !isLive(s))

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold tracking-tight flex items-center gap-2">
        <Hand className="w-5 h-5" /> Assisted Apply
        <span className="text-xs mono font-normal text-zinc-500">you stay in control of every pause</span>
      </h1>

      <p className="text-xs mono text-zinc-500">
        Passwords, verification codes and bot checks are entered by you in the browser — this page never
        asks for them. Resuming re-checks the job, the posting URL, the employer and the application before
        it types anything, and fields that are already done are never filled twice.
      </p>

      {browserRuntime?.available && browserRuntime.mode === 'headless' && (
        <div className="card p-3 text-xs mono text-zinc-600 dark:text-zinc-400" data-testid="browser-mode">
          This host has no display, so a pass runs a <b>headless</b> browser you cannot watch
          {browserRuntime.mode_reason ? ` (${browserRuntime.mode_reason})` : ''}. It still records every step
          below; steps that need your eyes and hands (a sign-in, a code, a bot check) are handed to you.
        </div>
      )}
      {browserRuntime?.available && browserRuntime.mode === 'headed' && (
        <div className="card p-3 text-xs mono text-emerald-700 dark:text-emerald-300" data-testid="browser-mode">
          A pass opens a <b>visible browser window</b> on this machine — watch it work, and take over in that
          same window when it hands a step to you.
        </div>
      )}

      {browserRuntime && !browserRuntime.available && (
        <div className="card p-3 text-xs mono text-amber-700 dark:text-amber-300 flex items-center gap-2" role="alert">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          <span><b>Assisted browser unavailable.</b> {browserRuntime.reason || 'The browser runtime is not ready.'} You can still review the prepared application, but a browser pass cannot start until this is resolved.</span>
        </div>
      )}

      {msg && <div className="card p-3 text-xs mono text-emerald-700 dark:text-emerald-300">{msg}</div>}
      {err && <div className="card p-3 text-xs mono text-red-700 dark:text-red-300 flex items-center gap-2">
        <AlertTriangle className="w-4 h-4" /> {err}
      </div>}

      {/* Start a session */}
      <div className="card p-4 space-y-2">
        <div className="text-sm font-medium flex items-center gap-2"><Play className="w-4 h-4" /> Start an assisted session</div>
        <div className="flex flex-wrap items-center gap-2">
          <select className="input" value={jobId} onChange={e => setJobId(e.target.value)} aria-label="Job">
            <option value="">Choose a job…</option>
            {jobId && !jobs.some(j => String(j.id) === jobId) && <option value={jobId}>#{jobId} prepared application</option>}
            {jobs.map(j => <option key={j.id} value={j.id}>#{j.id} {j.title} — {j.company}</option>)}
          </select>
          <button className="btn" onClick={startSession} disabled={!jobId || busy === 'start'}>
            {busy === 'start' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />} Start
          </button>
        </div>
      </div>

      {/* The actionable queue: one row per human step. */}
      <div className="card p-4 space-y-3" data-testid="action-queue">
        <div className="text-sm font-medium flex items-center gap-2">
          <ShieldCheck className="w-4 h-4" /> Waiting on you
          <span className="text-xs mono font-normal text-zinc-500">{actions.length} item(s)</span>
        </div>
        {actions.length === 0 && <div className="text-xs mono text-zinc-500">Nothing needs you right now.</div>}
        {actions.map(a => {
          const handoff = actsInBrowser(a.kind)
          const descriptor = descriptors[a.id] || a.handoff
          return (
            <div key={a.id} className="border rounded-xl p-3 space-y-2" data-testid={`action-${a.kind}`}>
              <div className="flex items-start justify-between gap-2">
                <div className="text-sm font-medium flex items-center gap-2">
                  {a.kind === 'login' && <LogIn className="w-4 h-4" />}
                  {a.kind === 'mfa' && <ShieldCheck className="w-4 h-4" />}
                  {a.kind === 'captcha' && <Hand className="w-4 h-4" />}
                  {!actsInBrowser(a.kind) && <AlertTriangle className="w-4 h-4" />}
                  {KIND_LABEL[a.kind] || a.title || a.kind}
                  {a.occurrences > 1 && <span className="text-[11px] mono text-zinc-500">seen {a.occurrences}×</span>}
                </div>
                <span className="text-[11px] mono text-zinc-500">session #{a.session_id} • job #{a.job_id}</span>
              </div>
              {a.instructions && <div className="text-xs mono text-zinc-600 dark:text-zinc-400">{a.instructions}</div>}

              {a.fields?.length > 0 && (
                <ul className="text-[11px] mono text-zinc-500 list-disc pl-4">
                  {a.fields.slice(0, 6).map((f, i) => <li key={i}>{f.label || f.name}{f.required ? ' (required)' : ''}</li>)}
                </ul>
              )}

              {handoff && (
                <div className="space-y-2">
                  <button className="btn" onClick={() => openHandoff(a)} disabled={busy === a.id}>
                    {busy === a.id ? <Loader2 className="w-4 h-4 animate-spin" /> : <ExternalLink className="w-4 h-4" />}
                    {a.kind === 'session_expired' ? 'Issue a fresh sign-in'
                    : a.kind === 'review_required' ? 'Open a window on the posting'
                    : 'Start a handoff window'}
                  </button>
                  {descriptor && (
                    <div className="text-[11px] mono text-zinc-500 space-y-1">
                      <div className="flex items-center gap-1"><Clock className="w-3 h-3" /> one-time window, expires {descriptor.expires_at || '—'}</div>
                      {descriptor.window_opened ? (
                        <div className="text-emerald-700 dark:text-emerald-300" data-testid="handoff-window-open">
                          ✓ a visible browser window is open on this machine — the step is yours to do
                          there; this page never sees the password, the code or the check.
                        </div>
                      ) : (
                        <div>
                          no browser window could be shown here{descriptor.reason ? ` (${descriptor.reason})` : ''} —
                          the browser window is yours for this step — a password, a code or a bot check is typed
                          there, never here.
                        </div>
                      )}
                      {descriptor.url && (
                        <>
                          {' '}
                          <a className="underline" href={descriptor.url} target="_blank" rel="noreferrer">
                            open the posting
                          </a>
                        </>
                      )}
                      {(descriptor.never || []).slice(0, 4).map((line: string, i: number) => (
                        <div key={i} className="flex items-center gap-1"><XCircle className="w-3 h-3" /> {line}</div>
                      ))}
                    </div>
                  )}
                  <button className="btn" onClick={() => complete(a)} disabled={busy === a.id}>
                    <CheckCircle className="w-4 h-4" /> {a.kind === 'review_required'
                      ? 'I finished the application in the browser'
                      : 'I finished this step in the browser'}
                  </button>
                </div>
              )}

              {!handoff && (a.fields?.length ?? 0) > 0 && (
                <div className="flex flex-wrap items-center gap-2">
                  <input
                    className="input flex-1 min-w-[16rem]"
                    placeholder="Your answer for this field (never a password or code)"
                    value={answers[a.id] || ''}
                    onChange={e => setAnswers(prev => ({ ...prev, [a.id]: e.target.value }))}
                    aria-label={`Answer for action ${a.id}`}
                  />
                  <button className="btn" onClick={() => complete(a)} disabled={busy === a.id || !(answers[a.id] || '').trim()}>
                    {busy === a.id ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle className="w-4 h-4" />} Save answer
                  </button>
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* Sessions, live first. */}
      <div className="card p-4 space-y-3">
        <div className="text-sm font-medium flex items-center gap-2">
          <RefreshCw className="w-4 h-4" /> Sessions
          <span className="text-xs mono font-normal text-zinc-500">{live.length} live • {closed.length} closed</span>
        </div>
        {sessions.length === 0 && <div className="text-xs mono text-zinc-500">No sessions yet.</div>}
        {[...live, ...closed].map(s => (
          <div key={s.id} className="border rounded-xl p-3 space-y-2" data-testid={`session-${s.id}`}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-sm">
                <span className="font-medium">#{s.id} {s.job.title}</span>
                <span className="text-zinc-500"> — {s.job.company}</span>
              </div>
              <div className={`text-xs mono ${STATE_TONE[s.state] || 'text-zinc-500'}`}>
                {s.state}{s.pause?.kind ? ` • ${KIND_LABEL[s.pause.kind] || s.pause.kind}` : ''}
              </div>
            </div>
            <div className="text-[11px] mono text-zinc-500">
              {s.progress?.fields_total ?? 0} field(s) seen • {s.progress?.filled ?? 0} filled •{' '}
              {(s.progress?.asking ?? 0)} asking • {(s.progress?.handoffs ?? 0)} handoff
              {s.checkpoint?.completed_fields?.length
                ? ` • already done: ${s.checkpoint.completed_fields.slice(0, 6).join(', ')}`
                : ''}
              {s.expires_at ? ` • expires ${new Date(s.expires_at).toLocaleTimeString()}` : ''}
            </div>
            {s.state_reason && <div className="text-[11px] mono text-zinc-500">why: {s.state_reason}</div>}
            {s.window?.open && (
              <div className="text-[11px] mono text-emerald-700 dark:text-emerald-300" data-testid={`window-${s.id}`}>
                ● the browser window for this session is open — do sign-in, bot-check or final-submit
                steps in it; what you type there (never a password or code) is recorded for replay
              </div>
            )}
            {s.plan && (
              <div className="text-[11px] mono text-zinc-500">
                plan: {s.plan.autofillable.length} fillable
                {s.plan.asking.length ? ` • ${s.plan.asking.length} to ask` : ''}
                {s.plan.handoffs.length ? ` • ${s.plan.handoffs.length} handoff` : ''}
                {s.plan.must_pause ? ' • will pause' : ''}
              </div>
            )}
            {/* What the run actually did, in order — the recording the user keeps. */}
            {(s.flow?.journal?.length ?? 0) > 0 && (
              <details className="text-[11px] mono text-zinc-600 dark:text-zinc-400" data-testid={`flow-${s.id}`}>
                <summary className="cursor-pointer">
                  What this session did — {s.flow!.journal.length} recorded step(s)
                  {s.flow?.progress?.browser_mode
                    ? ` • ${s.flow.progress.browser_mode} browser`
                    : ''}
                  {s.flow?.progress?.pages ? ` • ${s.flow.progress.pages} page(s)` : ''}
                </summary>
                <ol className="mt-1 space-y-0.5 list-decimal pl-5">
                  {s.flow!.journal.slice(-25).map((entry, i) => (
                    <li key={i}>
                      <span className="text-zinc-500">{EVENT_LABEL[entry.event || ''] || entry.event}</span>{' '}
                      {flowLine(entry)}
                      {entry.host ? <span className="text-zinc-500"> — {entry.host}</span> : null}
                    </li>
                  ))}
                </ol>
              </details>
            )}
            <div className="flex flex-wrap gap-2">
              {isLive(s) && s.state !== 'awaiting_user' && (
                <button className="btn" onClick={() => act(s, 'pass')} disabled={busy === s.id || browserRuntime?.available === false}>
                  <Play className="w-4 h-4" /> Run a pass
                </button>
              )}
              {s.state === 'active' && (
                <button className="btn" onClick={() => act(s, 'pause')} disabled={busy === s.id}>
                  <Pause className="w-4 h-4" /> Pause
                </button>
              )}
              {(s.state === 'awaiting_user' || s.state === 'paused') && (
                <button className="btn" onClick={() => act(s, 'resume')} disabled={busy === s.id}>
                  <Play className="w-4 h-4" /> Resume from the checkpoint
                </button>
              )}
              {s.state === 'expired' && (
                <button className="btn" onClick={() => act(s, 'reauthenticate')} disabled={busy === s.id}>
                  <LogIn className="w-4 h-4" /> Re-authenticate safely
                </button>
              )}
              {isLive(s) && (
                <button className="btn" onClick={() => act(s, 'cancel')} disabled={busy === s.id}>
                  <XCircle className="w-4 h-4" /> Cancel
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
