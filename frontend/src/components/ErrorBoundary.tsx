import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * Top-level error boundary — stops a single render error from blanking the
 * whole app. Reports the failure to the console and offers a recovery button
 * that wipes local auth state and reloads, so a corrupted token / bad response
 * can't leave the user stranded on a white screen.
 */
type Props = { children: ReactNode; scope?: 'app' | 'route'; resetKey?: string }
type State = { error: Error | null }

/**
 * React's production build minifies error #31 ("objects are not valid as a
 * React child") into an opaque URL. Decode the common cases so the screen tells
 * a human what actually broke instead of sending them to reactjs.org.
 */
function explain(error: Error | null): string {
  const raw = String(error?.message || error || 'Unknown error')
  if (raw.includes('invariant=31') || raw.includes('Objects are not valid as a React child')) {
    const keys = decodeURIComponent(raw).match(/object with keys \{([^}]*)\}/)?.[1]
    return keys
      ? `A screen tried to display raw API data ({${keys}}) instead of a value. This is a bug in the page, not in your account.`
      : 'A screen tried to display raw API data instead of a value.'
  }
  return raw
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error('[ErrorBoundary] render error:', error, info)
  }

  componentDidUpdate(prev: Props) {
    // Navigating away from a broken route clears the error automatically.
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null })
    }
  }

  private reset = () => {
    try {
      // Be conservative: clear auth tokens so a refresh can re-bootstrap cleanly.
      ;['jh.access_token', 'jh.refresh_token'].forEach((k) => localStorage.removeItem(k))
    } catch {
      /* ignore */
    }
    window.location.href = '/login'
  }

  render() {
    if (!this.state.error) return this.props.children
    const isRoute = this.props.scope === 'route'
    return (
      <div
        className={
          isRoute
            ? 'flex items-start justify-center p-4'
            : 'min-h-screen bg-zinc-50 dark:bg-zinc-950 flex items-center justify-center p-4'
        }
      >
        <div className="card p-6 max-w-md w-full">
          <h1 className="text-lg font-semibold text-red-700 dark:text-red-400">
            {isRoute ? 'This page failed to render' : 'Something went wrong'}
          </h1>
          <p className="mt-2 text-sm mono text-zinc-600 dark:text-zinc-400 break-words">
            {explain(this.state.error)}
          </p>
          {isRoute && (
            <p className="mt-2 text-xs mono text-zinc-500">
              Your session is still active — you can keep using the rest of the app from the menu.
            </p>
          )}
          <div className="mt-4 flex gap-2">
            <button
              onClick={() => {
                this.setState({ error: null })
                window.location.reload()
              }}
              className="px-4 py-2 rounded-xl bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm"
            >
              Reload
            </button>
            <button
              onClick={this.reset}
              className="px-4 py-2 rounded-xl border text-sm"
            >
              Sign out &amp; retry
            </button>
          </div>
        </div>
      </div>
    )
  }
}

export default ErrorBoundary
