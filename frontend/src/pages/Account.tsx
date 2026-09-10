import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Download, KeyRound, ShieldCheck, Trash2, UserCog } from 'lucide-react'
import client, { apiError } from '../api/client'
import { useAuth } from '../context/AuthContext'

type Disclosures = Record<string, { title: string; summary: string }>
type ConsentState = { consents: Record<string, string>; granted: Record<string, boolean> }
type AuditRow = { id: number; action: string; target: string; created_at: string; ip?: string }
type ApiKeyRow = { id: number; name: string; prefix: string; last_used_at?: string; revoked_at?: string }

/**
 * Account & privacy: consent management, GDPR export, API keys and the
 * irreversible delete. Everything here maps to a documented API endpoint.
 */
export default function Account() {
  const { user, logout } = useAuth()
  const [disclosures, setDisclosures] = useState<Disclosures>({})
  const [consent, setConsent] = useState<ConsentState>({ consents: {}, granted: {} })
  const [audit, setAudit] = useState<AuditRow[]>([])
  const [keys, setKeys] = useState<ApiKeyRow[]>([])
  const [newKeyName, setNewKeyName] = useState('')
  const [freshKey, setFreshKey] = useState<string | null>(null)
  const [usage, setUsage] = useState<Record<string, number> | null>(null)
  const [confirmEmail, setConfirmEmail] = useState('')
  const [banner, setBanner] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    const [d, c, a, k, u] = await Promise.all([
      client.get('/api/account/disclosures'),
      client.get('/api/account/consent'),
      client.get('/api/account/audit?limit=50'),
      client.get('/api/auth/api-keys'),
      client.get('/api/account/billing-usage')
    ])
    setDisclosures(d.data.disclosures || {})
    setConsent(c.data)
    setAudit(a.data || [])
    setKeys(k.data || [])
    setUsage(u.data || null)
  }, [])

  useEffect(() => {
    load().catch((err) => setBanner({ kind: 'err', text: apiError(err, 'Could not load account data') }))
  }, [load])

  const toggleConsent = async (key: string, value: boolean) => {
    setBusy(true)
    try {
      await client.post('/api/account/consent', { [key]: value })
      await load()
      setBanner({ kind: 'ok', text: value ? `Recorded consent for ${key}` : `Revoked consent for ${key}` })
    } catch (err) {
      setBanner({ kind: 'err', text: apiError(err, 'Could not update consent') })
    } finally {
      setBusy(false)
    }
  }

  const exportData = async () => {
    try {
      const response = await client.get('/api/account/export', { responseType: 'blob' })
      const url = URL.createObjectURL(response.data as Blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `jobhunter-export-${new Date().toISOString().slice(0, 10)}.json`
      link.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      setBanner({ kind: 'err', text: apiError(err, 'Export failed') })
    }
  }

  const createKey = async () => {
    if (!newKeyName.trim()) return
    try {
      const { data } = await client.post('/api/auth/api-keys', { name: newKeyName.trim() })
      setFreshKey(data.api_key)
      setNewKeyName('')
      await load()
    } catch (err) {
      setBanner({ kind: 'err', text: apiError(err, 'Could not create the key') })
    }
  }

  const revokeKey = async (id: number) => {
    await client.delete(`/api/auth/api-keys/${id}`)
    await load()
  }

  const deleteAccount = async () => {
    if (!user) return
    setBusy(true)
    try {
      await client.delete(`/api/account?confirm=${encodeURIComponent(confirmEmail)}`)
      await logout()
    } catch (err) {
      setBanner({ kind: 'err', text: apiError(err, 'Deletion failed — is the email exactly right?') })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold flex items-center gap-2">
          <UserCog className="w-5 h-5" /> Account &amp; privacy
        </h1>
        <button onClick={() => void logout()} className="px-3 py-1.5 rounded-full border text-sm">
          Sign out
        </button>
      </div>

      {banner && (
        <div
          className={`text-sm mono p-2 rounded-lg border ${
            banner.kind === 'ok'
              ? 'bg-emerald-50 dark:bg-emerald-950 border-emerald-200 dark:border-emerald-800 text-emerald-700 dark:text-emerald-300'
              : 'bg-red-50 dark:bg-red-950 border-red-200 dark:border-red-900 text-red-700 dark:text-red-300'
          }`}
        >
          {banner.text}
        </div>
      )}

      <div className="card p-5">
        <h2 className="font-medium flex items-center gap-2">
          <ShieldCheck className="w-4 h-4" /> Consents
        </h2>
        <p className="text-xs mono text-zinc-500 mt-1">
          Automation and outreach stay blocked until you accept these. Every change is timestamped and audited.
        </p>
        <div className="mt-4 space-y-3">
          {Object.entries(disclosures).map(([key, item]) => (
            <label key={key} className="flex items-start gap-3 p-3 rounded-xl border dark:border-zinc-800">
              <input
                type="checkbox"
                className="mt-1"
                disabled={busy}
                checked={!!consent.granted[key]}
                onChange={(e) => void toggleConsent(key, e.target.checked)}
              />
              <span>
                <span className="text-sm font-medium block">{item.title}</span>
                <span className="text-xs text-zinc-500 dark:text-zinc-400">{item.summary}</span>
                {consent.consents[`${key}_accepted_at`] && (
                  <span className="text-[11px] mono text-emerald-600 dark:text-emerald-400 block mt-1">
                    accepted {consent.consents[`${key}_accepted_at`].slice(0, 19).replace('T', ' ')}
                  </span>
                )}
              </span>
            </label>
          ))}
        </div>
      </div>

      <div className="grid lg:grid-cols-2 gap-4">
        <div className="card p-5">
          <h2 className="font-medium flex items-center gap-2">
            <Download className="w-4 h-4" /> Your data
          </h2>
          <p className="text-xs mono text-zinc-500 mt-1">
            Machine-readable export of your profile, resumes, jobs, emails, vault (decrypted for you) and audit trail.
          </p>
          <button onClick={() => void exportData()} className="mt-3 px-4 py-2 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm">
            Download JSON export
          </button>
          {usage && (
            <div className="mt-4 grid grid-cols-3 gap-2 text-center">
              {['jobs', 'resumes', 'emails'].map((key) => (
                <div key={key} className="p-2 rounded-xl bg-zinc-50 dark:bg-zinc-800">
                  <div className="text-lg font-semibold">{usage[key] ?? 0}</div>
                  <div className="text-[11px] mono text-zinc-500">{key}</div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="card p-5">
          <h2 className="font-medium flex items-center gap-2">
            <KeyRound className="w-4 h-4" /> API keys
          </h2>
          <p className="text-xs mono text-zinc-500 mt-1">
            For scripts and CI. Keys are shown once and stored hashed.
          </p>
          <div className="mt-3 flex gap-2">
            <input
              value={newKeyName}
              onChange={(e) => setNewKeyName(e.target.value)}
              placeholder="ci-runner"
              className="flex-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"
            />
            <button onClick={() => void createKey()} className="px-4 py-2 rounded-xl border text-sm">
              Create
            </button>
          </div>
          {freshKey && (
            <div className="mt-3 p-2 rounded-lg bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-900 text-xs mono break-all">
              {freshKey}
              <div className="text-[11px] mt-1">Copy it now — it will not be shown again.</div>
            </div>
          )}
          <ul className="mt-3 space-y-2">
            {keys.map((key) => (
              <li key={key.id} className="flex items-center justify-between text-xs mono border-b dark:border-zinc-800 pb-2">
                <span>
                  {key.name} <span className="text-zinc-400">{key.prefix}…</span>
                </span>
                {key.revoked_at ? (
                  <span className="text-zinc-400">revoked</span>
                ) : (
                  <button onClick={() => void revokeKey(key.id)} className="text-red-600 dark:text-red-400">
                    revoke
                  </button>
                )}
              </li>
            ))}
            {!keys.length && <li className="text-xs mono text-zinc-500">No API keys yet.</li>}
          </ul>
        </div>
      </div>

      <div className="card p-5">
        <h2 className="font-medium">Recent security activity</h2>
        <ul className="mt-3 space-y-1 text-xs mono max-h-64 overflow-auto scrollbar-thin">
          {audit.map((row) => (
            <li key={row.id} className="flex items-center gap-3 border-b dark:border-zinc-800 py-1">
              <span className="text-zinc-400 w-36 shrink-0">{String(row.created_at).slice(0, 19).replace('T', ' ')}</span>
              <span className="font-medium">{row.action}</span>
              <span className="text-zinc-500 truncate">{row.target}</span>
            </li>
          ))}
          {!audit.length && <li className="text-zinc-500">Nothing recorded yet.</li>}
        </ul>
      </div>

      <div className="card p-5 border-red-200 dark:border-red-900">
        <h2 className="font-medium flex items-center gap-2 text-red-700 dark:text-red-400">
          <AlertTriangle className="w-4 h-4" /> Delete account
        </h2>
        <p className="text-xs mono text-zinc-500 mt-1">
          Removes every row and file this account owns — jobs, resumes, emails, credentials, settings. The audit
          entry recording the deletion is kept without your identity. This cannot be undone.
        </p>
        <div className="mt-3 flex gap-2">
          <input
            value={confirmEmail}
            onChange={(e) => setConfirmEmail(e.target.value)}
            placeholder={user?.email}
            className="flex-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"
          />
          <button
            onClick={() => void deleteAccount()}
            disabled={busy || confirmEmail !== user?.email}
            className="px-4 py-2 rounded-xl bg-red-600 text-white text-sm disabled:opacity-50 inline-flex items-center gap-2"
          >
            <Trash2 className="w-4 h-4" /> Delete forever
          </button>
        </div>
      </div>
    </div>
  )
}
