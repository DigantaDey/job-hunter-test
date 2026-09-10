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
import { AuthProvider, useAuth } from './context/AuthContext'

/** Everything except /login requires a session; the backend enforces it too. */
function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()
  const location = useLocation()
  if (loading) {
    return <div className="min-h-screen flex items-center justify-center mono text-sm text-zinc-500">Loading…</div>
  }
  if (!user) return <Navigate to="/login" state={{ from: location }} replace />
  return <>{children}</>
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route
            element={
              <RequireAuth>
                <Layout />
              </RequireAuth>
            }
          >
            <Route path="/" element={<Dashboard />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route path="/queues" element={<Queues />} />
            <Route path="/resumes" element={<Resumes />} />
            <Route path="/emails" element={<Emails />} />
            <Route path="/funding" element={<Funding />} />
            <Route path="/vault" element={<Vault />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/account" element={<Account />} />
            <Route path="/logs" element={<Logs />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  )
}
