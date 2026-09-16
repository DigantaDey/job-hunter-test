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

/**
 * Wait budget for AI-backed requests (upload, generate, score, draft, reflect…).
 *
 * The backend gives a healthy generation up to AI_TIMEOUT per attempt (default
 * 300s) plus one guardrail repair pass, so the browser must allow ~10 minutes —
 * aborting earlier would cut off a healthy in-progress generation that the
 * server would otherwise complete. Non-AI requests keep the short default
 * below and fail fast on hangs.
 */
export const AI_REQUEST_TIMEOUT_MS = 600_000

const client = axios.create({ baseURL: '', timeout: 30000 })

client.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = tokenStore.access()
  if (token) config.headers.set?.('Authorization', `Bearer ${token}`)
  return config
})

let refreshInFlight: Promise<string | null> | null = null
let authExpiredDispatched = false

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
      // Dispatch the auth-expired event exactly once per "burst" — otherwise
      // every concurrent 401 would re-clear the user in AuthContext and cause
      // thrash / redirect races.
      if (!authExpiredDispatched) {
        authExpiredDispatched = true
        window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT))
        // Re-arm after a tick so a genuine subsequent expiry (e.g. after a
        // fresh login) still fires.
        setTimeout(() => { authExpiredDispatched = false }, 500)
      }
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


/** Structured explanation the backend sends when the model cannot be used. */
export type AIOutage = {
  reason?: string
  message?: string
  fix?: string
  workflow?: string
  detail?: string
  base_url?: string | null
  model?: string | null
  hint?: string | null
  /** v2.1 three-state signal: transient_outage | blocked_needs_action */
  state?: string
  /** v2.1 dedicated status: ai_paused | ai_blocked */
  status?: string
  pausable?: boolean
  retry_after_hint?: number | null
  /** v2.0.5 accounting carried into the response. */
  attempts?: number
  possibly_billed?: boolean
}

/**
 * Pull the AI diagnosis out of a 503, or null when the failure is unrelated.
 *
 * Every AI-backed action (upload, generate, score, draft, reflect) now fails
 * loudly with this shape instead of quietly producing a degraded result, so the
 * UI can tell the user *why* the model is offline and how to fix it.
 */
export function aiOutage(error: unknown): AIOutage | null {
  const err = error as AxiosError<{ code?: string; reason?: string; message?: string; fix?: string }>
  const data = err?.response?.data
  if (err?.response?.status === 503 || data?.code === 'ai_unavailable') {
    return (data || {}) as AIOutage
  }
  return null
}

/** Guardrail verdict for a 422 from a guardrailed endpoint. */
export function guardrailFailure(error: unknown): { issues: { code: string; field?: string; message?: string }[]; message?: string } | null {
  const err = error as AxiosError<{ code?: string; issues?: any[]; message?: string }>
  const data = err?.response?.data
  if (err?.response?.status === 422 && data?.code === 'guardrail_failed') {
    return { issues: data.issues || [], message: data.message }
  }
  return null
}

/**
 * Download a resume through the authenticated signed-URL endpoint.
 *
 * A bare `<a href="/api/resumes/2/download">` sends no Authorization header,
 * which is why downloads used to open a JSON `{"detail":"Not authenticated"}`
 * page. We fetch a short-lived signed URL first, then let the browser navigate
 * to it so the native download/preview UI is used.
 */
export async function downloadResume(resumeId: number, format: 'pdf' | 'docx'): Promise<string> {
  const { data } = await client.get(`/api/resumes/${resumeId}/download-url`, { params: { format } })
  window.location.assign(data.url)
  return data.filename as string
}

/**
 * Download a vault CSV export the authenticated way.
 *
 * The Vault page's buttons used to be bare `<a href="/api/vault/export/chrome">`
 * links — a browser navigation sends no Authorization header, so they opened a
 * JSON `{"detail":"Not authenticated"}` page. Same fix as `downloadResume`:
 * fetch a short-lived signed URL with the bearer token attached, then hand the
 * browser a URL the export endpoint will accept without that header.
 */
export async function downloadVaultCsv(flavour: 'chrome' | 'apple'): Promise<string> {
  const { data } = await client.get(`/api/vault/export/${flavour}/url`)
  window.location.assign(data.url)
  return data.filename as string
}

export default client
