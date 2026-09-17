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
import { AuthProvider, useAuth } from './context/AuthContext'
import { ErrorBoundary } from './components/ErrorBoundary'

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()
  const location = useLocation()
  if (loading) {
    return <div className="min-h-screen flex items-center justify-center mono text-sm text-zinc-500">Loading…</div>
  }
  if (!user) return <Navigate to="/login" state={{ from: location }} replace />
  return <>{children}</>
}

export function AppRoutes() {
  const location = useLocation()
  return (
    <ErrorBoundary scope="app" resetKey={location.pathname}>
      <Routes>
        <Route
          path="/login"
          element={
            <ErrorBoundary scope="route" resetKey={location.pathname}>
              <Login />
            </ErrorBoundary>
          }
        />
        <Route
          element={
            <RequireAuth>
              <Layout />
            </RequireAuth>
          }
        >
          <Route
            path="/"
            element={
              <ErrorBoundary scope="route" resetKey={location.pathname}>
                <Dashboard />
              </ErrorBoundary>
            }
          />
          <Route
            path="/jobs"
            element={
              <ErrorBoundary scope="route" resetKey={location.pathname}>
                <Jobs />
              </ErrorBoundary>
            }
          />
          <Route
            path="/queues"
            element={
              <ErrorBoundary scope="route" resetKey={location.pathname}>
                <Queues />
              </ErrorBoundary>
            }
          />
          <Route
            path="/personas"
            element={
              <ErrorBoundary scope="route" resetKey={location.pathname}>
                <Personas />
              </ErrorBoundary>
            }
          />
          <Route
            path="/resumes"
            element={
              <ErrorBoundary scope="route" resetKey={location.pathname}>
                <Resumes />
              </ErrorBoundary>
            }
          />
          <Route
            path="/emails"
            element={
              <ErrorBoundary scope="route" resetKey={location.pathname}>
                <Emails />
              </ErrorBoundary>
            }
          />
          <Route
            path="/funding"
            element={
              <ErrorBoundary scope="route" resetKey={location.pathname}>
                <Funding />
              </ErrorBoundary>
            }
          />
          <Route
            path="/vault"
            element={
              <ErrorBoundary scope="route" resetKey={location.pathname}>
                <Vault />
              </ErrorBoundary>
            }
          />
          <Route
            path="/interview"
            element={
              <ErrorBoundary scope="route" resetKey={location.pathname}>
                <Interview />
              </ErrorBoundary>
            }
          />
          <Route
            path="/analytics"
            element={
              <ErrorBoundary scope="route" resetKey={location.pathname}>
                <Analytics />
              </ErrorBoundary>
            }
          />
          <Route
            path="/notifications"
            element={
              <ErrorBoundary scope="route" resetKey={location.pathname}>
                <Notifications />
              </ErrorBoundary>
            }
          />
          <Route
            path="/billing"
            element={
              <ErrorBoundary scope="route" resetKey={location.pathname}>
                <Billing />
              </ErrorBoundary>
            }
          />
          <Route
            path="/pricing"
            element={
              <ErrorBoundary scope="route" resetKey={location.pathname}>
                <Pricing />
              </ErrorBoundary>
            }
          />
          <Route
            path="/settings"
            element={
              <ErrorBoundary scope="route" resetKey={location.pathname}>
                <Settings />
              </ErrorBoundary>
            }
          />
          <Route
            path="/account"
            element={
              <ErrorBoundary scope="route" resetKey={location.pathname}>
                <Account />
              </ErrorBoundary>
            }
          />
          <Route
            path="/logs"
            element={
              <ErrorBoundary scope="route" resetKey={location.pathname}>
                <Logs />
              </ErrorBoundary>
            }
          />
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
