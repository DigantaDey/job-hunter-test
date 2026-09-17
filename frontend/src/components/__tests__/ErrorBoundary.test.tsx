import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { ErrorBoundary } from '../ErrorBoundary'
import { AppRoutes } from '../../App'
import { AuthProvider } from '../../context/AuthContext'
import { ThemeProvider } from '../../hooks/useTheme'

// Suppress React error boundary console.error noise during intentional throw tests
beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
  localStorage.clear()
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

// Mock client so Layout's AI status, billing, and auth calls do not fail
vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return {
    ...actual,
    default: {
      get: vi.fn().mockImplementation((url: string) => {
        if (url === '/api/settings/ai/status') {
          return Promise.resolve({ data: { online: true, rpm: 60, remaining: 50, latency_ms: 12 } })
        }
        if (url === '/api/billing/subscription') {
          return Promise.resolve({
            data: {
              plan_label: 'Pro',
              limits: { ai_credits_per_month: 50000 },
              usage: { ai_credits_per_month: { used: 1000 } },
            },
          })
        }
        if (url === '/api/auth/status') {
          return Promise.resolve({ data: { auth_required: true, bootstrap_required: false, allow_registration: false } })
        }
        if (url === '/api/auth/me') {
          return Promise.resolve({
            data: { id: 1, email: 'user@example.com', name: 'Test User', role: 'member', is_owner: false, is_active: true },
          })
        }
        if (url === '/api/profile/current') {
          return Promise.resolve({
            data: { data: { name: 'Test User', email: 'user@example.com', skills: [] } },
          })
        }
        if (url === '/api/queues/ai') {
          return Promise.resolve({
            data: { counts: { queued: 0, processing: 0, paused: 0, needs_input: 0 }, items: [], recent: [], processing_now: null },
          })
        }
        return Promise.resolve({ data: {} })
      }),
      post: vi.fn().mockResolvedValue({ data: {} }),
    },
    tokenStore: {
      access: () => localStorage.getItem('jh.access_token'),
      refresh: () => localStorage.getItem('jh.refresh_token'),
      set: (a: string, r: string) => {
        localStorage.setItem('jh.access_token', a)
        localStorage.setItem('jh.refresh_token', r)
      },
      clear: () => {
        localStorage.removeItem('jh.access_token')
        localStorage.removeItem('jh.refresh_token')
      },
    },
  }
})

// Throwing dummy component to simulate render errors
function Bomb({ message = 'Page exploded' }: { message?: string }): JSX.Element {
  throw new Error(message)
}

function HealthyPage({ name = 'Healthy Page' }: { name?: string }): JSX.Element {
  return <div data-testid="healthy-page">{name}</div>
}

