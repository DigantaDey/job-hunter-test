/**
 * The tracking client's pure half.
 *
 * These are the derivations the UI is allowed to do locally: reading a rate,
 * naming an origin, wording a timeline row, saying when a reminder is due. The
 * rule each one exists for is in the comment — and the two that matter most are
 * that `null` is never rendered as 0%, and that an origin badge never drops.
 */
import { describe, expect, it } from 'vitest'
import {
  artifactLabel,
  canRecordInterview,
  conversionText,
  dueLabel,
  eventHeadline,
  formatDay,
  formatWhen,
  groupLabel,
  isClosed,
  matchLabel,
  originBadge,
  rateText,
  relativeLabel,
  timelineSlice,
  toIsoUtc,
  toLocalInput,
  type TrackingDocument,
  type TrackingEvent,
  type TrackingTally,
} from '../tracking'

const NOW = new Date('2026-09-19T12:00:00Z')

function event(over: Partial<TrackingEvent> = {}): TrackingEvent {
  return {
    id: 1, event_id: 'e-1', sequence: 1, event_type: 'application.tracking_opened',
    state_from: null, state_from_label: null, state_to: 'applied', state_to_label: 'Applied',
    phase: 'applied', origin: 'user_reported', actor_type: 'user', actor_label: 'ada@example.com',
    trigger: 'user', severity: 'info', message: 'Tracking started', note: '', payload: {},
    evidence: [], is_correction: false, correction_of_event_id: null, correction_reason: '',
    is_provisional: false, follow_up_at: null, occurred_at: '2026-09-10T09:00:00Z',
    recorded_at: '2026-09-10T09:00:00Z', late_reported: false, ...over,
  }
}

function tally(over: Partial<TrackingTally> = {}): TrackingTally {
  return {
    tracked: 4, applied: 4, responses: 2, interviews: 2, offers: 1, rejections: 1, withdrawn: 0,
    provisional: 0, corrections: 1, application_to_interview_rate: 50.0,
    applied_to_response_rate: 50.0, interview_to_offer_rate: 50.0, avg_match_score: 81.0,
    avg_days_to_interview: 6.5, by_origin: { user_reported: 3, system_observed: 1 }, ...over,
  }
}

function document(over: Partial<TrackingDocument> = {}): TrackingDocument {
  return {
    tracking_id: 4, job_id: 9,
    job: { id: 9, title: 'Senior Backend Engineer', company: 'FinCo',
           url: 'https://jobs.lever.co/finco/1', status: 'applied', source: 'lever' },
    state: 'applied', state_label: 'Applied', phase: 'applied', phase_label: 'Applied',
    job_status: 'applied', state_origin: 'user_reported', is_provisional: false,
    provisional_evidence: {},
    attribution: { source: 'lever', channel: 'manual_user', role_family: 'Backend Engineer' },
    snapshots: {
      match: { score: 88, band: 'strong', score_source: 'ai', scorer_version: '1.4.0', match_id: 3 },
      artifact: { kind: 'packet', id: 12, version: 3, label: 'FinCo packet', sha256: 'h'.repeat(64) },
    },
    timestamps: {
      applied_at: '2026-09-10T09:00:00Z', first_response_at: null, interview_scheduled_at: null,
      interview_at: null, interview_completed_at: null, outcome_at: null, closed_at: null,
      created_at: '2026-09-10T08:00:00Z', updated_at: '2026-09-10T09:00:00Z',
    },
    durations_days: { applied_to_first_response: null, applied_to_interview: null,
                      applied_to_outcome: null },
    follow_up: { due_at: null, note: '', notified_at: null, completed_at: null, overdue: false,
                 pending: false },
    counts: { events: 1, corrections: 0, last_sequence: 1 },
    note: '',
    transitions: { allowed: ['recruiter_response', 'interview_scheduled', 'withdrawn'],
                   allowed_labels: ['Recruiter responded', 'Interview scheduled', 'Withdrawn'],
                   terminal: false },
    timeline: [event()],
    actions: [
      { key: 'interview', label: 'I got an interview', method: 'POST',
        route: '/api/application-tracking/4/interview', primary: true, disabled: false,
        disabled_reason: null },
      { key: 'rejected', label: 'Rejected', method: 'POST',
        route: '/api/application-tracking/4/rejection', primary: false, disabled: false,
        disabled_reason: null },
    ],
    server_time: '2026-09-19T12:00:00Z', exists: true, ...over,
  }
}

