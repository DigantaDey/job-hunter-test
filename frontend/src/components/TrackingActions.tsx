import { useState } from 'react'
import { AlertCircle, Check, Loader2, X } from 'lucide-react'
import { apiError } from '../api/client'
import {
  APPLICATION_TRACKING_STATES,
  INTERVIEW_FORMATS,
  INTERVIEW_FORMAT_LABELS,
  INTERVIEW_SELF_ASSESSMENTS,
  INTERVIEW_SELF_ASSESSMENT_LABELS,
  RESPONSE_CHANNELS,
  SUBMISSION_CHANNELS,
  TRACKING_MANUAL_SOURCES,
  TRACKING_STATE_LABELS,
} from '../lib/contracts'
import {
  addNote,
  completeFollowUp,
  completeInterview,
  confirmSuggestion,
  correctState,
  dismissSuggestion,
  recordInterview,
  recordOffer,
  recordResponse,
  recordRejection,
  recordWithdrawal,
  setFollowUp,
  toIsoUtc,
  updateAttribution,
  updateStatus,
  actionFor,
  type TrackingAction,
  type TrackingDocument,
  type TrackingWriteResponse,
} from '../lib/tracking'

/**
 * The buttons a tracking document offers, and the small form behind each one.
 *
 * Which buttons exist, which are disabled and why they are disabled all come
 * from the server (`document.actions`, contracts/13 §7 rule 2): this component
 * renders the plan, it does not decide it. What is local is only the form a
 * given key opens and the field names the endpoint declares.
 *
 * Every write answers with the same envelope, and `duplicate: true` is a normal
 * answer — a double-clicked "I got an interview" is told "already recorded"
 * rather than creating a second interview.
 */

type FormKey =
  | null | 'interview' | 'interview_completed' | 'response' | 'outcome'
  | 'note' | 'follow_up' | 'correct' | 'attribution'

const OUTCOME_BY_KEY: Record<string, {
  state: string
  verb: string
  title: string
  call: (id: number, body: { reason?: string; note?: string | null }) => Promise<TrackingWriteResponse>
}> = {
  offer: {
    state: 'offer_received', verb: 'Record the offer', title: 'An offer arrived',
    call: (id, body) => recordOffer(id, body),
  },
  rejected: {
    state: 'rejected_by_employer', verb: 'Record the rejection', title: 'The employer said no',
    call: (id, body) => recordRejection(id, body),
  },
  withdraw: {
    state: 'withdrawn', verb: 'Record the withdrawal', title: 'You pulled out',
    call: (id, body) => recordWithdrawal(id, body),
  },
}

const label = (value: string): string => TRACKING_STATE_LABELS[value] || value

