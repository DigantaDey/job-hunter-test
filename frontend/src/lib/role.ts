import type { SessionUser } from '../context/AuthContext'

/**
 * Role helpers — the single place that decides "owner vs ordinary user".
 *
 * The backend is the authority (every owner endpoint 403s a member), but the
 * SPA uses these to route and render: members never see the admin surface, so
 * the ordinary product stays a job-seeker's workspace.
 */
export function isOwner(user: SessionUser | null | undefined): boolean {
  return !!user && (user.role || '').toLowerCase() === 'owner'
}

/** The admin routes — mounted behind <RequireOwner> in App.tsx. */
export const ADMIN_ROUTES = ['/admin', '/admin/ai', '/admin/audit', '/admin/plans'] as const
