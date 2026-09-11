import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * Top-level error boundary — stops a single render error from blanking the
 * whole app. Reports the failure to the console and offers a recovery button
 * that wipes local auth state and reloads, so a corrupted token / bad response
 * can't leave the user stranded on a white screen.
 */
type Props = { children: ReactNode }
type State = { error: Error | null }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error('[ErrorBoundary] render error:', error, info)
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
    return (
      <div className="min-h-screen bg-zinc-50 dark:bg-zinc-950 flex items-center justify-center p-4">
        <div className="card p-6 max-w-md w-full">
          <h1 className="text-lg font-semibold text-red-700 dark:text-red-400">Something went wrong</h1>
          <p className="mt-2 text-sm mono text-zinc-600 dark:text-zinc-400 break-words">
            {String(this.state.error?.message || this.state.error)}
          </p>
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
