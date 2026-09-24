/**
 * Settings → Assisted apply: the two explicit opt-ins for account creation.
 *
 * The contract pinned here:
 *   • both toggles render OFF by default (`browser.create_accounts`,
 *     `browser.ai_assist`) with copy that states what turning them on means —
 *     passwords typed from the vault, AI mapping of unfamiliar fields;
 *   • flipping one and saving PUTs it through the normal settings contract
 *     (`_meta.writable` drives the payload, exactly like every other card);
 *   • a server ceiling (`create_accounts_available === false`) greys the
 *     checkbox out instead of letting the UI pretend the setting would work.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import client from '../../api/client'
import Settings from '../Settings'

/* ── mocks ──────────────────────────────────────────────────────────── */
vi.mock('../../api/client', () => {
  const get = vi.fn()
  const post = vi.fn()
  const put = vi.fn()
  return {
    default: { get, post, put },
    apiError: (_e: unknown, fb?: string) => fb ?? 'error',
  }
})
const get = client.get as unknown as Mock
const put = client.put as unknown as Mock

vi.mock('../../components/AIBanner', () => ({ AIStatusBanner: () => null }))

vi.mock('../../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, email: 'owner@example.com', name: 'Owner', role: 'owner', is_owner: true } }),
}))

/* ── fixtures ───────────────────────────────────────────────────────── */
const settingsFixture = (browser: Record<string, unknown>) => ({
  ai: { base_url: '', model: '', api_key_set: false, api_key_masked: '', rpm: 60, timeout: 300,
        max_input_tokens: 0, max_output_tokens: 0, platform_max_input_tokens: 0,
        platform_max_output_tokens: 0, token_limits_editable: true, is_owner: true },
  scraping: { keywords: [], freshness_hours: 48, sources: [], live_enabled: false },
  funding: { context_notes: '', industries: '' },
  general: { strict_skeleton: false, auto_approve_email: false, default_resume_format: 'ai_generated' },
  email: { host: '', port: 587, username: '' },
  workflows: { parse: true },
  automation: { auto_mode: false, can_use: true, cadence: {}, locked_reason: null },
  browser,
  _meta: { writable: { browser: ['create_accounts', 'ai_assist'] } },
})

const ASSISTANT = { configured: true, state: 'online', online: true, reason: null, hint: null, paused_items: 0, message: 'ok' }

function wrapper({ children }: { children: React.ReactNode }) {
  return <MemoryRouter>{children}</MemoryRouter>
}

/** `load()` chains several awaits; drain the microtask queue until they settle. */
async function flush(rounds = 12) {
  for (let i = 0; i < rounds; i++) await act(async () => { await Promise.resolve() })
}

/** Render with a given `browser` settings bucket and wait for first paint. */
async function renderWith(browser: Record<string, unknown>) {
  get.mockImplementation((url: string) => {
    if (url === '/api/settings') return Promise.resolve({ data: settingsFixture(browser) })
    if (url === '/api/me/assistant') return Promise.resolve({ data: ASSISTANT })
    if (url === '/api/context/keywords') return Promise.resolve({ data: { keywords: [], source: 'ai' } })
    if (url === '/api/automation') return Promise.resolve({ data: { enabled: false, can_use: true, cadence: {}, toggles: {}, blocking: [], quota: { used: 0, limit: 0 } } })
    return Promise.resolve({ data: {} })
  })
  render(<Settings />, { wrapper })
  await flush()
}

/* ── suite ──────────────────────────────────────────────────────────── */
describe('Settings → Assisted apply opt-ins', () => {
  beforeEach(() => {
    get.mockReset()
    put.mockReset()
    put.mockResolvedValue({ data: { ok: true } })
  })
  afterEach(cleanup)

  it('renders both toggles, off by default, with honest copy', async () => {
    await renderWith({
      create_accounts: false, ai_assist: false,
      create_accounts_available: true, ai_assist_available: true,
    })
    const createBox = screen.getByRole('checkbox', { name: /Create portal accounts/i }) as HTMLInputElement
    const aiBox = screen.getByRole('checkbox', { name: /Let AI map unfamiliar form fields/i }) as HTMLInputElement
    expect(createBox.checked).toBe(false)
    expect(createBox.disabled).toBe(false)
    expect(aiBox.checked).toBe(false)
    expect(aiBox.disabled).toBe(false)
    // The copy must say where the login ends up (the vault export)…
    expect(screen.getByText(/lands in your Vault/i)).toBeTruthy()
    // …and that codes and bot checks are never covered by the opt-in.
    expect(screen.getByText(/MFA codes and bot\s+checks always wait for you/i)).toBeTruthy()
  })

  it('saves a flipped toggle through the normal settings contract', async () => {
    await renderWith({
      create_accounts: false, ai_assist: false,
      create_accounts_available: true, ai_assist_available: true,
    })
    fireEvent.click(screen.getByRole('checkbox', { name: /Create portal accounts/i }))
    fireEvent.click(screen.getByRole('button', { name: /Save changes/i }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    const [url, payload] = put.mock.calls[0]
    expect(url).toBe('/api/settings')
    expect(payload).toEqual({ browser: { create_accounts: true, ai_assist: false } })
  })

  it('greys the toggle out when the deployment ceiling is off', async () => {
    await renderWith({
      create_accounts: false, ai_assist: true,
      create_accounts_available: false, ai_assist_available: true,
    })
    const createBox = screen.getByRole('checkbox', { name: /Create portal accounts/i }) as HTMLInputElement
    const aiBox = screen.getByRole('checkbox', { name: /Let AI map unfamiliar form fields/i }) as HTMLInputElement
    expect(createBox.disabled).toBe(true)
    expect(aiBox.disabled).toBe(false) // the other one is still usable
    expect(screen.getByText(/disabled on this deployment/i)).toBeTruthy()
  })
})
