import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import client, { AUTH_EXPIRED_EVENT, apiError, tokenStore } from '../api/client'

export type SessionUser = {
  id: number
  email: string
  name: string
  role: string
  is_owner: boolean
  is_active?: boolean
}

type AuthStatus = {
  bootstrap_required: boolean
  /** `registration_open` is the canonical key; `allow_registration` is kept by the API for older clients. */
  registration_open?: boolean
  allow_registration?: boolean
  auth_required: boolean
  password_min_length?: number
  version?: string
  environment?: string
}

type AuthContextValue = {
  user: SessionUser | null
  status: AuthStatus | null
  loading: boolean
  error: string | null
  bootstrap: (email: string, password: string, name: string) => Promise<void>
  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string, name: string) => Promise<void>
  logout: () => Promise<void>
  refreshStatus: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<SessionUser | null>(null)
  const [status, setStatus] = useState<AuthStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refreshStatus = useCallback(async () => {
    try {
      const { data } = await client.get<AuthStatus>('/api/auth/status')
      setStatus(data)
    } catch {
      setStatus(null)
    }
  }, [])

  const loadSession = useCallback(async () => {
    if (!tokenStore.access()) {
      setUser(null)
      setLoading(false)
      return
    }
    try {
      const { data } = await client.get<SessionUser & { consents?: Record<string, unknown>; settings?: unknown }>('/api/auth/me')
      // Normalise: /me omits is_owner, so derive it from role to keep the shape
      // identical to what login/bootstrap/register send inline.
      const normalized: SessionUser = {
        id: data.id,
        email: data.email,
        name: data.name || '',
        role: data.role || 'member',
        is_owner: data.is_owner ?? (data.role || '').toLowerCase() === 'owner',
        is_active: data.is_active ?? true,
      }
      setUser(normalized)
    } catch {
      tokenStore.clear()
      setUser(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void (async () => {
      await refreshStatus()
      await loadSession()
    })()
  }, [loadSession, refreshStatus])

  useEffect(() => {
    const onExpired = () => {
      tokenStore.clear()
      setUser(null)
    }
    window.addEventListener(AUTH_EXPIRED_EVENT, onExpired)
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, onExpired)
  }, [])

  const finishAuth = useCallback((payload: { access_token: string; refresh_token: string; user?: SessionUser | null }) => {
    tokenStore.set(payload.access_token, payload.refresh_token)
    setError(null)
    // Normalise the user shape so is_owner is always present.
    if (payload.user) {
      const u = payload.user
      const normalized: SessionUser = {
        id: u.id,
        email: u.email,
        name: u.name || '',
        role: u.role || 'member',
        is_owner: u.is_owner ?? (u.role || '').toLowerCase() === 'owner',
        is_active: u.is_active ?? true,
      }
      setUser(normalized)
    }
  }, [])

  const bootstrap = useCallback(
    async (email: string, password: string, name: string) => {
      setError(null)
      try {
        const { data } = await client.post('/api/auth/bootstrap', { email, password, name })
        finishAuth(data)
        await refreshStatus()
      } catch (err) {
        setError(apiError(err, 'Could not create the owner account'))
        throw err
      }
    },
    [finishAuth, refreshStatus]
  )

  const login = useCallback(
    async (email: string, password: string) => {
      setError(null)
      try {
        const { data } = await client.post('/api/auth/login', { email, password })
        finishAuth(data)
      } catch (err) {
        setError(apiError(err, 'Sign-in failed'))
        throw err
      }
    },
    [finishAuth]
  )

  const register = useCallback(
    async (email: string, password: string, name: string) => {
      setError(null)
      try {
        const { data } = await client.post('/api/auth/register', { email, password, name })
        finishAuth(data)
      } catch (err) {
        setError(apiError(err, 'Registration failed'))
        throw err
      }
    },
    [finishAuth]
  )

  const logout = useCallback(async () => {
    const refresh = tokenStore.refresh()
    try {
      if (refresh) await client.post('/api/auth/logout', { refresh_token: refresh })
    } catch {
      /* logging out locally must always succeed */
    } finally {
      tokenStore.clear()
      setUser(null)
    }
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({ user, status, loading, error, bootstrap, login, register, logout, refreshStatus }),
    [user, status, loading, error, bootstrap, login, register, logout, refreshStatus]
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside <AuthProvider>')
  return context
}
