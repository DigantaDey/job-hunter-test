/**
 * The live layer's pure half (v2.2.8).
 *
 * Pages render *derived* state from a `pipeline_jobs` row; these tests pin the
 * derivations that the acceptance path depends on — Queued → Preparing →
 * Applied / Needs input, the fact-guard rejection, and the sessionStorage
 * re-attach after F5 (tracked receipts expire, keys replace, untrack forgets).
 */
import { beforeEach, describe, expect, it } from 'vitest'
import {
  TRACKED_KEY, TRACKED_TTL_MS, deriveApplyState, deriveDiscoveryState, deriveFundingState,
  deriveResumeState, deriveState, latestRowFor, readTracked, rowById, track, untrack,
} from '../aiWork'
import type { AIQueueSnapshot, QueueItem } from '../../hooks/useAIQueue'

function row(over: Partial<QueueItem>): QueueItem {
  return {
    id: 1, pipeline: 'application', task: null, job_id: 7, status: 'queued', priority: 5, attempts: 0,
    max_attempts: 3, paused_count: 0, error: '', created_at: 't', updated_at: 't', scheduled_at: 't',
    payload: { job_id: 7, mode: 'prepare' }, result: null, ...over,
  }
}
function snap(items: QueueItem[], recent: QueueItem[] = []): AIQueueSnapshot {
  return { counts: { queued: 0, processing: 0, paused: 0, needs_input: 0 }, items, recent, processing_now: null }
}

describe('tracked receipts (sessionStorage)', () => {
  beforeEach(() => sessionStorage.clear())

  it('remembers a receipt, replaces same key, forgets on untrack', () => {
    track({ id: 11, pipeline: 'application', key: 'apply:7' })
    track({ id: 12, pipeline: 'application', key: 'apply:7' })
    expect(readTracked().map((t) => t.id)).toEqual([12])
    untrack('apply:7')
    expect(readTracked()).toEqual([])
  })

  it('drops entries older than the TTL', () => {
    sessionStorage.setItem(TRACKED_KEY, JSON.stringify([{ id: 1, pipeline: 'ai', key: 'old', at: Date.now() - TRACKED_TTL_MS - 1 }]))
    expect(readTracked()).toEqual([])
  })
})

describe('finding rows', () => {
  it('finds by id in items and recent', () => {
    const s = snap([row({ id: 1 })], [row({ id: 2, status: 'done' })])
    expect(rowById(s, 2)?.id).toBe(2)
    expect(rowById(s, 3)).toBeNull()
  })
  it('prefers the live row for a job over a newer finished one', () => {
    const s = snap([row({ id: 5, status: 'processing' })], [row({ id: 9, status: 'done' })])
    expect(latestRowFor(s, 'application', 7)?.id).toBe(5)
    expect(latestRowFor(s, 'application', 99)).toBeNull()
  })
})

describe('apply derivation', () => {
  it('walks Queued → Preparing → preparation complete with an Assisted Apply next step', () => {
    expect(deriveApplyState(row({ status: 'queued' }))?.label).toBe('Queued')
    const prep = deriveApplyState(row({ status: 'processing' }))!
    expect(prep.phase).toBe('preparing'); expect(prep.live).toBe(true)
    const done = deriveApplyState(row({ status: 'done', job_id: 42, result: { status: 'ready_to_apply', resume_decision: 'master' } }))!
    expect(done.label).toBe('Preparation complete')
    expect(done.detail).toContain('No browser submission was run')
    expect(done.link).toBe('/assist?job=42')
    expect(done.linkLabel).toBe('Continue in Assisted Apply')
    expect(deriveApplyState(row({ status: 'processing', payload: { mode: 'execute' } }))?.phase).toBe('applying')
    expect(deriveApplyState(row({ status: 'done', result: { status: 'applied' } }))?.phase).toBe('applied')
  })
  it('needs_input links to Queues → User Input Needed and names the fields', () => {
    const st = deriveApplyState(row({ status: 'needs_input', result: { missing_fields: [{ label: 'Visa status' }] } }))!
    expect(st.needsInput).toBe(true)
    expect(st.detail).toContain('Visa status')
    expect(st.link).toBe('/queues')
  })
  it('paused says it will resume', () => {
    expect(deriveApplyState(row({ status: 'paused', paused_count: 2 }))?.detail).toMatch(/resume automatically/)
  })
})

describe('other pipelines', () => {
  it('resume: fact-guard rejection is a rejected outcome, not a failure', () => {
    const st = deriveResumeState(row({ pipeline: 'ai', task: 'generate_resume', status: 'done', result: { status: 'rejected', code: 'fact_guard_failed', issues: ['x'] } }))!
    expect(st.phase).toBe('rejected'); expect(st.tone).toBe('bad')
    expect(deriveResumeState(row({ pipeline: 'ai', status: 'done', result: { resume_id: 3, display_name: 'Acme' } }))?.phase).toBe('generated')
  })
  it('funding and discovery report their running/finished phases', () => {
    expect(deriveFundingState(row({ pipeline: 'funding', status: 'processing' }))?.live).toBe(true)
    expect(deriveDiscoveryState(row({ pipeline: 'discovery', status: 'queued' }))?.phase).toBe('queued')
  })
  it('deriveState dispatches on pipeline/task', () => {
    expect(deriveState(row({ pipeline: 'ai', task: 'generate_resume', status: 'processing' }))?.phase).toBe('generating')
    expect(deriveState(row({ pipeline: 'application', status: 'processing' }))?.phase).toBe('preparing')
    expect(deriveState(null)).toBeNull()
  })
})
