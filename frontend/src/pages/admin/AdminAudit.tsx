import { useEffect, useRef, useState } from 'react'
import client, { apiError } from '../../api/client'
import { ScrollText } from 'lucide-react'
import { EmptyState, ErrorState, LoadingBlock, SectionCard } from '../../components/states'

/**
 * The workspace-wide audit log (owner-only).
 *
 * Every security-relevant action — sign-ins, settings changes, plan changes,
 * flag flips, exports — lands here with its actor and origin. End users keep
 * their *own* trail at Account → Activity (`/api/account/audit`); this is the
 * cross-user view, and the server refuses it to anyone but the owner.
 */
export default function AdminAudit() {
  const [items, setItems] = useState<any[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [actionPrefix, setActionPrefix] = useState('')
  const [appliedPrefix, setAppliedPrefix] = useState('')
  const [loading, setLoading] = useState(true)
  const mounted = useRef(true)

  const load = async (prefix: string) => {
    setLoading(true)
    try {
      const { data } = await client.get('/api/admin/audit', { params: { limit: 200, action_prefix: prefix || undefined } })
      if (!mounted.current) return
      setItems(data.items || [])
      setError(null)
    } catch (e) {
      if (!mounted.current) return
      setItems([])
      setError(apiError(e, 'The audit log could not be loaded.'))
    } finally {
      if (mounted.current) setLoading(false)
    }
  }

  useEffect(() => {
    mounted.current = true
    void load('')
    return () => { mounted.current = false }
  }, [])

  return (
    <div className="space-y-4" data-testid="admin-audit">
      <div>
        <h1 className="text-xl font-semibold flex items-center gap-2"><ScrollText className="w-5 h-5" /> Audit log</h1>
        <p className="text-sm text-zinc-500 dark:text-zinc-400 mt-0.5">Who did what, when, from where — across the whole workspace.</p>
      </div>

      <SectionCard title="Filters">
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={e => { e.preventDefault(); setAppliedPrefix(actionPrefix.trim()); void load(actionPrefix.trim()) }}
        >
          <div>
            <label className="text-xs mono font-medium" htmlFor="audit-prefix">Action prefix</label>
            <input
              id="audit-prefix"
              value={actionPrefix}
              onChange={e => setActionPrefix(e.target.value)}
              placeholder="e.g. admin. or auth."
              className="w-64 mt-1 border rounded-xl px-3 py-2 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700"
            />
          </div>
          <button type="submit" className="px-4 py-2 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm">Apply</button>
          {(appliedPrefix || actionPrefix) && (
            <button type="button" onClick={() => { setActionPrefix(''); setAppliedPrefix(''); void load('') }}
                    className="px-4 py-2 rounded-full border dark:border-zinc-700 text-sm">Clear</button>
          )}
        </form>
      </SectionCard>

      <SectionCard title={`Recent actions${appliedPrefix ? ` matching “${appliedPrefix}”` : ''}`}>
        {error ? (
          <ErrorState message={error} onRetry={() => void load(appliedPrefix)} />
        ) : loading ? (
          <LoadingBlock label="Reading the audit trail…" />
        ) : !items?.length ? (
          <EmptyState
            title={appliedPrefix ? 'Nothing matches this filter' : 'No activity recorded yet'}
            hint="Actions appear as people use the platform — sign-ins, settings changes, plan updates and more."
          />
        ) : (
          <div className="overflow-x-auto" data-testid="audit-table">
            <table className="w-full text-xs mono">
              <thead className="text-zinc-500 text-left">
                <tr>
                  <th className="py-1.5 pr-3">When</th>
                  <th className="py-1.5 pr-3">Action</th>
                  <th className="py-1.5 pr-3">Actor</th>
                  <th className="py-1.5 pr-3">Target</th>
                  <th className="py-1.5 pr-3">IP</th>
                  <th className="py-1.5">Detail</th>
                </tr>
              </thead>
              <tbody>
                {items.map(item => (
                  <tr key={item.id} className="border-t dark:border-zinc-800 align-top">
                    <td className="py-1.5 pr-3 whitespace-nowrap">{item.created_at ? new Date(item.created_at).toLocaleString() : '—'}</td>
                    <td className="py-1.5 pr-3 font-medium">{item.action}</td>
                    <td className="py-1.5 pr-3">{item.actor || '—'}</td>
                    <td className="py-1.5 pr-3">{item.target || '—'}</td>
                    <td className="py-1.5 pr-3">{item.ip || '—'}</td>
                    <td className="py-1.5 max-w-[280px] break-all text-zinc-500">{Object.keys(item.detail || {}).length ? JSON.stringify(item.detail) : '—'}</td>
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
