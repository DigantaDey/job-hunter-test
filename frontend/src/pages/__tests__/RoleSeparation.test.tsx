/**
 * Role separation — the client half of the owner/user split (v2.3).
 *
 * The product has two surfaces and the SPA must keep them apart:
 *
 *   • the end-user surface (dashboard, jobs, tracking…) for every account;
 *   • the owner/admin surface (/admin, /admin/audit, /admin/plans) mounted
 *     only for the workspace owner.
 *
 * Route protection is enforced by <RequireOwner> before any admin page mounts,
 * so a member who types (or is sent) an admin URL is redirected to their own
 * dashboard and no admin read is ever fired. This is UX layered on top of the
 * server's 403 `owner_required` — not a replacement for it (see
 * backend/tests/test_role_separation.py for the enforcement half).
 *
 * The suite also pins what each role *sees*: a member's Settings has no
 * provider controls, and the owner nav carries the admin links.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import client from '../../api/client'

const mockAuth = vi.hoisted(() => ({ user: null as any }))
vi.mock('../../context/AuthContext', () => ({
  useAuth: () => ({ user: mockAuth.user, loading: false }),
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))

import { RequireOwner } from '../../App'
import Layout from '../../components/Layout'
import Settings from '../Settings'
import AdminConsole from '../admin/AdminConsole'

vi.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({ theme: 'light', setTheme: () => {} }),
}))

vi.mock('../../api/client', () => {
  const get = vi.fn()
  const post = vi.fn()
  const put = vi.fn()
  return { default: { get, post, put }, apiError: (_e: unknown, fb?: string) => fb ?? 'error' }
})
const get = client.get as unknown as Mock

const OWNER = { id: 1, email: 'owner@example.com', name: 'Owner', role: 'owner', is_owner: true }
const MEMBER = { id: 2, email: 'member@example.com', name: 'Member', role: 'member', is_owner: false }

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="location">{location.pathname}</div>
}

/** Render <RequireOwner> inside a router, with a probe for where we ended up. */
function renderGuard() {
  return render(
    <MemoryRouter initialEntries={['/admin']}>
      <LocationProbe />
      <Routes>
        <Route path="/admin" element={
          <RequireOwner>
            <div data-testid="admin-page">ADMIN PAGE</div>
          </RequireOwner>
        } />
        <Route path="/" element={<div data-testid="user-home">USER HOME</div>} />
        <Route path="/login" element={<div data-testid="login" />} />
      </Routes>
    </MemoryRouter>
  )
}

/* ── suite ──────────────────────────────────────────────────────────── */
describe('Role separation: route protection', () => {
  beforeEach(() => {
    get.mockReset()
    get.mockImplementation(() => Promise.resolve({ data: {} }))
  })
  afterEach(cleanup)

  it('an ordinary user hitting /admin is redirected to their dashboard — the admin page never mounts', () => {
    mockAuth.user = MEMBER
    renderGuard()
    expect(screen.getByTestId('location').textContent).toBe('/')
    expect(screen.getByTestId('user-home')).toBeTruthy()
    expect(screen.queryByTestId('admin-page')).toBeNull()
  })

  it('the workspace owner mounts the admin page at /admin', () => {
    mockAuth.user = OWNER
    renderGuard()
    expect(screen.getByTestId('location').textContent).toBe('/admin')
    expect(screen.getByTestId('admin-page')).toBeTruthy()
  })

  it('an unauthenticated visitor is sent to /login, not into the admin surface', () => {
    mockAuth.user = null
    renderGuard()
    expect(screen.queryByTestId('admin-page')).toBeNull()
    expect(screen.queryByTestId('user-home')).toBeNull()
  })
})

describe('Role separation: navigation', () => {
  beforeEach(() => {
    get.mockReset()
    get.mockImplementation((url: string) => {
      if (url === '/api/me/assistant')
        return Promise.resolve({ data: { configured: true, state: 'online', online: true, paused_items: 0, message: 'ok' } })
      return Promise.resolve({ data: {} })
    })
  })
  afterEach(cleanup)

  it('the member nav has no admin links and no owner section', async () => {
    mockAuth.user = MEMBER
    render(<MemoryRouter><Layout /></MemoryRouter>)
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByTestId('admin-nav-link')).toBeNull()
    expect(screen.queryByText('Admin console')).toBeNull()
    expect(screen.queryByText('Audit log')).toBeNull()
    expect(screen.queryByText('Plans & users')).toBeNull()
  })

  it('the owner nav carries the admin section', async () => {
    mockAuth.user = OWNER
    render(<MemoryRouter><Layout /></MemoryRouter>)
    await act(async () => { await Promise.resolve() })
    const links = screen.getAllByTestId('admin-nav-link')
    const labels = links.map(l => l.textContent)
    expect(labels.some(t => t?.includes('Admin console'))).toBe(true)
    expect(labels.some(t => t?.includes('Audit log'))).toBe(true)
    expect(labels.some(t => t?.includes('Plans & users'))).toBe(true)
  })
})

