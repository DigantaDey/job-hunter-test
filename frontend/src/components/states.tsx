import type { ReactNode } from 'react'
import { AlertTriangle, CloudOff, Inbox, Loader2, RefreshCw } from 'lucide-react'

/**
 * Shared surface states — loading, empty, error and offline.
 *
 * Every user-facing section picks from these instead of inventing its own
 * markup, so "nothing here yet", "still working", "that failed" and "no
 * connection" always look and read the same. Empty states carry the next
 * action in user language; error states carry a retry that re-runs exactly
 * the failed read.
 */

export function LoadingBlock({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-10 text-sm text-zinc-500 dark:text-zinc-400" role="status" aria-live="polite">
      <Loader2 className="w-4 h-4 animate-spin" />
      <span className="mono">{label}</span>
    </div>
  )
}

export function SectionSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="animate-pulse space-y-2" role="status" aria-label="Loading" data-testid="skeleton">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-12 rounded-xl bg-zinc-100 dark:bg-zinc-800" />
      ))}
    </div>
  )
}

export function EmptyState({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="py-8 text-center" data-testid="empty-state">
      <Inbox className="w-8 h-8 mx-auto text-zinc-300 dark:text-zinc-600" aria-hidden />
      <div className="mt-2 text-sm font-medium text-zinc-600 dark:text-zinc-300">{title}</div>
      {hint && <div className="mt-1 text-xs text-zinc-500 dark:text-zinc-400 max-w-sm mx-auto">{hint}</div>}
      {action && <div className="mt-3 flex justify-center">{action}</div>}
    </div>
  )
}

export function ErrorState({ message, onRetry, retrying }: { message: string; onRetry?: () => void; retrying?: boolean }) {
  return (
    <div className="py-6 text-center" role="alert" data-testid="error-state">
      <AlertTriangle className="w-8 h-8 mx-auto text-red-500" aria-hidden />
      <div className="mt-2 text-sm font-medium">Something went wrong</div>
      <div className="mt-1 text-xs text-zinc-500 dark:text-zinc-400 max-w-sm mx-auto">{message}</div>
      {onRetry && (
        <button
          onClick={onRetry}
          disabled={retrying}
          className="mt-3 inline-flex items-center gap-2 px-4 py-2 rounded-full bg-blue-600 hover:bg-blue-700 disabled:opacity-60 text-white text-sm font-medium focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${retrying ? 'animate-spin' : ''}`} />
          {retrying ? 'Retrying…' : 'Try again'}
        </button>
      )}
    </div>
  )
}

export function OfflineBanner() {
  return (
    <div role="alert" data-testid="offline-banner"
         className="mb-4 p-3 rounded-xl border border-zinc-300 dark:border-zinc-700 bg-zinc-100 dark:bg-zinc-800 text-sm flex items-center gap-2 text-zinc-700 dark:text-zinc-200">
      <CloudOff className="w-4 h-4 shrink-0" aria-hidden />
      <span>
        You're offline — showing your last synced data. Everything you see here is safe; new work resumes when the connection returns.
      </span>
    </div>
  )
}

/** A section shell with a consistent title, so the dashboard reads as one product. */
export function SectionCard({ title, icon: Icon, children, aside }: {
  title: string
  icon?: React.ComponentType<{ className?: string }>
  children: ReactNode
  aside?: ReactNode
}) {
  return (
    <div className="card p-5" data-testid="section-card">
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-medium flex items-center gap-2">
          {Icon && <Icon className="w-4 h-4" aria-hidden />}
          {title}
        </h3>
        {aside}
      </div>
      <div className="mt-3">{children}</div>
    </div>
  )
}