describe('rates: null is not zero', () => {
  it('renders a missing denominator as a dash, and a real zero as 0%', () => {
    expect(rateText(null)).toBe('—')
    expect(rateText(undefined)).toBe('—')
    expect(rateText(0)).toBe('0%')
    expect(rateText(66.7)).toBe('66.7%')
  })

  it('says so in words when nothing has been applied to', () => {
    expect(conversionText(tally({ applied: 0, interviews: 0, application_to_interview_rate: null })))
      .toBe('No applications recorded yet')
    expect(conversionText(tally())).toBe('2 of 4 applications reached an interview (50%)')
  })
})

describe('origin badges: the claim travels with the fact', () => {
  it('labels each origin differently, and says an inference is unconfirmed', () => {
    expect(originBadge('system_observed').label).toBe('System observed')
    expect(originBadge('user_reported').label).toBe('You reported')
    expect(originBadge('email_inferred').label).toMatch(/unconfirmed/i)
    expect(originBadge('email_inferred').title).toMatch(/never|confirm/i)
    expect(originBadge('verified_integration').label).toBe('Verified integration')
    // An origin this build has never seen still renders — as itself, not blank.
    expect(originBadge('carrier_pigeon').label).toBe('carrier_pigeon')
    expect(originBadge(null).label).toBe('You reported')
  })
})

describe('timeline wording', () => {
  it('reads a transition, a correction and a note apart', () => {
    expect(eventHeadline(event({ state_from: 'applied', state_from_label: 'Applied',
                                 state_to: 'interview_scheduled', state_to_label: 'Interview scheduled' })))
      .toBe('Applied → Interview scheduled')
    expect(eventHeadline(event({
      event_type: 'application.state_corrected', is_correction: true, state_from: 'interview_scheduled',
      state_from_label: 'Interview scheduled', state_to: 'recruiter_response',
      state_to_label: 'Recruiter responded',
    }))).toBe('Corrected Interview scheduled → Recruiter responded')
    // A correction that moved nothing says so rather than implying a change.
    expect(eventHeadline(event({ event_type: 'application.state_corrected', is_correction: true,
                                 state_to: null, state_to_label: null }))).toBe('Correction')
    expect(eventHeadline(event({ event_type: 'application.note_added', state_to: null,
                                 state_to_label: null }))).toBe('Note')
    expect(eventHeadline(event({ event_type: 'application.whatever_new', state_to: null,
                                 state_to_label: null }))).toBe('application.whatever_new')
  })

  it('slices the newest rows, oldest first', () => {
    const rows = [1, 2, 3, 4, 5, 6].map((sequence) => event({ sequence, id: sequence }))
    expect(timelineSlice({ ...document(), timeline: rows }, 3).map((row) => row.sequence)).toEqual([4, 5, 6])
    expect(timelineSlice({ ...document(), timeline: rows }, 0).length).toBe(6)
    expect(timelineSlice(null, 3)).toEqual([])
  })
})

describe('follow-up wording', () => {
  it('separates "not set", "due" and "overdue"', () => {
    expect(dueLabel(null)).toBe('No follow-up set')
    expect(dueLabel(document().follow_up)).toBe('No follow-up set')
    expect(dueLabel({ due_at: '2026-09-22T09:00:00Z', note: '', notified_at: null, completed_at: null,
                      overdue: false, pending: true }, NOW)).toBe('Due in 3 days')
    expect(dueLabel({ due_at: '2026-09-16T09:00:00Z', note: '', notified_at: null, completed_at: null,
                      overdue: true, pending: true }, NOW)).toBe('Overdue — was due 3 days ago')
    // Completed: nothing is promised any more.
    expect(dueLabel({ due_at: '2026-09-16T09:00:00Z', note: '', notified_at: null,
                      completed_at: '2026-09-16T10:00:00Z', overdue: false, pending: false }, NOW))
      .toBe('No follow-up set')
  })

  it('speaks in days, weeks and dates', () => {
    expect(relativeLabel('2026-09-19T09:00:00Z', NOW)).toBe('today')
    expect(relativeLabel('2026-09-20T09:00:00Z', NOW)).toBe('tomorrow')
    expect(relativeLabel('2026-09-18T09:00:00Z', NOW)).toBe('yesterday')
    expect(relativeLabel('2026-09-14T12:00:00Z', NOW)).toBe('5 days ago')
    expect(relativeLabel('2026-09-05T12:00:00Z', NOW)).toBe('2 weeks ago')
    expect(relativeLabel('2026-10-17T12:00:00Z', NOW)).toBe('in 4 weeks')
    expect(relativeLabel('2024-01-01T12:00:00Z', NOW)).toBe(formatDay('2024-01-01T12:00:00Z'))
    expect(relativeLabel(null, NOW)).toBe('—')
  })
})

