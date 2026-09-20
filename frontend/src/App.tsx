import type { ReactNode } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Jobs from './pages/Jobs'
import Queues from './pages/Queues'
import Resumes from './pages/Resumes'
import Emails from './pages/Emails'
import Vault from './pages/Vault'
import Settings from './pages/Settings'
import Logs from './pages/Logs'
import Funding from './pages/Funding'
import Account from './pages/Account'
import Login from './pages/Login'
import Pricing from './pages/Pricing'
import Billing from './pages/Billing'
import Analytics from './pages/Analytics'
import Interview from './pages/Interview'
import Notifications from './pages/Notifications'
import Personas from './pages/Personas'
import ProfileReview from './pages/ProfileReview'
import Onboarding from './pages/Onboarding'
import Packets from './pages/Packets'
import Assist from './pages/Assist'
import Tracking from './pages/Tracking'
import AdminConsole from './pages/admin/AdminConsole'
import AdminAudit from './pages/admin/AdminAudit'
import AdminPlans from './pages/admin/AdminPlans'
import { AuthProvider, useAuth } from './context/AuthContext'
import { ErrorBoundary } from './components/ErrorBoundary'
import { LoadingBlock } from './components/states'

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()
  const location = useLocation()
  if (loading) {
    return <div className="min-h-screen flex items-center justify-center mono text-sm text-zinc-500">Loading…</div>
  }
  if (!user) return <Navigate to="/login" state={{ from: location }} replace />
  return <>{children}</>
}

/**
 * Route-level role separation — the client half of the backend's
 * `require_owner` enforcement.
 *
 * A member who navigates (or is sent) to an admin URL is redirected to their
 * own dashboard before the page component ever mounts, so no admin data is
 * fetched or rendered. The server remains the authority: even a hand-crafted
 * member token gets 403 `owner_required` from every `/api/admin/*` and
 * owner-only endpoint.
 */
export function RequireOwner({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()
  const location = useLocation()
  if (loading) {
    return <div className="min-h-screen flex items-center justify-center mono text-sm text-zinc-500">Loading…</div>
  }
  if (!user) return <Navigate to="/login" state={{ from: location }} replace />
  if (!user.is_owner) return <Navigate to="/" replace />
  return <>{children}</>
}

function RoutePage({ children }: { children: ReactNode }) {
  const location = useLocation()
  return (
    <ErrorBoundary scope="route" resetKey={location.pathname}>
      {children}
    </ErrorBoundary>
  )
}

export function AppRoutes() {
  const location = useLocation()
  return (
    <ErrorBoundary scope="app" resetKey={location.pathname}>
      <Routes>
        <Route
          path="/login"
          element={
            <RoutePage>
              <Login />
            </RoutePage>
          }
        />
        <Route path="/onboarding" element={<RequireAuth><RoutePage><Onboarding /></RoutePage></RequireAuth>} />
        <Route
          element={
            <RequireAuth>
              <Layout />
            </RequireAuth>
          }
        >
          {/* ── End-user surface: every authenticated account. ── */}
          <Route path="/" element={<RoutePage><Dashboard /></RoutePage>} />
          <Route path="/jobs" element={<RoutePage><Jobs /></RoutePage>} />
          <Route path="/assist" element={<RoutePage><Assist /></RoutePage>} />
          <Route path="/queues" element={<RoutePage><Queues /></RoutePage>} />
          <Route path="/personas" element={<RoutePage><Personas /></RoutePage>} />
          <Route path="/profile-review" element={<RoutePage><ProfileReview /></RoutePage>} />
          <Route path="/packets" element={<RoutePage><Packets /></RoutePage>} />
          <Route path="/resumes" element={<RoutePage><Resumes /></RoutePage>} />
          <Route path="/emails" element={<RoutePage><Emails /></RoutePage>} />
          <Route path="/funding" element={<RoutePage><Funding /></RoutePage>} />
          <Route path="/vault" element={<RoutePage><Vault /></RoutePage>} />
          <Route path="/interview" element={<RoutePage><Interview /></RoutePage>} />
          <Route path="/tracking" element={<RoutePage><Tracking /></RoutePage>} />
          <Route path="/analytics" element={<RoutePage><Analytics /></RoutePage>} />
          <Route path="/notifications" element={<RoutePage><Notifications /></RoutePage>} />
          <Route path="/billing" element={<RoutePage><Billing /></RoutePage>} />
          <Route path="/pricing" element={<RoutePage><Pricing /></RoutePage>} />
          <Route path="/settings" element={<RoutePage><Settings /></RoutePage>} />
          <Route path="/account" element={<RoutePage><Account /></RoutePage>} />
          <Route path="/logs" element={<RoutePage><Logs /></RoutePage>} />
        </Route>
        {/* ── Owner/admin surface: mounted only for the workspace owner. ── */}
        <Route
          element={
            <RequireAuth>
              <RequireOwner>
                <Layout />
              </RequireOwner>
            </RequireAuth>
          }
        >
          <Route path="/admin" element={<RoutePage><AdminConsole /></RoutePage>} />
          <Route path="/admin/audit" element={<RoutePage><AdminAudit /></RoutePage>} />
          <Route path="/admin/plans" element={<RoutePage><AdminPlans /></RoutePage>} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </ErrorBoundary>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </AuthProvider>
  )
}
