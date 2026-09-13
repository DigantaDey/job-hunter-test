/**
 * The empty-board banner (v2.2.4) — all five states, plus the not-known-yet one.
 *
 * The banner renders the backend's verdict (`GET /api/jobs/discovery/last-run`)
 * and derives nothing, so these tests drive it with the exact payloads the
 * endpoint serves. The fifth state matters as much as the other four: a
 * completed run with no `why_empty` on a board that is empty anyway must still
 * say something true, because the UI must never render a reason-less empty
 * state.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import {
  EmptyBoardBanner,
  emptyBoardState,
  formatRunDate,
  truncateError,
  type LastRun,
} from '../EmptyBoardBanner'

// Vitest runs without globals, so RTL cannot register its own auto-cleanup:
// without this, each render leaks into the next and queries find duplicates.
afterEach(cleanup)

function renderBanner(lastRun: LastRun | null | undefined, onDiscover = () => {}) {
  return render(
    <MemoryRouter>
      <EmptyBoardBanner lastRun={lastRun} onDiscover={onDiscover} />
    </MemoryRouter>
  )
}

const SUMMARY = {
  configured_sources: ['lever', 'greenhouse'],
  failed_sources: {},
  needs_credentials: [],
  demo_pool_enabled: false,
  freshness_window_hours: 48,
  fetched: 0,
  fresh: 0,
}

/** The run the endpoint serves when it found jobs: no `why_empty` key at all. */
const FOUND_JOBS: LastRun = {
  at: '2026-09-12T09:30:00',
  added: 3,
  summary: { ...SUMMARY, fetched: 3, fresh: 3 },
  ai_rescore: { enabled: false, skipped: 'no_profile', scored: 0 },
}

describe('emptyBoardState', () => {
  it('keeps "not known" apart from "no run at all"', () => {
    expect(emptyBoardState(undefined)).toBe('unknown')
    expect(emptyBoardState(null)).toBe('no_run')
  })

  it('passes the three reported reasons through', () => {
    for (const why of ['no_sources_configured', 'all_sources_failed', 'no_fresh_postings']) {
      expect(emptyBoardState({ added: 0, why_empty: why })).toBe(why)
    }
  })

  it('falls back rather than inventing a reason', () => {
    expect(emptyBoardState(FOUND_JOBS)).toBe('unexplained')
    // An unknown string from a newer backend is still not a reason to guess at.
    expect(emptyBoardState({ added: 0, why_empty: 'something_new' })).toBe('unexplained')
  })
})