describe('ErrorBoundary component', () => {
  it('renders children when no error occurs', () => {
    render(
      <ErrorBoundary>
        <div>All systems nominal</div>
      </ErrorBoundary>
    )
    expect(screen.getByText('All systems nominal')).toBeTruthy()
  })

  it('catches render errors and displays error message with Reload and Go Home buttons', () => {
    render(
      <ErrorBoundary scope="route">
        <Bomb message="Render crash in test" />
      </ErrorBoundary>
    )

    // Heading for route scope
    expect(screen.getByText('This page failed to render')).toBeTruthy()
    // Error explanation
    expect(screen.getByText('Render crash in test')).toBeTruthy()
    // Session active hint
    expect(screen.getByText(/Your session is still active/)).toBeTruthy()
    // Action buttons
    expect(screen.getByRole('button', { name: /Reload/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Go Home/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Sign out & retry/i })).toBeTruthy()
  })

  it('displays generic heading when scope is app', () => {
    render(
      <ErrorBoundary scope="app">
        <Bomb message="Global failure" />
      </ErrorBoundary>
    )

    expect(screen.getByText('Something went wrong')).toBeTruthy()
    expect(screen.getByText('Global failure')).toBeTruthy()
    expect(screen.queryByText(/Your session is still active/)).toBeNull()
  })

  it('formats React invariant #31 nicely instead of cryptic reactjs URL', () => {
    const rawError = new Error(
      'Objects are not valid as a React child (found: object with keys {id, title}). If you meant to render a collection of children, use an array instead.'
    )
    function InvariantBomb(): JSX.Element {
      throw rawError
    }

    render(
      <ErrorBoundary scope="route">
        <InvariantBomb />
      </ErrorBoundary>
    )

    expect(
      screen.getByText(
        'A screen tried to display raw API data ({id, title}) instead of a value. This is a bug in the page, not in your account.'
      )
    ).toBeTruthy()
  })

  it('calls onReload and clears error when Reload button is clicked', () => {
    let shouldCrash = true
    const onReload = vi.fn(() => {
      shouldCrash = false
    })

    function ConditionalBomb() {
      if (shouldCrash) throw new Error('First try failed')
      return <div>Recovered!</div>
    }

    const { rerender } = render(
      <ErrorBoundary scope="route" onReload={onReload}>
        <ConditionalBomb />
      </ErrorBoundary>
    )

    expect(screen.getByText('This page failed to render')).toBeTruthy()
    const reloadBtn = screen.getByRole('button', { name: /Reload/i })
    fireEvent.click(reloadBtn)

    expect(onReload).toHaveBeenCalledTimes(1)
    rerender(
      <ErrorBoundary scope="route" onReload={onReload}>
        <ConditionalBomb />
      </ErrorBoundary>
    )
    expect(screen.getByText('Recovered!')).toBeTruthy()
  })

  it('calls onGoHome and clears error when Go Home button is clicked', () => {
    let shouldCrash = true
    const onGoHome = vi.fn(() => {
      shouldCrash = false
    })

    function ConditionalBomb() {
      if (shouldCrash) throw new Error('First try failed')
      return <div>Home page loaded</div>
    }

    const { rerender } = render(
      <ErrorBoundary scope="route" onGoHome={onGoHome}>
        <ConditionalBomb />
      </ErrorBoundary>
    )

    expect(screen.getByText('This page failed to render')).toBeTruthy()
    const homeBtn = screen.getByRole('button', { name: /Go Home/i })
    fireEvent.click(homeBtn)

    expect(onGoHome).toHaveBeenCalledTimes(1)
    rerender(
      <ErrorBoundary scope="route" onGoHome={onGoHome}>
        <ConditionalBomb />
      </ErrorBoundary>
    )
    expect(screen.getByText('Home page loaded')).toBeTruthy()
  })

  it('clears auth tokens and redirects to /login on Sign out & retry', () => {
    localStorage.setItem('jh.access_token', 'token-123')
    localStorage.setItem('jh.refresh_token', 'refresh-123')

    render(
      <ErrorBoundary scope="route">
        <Bomb />
      </ErrorBoundary>
    )

    const signoutBtn = screen.getByRole('button', { name: /Sign out & retry/i })
    fireEvent.click(signoutBtn)

    expect(localStorage.getItem('jh.access_token')).toBeNull()
    expect(localStorage.getItem('jh.refresh_token')).toBeNull()
  })

  it('clears error automatically when resetKey changes (e.g. route navigation)', () => {
    let shouldCrash = true

    function MaybeBomb() {
      if (shouldCrash) throw new Error('Crash on route A')
      return <div>Clean route B</div>
    }

    const { rerender } = render(
      <ErrorBoundary scope="route" resetKey="/route-a">
        <MaybeBomb />
      </ErrorBoundary>
    )

    expect(screen.getByText('This page failed to render')).toBeTruthy()
    expect(screen.getByText('Crash on route A')).toBeTruthy()

    // Simulate navigation to /route-b with a non-crashing component
    shouldCrash = false
    rerender(
      <ErrorBoundary scope="route" resetKey="/route-b">
        <MaybeBomb />
      </ErrorBoundary>
    )

    expect(screen.queryByText('This page failed to render')).toBeNull()
    expect(screen.getByText('Clean route B')).toBeTruthy()
  })
})

