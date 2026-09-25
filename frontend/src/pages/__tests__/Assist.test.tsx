/**
 * Assisted Apply — the human-in-the-loop surface.
 *
 * The backend stops a browser run at the first moment a person is required and
 * raises a queue item. This page is where the person is, so the test covers the
 * two things that must never drift:
 *
 *   • a handoff item (sign-in / MFA / bot check) is closed with a *note* — the
 *     completion call carries no value, because the password or code was typed in
 *     the browser and the API answers `422 restricted_field` for anything that
 *     looks like one;
 *   • an answerable item (an unmapped or sensitive field) is closed with the
 *     user's answer for that field name, so the resumed pass can type it without
 *     asking twice.
 *
 * Plus the reader's contract: the queue and the session list come from the two
 * endpoints the page polls, and a resume posts to the checkpoint-validating
 * `/resume` route rather than silently continuing.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import client from '../../api/client'
import Assist from '../Assist'

vi.mock('../../api/client', () => {
  const get = vi.fn()
  const post = vi.fn()
  return {
    default: { get, post },
    apiError: (_e: unknown, fb?: string) => fb ?? 'error',
  }
})
const get = client.get as unknown as Mock
const post = client.post as unknown as Mock

const captchaAction = {
  id: 11, session_id: 5, job_id: 3, kind: 'captcha', status: 'pending',
  title: 'Complete the bot check', instructions: 'Finish the check in the browser window.',
  reason: 'captcha_detected', fields: [{ name: 'g-recaptcha-response', label: 'Verify you are human' }],
  handoff: { url: 'https://jobs.lever.co/acme/1' }, occurrences: 1, created_at: '2026-09-19T10:00:00',
  job: { title: 'Backend Engineer', company: 'Acme', url: 'https://jobs.lever.co/acme/1' },
}
const unknownAction = {
  id: 12, session_id: 5, job_id: 3, kind: 'unknown_field', status: 'pending',
  title: 'A field we do not recognise', instructions: 'Tell us what to put here.',
  reason: 'unclassified_field', fields: [{ name: 'q_17', label: 'Favourite colour?' }],
  handoff: {}, occurrences: 2, created_at: '2026-09-19T10:00:00',
  job: { title: 'Backend Engineer', company: 'Acme', url: 'https://jobs.lever.co/acme/1' },
}
const session = {
  id: 5, job_id: 3, job: { title: 'Backend Engineer', company: 'Acme', url: 'https://jobs.lever.co/acme/1' },
  state: 'awaiting_user', phase: 'waiting', state_reason: 'captcha_detected',
  pause: { kind: 'captcha', reason: 'captcha_detected' },
  progress: { fields_total: 3, filled: 2, asking: 0, handoffs: 1 },
  checkpoint: { fields: {}, completed_fields: ['first_name', 'email'], answer_fields: [] },
  plan: null, expires_at: '2099-01-01T00:00:00',
}

const activeSession = { ...session, state: 'active', phase: 'running', pause: { kind: '', reason: '' } }

/** The page's three reads, with whatever a test needs to differ. */
function mockGet(overrides: { sessions?: any[]; actions?: any[]; jobs?: any[] } = {}) {
  get.mockImplementation((url: string) => {
    if (url === '/api/application-sessions') {
      return Promise.resolve({ data: { sessions: overrides.sessions ?? [session] } })
    }
    if (url === '/api/application-sessions/actions') {
      return Promise.resolve({ data: { actions: overrides.actions ?? [captchaAction, unknownAction] } })
    }
    if (url === '/api/jobs') {
      return Promise.resolve({ data: { jobs: overrides.jobs ?? [{ id: 3, title: 'Backend Engineer', company: 'Acme' }] } })
    }
    if (url === '/api/account/runtime') {
      return Promise.resolve({ data: { assisted_apply_runtime: { available: true, mode: 'headed' } } })
    }
    return Promise.resolve({ data: {} })
  })
}

const wrapper = ({ children }: { children: React.ReactNode }) => <MemoryRouter>{children}</MemoryRouter>

