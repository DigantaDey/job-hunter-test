import { useEffect, useRef, useState } from 'react'
import client, { apiError as apiErrorMessage } from '../api/client'
import { Settings as SettingsIcon, Save, Bot, Search, Sliders, Mail, Zap, TrendingUp, Sparkles, RefreshCw, ShieldCheck, ArrowRight, CalendarClock } from 'lucide-react'
import { Link } from 'react-router-dom'
import { AIStatusBanner } from '../components/AIBanner'
import { AUTO_BLOCKERS, AUTO_WORKFLOW_LABELS, autoStateCopy, fmtCadence, fmtWhen, type AutoOverview } from '../lib/automation'
import { useAuth } from '../context/AuthContext'
import { isOwner } from '../lib/role'

/**
 * User settings (v2.3).
 *
 * The AI provider configuration — base URLs, API keys, models, token budgets,
 * per-workflow overrides — is owner-level now: it lives in the admin console
 * (`/admin`) and the server refuses member writes to the `ai` category with
 * 403 `owner_required`. What remains here is the user's own product setup:
 * search keywords, funding focus, resume/email preferences, workflow toggles
 * and auto mode — plus a plain-language "Your assistant" card.
 */

/** How often the Auto-mode card re-reads its schedule/history while it is on screen. */
const AUTOMATION_POLL_MS = 30_000

type AssistantInfo = {
  configured: boolean
  state?: string
  online?: boolean
  reason?: string | null
  hint?: string | null
  paused_items?: number
  message?: string
}

