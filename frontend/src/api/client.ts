import axios, { AxiosError, AxiosRequestConfig, InternalAxiosRequestConfig } from 'axios'

/**
 * Single HTTP entry point for the SPA.
 *
 * Responsibilities: attach the bearer token, transparently refresh it once when
 * a 401 arrives, and (when the refresh also fails) tell the app the session is
 * gone so it can route back to /login. All other errors are surfaced to callers
 * unchanged; `apiError()` turns them into a human string.
 */

const ACCESS_KEY = 'jh.access_token'
const REFRESH_KEY = 'jh.refresh_token'

export const AUTH_EXPIRED_EVENT = 'jh:auth-expired'

export const tokenStore = {
  access: (): string | null => localStorage.getItem(ACCESS_KEY),
  refresh: (): string | null => localStorage.getItem(REFRESH_KEY),
  set(access: string, refresh?: string): void {
    localStorage.setItem(ACCESS_KEY, access)
    if (refresh) localStorage.setItem(REFRESH_KEY, refresh)
  },
  clear(): void {
    localStorage.removeItem(ACCESS_KEY)
    localStorage.removeItem(REFRESH_KEY)
  }
}

const client = axios.create({ baseURL: '', timeout: 30000 })

client.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = tokenStore.access()
  if (token) config.headers.set?.('Authorization', `Bearer ${token}`)
  return config
})

let refreshInFlight: Promise<string | null> | null = null

/** Refresh once per burst of 401s — concurrent failures share one request. */
async function refreshAccessToken(): Promise<string | null> {
  const refresh = tokenStore.refresh()
  if (!refresh) return null
  if (!refreshInFlight) {
    refreshInFlight = axios
      .post('/api/auth/refresh', { refresh_token: refresh })
      .then(({ data }) => {
        tokenStore.set(data.access_token, data.refresh_token)
        return data.access_token as string
      })
      .catch(() => {
        tokenStore.clear()
        return null
      })
      .finally(() => {
        refreshInFlight = null
      })
  }
  return refreshInFlight
}

type RetriableConfig = AxiosRequestConfig & { __retried?: boolean }

client.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const config = error.config as RetriableConfig | undefined
    const status = error.response?.status
    const url = config?.url || ''
    const isAuthCall = url.includes('/api/auth/')

    if (status === 401 && config && !config.__retried && !isAuthCall) {
      config.__retried = true
      const token = await refreshAccessToken()
      if (token) {
        config.headers = { ...(config.headers || {}), Authorization: `Bearer ${token}` }
        return client(config)
      }
      window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT))
    }
    return Promise.reject(error)
  }
)

/** Best-effort human message from an axios/FastAPI error. */
export function apiError(error: unknown, fallback = 'Something went wrong'): string {
  const err = error as AxiosError<{ detail?: unknown }>
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    const shaped = detail as { message?: string; code?: string; problems?: string[] }
    if (shaped.message) return shaped.message
    if (shaped.problems?.length) return shaped.problems.join(', ')
    if (shaped.code) return shaped.code.split('_').join(' ')
  }
  if (err?.code === 'ECONNABORTED') return 'The request timed out — the backend may be busy.'
  if (err?.response?.status === 401) return 'Your session expired — please sign in again.'
  if (err?.response?.status === 429) return 'Rate limited — slow down for a moment.'
  if (!err?.response) return 'Cannot reach the API. Is the backend running?'
  return fallback
}

export default client