export default function TrackingActions({ document, onWritten, dense = false }: {
  document: TrackingDocument
  /** Called with every write result — the caller re-reads or uses `result.document`. */
  onWritten: (result: TrackingWriteResponse) => void
  /** Tighter layout for the Jobs drawer. */
  dense?: boolean
}) {
  const id = document.tracking_id
  const [form, setForm] = useState<FormKey>(null)
  const [outcomeKey, setOutcomeKey] = useState<string>('rejected')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  // Form fields. One flat bag: only the open form reads any of them.
  const [interviewAt, setInterviewAt] = useState('')
  const [format, setFormat] = useState('video')
  const [roundName, setRoundName] = useState('')
  const [selfAssessment, setSelfAssessment] = useState('unknown')
  const [nextStep, setNextStep] = useState('')
  const [channel, setChannel] = useState('email')
  const [summary, setSummary] = useState('')
  const [reason, setReason] = useState('')
  const [note, setNote] = useState('')
  const [followUpAt, setFollowUpAt] = useState('')
  const [followUpNote, setFollowUpNote] = useState('')
  const [correctionState, setCorrectionState] = useState(document.state)
  const [correctionReason, setCorrectionReason] = useState('')
  const [source, setSource] = useState(document.attribution?.source || 'referral')
  const [submissionChannel, setSubmissionChannel] = useState(document.attribution?.channel || 'manual_user')

  const close = (): void => { setForm(null); setError(''); setMessage('') }

  async function run(call: () => Promise<TrackingWriteResponse>, success: string): Promise<void> {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      const result = await call()
      setMessage(result.duplicate ? `Already recorded — nothing was added. ${success}` : success)
      onWritten(result)
      setForm(null)
    } catch (caught) {
      setError(apiError(caught, 'Could not record that'))
    } finally {
      setBusy(false)
    }
  }

  const submit = (): void => {
    if (form === 'interview') {
      void run(() => recordInterview(id, {
        interview_at: toIsoUtc(interviewAt),
        format,
        round_name: roundName,
        note: note || null,
        follow_up_at: toIsoUtc(followUpAt),
        follow_up_note: followUpNote,
      }), 'Interview added to the timeline')
      return
    }
    if (form === 'interview_completed') {
      void run(() => completeInterview(id, {
        self_assessment: selfAssessment,
        next_step: nextStep,
        note: note || null,
        follow_up_at: toIsoUtc(followUpAt),
        follow_up_note: followUpNote,
      }), 'Interview marked as done')
      return
    }
    if (form === 'response') {
      void run(() => recordResponse(id, {
        channel, summary, note: note || null,
        follow_up_at: toIsoUtc(followUpAt), follow_up_note: followUpNote,
      }), 'Response recorded — not an interview')
      return
    }
    if (form === 'outcome') {
      const outcome = OUTCOME_BY_KEY[outcomeKey]
      void run(() => outcome.call(id, { reason, note: note || null }), outcome.title)
      return
    }
    if (form === 'note') {
      void run(() => addNote(id, note), 'Note added')
      return
    }
    if (form === 'follow_up') {
      const when = toIsoUtc(followUpAt)
      if (!when) { setError('Pick a date to be reminded on.'); return }
      void run(() => setFollowUp(id, { follow_up_at: when, note: followUpNote }), 'Reminder set')
      return
    }
    if (form === 'correct') {
      if (correctionReason.trim().length < 3) {
        setError('A correction needs a reason — it changes what the report says happened.')
        return
      }
      void run(() => correctState(id, {
        state: correctionState, reason: correctionReason.trim(), note: note || null,
      }), `Corrected to ${label(correctionState)} — the earlier rows are kept`)
      return
    }
    if (form === 'attribution') {
      void run(() => updateAttribution(id, { source, channel: submissionChannel, reason }),
        'Source attribution updated')
    }
  }

  const openForm = (key: FormKey, extra?: string): void => {
    setForm(key)
    setError('')
    setMessage('')
    if (extra) setOutcomeKey(extra)
  }

  /** What a button does: open a form, or write straight away. */
  const activate = (action: TrackingAction): void => {
    switch (action.key) {
      case 'applied':
        void run(() => updateStatus(id, { state: 'applied' }), 'Recorded as applied')
        return
      case 'interview': openForm('interview'); return
      case 'interview_completed': openForm('interview_completed'); return
      case 'response': openForm('response'); return
      case 'offer':
      case 'rejected':
      case 'withdraw': openForm('outcome', action.key); return
      case 'note': openForm('note'); return
      case 'follow_up': openForm('follow_up'); return
      case 'follow_up_done':
        void run(() => completeFollowUp(id, note || null), 'Follow-up marked done')
        return
      case 'correct':
        setCorrectionState(document.state)
        openForm('correct')
        return
      case 'attribution': openForm('attribution'); return
      case 'confirm_suggestion':
        void run(() => confirmSuggestion(id, {}), 'Suggestion confirmed as your own report')
        return
      case 'dismiss_suggestion':
        void run(() => dismissSuggestion(id, { reason: reason || 'Not what it looked like' }),
          'Suggestion dismissed — nothing was recorded')
        return
      default: return
    }
  }

  const buttons = document.actions.filter((action) => action.key !== 'open_job')
  const input = 'w-full text-sm border rounded-lg px-3 py-2 bg-white dark:bg-zinc-900 dark:border-zinc-700 min-h-[44px]'

  return (
    <div className="space-y-2" data-testid="tracking-actions">
      <div className={`flex flex-wrap gap-2 ${dense ? '' : ''}`}>
        {buttons.map((action) => (
          <button
            key={action.key}
            type="button"
            onClick={() => activate(action)}
            disabled={action.disabled || busy}
            title={action.disabled ? action.disabled_reason || 'Not available in this status' : action.label}
            data-testid={`action-${action.key}`}
            className={`min-h-[44px] px-3 py-2 rounded-full text-xs font-medium inline-flex items-center gap-1.5
              focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-45 disabled:cursor-not-allowed
              ${action.primary
                ? 'bg-blue-600 text-white'
                : 'border dark:border-zinc-700 text-zinc-700 dark:text-zinc-200 bg-white dark:bg-zinc-900'}`}
          >
            {action.label}
          </button>
        ))}
      </div>

      {form && (
        <div className="p-3 rounded-xl border dark:border-zinc-700 bg-zinc-50 dark:bg-zinc-800/60 space-y-2"
             data-testid={`form-${form}`}>
          <div className="flex items-center justify-between">
            <span className="text-xs mono font-medium uppercase tracking-wide text-zinc-500">
              {form === 'interview' && 'I got an interview'}
              {form === 'interview_completed' && 'The interview happened'}
              {form === 'response' && 'A recruiter responded'}
              {form === 'outcome' && OUTCOME_BY_KEY[outcomeKey].title}
              {form === 'note' && 'Add a note'}
              {form === 'follow_up' && 'Remind me to follow up'}
              {form === 'correct' && 'Correct the status'}
              {form === 'attribution' && 'Where the application really came from'}
            </span>
            <button type="button" onClick={close} aria-label="Close"
                    className="p-2 min-h-[32px] min-w-[32px] rounded hover:bg-zinc-200 dark:hover:bg-zinc-700">
              <X className="w-3.5 h-3.5" />
            </button>
          </div>

          {form === 'interview' && (
            <div className="grid sm:grid-cols-2 gap-2">
              <label className="text-xs block">
                <span className="mono text-zinc-500">When</span>
                <input type="datetime-local" value={interviewAt} onChange={(e) => setInterviewAt(e.target.value)}
                       className={input} data-testid="interview-at" />
              </label>
              <label className="text-xs block">
                <span className="mono text-zinc-500">Format</span>
                <select value={format} onChange={(e) => setFormat(e.target.value)} className={input}>
                  {INTERVIEW_FORMATS.map((value) => (
                    <option key={value} value={value}>{INTERVIEW_FORMAT_LABELS[value] || value}</option>
                  ))}
                </select>
              </label>
              <label className="text-xs block sm:col-span-2">
                <span className="mono text-zinc-500">Round / who (optional)</span>
                <input value={roundName} onChange={(e) => setRoundName(e.target.value)}
                       placeholder="Technical screen — Staff Engineer" className={input} />
              </label>
              <label className="text-xs block">
                <span className="mono text-zinc-500">Remind me on (optional)</span>
                <input type="datetime-local" value={followUpAt} onChange={(e) => setFollowUpAt(e.target.value)}
                       className={input} data-testid="follow-up-at" />
              </label>
              <label className="text-xs block">
                <span className="mono text-zinc-500">Reminder note</span>
                <input value={followUpNote} onChange={(e) => setFollowUpNote(e.target.value)}
                       placeholder="Send the thank-you note" className={input} />
              </label>
              <label className="text-xs block sm:col-span-2">
                <span className="mono text-zinc-500">Note (optional)</span>
                <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} className={input}
                          placeholder="What they asked you to prepare" />
              </label>
            </div>
          )}

          {form === 'interview_completed' && (
            <div className="grid sm:grid-cols-2 gap-2">
              <label className="text-xs block">
                <span className="mono text-zinc-500">How it went (your read, not a prediction)</span>
                <select value={selfAssessment} onChange={(e) => setSelfAssessment(e.target.value)} className={input}>
                  {INTERVIEW_SELF_ASSESSMENTS.map((value) => (
                    <option key={value} value={value}>{INTERVIEW_SELF_ASSESSMENT_LABELS[value] || value}</option>
                  ))}
                </select>
              </label>
              <label className="text-xs block">
                <span className="mono text-zinc-500">Next step they mentioned</span>
                <input value={nextStep} onChange={(e) => setNextStep(e.target.value)}
                       placeholder="Panel next week" className={input} />
              </label>
              <label className="text-xs block sm:col-span-2">
                <span className="mono text-zinc-500">Note (optional)</span>
                <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} className={input} />
              </label>
            </div>
          )}

          {form === 'response' && (
            <div className="grid sm:grid-cols-2 gap-2">
              <label className="text-xs block">
                <span className="mono text-zinc-500">Where it came from</span>
                <select value={channel} onChange={(e) => setChannel(e.target.value)} className={input}>
                  {RESPONSE_CHANNELS.map((value) => (
                    <option key={value} value={value}>{value.replace('_', ' ')}</option>
                  ))}
                </select>
              </label>
              <label className="text-xs block">
                <span className="mono text-zinc-500">Remind me on (optional)</span>
                <input type="datetime-local" value={followUpAt} onChange={(e) => setFollowUpAt(e.target.value)}
                       className={input} />
              </label>
              <label className="text-xs block sm:col-span-2">
                <span className="mono text-zinc-500">What was said</span>
                <textarea value={summary} onChange={(e) => setSummary(e.target.value)} rows={2} className={input}
                          placeholder="Stored as your note. Nothing here is parsed, and a response is never promoted to an interview." />
              </label>
            </div>
          )}

          {form === 'outcome' && (
            <div className="grid sm:grid-cols-2 gap-2">
              <label className="text-xs block sm:col-span-2">
                <span className="mono text-zinc-500">Reason (optional)</span>
                <input value={reason} onChange={(e) => setReason(e.target.value)} className={input}
                       placeholder={outcomeKey === 'withdraw' ? 'Accepted another offer' : 'Went with an internal candidate'} />
              </label>
              <label className="text-xs block sm:col-span-2">
                <span className="mono text-zinc-500">Note (optional)</span>
                <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} className={input} />
              </label>
            </div>
          )}

          {form === 'note' && (
            <label className="text-xs block">
              <span className="mono text-zinc-500">Note</span>
              <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={3} className={input}
                        data-testid="note-input"
                        placeholder="Your own words about your own application — never a form field value." />
            </label>
          )}

          {form === 'follow_up' && (
            <div className="grid sm:grid-cols-2 gap-2">
              <label className="text-xs block">
                <span className="mono text-zinc-500">Remind me on</span>
                <input type="datetime-local" value={followUpAt} onChange={(e) => setFollowUpAt(e.target.value)}
                       className={input} data-testid="follow-up-at" />
              </label>
              <label className="text-xs block">
                <span className="mono text-zinc-500">What for</span>
                <input value={followUpNote} onChange={(e) => setFollowUpNote(e.target.value)} className={input}
                       placeholder="Chase the recruiter" />
              </label>
            </div>
          )}

          {form === 'correct' && (
            <div className="space-y-2">
              <label className="text-xs block">
                <span className="mono text-zinc-500">The status it should have been</span>
                <select value={correctionState} onChange={(e) => setCorrectionState(e.target.value)}
                        className={input} data-testid="correction-state">
                  {APPLICATION_TRACKING_STATES.map((value) => (
                    <option key={value} value={value}>{label(value)}</option>
                  ))}
                </select>
              </label>
              <label className="text-xs block">
                <span className="mono text-zinc-500">Why (required — the row being corrected is kept)</span>
                <textarea value={correctionReason} onChange={(e) => setCorrectionReason(e.target.value)} rows={2}
                          className={input} data-testid="correction-reason"
                          placeholder="That was a screening call, not an interview" />
              </label>
            </div>
          )}

          {form === 'attribution' && (
            <div className="grid sm:grid-cols-2 gap-2">
              <label className="text-xs block">
                <span className="mono text-zinc-500">Source</span>
                <select value={source} onChange={(e) => setSource(e.target.value)} className={input}>
                  {document.attribution?.source && !TRACKING_MANUAL_SOURCES.includes(document.attribution.source as never) && (
                    <option value={document.attribution.source}>{document.attribution.source} (current)</option>
                  )}
                  {TRACKING_MANUAL_SOURCES.map((value) => (
                    <option key={value} value={value}>{value.replace(/_/g, ' ')}</option>
                  ))}
                </select>
              </label>
              <label className="text-xs block">
                <span className="mono text-zinc-500">How it was submitted</span>
                <select value={submissionChannel} onChange={(e) => setSubmissionChannel(e.target.value)} className={input}>
                  {SUBMISSION_CHANNELS.map((value) => (
                    <option key={value} value={value}>{value.replace(/_/g, ' ')}</option>
                  ))}
                </select>
              </label>
              <label className="text-xs block sm:col-span-2">
                <span className="mono text-zinc-500">Why (optional)</span>
                <input value={reason} onChange={(e) => setReason(e.target.value)} className={input}
                       placeholder="A former colleague referred me; the board id was wrong" />
              </label>
            </div>
          )}

          <div className="flex gap-2">
            <button type="button" onClick={submit} disabled={busy} data-testid="form-submit"
                    className="min-h-[44px] px-4 py-2 rounded-full bg-blue-600 text-white text-xs font-medium inline-flex items-center gap-1.5 disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-blue-500">
              {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
              {busy ? 'Recording…' : 'Record it'}
            </button>
            <button type="button" onClick={close} disabled={busy}
                    className="min-h-[44px] px-4 py-2 rounded-full border dark:border-zinc-700 text-xs">
              Cancel
            </button>
          </div>
          {form === 'correct' && (
            <p className="text-[11px] mono text-zinc-500">
              A correction can reach any status, including one the state machine forbids — that is the point.
              The earlier rows stay exactly as they were, and the report counts the correction.
            </p>
          )}
          {form === 'response' && (
            <p className="text-[11px] mono text-zinc-500">
              Recorded as a response. An interview is only ever recorded by you pressing “I got an interview”.
            </p>
          )}
        </div>
      )}

      {error && (
        <div className="text-xs mono p-2 rounded-lg bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 text-red-700 dark:text-red-300 flex gap-2"
             data-testid="tracking-error" role="alert">
          <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />{error}
        </div>
      )}
      {message && !error && (
        <div className="text-xs mono p-2 rounded-lg bg-emerald-50 dark:bg-emerald-950 border border-emerald-200 dark:border-emerald-800 text-emerald-800 dark:text-emerald-200 flex gap-2 items-start"
             data-testid="tracking-message" role="status">
          <Check className="w-3.5 h-3.5 shrink-0 mt-0.5" />{message}
        </div>
      )}
      {actionFor(document, 'interview')?.disabled && document.transitions.terminal && (
        <p className="text-[11px] mono text-zinc-500">
          This application is closed. Nothing is deleted — use “Correct status” if that is wrong.
        </p>
      )}
    </div>
  )
}
