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
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  handoff: {}, occurrences: 1, created_at: '2026-09-19T10:00:00',
}
const unknownAction = {
  id: 12, session_id: 5, job_id: 3, kind: 'unknown_field', status: 'pending',
  title: 'A field we do not recognise', instructions: 'Tell us what to put here.',
  reason: 'unclassified_field', fields: [{ name: 'q_17', label: 'Favourite colour?' }],
  handoff: {}, occurrences: 2, created_at: '2026-09-19T10:00:00',
}
const session = {
  id: 5, job_id: 3, job: { title: 'Backend Engineer', company: 'Acme', url: 'https://jobs.lever.co/acme/1' },
  state: 'awaiting_user', phase: 'waiting', state_reason: 'captcha_detected',
  pause: { kind: 'captcha', reason: 'captcha_detected' },
  progress: { fields_total: 3, filled: 2, asking: 0, handoffs: 1 },
  checkpoint: { fields: {}, completed_fields: ['first_name', 'email'], answer_fields: [] },
  plan: null, expires_at: '2099-01-01T00:00:00',
}

const wrapper = ({ children }: { children: React.ReactNode }) => <MemoryRouter>{children}</MemoryRouter>

describe('Assisted Apply page', () => {
  beforeEach(() => {
    get.mockReset()
    post.mockReset()
    get.mockImplementation((url: string) => {
      if (url === '/api/application-sessions') return Promise.resolve({ data: { sessions: [session] } })
      if (url === '/api/application-sessions/actions') return Promise.resolve({ data: { actions: [captchaAction, unknownAction] } })
      if (url === '/api/jobs') return Promise.resolve({ data: { jobs: [{ id: 3, title: 'Backend Engineer', company: 'Acme' }] } })
      return Promise.resolve({ data: {} })
    })
    post.mockImplementation(() => Promise.resolve({ data: {} }))
  })
  afterEach(() => cleanup())

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
            never: ['We never solve a CAPTCHA', 'We never relay a solution'],
          },
        })
      }
      return Promise.resolve({ data: {} })
    })

    fireEvent.click(screen.getByText(/Start a handoff window/))
    await waitFor(() => expect(
      post.mock.calls.some((c: any[]) => String(c[0]).endsWith('/actions/11/handoff')),
    ).toBe(true))
    await waitFor(() => expect(screen.getByText(/We never solve a CAPTCHA/)).toBeTruthy())

    const buttons = screen.getAllByText(/I finished this step in the browser/)
    fireEvent.click(buttons[0])
    await waitFor(() => expect(
      post.mock.calls.some((c: any[]) => String(c[0]).endsWith('/actions/11/complete')),
    ).toBe(true))
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
})
