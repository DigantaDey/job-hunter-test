import { useEffect, useRef, useState } from 'react'
import client, { apiError } from '../../api/client'
import { Users } from 'lucide-react'
import { EmptyState, ErrorState, LoadingBlock, SectionCard } from '../../components/states'
import { useAuth } from '../../context/AuthContext'

/**
 * Plan & entitlement controls (owner-only).
 *
 * The owner sets any account's plan (manual entitlement — the same server-side
 * path webhooks use) and grants or revokes the owner role. The last owner
 * cannot demote themselves, and a member token is refused by the backend on
 * every one of these endpoints.
 */
const PLANS = ['free', 'pro', 'pro_plus']
const ROLES = ['member', 'owner']

const PLAN_LABEL: Record<string, string> = {
  free: 'Free',
  pro: 'Pro',
  pro_plus: 'Pro+ (unlimited)',
}

export default function AdminPlans() {
  const { user: me } = useAuth()
  const [items, setItems] = useState<any[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<number | null>(null)
  const [msg, setMsg] = useState('')
  const mounted = useRef(true)

  const load = async () => {
    setLoading(true)
    try {
      const { data } = await client.get('/api/admin/users')
      if (!mounted.current) return
      setItems(data.items || [])
      setError(null)
    } catch (e) {
      if (!mounted.current) return
      setItems([])
      setError(apiError(e, 'Accounts could not be loaded.'))
    } finally {
      if (mounted.current) setLoading(false)
    }
  }

  useEffect(() => {
    mounted.current = true
    void load()
    return () => { mounted.current = false }
  }, [])

  const setPlan = async (userId: number, plan: string) => {
    setBusy(userId); setMsg('')
    try {
      await client.put(`/api/admin/users/${userId}/plan`, { plan })
      setMsg(`Plan updated to ${PLAN_LABEL[plan] || plan} ✓`)
      setTimeout(() => setMsg(''), 3000)
      await load()
    } catch (e) {
      setMsg(apiError(e))
    } finally {
      setBusy(null)
    }
  }

  const setRole = async (userId: number, role: string) => {
    setBusy(userId); setMsg('')
    try {
      await client.put(`/api/admin/users/${userId}/role`, { role })
      setMsg(role === 'owner' ? 'Owner access granted ✓' : 'Owner access revoked ✓')
      setTimeout(() => setMsg(''), 3000)
      await load()
    } catch (e) {
      setMsg(apiError(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="space-y-4" data-testid="admin-plans">
      <div>
        <h1 className="text-xl font-semibold flex items-center gap-2"><Users className="w-5 h-5" /> Plans & users</h1>
        <p className="text-sm text-zinc-500 dark:text-zinc-400 mt-0.5">Set plan entitlements and manage owner access.</p>
      </div>
      {msg && <div className="text-sm mono p-3 rounded-xl bg-emerald-50 dark:bg-emerald-950 border border-emerald-200 dark:border-emerald-800 text-emerald-700 dark:text-emerald-300">{msg}</div>}

      <SectionCard title="Accounts">
        {error ? (
          <ErrorState message={error} onRetry={() => void load()} />
        ) : loading ? (
          <LoadingBlock label="Reading accounts…" />
        ) : !items?.length ? (
          <EmptyState title="No accounts yet" hint="Accounts appear here as people register." />
        ) : (
          <div className="overflow-x-auto" data-testid="users-table">
            <table className="w-full text-xs">
              <thead className="text-zinc-500 text-left mono">
                <tr>
                  <th className="py-1.5 pr-3">Account</th>
                  <th className="py-1.5 pr-3">Role</th>
                  <th className="py-1.5 pr-3">Plan</th>
                  <th className="py-1.5 pr-3">Last sign-in</th>
                  <th className="py-1.5">Controls</th>
                </tr>
              </thead>
              <tbody>
                {items.map(u => (
                  <tr key={u.id} className="border-t dark:border-zinc-800">
                    <td className="py-2 pr-3">
                      <div className="font-medium">{u.name || '—'}</div>
                      <div className="text-zinc-500 mono">{u.email}</div>
                    </td>
                    <td className="py-2 pr-3">
                      <span className={`text-[11px] px-2 py-0.5 rounded-full ${u.role === 'owner' ? 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300' : 'bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300'}`}>
                        {u.role === 'owner' ? 'Owner' : 'User'}
                      </span>
                      {u.id === me?.id && <span className="ml-1 text-[10px] text-zinc-400">(you)</span>}
                    </td>
                    <td className="py-2 pr-3 mono">{PLAN_LABEL[u.plan] || u.plan}</td>
                    <td className="py-2 pr-3 mono text-zinc-500">{u.last_login_at ? new Date(u.last_login_at).toLocaleString() : 'never'}</td>
                    <td className="py-2">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <select
                          value={u.plan}
                          disabled={busy === u.id}
                          onChange={e => void setPlan(u.id, e.target.value)}
                          aria-label={`Plan for ${u.email}`}
                          className="border rounded-lg px-2 py-1 text-xs mono bg-white dark:bg-zinc-900 dark:border-zinc-700"
                        >
                          {PLANS.map(p => <option key={p} value={p}>{PLAN_LABEL[p]}</option>)}
                        </select>
                        <select
                          value={u.role}
                          disabled={busy === u.id}
                          onChange={e => void setRole(u.id, e.target.value)}
                          aria-label={`Role for ${u.email}`}
                          className="border rounded-lg px-2 py-1 text-xs mono bg-white dark:bg-zinc-900 dark:border-zinc-700"
                        >
                          {ROLES.map(r => <option key={r} value={r}>{r === 'owner' ? 'Owner' : 'User'}</option>)}
                        </select>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>
    </div>
  )
}