describe('the five banner states', () => {
  it('1. no completed run → "No discovery run yet" + the discover CTA', () => {
    renderBanner(null)
    expect(screen.getByTestId('empty-board-banner').dataset.state).toBe('no_run')
    expect(screen.getByTestId('empty-board-headline').textContent).toBe('No discovery run yet')
    expect(screen.getByTestId('empty-board-cta').textContent).toContain('Discover jobs now')
    expect(screen.queryByTestId('failed-source-chips')).toBeNull()
    expect(screen.queryByTestId('last-run-accounting')).toBeNull()
    // Nothing to configure yet, so no Settings link and no retry wording.
    expect(screen.queryByText(/Settings → Sources/)).toBeNull()
  })

  it('2. no_sources_configured → the Settings source configuration + discover CTA', () => {
    renderBanner({
      at: '2026-09-12T09:30:00',
      added: 0,
      why_empty: 'no_sources_configured',
      summary: { ...SUMMARY, configured_sources: [] },
    })
    expect(screen.getByTestId('empty-board-banner').dataset.state).toBe('no_sources_configured')
    expect(screen.getByTestId('empty-board-headline').textContent).toBe('No job sources are enabled')
    const link = screen.getByRole('link', { name: /Settings → Sources/i })
    expect(link.getAttribute('href')).toBe('/settings#scraping')
    expect(screen.getByTestId('empty-board-cta').textContent).toContain('Discover jobs now')
    expect(screen.getByTestId('last-run-accounting').textContent).toContain('0 source(s) attempted')
  })

  it('3. all_sources_failed → one chip per source, the error truncated, retry CTA', () => {
    renderBanner({
      at: '2026-09-12T09:30:00',
      added: 0,
      why_empty: 'all_sources_failed',
      summary: {
        ...SUMMARY,
        failed_sources: {
          lever: 'HTTP 503 from boards-api.greenhouse.io after three attempts with backoff',
          greenhouse: 'timeout',
        },
      },
    })
    expect(screen.getByTestId('empty-board-banner').dataset.state).toBe('all_sources_failed')
    expect(screen.getByTestId('empty-board-headline').textContent).toBe('All sources failed on the last run')

    const chips = screen.getByTestId('failed-source-chips').querySelectorAll('span[data-testid="failed-source-id"]')
    expect(chips).toHaveLength(2)
    expect([...chips].map((chip) => chip.textContent)).toEqual(['lever', 'greenhouse'])

    const chipText = screen.getByTestId('failed-source-chips').textContent || ''
    expect(chipText).toContain('greenhouse — timeout')
    // The long one is truncated for the chip, and the full error survives as its
    // title — so truncation costs screen space, not information.
    expect(chipText).toContain('lever — HTTP 503 from boards-api.greenhouse.io')
    expect(chipText).toContain('…')
    expect(chipText).not.toContain('with backoff')
    const longChip = screen.getByTestId('failed-source-chips').querySelectorAll('span[title]')[0]
    expect(longChip.getAttribute('title'))
      .toBe('HTTP 503 from boards-api.greenhouse.io after three attempts with backoff')

    expect(screen.getByRole('link', { name: /Settings → Sources/i })).toBeTruthy()
    expect(screen.getByTestId('empty-board-cta').textContent).toContain('Retry discovery')
  })

  it('4. no_fresh_postings → the window in hours + retry CTA', () => {
    renderBanner({
      at: '2026-09-12T09:30:00',
      added: 0,
      why_empty: 'no_fresh_postings',
      summary: { ...SUMMARY, freshness_window_hours: 168, fetched: 0, fresh: 0 },
    })
    expect(screen.getByTestId('empty-board-banner').dataset.state).toBe('no_fresh_postings')
    expect(screen.getByTestId('empty-board-headline').textContent)
      .toBe('Sources are healthy, but nothing fresh in the window')
    expect(screen.getByTestId('freshness-window').textContent).toBe('168-hour')
    expect(screen.getByTestId('empty-board-cta').textContent).toContain('Retry discovery')
    expect(screen.queryByTestId('failed-source-chips')).toBeNull()
  })

  it('5. fallback → a run that found jobs, on a board that is empty anyway', () => {
    renderBanner(FOUND_JOBS)
    expect(screen.getByTestId('empty-board-banner').dataset.state).toBe('unexplained')
    expect(screen.getByTestId('empty-board-headline').textContent)
      .toBe('Last run found 3 jobs on 12 Sep 2026; your board is empty')
    expect(screen.getByTestId('empty-board-cta').textContent).toContain('Discover jobs now')
    expect(screen.getByTestId('last-run-accounting').textContent).toContain('3 added')
  })

  it('5b. the fallback pluralises honestly for a single job', () => {
    renderBanner({ ...FOUND_JOBS, added: 1 })
    expect(screen.getByTestId('empty-board-headline').textContent)
      .toBe('Last run found 1 job on 12 Sep 2026; your board is empty')
  })

  it('5c. a report from before v2.2.4 has no summary — the counts are not invented', () => {
    renderBanner({ at: '2026-09-12T09:30:00', added: 2 })
    expect(screen.getByTestId('empty-board-banner').dataset.state).toBe('unexplained')
    const accounting = screen.getByTestId('last-run-accounting').textContent || ''
    expect(accounting).toContain('Last run 12 Sep 2026')
    expect(accounting).toContain('2 added')
    expect(accounting).not.toContain('source(s) attempted')
    expect(accounting).not.toContain('demo seed data')
  })

  it('renders the plain empty copy until the verdict is known', () => {
    const { container } = renderBanner(undefined)
    expect(screen.queryByTestId('empty-board-banner')).toBeNull()
    expect(container.textContent).toContain('No jobs. Try Discover jobs')
  })
})

describe('the summary details the banner surfaces', () => {
  it('says when sources need credentials the operator never provided', () => {
    renderBanner({
      at: '2026-09-12T09:30:00',
      added: 0,
      why_empty: 'all_sources_failed',
      summary: { ...SUMMARY, failed_sources: { adzuna: 'set ADZUNA_APP_ID' }, needs_credentials: ['adzuna', 'jooble'] },
    })
    const note = screen.getByTestId('needs-credentials').textContent || ''
    expect(note).toContain('2 sources need credentials')
    expect(note).toContain('adzuna, jooble')
  })

  it('explains the demo pool being off instead of leaving it silent', () => {
    renderBanner({ at: '2026-09-12T09:30:00', added: 0, why_empty: 'no_fresh_postings', summary: SUMMARY })
    expect(screen.getByTestId('last-run-accounting').textContent)
      .toContain('demo seed data off, so nothing is fabricated to fill this board')

    renderBanner({
      at: '2026-09-12T09:30:00',
      added: 0,
      why_empty: 'no_fresh_postings',
      summary: { ...SUMMARY, demo_pool_enabled: true },
    })
    const accounts = screen.getAllByTestId('last-run-accounting')
    expect(accounts[accounts.length - 1].textContent).not.toContain('demo seed data off')
  })

  it('calls the CTA the page gave it', async () => {
    let clicked = 0
    renderBanner(null, () => { clicked += 1 })
    screen.getByTestId('empty-board-cta').click()
    expect(clicked).toBe(1)
  })
})

describe('formatting helpers', () => {
  it('truncates a source error to a chip-sized string', () => {
    expect(truncateError('timeout')).toBe('timeout')
    expect(truncateError('')).toBe('')
    const long = 'x'.repeat(120)
    expect(truncateError(long, 60)).toHaveLength(60)
    expect(truncateError(long, 60).endsWith('…')).toBe(true)
    expect(truncateError('  lots   of   space ')).toBe('lots of space')
  })

  it('formats the run date deterministically (UTC, no locale)', () => {
    expect(formatRunDate('2026-09-12T09:30:00')).toBe('12 Sep 2026')
    expect(formatRunDate('2026-01-01T23:59:59Z')).toBe('1 Jan 2026')
    expect(formatRunDate(null)).toBe('an unknown date')
    expect(formatRunDate('not-a-date')).toBe('not-a-date')  // unparsable → the raw prefix, never a crash
  })
})