describe('Role separation: Settings surface', () => {
  beforeEach(() => {
    get.mockReset()
    get.mockImplementation((url: string) => {
      if (url === '/api/settings') {
        return Promise.resolve({ data: mockAuth.user?.is_owner ? OWNER_SETTINGS : MEMBER_SETTINGS })
      }
      if (url === '/api/me/assistant') return Promise.resolve({ data: ASSISTANT })
      if (url === '/api/automation') return Promise.resolve({ data: { enabled: false } })
      return Promise.resolve({ data: {} })
    })
  })
  afterEach(cleanup)

  const MEMBER_SETTINGS = {
    ai: { configured: true },  // the sanitized payload — nothing else
    scraping: { keywords: [], freshness_hours: 48, sources: ['curated'], live_enabled: true },
    funding: { context_notes: '', industries: '' },
    general: { strict_skeleton: false, auto_approve_email: false, default_resume_format: 'ai_generated' },
    email: { host: '', port: 587, username: '' },
    workflows: { ai_for_discovery: true },
    automation: { auto_mode: false, can_use: false, locked_reason: 'Pro feature' },
    _meta: { writable: { scraping: ['keywords'] } },
  }
  const OWNER_SETTINGS = {
    ...MEMBER_SETTINGS,
    ai: { configured: true, base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini', api_key_set: true, rpm: 60 },
    _meta: { writable: { ...MEMBER_SETTINGS._meta.writable, ai: ['base_url', 'model'] } },
  }
  const ASSISTANT = { configured: true, state: 'online', online: true, paused_items: 0, message: 'Your assistant is ready to help.' }

  it('a member sees no AI provider configuration — no base URL, key, model or token budget fields', async () => {
    mockAuth.user = MEMBER
    render(<MemoryRouter><Settings /></MemoryRouter>)
    await act(async () => { for (let i = 0; i < 8; i++) await Promise.resolve() })

    const text = document.body.textContent || ''
    expect(screen.getByTestId('assistant-card')).toBeTruthy()
    expect(text).not.toContain('Base URL')
    expect(text).not.toContain('API key')
    expect(text).not.toContain('Max input tokens')
    expect(text).not.toContain('Max output tokens')
    expect(text).not.toContain('gpt-4o-mini')
    expect(text).not.toContain('api.openai.com')
    // and the member is told who runs the AI instead
    expect(text).toContain('workspace owner')
  })

  it('the owner is pointed at the admin console for provider configuration', async () => {
    mockAuth.user = OWNER
    render(<MemoryRouter><Settings /></MemoryRouter>)
    await act(async () => { for (let i = 0; i < 8; i++) await Promise.resolve() })
    expect(screen.getByText('Configure AI providers in the admin console')).toBeTruthy()
  })
})

describe('Role separation: admin console is owner-only end to end', () => {
  beforeEach(() => {
    get.mockReset()
  })
  afterEach(cleanup)

  it('the admin console fetches owner endpoints and renders platform sections', async () => {
    mockAuth.user = OWNER
    get.mockImplementation((url: string) => {
      if (url === '/api/admin/overview') {
        return Promise.resolve({ data: {
          queues: { totals: { queued: 1, processing: 0, done: 9, failed: 1, dead: 0, paused: 0, needs_input: 0 }, failure_rate_percent: 10 },
          usage_cost: { total_tokens: 0, estimated_cost_usd: 0, ai_operations: 0, ai_failure_rate_percent: 0 },
          failure_rates: { pipeline_percent: 10, application_percent: 0, ai_percent: 0 },
          automation_health: { active_users: 1, application: {}, failure_rate_percent: 0 },
          accounts: { active_users: 2, owners: 1, plans: { free: 2 } },
          sources: { job_sources: [{ id: 'greenhouse', available: true }], funding_providers: [] },
          flags: [{ key: 'assistant.interview_prep', label: 'Interview practice sessions', description: '', default: true, enabled: true, overridden: false }],
        } })
      }
      return Promise.resolve({ data: {} })
    })
    render(<MemoryRouter><AdminConsole /></MemoryRouter>)
    await act(async () => { for (let i = 0; i < 10; i++) await Promise.resolve() })

    expect(screen.getByTestId('admin-console')).toBeTruthy()
    expect(screen.getByText('AI provider configuration')).toBeTruthy()
    expect(screen.getByText('Source health')).toBeTruthy()
    expect(screen.getByText('Queue health')).toBeTruthy()
    expect(screen.getByText('Usage & cost (30 days)')).toBeTruthy()
    expect(screen.getByText('Failure rates')).toBeTruthy()
    expect(screen.getByText('Global feature flags')).toBeTruthy()
    const urls = get.mock.calls.map((c: any[]) => c[0])
    expect(urls).toContain('/api/admin/overview')
    expect(urls).toContain('/api/ops/status')
    expect(urls).toContain('/api/settings/ai/status')
  })
})