describe('timestamps are UTC on the wire', () => {
  it('formats a naive timestamp as UTC rather than as browser-local', () => {
    expect(formatWhen('2026-09-10T09:05:00Z')).toBe('10 Sep 2026, 09:05')
    expect(formatWhen('2026-09-10T09:05:00')).toBe('10 Sep 2026, 09:05')
    expect(formatDay('2026-09-10T23:30:00Z')).toBe('Thu, 10 Sep 2026')
    expect(formatWhen(null)).toBe('—')
    expect(formatWhen('not-a-date')).toBe('—')
  })

  it('round-trips a datetime-local input through the API format', () => {
    const iso = toIsoUtc('2026-09-25T14:30')
    expect(iso).not.toBeNull()
    expect(iso).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/)
    expect(toLocalInput(iso)).toBe('2026-09-25T14:30')
    expect(toIsoUtc('')).toBeNull()
    expect(toLocalInput(null)).toBe('')
  })
})

describe('snapshot wording', () => {
  it('names the frozen score with its provenance', () => {
    expect(matchLabel(document())).toBe('88 (strong) — AI verified')
    expect(matchLabel(document({ snapshots: { ...document().snapshots,
      match: { score: 74, band: 'good', score_source: 'preliminary', scorer_version: '', match_id: null } } })))
      .toBe('74 (good) — keyword estimate')
    expect(matchLabel(document({ snapshots: { ...document().snapshots,
      match: { score: null, band: null, score_source: null, scorer_version: null, match_id: null } } })))
      .toBe('No match score recorded')
  })

  it('names the artefact the way the report groups it', () => {
    expect(artifactLabel(document())).toBe('Application packet v3 — FinCo packet')
    expect(artifactLabel(document({ snapshots: { ...document().snapshots,
      artifact: { kind: 'resume_document', id: 7, version: null, label: null, sha256: null } } })))
      .toBe('Resume document')
    expect(artifactLabel(document({ snapshots: { ...document().snapshots,
      artifact: { kind: 'none', id: null, version: null, label: null, sha256: null } } })))
      .toBe('No artefact snapshot')
  })

  it('labels report group keys, including the empty ones', () => {
    expect(groupLabel('interview_scheduled', 'state')).toBe('Interview scheduled')
    expect(groupLabel('none', 'artifact_version')).toBe('No artefact')
    expect(groupLabel('none', 'channel')).toBe('Not recorded')
    expect(groupLabel('unscored', 'score_bucket')).toBe('Not scored')
    expect(groupLabel('lever', 'source')).toBe('lever')
  })
})

describe('what may be pressed', () => {
  it('takes the answer from the server, not from a local rule', () => {
    expect(canRecordInterview(document())).toBe(true)
    const closed = document({
      state: 'rejected_by_employer', state_label: 'Rejected',
      transitions: { allowed: [], allowed_labels: [], terminal: true },
      actions: [{ key: 'interview', label: 'I got an interview', method: 'POST',
                  route: '/api/application-tracking/4/interview', primary: true, disabled: true,
                  disabled_reason: 'Not reachable from ‘Rejected’ — use Correct.' }],
    })
    expect(canRecordInterview(closed)).toBe(false)
    expect(isClosed(closed)).toBe(true)
    expect(isClosed(document())).toBe(false)
    expect(canRecordInterview(null)).toBe(false)
  })
})
