import { useState, type FormEvent } from 'react'
import { Loader2, LockKeyhole, Mail, ShieldCheck, User as UserIcon } from 'lucide-react'
import { useAuth } from '../context/AuthContext'

/**
 * Sign-in / first-run bootstrap / invited registration.
 *
 * The API decides which flow is available: `/api/auth/status` reports whether the
 * instance still needs its owner account and whether open registration is on.
 */
export default function Login() {
  const { status, login, bootstrap, register, error } = useAuth()
  const [mode, setMode] = useState<'login' | 'register' | null>(null)
  const [form, setForm] = useState({ email: '', password: '', name: '' })
  const [busy, setBusy] = useState(false)

  const bootstrapRequired = status?.bootstrap_required === true
  // The API reports both keys (registration_open is canonical) — accept either
  // so an older backend still renders the register link.
  const canRegister = status?.registration_open === true || status?.allow_registration === true
  const minLength = status?.password_min_length || 10
  const activeMode = bootstrapRequired ? 'bootstrap' : mode || 'login'

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    try {
      if (activeMode === 'bootstrap') await bootstrap(form.email, form.password, form.name)
      else if (activeMode === 'register') await register(form.email, form.password, form.name)
      else await login(form.email, form.password)
    } catch {
      /* the context already stored a readable error */
    } finally {
      setBusy(false)
    }
  }

  const title = activeMode === 'bootstrap' ? 'Create the owner account' : activeMode === 'register' ? 'Create your account' : 'Sign in'

  return (
    <div className="min-h-screen bg-zinc-50 dark:bg-zinc-950 flex items-center justify-center p-4">
      <div className="w-full max-w-md">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-11 h-11 rounded-2xl bg-gradient-to-br from-blue-600 to-violet-600 flex items-center justify-center text-white font-bold">
            JH
          </div>
          <div>
            <div className="font-semibold tracking-tight text-lg">JobHunter AI</div>
            <div className="text-xs mono text-zinc-500">v{status?.version || '2.0.0'} • {status?.environment || 'production'}</div>
          </div>
        </div>

        <div className="card p-6">
          <h1 className="text-lg font-semibold flex items-center gap-2">
            <LockKeyhole className="w-4 h-4" /> {title}
          </h1>
          {bootstrapRequired && (
            <p className="text-xs mono text-zinc-500 mt-2">
              This instance has no users yet. The first account becomes the owner.
            </p>
          )}

          <form onSubmit={submit} className="mt-5 space-y-4">
            {activeMode !== 'login' && (
              <label className="block">
                <span className="text-xs mono text-zinc-500">Name</span>
                <div className="mt-1 flex items-center gap-2 border rounded-xl px-3 py-2 bg-white dark:bg-zinc-900 dark:border-zinc-700">
                  <UserIcon className="w-4 h-4 text-zinc-400" />
                  <input
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    className="flex-1 bg-transparent text-sm outline-none"
                    placeholder="Ada Lovelace"
                    autoComplete="name"
                  />
                </div>
              </label>
            )}

            <label className="block">
              <span className="text-xs mono text-zinc-500">Email</span>
              <div className="mt-1 flex items-center gap-2 border rounded-xl px-3 py-2 bg-white dark:bg-zinc-900 dark:border-zinc-700">
                <Mail className="w-4 h-4 text-zinc-400" />
                <input
                  type="email"
                  required
                  value={form.email}
                  onChange={(e) => setForm({ ...form, email: e.target.value })}
                  className="flex-1 bg-transparent text-sm outline-none"
                  placeholder="you@example.com"
                  autoComplete="email"
                />
              </div>
            </label>

            <label className="block">
              <span className="text-xs mono text-zinc-500">Password</span>
              <div className="mt-1 flex items-center gap-2 border rounded-xl px-3 py-2 bg-white dark:bg-zinc-900 dark:border-zinc-700">
                <LockKeyhole className="w-4 h-4 text-zinc-400" />
                <input
                  type="password"
                  required
                  value={form.password}
                  onChange={(e) => setForm({ ...form, password: e.target.value })}
                  className="flex-1 bg-transparent text-sm outline-none"
                  placeholder={`At least ${minLength} characters`}
                  autoComplete={activeMode === 'login' ? 'current-password' : 'new-password'}
                />
              </div>
              {activeMode !== 'login' && (
                <span className="text-[11px] mono text-zinc-500">
                  Minimum {minLength} characters; a passphrase beats a short password.
                </span>
              )}
            </label>

            {error && (
              <div className="text-xs mono rounded-lg p-2 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 text-red-700 dark:text-red-300">
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={busy}
              className="w-full py-2.5 rounded-xl bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm font-medium inline-flex items-center justify-center gap-2 disabled:opacity-50"
            >
              {busy && <Loader2 className="w-4 h-4 animate-spin" />}
              {activeMode === 'bootstrap' ? 'Create owner account' : activeMode === 'register' ? 'Create account' : 'Sign in'}
            </button>
          </form>

          {!bootstrapRequired && canRegister && (
            <button
              onClick={() => setMode(activeMode === 'login' ? 'register' : 'login')}
              className="mt-4 text-xs mono text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200"
            >
              {activeMode === 'login' ? 'Need an account? Register →' : '← Back to sign in'}
            </button>
          )}

          {!bootstrapRequired && !canRegister && (
            <p className="mt-4 text-[11px] mono text-zinc-500">
              Registration is closed on this instance — ask the owner for an account.
            </p>
          )}
        </div>

        <p className="mt-4 text-[11px] mono text-zinc-500 flex items-start gap-2">
          <ShieldCheck className="w-3.5 h-3.5 mt-0.5 shrink-0" />
          Sessions use short-lived JWTs with rotating refresh tokens. Every credential and API key is
          stored encrypted; you can export or delete all of your data from the Account page.
        </p>
      </div>
    </div>
  )
}