export default function Settings() {
  const { user } = useAuth()
  const owner = isOwner(user)
  const [data, setData] = useState<any>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')
  const [assistant, setAssistant] = useState<AssistantInfo | null>(null)
  const [resuming, setResuming] = useState(false)
  const [extracted, setExtracted] = useState<any>(null)
  // Auto mode (v2.2): the schedule is the backend's, never recomputed here.
  const [auto, setAuto] = useState<AutoOverview | null>(null)
  const [autoBusy, setAutoBusy] = useState(false)
  const [autoNote, setAutoNote] = useState('')
  const mounted = useRef(true)

  const loadAuto = async () => {
    try {
      const { data } = await client.get('/api/automation')
      if (mounted.current) setAuto(data as AutoOverview)
    } catch {
      // A failed poll must not blank the card: keep the last read.
    }
  }

  const loadAssistant = async () => {
    try {
      const { data } = await client.get('/api/me/assistant')
      if (mounted.current) setAssistant(data)
    } catch {
      // Keep the last read; the layout banner carries the live signal anyway.
    }
  }

  /**
   * Flipping auto mode goes through the same PUT /api/settings the rest of this
   * page uses — one settings contract, one tier gate. A 403 `upgrade_required`
   * carries a human message, so it is shown as the message (never an error code,
   * never a checkbox that silently snaps back), and the switch is re-read either
   * way so what is on screen is what the server stored.
   */
  const setAutoMode = async (on: boolean) => {
    setAutoBusy(true); setAutoNote('')
    try {
      await client.put('/api/settings', { automation: { auto_mode: on } })
    } catch (e: any) {
      const detail = e?.response?.data?.detail
      setAutoNote(detail && typeof detail === 'object' && detail.message ? String(detail.message) : apiErrorMessage(e))
    }
    await load()
    setAutoBusy(false)
  }

  const load = async () => {
    try {
      const { data } = await client.get('/api/settings'); setData(data)
      setLoadError(null)
    } catch (e) {
      if (mounted.current) setLoadError(apiErrorMessage(e, 'Settings could not be loaded.'))
      return
    }
    await Promise.all([loadAssistant(), loadAuto()])
    try { const { data: ctx } = await client.get('/api/context/keywords'); if (mounted.current) setExtracted(ctx) } catch { if (mounted.current) setExtracted(null) }
  }
  useEffect(() => { load() }, [])

  /**
   * One tick of the page's live layer: the auto-mode schedule (last/next run,
   * quota, blocking reasons) and the assistant status refresh while the tab is
   * visible. A failed tick keeps the last read — neither card ever blanks out
   * because one request was late.
   */
  const refreshLive = async () => {
    await Promise.all([loadAuto(), loadAssistant()])
  }

  // While this page is open the schedule is live — last/next run, the quota and
  // the assistant banner refresh on a 30 s timer that only runs while the tab is
  // *visible*: a hidden tab polls nothing at all (~2 wasted req/min per
  // background tab before this), and becoming visible again re-reads at once
  // instead of waiting out the rest of the interval. Same cadence pattern as
  // `hooks/useAIQueue.ts` and `pages/Emails.tsx`; the timer and the listener
  // are both cleared on unmount.
  useEffect(() => {
    mounted.current = true
    let timer: ReturnType<typeof setInterval> | null = null
    /** Arm the interval; `readNow` also re-reads before the first tick. */
    const start = (readNow = true) => {
      if (timer) return
      if (readNow) void refreshLive()
      timer = setInterval(() => void refreshLive(), AUTOMATION_POLL_MS)
    }
    const stop = () => {
      if (timer) clearInterval(timer)
      timer = null
    }
    const onVisibility = () => {
      if (typeof document === 'undefined') return
      if (document.visibilityState === 'hidden') stop()
      else start()
    }
    // Mount: `load()` above has just read the schedule and the assistant, so
    // only the timer is armed here — no duplicate pair of requests on open.
    // A tab that opens hidden arms nothing and reads the moment it is shown.
    if (typeof document === 'undefined' || document.visibilityState !== 'hidden') start(false)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      mounted.current = false
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [])

  const resumePaused = async () => {
    setResuming(true)
    try {
      await client.post('/api/settings/ai/resume')
      await loadAssistant()
    } catch { /* the banner explains the state */ } finally { setResuming(false) }
  }

  const save = async () => {
    setSaving(true); setMsg('')
    try {
      const writable = (data._meta?.writable || {}) as Record<string, string[]>
      const payload: Record<string, Record<string, unknown>> = {}
      Object.entries(writable).forEach(([cat, keys]) => {
        if (cat === 'ai') return // owner-level; edited in the admin console
        const values: Record<string, unknown> = {}
        keys.forEach((key: string) => { if (data[cat] && key in data[cat]) values[key] = data[cat][key] })
        if (Object.keys(values).length) payload[cat] = values
      })
      if (Object.keys(payload).length) {
        await client.put('/api/settings', payload)
        setMsg('Saved ✓'); setTimeout(() => setMsg(''), 2000)
      } else {
        setMsg('Nothing to save yet')
      }
    } catch (e) { setMsg(apiErrorMessage(e)) } finally { setSaving(false) }
  }
  const update = (cat: string, key: string, value: any) => setData({ ...data, [cat]: { ...data[cat], [key]: value } })

  if (loadError && !data) {
    return (
      <div className="p-8" role="alert">
        <div className="card max-w-lg mx-auto p-8 text-center">
          <div className="text-lg font-semibold">Settings could not be loaded</div>
          <p className="mt-2 text-sm text-zinc-500">{loadError}</p>
          <button onClick={() => void load()} className="mt-4 inline-flex items-center gap-2 px-5 py-2.5 rounded-full bg-blue-600 text-white text-sm font-medium">
            <RefreshCw className="w-4 h-4" /> Try again
          </button>
        </div>
      </div>
    )
  }

  if (!data) return <div className="p-8 mono text-sm" role="status">Loading settings…</div>

  // Auto mode: the switch's editability and the cadence table both come from
  // GET /api/settings (`automation.can_use/cadence/locked_reason`) — the same
  // predicates the PUT gate and the sweep use, so the card can never advertise a
  // schedule the server would refuse.
  const canAuto = !!data.automation?.can_use
  const autoCadence = (data.automation?.cadence || {}) as Record<string, number>
  const assistantState = assistant?.state || (assistant?.configured ? 'unknown' : 'not_configured')

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold flex items-center gap-2"><SettingsIcon className="w-5 h-5" /> Settings <span className="text-xs mono font-normal text-zinc-500">your preferences, your way</span></h1>
        <button onClick={save} disabled={saving} className="px-4 py-2 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 text-sm inline-flex items-center gap-2 disabled:opacity-50"><Save className="w-4 h-4" /> {saving ? 'Saving…' : 'Save changes'}</button>
      </div>
      {msg && <div className="text-sm mono p-3 rounded-xl bg-emerald-50 dark:bg-emerald-950 border border-emerald-200 dark:border-emerald-800 text-emerald-700 dark:text-emerald-300">{msg}</div>}

      <div className="grid lg:grid-cols-2 gap-4">
        {/* Your assistant — plain language, no provider detail. Provider URLs,
            API keys, models and token budgets are owner-level (admin console);
            the backend refuses member writes to that category regardless. */}
        <div className="card p-5 lg:col-span-2 border border-zinc-200 dark:border-zinc-800">
          <h3 className="font-medium flex items-center gap-2"><Bot className="w-5 h-5" /> Your assistant</h3>
          <p className="text-xs text-zinc-500 dark:text-zinc-400 mt-1">
            The assistant reads job descriptions, explains matches, drafts resumes and emails for you.
          </p>
          <div className={`mt-3 p-3 rounded-xl border text-sm flex flex-wrap items-center gap-2 ${
            assistantState === 'online'
              ? 'bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-800 dark:text-emerald-300'
              : assistantState === 'transient_outage'
                ? 'bg-amber-50 border-amber-200 text-amber-700 dark:bg-amber-950 dark:border-amber-800 dark:text-amber-300'
                : 'bg-zinc-50 border-zinc-200 text-zinc-600 dark:bg-zinc-800 dark:border-zinc-700 dark:text-zinc-300'
          }`} data-testid="assistant-card">
            <Sparkles className="w-4 h-4 shrink-0" />
            <span>{assistant?.message || 'Checking your assistant…'}</span>
            {(assistant?.paused_items ?? 0) > 0 && (
              <button onClick={() => void resumePaused()} disabled={resuming}
                      className="ml-auto inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-full bg-amber-600 hover:bg-amber-700 disabled:opacity-60 text-white">
                <RefreshCw className={`w-3.5 h-3.5 ${resuming ? 'animate-spin' : ''}`} />
                {resuming ? 'Resuming…' : 'Resume my paused work'}
              </button>
            )}
          </div>
          {owner ? (
            <Link to="/admin" className="mt-3 inline-flex items-center gap-1.5 text-xs px-3 py-2 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900">
              <ShieldCheck className="w-3.5 h-3.5" /> Configure AI providers in the admin console <ArrowRight className="w-3 h-3" />
            </Link>
          ) : (
            <div className="mt-3 text-[11px] mono text-zinc-500 dark:text-zinc-400">
              AI providers, models and budgets are managed by the workspace owner. If your assistant seems stuck, ask them to check the admin console.
            </div>
          )}
        </div>

        {/* Anchor for the empty-board banner's "Settings → Sources" link: the
            source configuration is the fix for `no_sources_configured`. */}
        <div className="card p-5" id="scraping">
          <h3 className="font-medium flex items-center gap-2"><Search className="w-4 h-4" /> Job search</h3>
          <div className="mt-3 space-y-3">
            <div>
              <label className="text-xs mono font-medium">Extracted from your resume (read-only)</label>
              <div className="mt-1 min-h-[38px] border rounded-xl px-3 py-2 bg-zinc-50 dark:bg-zinc-800 dark:border-zinc-700 flex flex-wrap gap-1">
                {(extracted?.keywords || []).length === 0
                  ? <span className="text-xs text-zinc-500 mono">Nothing yet — upload a master resume and these fill in automatically.</span>
                  : (extracted.keywords || []).map((k: string) => <span key={k} className="text-[11px] px-2 py-0.5 rounded-full bg-white dark:bg-zinc-900 border dark:border-zinc-700 mono">{k}</span>)}
              </div>
            </div>
            <div>
              <label className="text-xs mono">Extra search keywords (comma-separated, optional)</label>
              <input value={Array.isArray(data.scraping.keywords) ? data.scraping.keywords.join(', ') : (data.scraping.keywords || '')} onChange={e => update('scraping', 'keywords', e.target.value.split(',').map((s: string) => s.trim()).filter(Boolean))} placeholder="empty by default — add terms your resume does not cover" className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700" />
              <div className="text-[11px] mono text-zinc-500 mt-1">Added <em>on top of</em> the extracted keywords. This starts empty on purpose — nothing generic is prefilled.</div>
            </div>
            <div><label className="text-xs mono">Only show jobs posted within (hours)</label><input type="number" value={data.scraping.freshness_hours} onChange={e => update('scraping', 'freshness_hours', Number(e.target.value))} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700" /></div>
            <div><label className="text-xs mono">Your sources</label><div className="flex flex-wrap gap-1 mt-1">{(data.scraping.sources || []).map((s: string) => <span key={s} className="text-xs px-2 py-1 rounded-full bg-zinc-100 dark:bg-zinc-800 mono">{s}</span>)}</div></div>
            <label className="flex items-center gap-2 text-sm pt-1"><input type="checkbox" checked={!!data.scraping.live_enabled} onChange={e => update('scraping', 'live_enabled', e.target.checked)} /> Include live public job boards</label>
            <div className="text-[11px] mono text-zinc-500 leading-relaxed">When on, we also look at real postings from public job APIs on top of your curated pool.</div>
          </div>
        </div>

        <div className="card p-5">
          <h3 className="font-medium flex items-center gap-2"><TrendingUp className="w-4 h-4" /> Funding Radar</h3>
          <p className="text-xs mono text-zinc-500 mt-1">We auto-extract focus areas from your profile. Add extra focus here (optional).</p>
          <div className="mt-3 space-y-3">
            <div><label className="text-xs mono">Context notes</label><textarea value={data.funding?.context_notes || ''} onChange={e => update('funding', 'context_notes', e.target.value)} rows={2} placeholder="e.g. AI infrastructure, fintech in India" className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700" /></div>
            <div><label className="text-xs mono">Industries to prioritize (optional)</label><input value={data.funding?.industries || ''} onChange={e => update('funding', 'industries', e.target.value)} placeholder="ai/ml, fintech" className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700" /></div>
          </div>
        </div>

        <div className="card p-5">
          <h3 className="font-medium flex items-center gap-2"><Sliders className="w-4 h-4" /> Resumes & approvals</h3>
          <div className="mt-3 space-y-3">
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={data.general.strict_skeleton} onChange={e => update('general', 'strict_skeleton', e.target.checked)} /> Keep my resume layout exactly as uploaded</label>
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={data.general.auto_approve_email} onChange={e => update('general', 'auto_approve_email', e.target.checked)} /> Send emails without showing me each draft first</label>
            <div><label className="text-xs mono">Default resume format</label><select value={data.general.default_resume_format} onChange={e => update('general', 'default_resume_format', e.target.value)} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700"><option value="ai_generated">Tailored by the assistant</option><option value="user_skeleton">My own layout</option></select></div>
          </div>
        </div>

        <div className="card p-5">
          <h3 className="font-medium flex items-center gap-2"><Mail className="w-4 h-4" /> Email account</h3>
          <div className="mt-3 space-y-3">
            <div><label className="text-xs mono">Outgoing mail server</label><input value={data.email.host || ''} onChange={e => update('email', 'host', e.target.value)} placeholder="smtp.gmail.com" className="w-full mt-1 border rounded-xl px-3 py-2 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700" /></div>
            <div className="grid grid-cols-2 gap-2"><div><label className="text-xs mono">Port</label><input type="number" value={data.email.port || 587} onChange={e => update('email', 'port', Number(e.target.value))} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700" /></div><div><label className="text-xs mono">Username (your email)</label><input value={data.email.username || ''} onChange={e => update('email', 'username', e.target.value)} className="w-full mt-1 border rounded-xl px-3 py-2 text-sm bg-white dark:bg-zinc-900 dark:border-zinc-700" /></div></div>
            <div className="text-[11px] mono text-zinc-500">Two-factor providers are supported — we ask for a one-time code when your provider requires it.</div>
          </div>
        </div>

        <div className="card p-5">
          <h3 className="font-medium flex items-center gap-2"><Zap className="w-4 h-4" /> Where the assistant helps</h3>
          <div className="mt-3 grid md:grid-cols-2 gap-3">
            {Object.entries(data.workflows || {}).map(([k, v]: any) => (
              <label key={k} className="flex items-center gap-2 text-sm p-3 rounded-xl border dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-800"><input type="checkbox" checked={!!v} onChange={e => update('workflows', k, e.target.checked)} /><span className="mono text-xs">{k.replace(/_/g, ' ')}</span></label>
            ))}
          </div>
          <div className="mt-3 text-[11px] mono text-zinc-500">Turn off a step if you would rather always do it yourself.</div>
        </div>

        {/* Assisted apply — two explicit opt-ins (default off, server-capped):
            account creation with vault passwords, and AI field mapping. */}
        <div className="card p-5">
          <h3 className="font-medium flex items-center gap-2"><ShieldCheck className="w-5 h-5" /> Assisted apply</h3>
          <div className="mt-3 space-y-3">
            <label className="flex items-start gap-2 text-sm">
              <input type="checkbox" className="mt-0.5" disabled={data.browser?.create_accounts_available === false}
                     checked={!!data.browser?.create_accounts}
                     onChange={e => update('browser', 'create_accounts', e.target.checked)} />
              <span>
                Create portal accounts &amp; fill passwords for me
                <span className="block text-[11px] mono text-zinc-500 mt-1 leading-relaxed">
                  On a sign-up page (Lever, Greenhouse, Workday, Ashby, Workable, SmartRecruiters,
                  etc.) the assistant generates a unique password in your vault, creates the
                  account and types the password straight into both &ldquo;new password&rdquo; and
                  &ldquo;confirm password&rdquo; fields. On a sign-in page it fills the stored vault
                  credential. The login lands in your Vault automatically — export any of them as a
                  Chrome/Apple/1Password CSV to sign in yourself in another browser. Off by default;
                  MFA codes and bot checks always wait for you. Cross-site redirects (a job board
                  sending you to the company ATS, or an ATS redirecting to its auth host) are
                  followed inside known ATS families.
                </span>
              </span>
            </label>
            <label className="flex items-start gap-2 text-sm">
              <input type="checkbox" className="mt-0.5" disabled={data.browser?.ai_assist_available === false}
                     checked={!!data.browser?.ai_assist}
                     onChange={e => update('browser', 'ai_assist', e.target.checked)} />
              <span>
                Let AI map unfamiliar form fields
                <span className="block text-[11px] mono text-zinc-500 mt-1 leading-relaxed">
                  Higher accuracy on fields the rules do not recognise. Needs the assistant&apos;s AI
                  configured — while this is on and AI is unreachable, session start reports the
                  outage instead of guessing; turn it off to run on heuristics alone.
                </span>
              </span>
            </label>
            {data.browser?.create_accounts_available === false || data.browser?.ai_assist_available === false ? (
              <div className="text-[11px] mono text-zinc-500">One or both options are disabled on this deployment.</div>
            ) : null}
          </div>
        </div>

        {/* Auto mode — scheduled work (v2.2). Switch, cadence, clocks, last runs. */}
        <div className="card p-5 lg:col-span-2">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h3 className="font-medium flex items-center gap-2">
                <CalendarClock className="w-4 h-4" /> Auto mode
                {auto && (
                  <span className={`text-[11px] mono px-2 py-0.5 rounded-full border ${!auto.enabled ? 'bg-zinc-100 border-zinc-300 text-zinc-600' : auto.active ? 'bg-emerald-50 border-emerald-200 text-emerald-700' : 'bg-amber-50 border-amber-200 text-amber-800'}`}>
                    {!auto.enabled ? 'off' : auto.active ? 'scheduled' : 'on — not queueing right now'}
                  </span>
                )}
              </h3>
              <p className="text-xs mono text-zinc-500 mt-1 max-w-3xl leading-relaxed">
                Job discovery, the funding radar and application prep run on a schedule instead of waiting for a click. Auto mode only decides <em>when to start</em>: everything still passes through your approvals — nothing is ever submitted to an employer without you.
              </p>
            </div>
            <label className={`shrink-0 flex items-center gap-2 text-sm p-3 rounded-xl border ${canAuto ? 'bg-white dark:bg-zinc-900 cursor-pointer' : 'bg-zinc-50 dark:bg-zinc-800'}`} title={canAuto ? 'Starts work on the cadence below' : (data.automation?.locked_reason || 'Locked on your plan')}>
              <input type="checkbox" checked={!!data.automation?.auto_mode} disabled={!canAuto || autoBusy} onChange={e => void setAutoMode(e.target.checked)} />
              <span className="mono text-xs">{autoBusy ? 'Saving…' : canAuto ? (data.automation?.auto_mode ? 'On' : 'Off') : 'Locked'}</span>
            </label>
          </div>

          {autoNote && (
            <div role="alert" className="mt-3 p-3 rounded-xl border border-red-300 bg-red-50 dark:bg-red-950 dark:border-red-800 text-red-700 dark:text-red-300 text-xs mono">{autoNote}</div>
          )}
          {!canAuto && (
            <div className="mt-2 text-[11px] mono text-zinc-500">
              Auto mode is a Pro feature — <Link to="/billing" className="underline font-medium">see plans</Link> for scheduled runs (discovery every 12 h, funding radar every 24 h).
            </div>
          )}
          {auto && canAuto && !auto.consent_ok && (
            <div className="mt-2 p-3 rounded-xl border border-amber-200 bg-amber-50 dark:bg-amber-950 dark:border-amber-800 text-amber-800 dark:text-amber-200 text-xs mono">
              The automation disclosure has not been accepted, so nothing is queued even with the switch on. Accept it in <Link to="/account" className="underline font-medium">Account → Consent</Link>.
            </div>
          )}
          <AIStatusBanner status={assistant ? { state: assistant.state, reason: assistant.reason, hint: assistant.hint, paused_count: assistant.paused_items } : null} owner={owner} />
          {auto && auto.enabled && !auto.active && auto.blocking?.length ? (
            <div className="mt-2 text-[11px] mono text-amber-700 dark:text-amber-300">
              Not queueing right now: {auto.blocking.map(code => AUTO_BLOCKERS[code] || code).join(' · ')}.
            </div>
          ) : null}
          {auto && auto.enabled && !auto.schedulable && (
            <div className="mt-2 text-[11px] mono text-zinc-500">Scheduled runs are disabled on this server, so nothing runs automatically.</div>
          )}

          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-left text-xs mono">
              <thead className="text-zinc-500">
                <tr>
                  <th className="py-1 pr-3 font-medium">Step</th>
                  <th className="py-1 pr-3 font-medium">Runs every</th>
                  <th className="py-1 pr-3 font-medium">Last run</th>
                  <th className="py-1 font-medium">Next</th>
                </tr>
              </thead>
              <tbody>
                {Object.keys(AUTO_WORKFLOW_LABELS).map(wf => {
                  const secs = autoCadence[wf]
                  const last = auto?.last_run?.[wf] || null
                  const next = auto?.next_run?.[wf] ?? null
                  const chip = autoStateCopy(last?.state)
                  const toggleOff = !!auto && wf in (auto.toggles || {}) && auto.toggles[wf] === false
                  return (
                    <tr key={wf} className="border-t dark:border-zinc-800 align-top">
                      <td className="py-1.5 pr-3">
                        {AUTO_WORKFLOW_LABELS[wf]}
                        {toggleOff ? <div className="text-[10px] text-zinc-500">turned off above — never scheduled</div> : null}
                      </td>
                      <td className="py-1.5 pr-3">{fmtCadence(secs)}</td>
                      <td className="py-1.5 pr-3">
                        {last ? (
                          <div className="flex flex-col gap-0.5">
                            <span className={`self-start text-[10px] px-1.5 py-0.5 rounded-full border ${chip?.cls || 'bg-zinc-100 border-zinc-300 text-zinc-700'}`}>{chip?.label || String(last.state)}</span>
                            <span className="text-[10px] text-zinc-500">{fmtWhen(last.at)}</span>
                            {last.reason ? <span className="text-[10px] text-zinc-500 break-words">{String(last.reason).slice(0, 120)}</span> : null}
                          </div>
                        ) : <span className="text-zinc-500">not yet</span>}
                      </td>
                      <td className="py-1.5">
                        {next
                          ? (new Date(next).getTime() <= Date.now() ? <span title={fmtWhen(next)}>due — starts shortly</span> : fmtWhen(next))
                          : <span className="text-zinc-500">{!auto?.enabled ? '—' : auto.active ? 'running now' : 'blocked — see above'}</span>}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          {auto?.quota && (
            <div className="mt-3 text-[11px] mono text-zinc-500">
              Automation runs this month: <strong className="text-inherit">{auto.quota.used}{auto.quota.limit ? `/${auto.quota.limit}` : ''}</strong>
              {auto.quota.limit ? ` · ${auto.quota.remaining} left` : ' · no cap on this plan'}
              {' '}— resets with the calendar month.
            </div>
          )}

          <div className="mt-4">
            <div className="text-xs mono font-medium">Recent scheduled runs</div>
            {!auto || !auto.recent?.length ? (
              <div className="mt-1 text-[11px] mono text-zinc-500">
                Nothing scheduled yet{auto?.enabled ? ' — the next run starts within a few minutes and appears here.' : ' — turn auto mode on to start.'}
              </div>
            ) : (
              <ul className="mt-1 space-y-1">
                {auto.recent.slice(0, 5).map((row, i) => {
                  const chip = autoStateCopy(row.state)
                  return (
                    <li key={`${row.workflow}-${row.at}-${i}`} className="flex flex-wrap items-center gap-2 text-[11px] mono p-2 rounded-lg border dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-800/50">
                      <span className={`px-1.5 py-0.5 rounded-full border ${chip?.cls || 'bg-zinc-100 border-zinc-300 text-zinc-700'}`}>{chip?.label || String(row.state)}</span>
                      <span>{AUTO_WORKFLOW_LABELS[row.workflow || ''] || row.workflow || 'Scheduled run'}</span>
                      <span className="text-zinc-500">{fmtWhen(row.at)}</span>
                      {row.queue_id ? <Link to="/queues" className="underline text-zinc-600 dark:text-zinc-300">view in My Queue</Link> : null}
                      {row.job_id ? <Link to="/jobs" className="underline text-zinc-600 dark:text-zinc-300">view job</Link> : null}
                      {row.state !== 'done' && chip?.note ? <span className="text-zinc-500">{chip.note}</span> : null}
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