describe('Wrap Routes in Error Boundary (P1 #7)', () => {
  it('keeps navigation functional and allows navigating away when a route throws', () => {
    function NavAndRoutes() {
      const location = useLocation()
      return (
        <div className="flex">
          <aside>
            <NavLink to="/working" data-testid="nav-working">
              Working Page
            </NavLink>
            <NavLink to="/crashing" data-testid="nav-crashing">
              Crashing Page
            </NavLink>
          </aside>
          <main>
            <Routes>
              <Route
                path="/crashing"
                element={
                  <ErrorBoundary scope="route" resetKey={location.pathname}>
                    <Bomb message="Jobs page crashed" />
                  </ErrorBoundary>
                }
              />
              <Route
                path="/working"
                element={
                  <ErrorBoundary scope="route" resetKey={location.pathname}>
                    <HealthyPage name="Working Page Content" />
                  </ErrorBoundary>
                }
              />
            </Routes>
          </main>
        </div>
      )
    }

    render(
      <MemoryRouter initialEntries={['/crashing']}>
        <NavAndRoutes />
      </MemoryRouter>
    )

    // Acceptance criterion 1: Error message with Reload or Go Home button instead of blank screen
    expect(screen.getByText('This page failed to render')).toBeTruthy()
    expect(screen.getByText('Jobs page crashed')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Reload/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Go Home/i })).toBeTruthy()

    // Acceptance criterion 2: The sidebar/navigation remains functional so the user can navigate
    const workingLink = screen.getByTestId('nav-working')
    expect(workingLink).toBeTruthy()

    // Click the working link to navigate
    fireEvent.click(workingLink)

    // The working page is displayed and error boundary is cleared
    expect(screen.queryByText('This page failed to render')).toBeNull()
    expect(screen.getByTestId('healthy-page')).toBeTruthy()
    expect(screen.getByText('Working Page Content')).toBeTruthy()
  })

  it('renders AppRoutes cleanly on login route without white-screening', async () => {
    render(
      <AuthProvider>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/login']}>
            <AppRoutes />
          </MemoryRouter>
        </ThemeProvider>
      </AuthProvider>
    )

    // Wait for auth status to settle
    await act(async () => {
      await Promise.resolve()
    })

    expect(await screen.findByRole('button', { name: /Sign in/i })).toBeTruthy()
  })

  it('app-level error boundary catches unhandled crashes in route tree and displays error', () => {
    function BrokenTree() {
      return (
        <MemoryRouter initialEntries={['/']}>
          <AppRoutes />
        </MemoryRouter>
      )
    }

    // Render without ThemeProvider / AuthProvider to trigger an error caught by top-level boundary
    render(<BrokenTree />)

    // The top-level ErrorBoundary catches the error and displays "Something went wrong"
    expect(screen.getByText('Something went wrong')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Reload/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Go Home/i })).toBeTruthy()
  })

  it('renders Layout and sidebar when user is authenticated', async () => {
    localStorage.setItem('jh.access_token', 'token-123')
    localStorage.setItem('jh.refresh_token', 'refresh-123')

    const { container } = render(
      <AuthProvider>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/']}>
            <AppRoutes />
          </MemoryRouter>
        </ThemeProvider>
      </AuthProvider>
    )

    // Wait for session to load
    await act(async () => {
      await Promise.resolve()
    })

    // Verify sidebar is rendered with navigation links
    const aside = container.querySelector('aside')
    expect(aside).toBeTruthy()
    expect(aside?.textContent).toContain('Dashboard')
    expect(aside?.textContent).toContain('Jobs')
    expect(aside?.textContent).toContain('Settings')
  })
})