describe('Assisted Apply page', () => {
  let openSpy: any
  beforeEach(() => {
    get.mockReset()
    post.mockReset()
    // jsdom has no window.open; stub it so the synchronous client-tab open in
    // click handlers does not throw in tests.
    openSpy = vi.spyOn(window, 'open').mockImplementation(
      () => ({ closed: false, location: { replace: () => {} }, focus: () => {} } as any))
    get.mockImplementation((url: string) => {
      if (url === '/api/application-sessions') return Promise.resolve({ data: { sessions: [session] } })
      if (url === '/api/application-sessions/actions') return Promise.resolve({ data: { actions: [captchaAction, unknownAction] } })
      if (url === '/api/jobs') return Promise.resolve({ data: { jobs: [{ id: 3, title: 'Backend Engineer', company: 'Acme' }] } })
      if (url === '/api/account/runtime') {
        return Promise.resolve({ data: { assisted_apply_runtime: { available: true, mode: 'headed' } } })
      }
      return Promise.resolve({ data: {} })
    })
    post.mockImplementation(() => Promise.resolve({ data: {} }))
  })
  afterEach(() => {
    cleanup()
    openSpy?.mockRestore()
  })

  it('lists the queue and the session, with the already-done fields named', async () => {
    render(<Assist />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('action-captcha')).toBeTruthy())
    expect(screen.getByTestId('action-unknown_field')).toBeTruthy()
    expect(screen.getByText(/already done: first_name, email/)).toBeTruthy()
    // A handoff item is closed by *the user's* confirmation, and the page says so
    // instead of pretending it can type the check for them.
    expect(screen.getByText(/I finished this step in the browser/)).toBeTruthy()
  })

  it('preselects the prepared job from the Assisted Apply next-action link', async () => {
    const queryWrapper = ({ children }: { children: React.ReactNode }) => (
      <MemoryRouter initialEntries={['/assist?job=3']}>{children}</MemoryRouter>
    )
    render(<Assist />, { wrapper: queryWrapper })
    await waitFor(() => expect((screen.getByLabelText('Job') as HTMLSelectElement).value).toBe('3'))
  })

  it('closes a handoff item with a note — never with a value', async () => {
    render(<Assist />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('action-captcha')).toBeTruthy())
    post.mockImplementation((url: string) => {
      if (url.endsWith('/handoff')) {
        return Promise.resolve({
          data: {
            action_id: 11, session_id: 5, kind: 'captcha', token: 'ho_once',
            expires_at: '2099-01-01T00:10:00', user_completes_in_browser: true,
            window_opened: false, mode: 'headless', url: 'https://jobs.lever.co/acme/1',
            never: ['We never solve a CAPTCHA', 'We never relay a solution'],
          },
        })
      }
      return Promise.resolve({ data: {} })
    })

    fireEvent.click(screen.getByText(/Start a handoff window/))
    // Wait until the success message appears (POST resolved, busy cleared).
    await screen.findByText(/A new browser tab opened on the posting/)
    // There are two buttons in the captcha card (handoff + complete); pick the
    // complete ("I finished...") one specifically.
    const captchaCard = screen.getByTestId('action-captcha')
    const allBtns = captchaCard.querySelectorAll('button')
    const button = Array.from(allBtns).find(b => /I finished/.test(b.textContent || '')) as HTMLButtonElement
    expect(button).toBeTruthy()
    expect(button.disabled).toBe(false)
    fireEvent.click(button)
    // Wait for the complete POST; the click fires post in finally, after an await
    // so give the microtask queue a moment.
    await waitFor(() => {
      const called = post.mock.calls.some((c: any[]) => String(c[0]).endsWith('/actions/11/complete'))
      if (!called) throw new Error('complete not called yet; calls=' + JSON.stringify(post.mock.calls.map((c: any) => c[0])))
      return true
    }, { timeout: 3000 })
    const complete = post.mock.calls.find((c: any[]) => String(c[0]).endsWith('/actions/11/complete'))!
    const body = JSON.stringify(complete[1] ?? {})
    expect(body).not.toMatch(/ho_once/) // the handoff token is never echoed back
    expect(Object.keys(complete[1] ?? {})).toEqual(['note'])
  })

  it('sends the user\'s answer for a field they were asked about', async () => {
    render(<Assist />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('action-unknown_field')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Answer for action 12'), { target: { value: 'Blue' } })
    fireEvent.click(screen.getAllByText(/Save answer/)[0])
    await waitFor(() => expect(
      post.mock.calls.some((c: any[]) => String(c[0]).endsWith('/actions/12/complete')),
    ).toBe(true))
    const call = post.mock.calls.find((c: any[]) => String(c[0]).endsWith('/actions/12/complete'))!
    expect(call[1]).toEqual({ answers: { q_17: 'Blue' } })
  })

  it('reports what a pass actually did, straight from the run', async () => {
    mockGet({ sessions: [activeSession] })
    render(<Assist />, { wrapper })
    await waitFor(() => expect(screen.getByText(/Run a pass/)).toBeTruthy())
    post.mockImplementation((url: string) => {
      if (url.endsWith('/pass')) {
        return Promise.resolve({
          data: {
            pass: {
              pages: 3, filled: ['full_name', 'email'], advanced: ['Apply for this job'],
              confirmed: '', next_step: { reason: 'waiting_for_you_to_submit' },
            },
          },
        })
      }
      return Promise.resolve({ data: {} })
    })

    fireEvent.click(screen.getByText(/Run a pass/))
    await waitFor(() => expect(screen.getByText(/Looked at 3 page\(s\)/)).toBeTruthy())
    // The old message ("Pass finished") was true of nothing the user cared
    // about; the summary names the fields, the movement and the reason to stop.
    expect(screen.getByText(/typed 2 field\(s\)/)).toBeTruthy()
    expect(screen.getByText(/moved on with “Apply for this job”/)).toBeTruthy()
    expect(screen.getByText(/submitting is yours to do/)).toBeTruthy()
  })

  it('says the portal confirmed the application only when it did', async () => {
    mockGet({ sessions: [activeSession] })
    render(<Assist />, { wrapper })
    await waitFor(() => expect(screen.getByText(/Run a pass/)).toBeTruthy())
    post.mockImplementation((url: string) => {
      if (url.endsWith('/pass')) {
        return Promise.resolve({ data: { pass: { pages: 4, filled: ['email'], confirmed: 'submitted',
                                                 advanced: [] } } })
      }
      return Promise.resolve({ data: {} })
    })
    fireEvent.click(screen.getByText(/Run a pass/))
    await waitFor(() => expect(screen.getByText(/the portal confirmed the application/)).toBeTruthy())
  })

  it('hands a review_required item to the browser instead of asking for a value', async () => {
    const reviewAction = {
      ...captchaAction, id: 13, kind: 'review_required',
      title: 'Review the prepared application', fields: [],
      instructions: 'The page is filled — the final Submit is yours to press.',
      reason: 'waiting_for_you_to_submit',
    }
    mockGet({ actions: [reviewAction] })
    render(<Assist />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('action-review_required')).toBeTruthy())
    expect(screen.getByText(/I finished the application in the browser/)).toBeTruthy()
    expect(screen.queryByLabelText('Answer for action 13')).toBeNull()
  })

  it('shows the recorded flow so the run can be reviewed afterwards', async () => {
    const recorded = {
      ...session,
      state: 'active',
      flow: {
        journal: [
          { event: 'page', host: 'jobs.lever.co', title: 'Backend Engineer', fields_total: 0 },
          { event: 'advance', control: 'Apply for this job', kind: 'start_application', reason: 'moved' },
          { event: 'fill', fields: ['full_name', 'email'] },
          { event: 'pause', kind: 'captcha', reason: 'captcha_detected' },
        ],
        progress: { pages: 2, browser_mode: 'headless' },
      },
    }
    mockGet({ sessions: [recorded], actions: [] })
    render(<Assist />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('flow-5')).toBeTruthy())
    fireEvent.click(screen.getByText(/What this session did/))
    await waitFor(() => expect(screen.getByText(/full_name, email/)).toBeTruthy())
    expect(screen.getByText(/Apply for this job/)).toBeTruthy()
    expect(screen.getByText(/a bot check needs a human/)).toBeTruthy()
    // …and an invisible run says so, rather than pretending the user watched it.
    expect(screen.getByText(/headless/)).toBeTruthy()
  })

  it('resumes through the checkpoint-validating route', async () => {
    render(<Assist />, { wrapper })
    await waitFor(() => expect(screen.getByText(/Resume from the checkpoint/)).toBeTruthy())
    post.mockImplementation(() => Promise.resolve({ data: { continuing_pass: { queued: true, item_id: 9 } } }))
    fireEvent.click(screen.getByText(/Resume from the checkpoint/))
    await waitFor(() => expect(
      post.mock.calls.some((c: any[]) => c[0] === '/api/application-sessions/5/resume'),
    ).toBe(true))
    await waitFor(() => expect(screen.getByText(/continuing from the checkpoint/)).toBeTruthy())
  })

  it('opens a real window for a handoff and says so — never a silent no-op', async () => {
    render(<Assist />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('action-captcha')).toBeTruthy())
    post.mockImplementation((url: string) => {
      if (url.endsWith('/handoff')) {
        return Promise.resolve({
          data: {
            action_id: 11, session_id: 5, kind: 'captcha', token: 'ho_once',
            expires_at: '2099-01-01T00:10:00', user_completes_in_browser: true,
            window_opened: true, mode: 'headed',
            never: ['We never solve a CAPTCHA'],
          },
        })
      }
      return Promise.resolve({ data: {} })
    })

    fireEvent.click(screen.getByText(/Start a handoff window/))
    await waitFor(() => expect(screen.getByTestId('handoff-window-open')).toBeTruthy())
    // The message names what happened: a visible server window AND a fallback
    // tab, with the honest rule about what is (and is not) saved.
    await waitFor(() => expect(
      screen.getByText(/A visible browser window just opened/)).toBeTruthy())
    expect(screen.getByText(/A tab also opened in your browser as a fallback/)).toBeTruthy()
    expect(screen.getByText(/saved for this and future sessions/)).toBeTruthy()
  })

  it('says honestly when no window can be shown, and keeps the link', async () => {
    render(<Assist />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('action-captcha')).toBeTruthy())
    post.mockImplementation((url: string) => {
      if (url.endsWith('/handoff')) {
        return Promise.resolve({
          data: {
            action_id: 11, session_id: 5, kind: 'captcha', token: 'ho_once',
            expires_at: '2099-01-01T00:10:00', user_completes_in_browser: true,
            window_opened: false, mode: 'headless', reason: 'no display (DISPLAY unset)',
            url: 'https://jobs.lever.co/acme/1',
            never: ['We never solve a CAPTCHA'],
          },
        })
      }
      return Promise.resolve({ data: {} })
    })

    fireEvent.click(screen.getByText(/Start a handoff window/))
    // Even when the server cannot show a headed window, the click synchronously
    // opens the posting in the user's own tab — the fix for cloud/headless.
    await waitFor(() => expect(screen.getByText(/A new browser tab opened on the posting/)).toBeTruthy())
    expect(screen.getByText(/Passwords, codes and bot checks stay in the tab/)).toBeTruthy()
    expect(screen.queryByTestId('handoff-window-open')).toBeNull()
  })

  it('marks the session whose window is still open', async () => {
    mockGet({ sessions: [{ ...session, window: { open: true, idle_seconds: 3 } }] })
    render(<Assist />, { wrapper })
    await waitFor(() => expect(screen.getByTestId('window-5')).toBeTruthy())
    expect(screen.getByText(/do sign-in, bot-check or final-submit/)).toBeTruthy()
    expect(screen.getByText(/recorded for replay/)).toBeTruthy()
  })

  it('tells the user the window survived the pass', async () => {
    mockGet({ sessions: [activeSession] })
    render(<Assist />, { wrapper })
    await waitFor(() => expect(screen.getByText(/Run a pass/)).toBeTruthy())
    post.mockImplementation((url: string) => {
      if (url.endsWith('/pass')) {
        return Promise.resolve({
          data: {
            pass: { pages: 1, filled: ['email'], advanced: [],
                    confirmed: '', next_step: { reason: 'waiting_for_you_to_submit' } },
            window: { open: true },
          },
        })
      }
      return Promise.resolve({ data: {} })
    })
    fireEvent.click(screen.getByText(/Run a pass/))
    await waitFor(() => expect(screen.getByText(/window is still open/)).toBeTruthy())
  })
})
