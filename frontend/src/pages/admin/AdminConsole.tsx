import { useEffect, useRef, useState } from 'react'
import client, { apiError } from '../../api/client'
import {
  Activity, AlertTriangle, Bot, CheckCircle, Cpu, Database, DollarSign, Eye, EyeOff, Gauge, Globe, Key,
  Layers, RefreshCw, Save, ShieldCheck, ToggleRight, TrendingUp, Zap,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import {
  budgetValue,
  overrideSaveError,
  overrideSavePayload,
  workflowCatalog,
  type WorkflowOption,
  type WorkflowOverrideDraft,
} from '../../lib/aiConfig'
import { WorkflowOverrides } from '../../components/WorkflowOverrides'
import { EmptyState, ErrorState, LoadingBlock, SectionCard, SectionSkeleton } from '../../components/states'

/**
 * The owner console (v2.3) — infrastructure and controls, deliberately out of
 * the end-user surface. Everything here is server-enforced owner-only:
 * `GET /api/admin/overview`, `GET /api/ops/status`, `GET /api/settings/ai/status`,
 * `GET|POST /api/ai/config` and `PUT /api/admin/flags` all 403 a member token.
 *
 * Sections: AI provider configuration (default + per-workflow overrides + token
 * budgets), the local decision engine (Laya) routing, source health, queue
 * health, usage & cost, failure rates, application-automation health, the
 * shared job pool (live corpus + aggregates for postings retention has already
 * deleted), and the global feature flags. The two diagnostics that need their
 * own page live at `/admin/ai`: the exact request sent for every AI call and
 * the decision engine's health (how Laya is doing).
 */

type Overview = any

export function FeatureFlagsCard({ flags, onChanged }: { flags: any[]; onChanged: () => void }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const toggle = async (key: string, value: boolean) => {
    setBusy(key); setError(null)
    try {
      await client.put('/api/admin/flags', { [key]: value })
      onChanged()
    } catch (e) {
      setError(apiError(e))
    } finally {
      setBusy(null)
    }
  }

  if (!flags.length) {
    return <EmptyState title="No feature flags registered" hint="Flags appear here when the backend registers them." />
  }
  return (
    <div data-testid="feature-flags">
      {error && <div role="alert" className="mb-2 text-xs mono p-2 rounded-lg bg-red-50 border border-red-200 text-red-700">{error}</div>}
      <ul className="space-y-2">
        {flags.map(flag => (
          <li key={flag.key} className="flex items-start justify-between gap-3 p-3 rounded-xl border dark:border-zinc-800">
            <div className="min-w-0">
              <div className="text-sm font-medium">{flag.label}</div>
              <div className="text-xs text-zinc-500 dark:text-zinc-400">{flag.description}</div>
              <div className="text-[10px] mono text-zinc-400 mt-1">{flag.key}{flag.overridden ? ' • overridden from default' : ` • default ${flag.default ? 'on' : 'off'}`}</div>
            </div>
            <label className="shrink-0 flex items-center gap-2 text-sm p-2 rounded-xl border dark:border-zinc-700 cursor-pointer bg-white dark:bg-zinc-900">
              <input
                type="checkbox"
                checked={!!flag.enabled}
                disabled={busy === flag.key}
                onChange={e => void toggle(flag.key, e.target.checked)}
                aria-label={`Toggle ${flag.label}`}
              />
              <span className="mono text-xs">{busy === flag.key ? 'Saving…' : flag.enabled ? 'On' : 'Off'}</span>
              <ToggleRight className={`w-4 h-4 ${flag.enabled ? 'text-emerald-600' : 'text-zinc-300 dark:text-zinc-600'}`} />
            </label>
          </li>
        ))}
      </ul>
    </div>
  )
}

export default function AdminConsole() {
  const [overview, setOverview] = useState<Overview | null>(null)
  const [ops, setOps] = useState<any>(null)
  const [aiStatus, setAiStatus] = useState<any>(null)
  const [settings, setSettings] = useState<any>(null)
  const [wfConfig, setWfConfig] = useState<Record<string, WorkflowOverrideDraft>>({})
  const [workflows, setWorkflows] = useState<WorkflowOption[]>(() => workflowCatalog({}))
  const [savedOverrideIds, setSavedOverrideIds] = useState<string[]>([])
  const [flags, setFlags] = useState<any[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [notice, setNotice] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [showKey, setShowKey] = useState(false)
  const [aiSaving, setAiSaving] = useState(false)
  const [busyOp, setBusyOp] = useState<string | null>(null)
  const [wfBusy, setWfBusy] = useState<string | null>(null)
  const [aiForm, setAiForm] = useState({ base_url: '', model: '', api_key: '', rpm: 60, timeout: 300, max_input_tokens: 0, max_output_tokens: 0, provider: 'openai_compatible' })
  const [layaForm, setLayaForm] = useState({ mode: 'auto', for_ranking: true, for_classification: true, for_field_mapping: true })
  const [layaSaving, setLayaSaving] = useState(false)
  const mounted = useRef(true)

  const load = async () => {
    try {
      const [ov, op, st, cf, fl] = await Promise.allSettled([
        client.get('/api/admin/overview'),
        client.get('/api/ops/status'),
        client.get('/api/settings/ai/status'),
        client.get('/api/ai/config'),
        client.get('/api/admin/flags'),
      ])
      if (!mounted.current) return
      if (ov.status === 'fulfilled') setOverview(ov.value.data)
      if (op.status === 'fulfilled') setOps(op.value.data)
      if (st.status === 'fulfilled') setAiStatus(st.value.data)
      if (cf.status === 'fulfilled') {
        const data = cf.value.data || {}
        const overrides = data.overrides && typeof data.overrides === 'object' && !Array.isArray(data.overrides)
          ? data.overrides as Record<string, WorkflowOverrideDraft>
          : {}
        setWorkflows(workflowCatalog(data))
        setWfConfig(overrides)
        setSavedOverrideIds(Object.keys(overrides))
      }
      if (fl.status === 'fulfilled') setFlags(fl.value.data.flags || [])
      const settingsResponse = await client.get('/api/settings')
      if (!mounted.current) return
      const data = settingsResponse.data
      setSettings(data)
      setError(null)
      if (data?.ai) {
        setAiForm({
          base_url: data.ai.base_url || st.status === 'fulfilled' && st.value.data?.user_config?.base_url || 'https://api.openai.com/v1',
          model: data.ai.model || st.status === 'fulfilled' && st.value.data?.user_config?.model || 'gpt-4o-mini',
          api_key: '',
          rpm: data.ai.rpm || 60,
          timeout: data.ai.timeout || 300,
          max_input_tokens: budgetValue(data.ai.max_input_tokens, data.ai.platform_max_input_tokens ?? 0),
          max_output_tokens: budgetValue(data.ai.max_output_tokens, data.ai.platform_max_output_tokens ?? 0),
          provider: data.ai.provider || 'openai_compatible',
        })
      }
      if (data?.laya) {
        setLayaForm({
          mode: data.laya.mode || 'auto',
          for_ranking: data.laya.for_ranking !== false,
          for_classification: data.laya.for_classification !== false,
          for_field_mapping: data.laya.for_field_mapping !== false,
        })
      }
    } catch (e) {
      if (mounted.current) setError(apiError(e, 'The admin console could not be loaded.'))
    } finally {
      if (mounted.current) setLoading(false)
    }
  }

  useEffect(() => {
    mounted.current = true
    void load()
    return () => { mounted.current = false }
  }, [])

  const flash = (kind: 'ok' | 'err', text: string, clearMs = 0) => {
    setNotice({ kind, text })
    if (clearMs > 0) {
      window.setTimeout(() => {
        setNotice(current => current?.text === text ? null : current)
      }, clearMs)
    }
  }

  const saveAiDefault = async () => {
    setAiSaving(true); setNotice(null)
    try {
      if (!aiForm.base_url || !aiForm.base_url.startsWith('http')) {
        flash('err', 'base_url must be a valid URL like https://api.openai.com/v1'); return
      }
      if (!aiForm.model || aiForm.model.length < 2) {
        flash('err', 'model must be at least 2 chars like gpt-4o-mini'); return
      }
      const timeoutSecs = Number(aiForm.timeout) || 300
      if (timeoutSecs < 5 || timeoutSecs > 1800) {
        flash('err', 'timeout must be between 5 and 1800 seconds'); return
      }
      const payload: any = {
        ai: {
          base_url: aiForm.base_url,
          model: aiForm.model,
          rpm: Number(aiForm.rpm) || 60,
          timeout: timeoutSecs,
          // 0 = unlimited (provider default) and is sent as 0.
          max_input_tokens: budgetValue(aiForm.max_input_tokens, settings?.ai?.platform_max_input_tokens ?? 0),
          max_output_tokens: budgetValue(aiForm.max_output_tokens, settings?.ai?.platform_max_output_tokens ?? 0),
          provider: aiForm.provider,
        },
      }
      if (aiForm.api_key && aiForm.api_key.trim()) payload.ai.api_key = aiForm.api_key.trim()
      await client.put('/api/settings', payload)
      flash('ok', 'AI provider saved', 4000)
      setAiForm({ ...aiForm, api_key: '' })
      void load()
    } catch (e) { flash('err', apiError(e)) } finally { setAiSaving(false) }
  }

  const patchWf = (wf: string, patch: Partial<WorkflowOverrideDraft>) => {
    setWfConfig(prev => ({ ...prev, [wf]: { ...(prev[wf] || {}), ...patch } }))
  }

  const saveLaya = async () => {
    setLayaSaving(true); setNotice(null)
    try {
      await client.put('/api/settings', { laya: { ...layaForm } })
      flash('ok', 'Decision engine routing saved', 4000)
      void load()
    } catch (e) { flash('err', apiError(e)) } finally { setLayaSaving(false) }
  }

  const saveWf = async (wf: string) => {
    const draft = wfConfig[wf] || {}
    const problem = overrideSaveError(draft)
    if (problem) {
      flash('err', problem)
      return
    }
    setWfBusy(wf)
    setNotice(null)
    try {
      // Blank api_key means "keep the stored key" — the API never returns the full key.
      await client.post('/api/ai/config', { [wf]: overrideSavePayload(draft) })
      flash('ok', `Saved override for "${wf}"`, 4000)
      await load()
    } catch (e) { flash('err', apiError(e)) } finally { setWfBusy(null) }
  }

  const clearWf = async (wf: string) => {
    setWfBusy(wf)
    setNotice(null)
    try {
      await client.delete(`/api/ai/config/${encodeURIComponent(wf)}`)
      flash('ok', `Cleared override for "${wf}"`, 4000)
      await load()
    } catch (e) { flash('err', apiError(e)) } finally { setWfBusy(null) }
  }

  const queueOp = async (path: string, key: string) => {
    setBusyOp(key)
    try {
      await client.post(path)
      flash('ok', key === 'recover' ? 'Stalled queue items recovered' : 'Dead items re-queued', 3000)
      void load()
    } catch (e) { flash('err', apiError(e)) } finally { setBusyOp(null) }
  }

  if (loading) {
    return (
      <div className="space-y-4" data-testid="admin-loading">
        <div className="h-8 w-72 rounded-lg bg-zinc-100 dark:bg-zinc-800 animate-pulse" />
        <SectionSkeleton rows={6} />
        <LoadingBlock label="Reading platform health…" />
      </div>
    )
  }

  if (error && !overview) {
    return (
      <div className="p-8" data-testid="admin-error">
        <div className="card max-w-lg mx-auto p-8">
          <ErrorState message={error} onRetry={() => void load()} />
        </div>
      </div>
    )
  }

  const queues = overview?.queues ?? {}
  const usage = overview?.usage_cost ?? {}
  const failures = overview?.failure_rates ?? {}
  const automation = overview?.automation_health ?? {}
  const accounts = overview?.accounts ?? {}
  const sources = overview?.sources ?? {}
  // Shared job pool (v2.3): the cross-user corpus's size, and — for postings
  // retention has already dropped — aggregate counters only. Owner-only by
  // construction: the endpoint behind it requires the owner role.
  const pool = overview?.job_pool ?? {}

  return (
    <div className="space-y-6" data-testid="admin-console">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold flex items-center gap-2"><ShieldCheck className="w-5 h-5" /> Admin console</h1>
          <p className="text-sm text-zinc-500 dark:text-zinc-400 mt-0.5">
            Platform health and controls — only you (the workspace owner) can see this.
          </p>
        </div>
        <div className="flex gap-2">
          <Link to="/admin/ai" className="text-xs px-3 py-2 rounded-full border dark:border-zinc-700">AI log</Link>
          <Link to="/admin/audit" className="text-xs px-3 py-2 rounded-full border dark:border-zinc-700">Audit log</Link>
          <Link to="/admin/plans" className="text-xs px-3 py-2 rounded-full border dark:border-zinc-700">Plans & users</Link>
        </div>
      </div>
      {notice && (
        <div role={notice.kind === 'err' ? 'alert' : 'status'} data-testid="admin-notice"
             className={`text-sm mono p-3 rounded-xl border ${notice.kind === 'err'
               ? 'bg-red-50 dark:bg-red-950 border-red-200 dark:border-red-800 text-red-700 dark:text-red-300'
               : 'bg-emerald-50 dark:bg-emerald-950 border-emerald-200 dark:border-emerald-800 text-emerald-700 dark:text-emerald-300'}`}>
          {notice.text}
        </div>
      )}

      {/* ── AI provider configuration ─────────────────────────────────────── */}
      <SectionCard title="AI provider configuration" icon={Bot}
                   aside={<span className="text-[11px] px-2 py-0.5 rounded-full bg-amber-500 text-white">Owner • global default</span>}>
        {aiStatus && (
          <div className={`p-3 rounded-xl border text-xs mono flex flex-wrap items-center gap-2 ${aiStatus.online ? 'bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-800 dark:text-emerald-300' : 'bg-amber-50 border-amber-200 text-amber-700 dark:bg-amber-950 dark:border-amber-800 dark:text-amber-300'}`}>
            {aiStatus.online ? <CheckCircle className="w-4 h-4" /> : <AlertTriangle className="w-4 h-4" />}
            <span>
              {aiStatus.online
                ? `Online • ${aiStatus.latency_ms ?? '—'}ms • ${aiStatus.model}`
                : `Offline • ${aiStatus.reason || 'no_api_key'}${aiStatus.detail ? ` — ${aiStatus.detail}` : ''}`}
            </span>
            {aiStatus.key_preview && (
              <span className="px-2 py-0.5 rounded-full bg-white/60 dark:bg-zinc-800/60">key {aiStatus.key_preview} · {aiStatus.key_source || 'env'}</span>
            )}
          </div>
        )}
        {aiStatus?.stored_key_error && (
          <div className="mt-2 p-3 rounded-xl border border-red-300 bg-red-50 dark:bg-red-950 dark:border-red-800 text-red-700 dark:text-red-300 text-xs mono">
            Your saved API key can't be decrypted on this server (its ENCRYPTION_KEY changed). Re-enter the key below and save to fix it.
          </div>
        )}
        <div className="mt-3 grid md:grid-cols-3 gap-3">
          <div>
            <label className="text-xs mono font-medium flex items-center gap-1"><Cpu className="w-3 h-3" /> Provider</label>
            <select value={aiForm.provider} onChange={e => setAiForm({ ...aiForm, provider: e.target.value })}
                    className="w-full mt-1 border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700">
              <option value="openai_compatible">OpenAI compatible — OpenAI, Groq, Together, Ollama…</option>
              <option value="google">Google AI Studio (Gemini)</option>
            </select>
          </div>
          <div className="md:col-span-2">
            <label className="text-xs mono font-medium flex items-center gap-1"><Globe className="w-3 h-3" /> Base URL</label>
            <input value={aiForm.base_url} onChange={e => setAiForm({ ...aiForm, base_url: e.target.value })} placeholder="https://api.openai.com/v1"
                   className="w-full mt-1 border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700" />
          </div>
          <div>
            <label className="text-xs mono font-medium flex items-center gap-1"><Cpu className="w-3 h-3" /> Model</label>
            <input value={aiForm.model} onChange={e => setAiForm({ ...aiForm, model: e.target.value })} placeholder="gpt-4o-mini"
                   className="w-full mt-1 border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700" />
          </div>
          <div>
            <label className="text-xs mono font-medium flex items-center gap-1"><Key className="w-3 h-3" /> API key (encrypted at rest)</label>
            <div className="relative mt-1">
              <input value={aiForm.api_key} onChange={e => setAiForm({ ...aiForm, api_key: e.target.value })} type={showKey ? 'text' : 'password'}
                     placeholder={settings?.ai?.api_key_set ? '•••• (set) — leave blank to keep' : 'sk-…'}
                     className="w-full border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700 pr-10" />
              <button onClick={() => setShowKey(!showKey)} className="absolute right-2 top-1/2 -translate-y-1/2 p-1.5 rounded-full hover:bg-zinc-100 dark:hover:bg-zinc-800" aria-label={showKey ? 'Hide key' : 'Show key'}>
                {showKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2 items-end">
            <div><label className="text-xs mono">RPM</label><input type="number" value={aiForm.rpm} onChange={e => setAiForm({ ...aiForm, rpm: Number(e.target.value) })} className="w-full mt-1 border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700" /></div>
            <div><label className="text-xs mono">Timeout (s)</label><input type="number" min={5} max={1800} value={aiForm.timeout} onChange={e => setAiForm({ ...aiForm, timeout: Number(e.target.value) })} className="w-full mt-1 border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700" /></div>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="text-xs mono flex items-center gap-1.5">Max input tokens
                {aiForm.max_input_tokens === 0 && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-violet-100 dark:bg-violet-950 text-violet-600 dark:text-violet-300">Unlimited</span>}
              </label>
              <input type="number" min={0} max={200000} value={aiForm.max_input_tokens} disabled={!settings?.ai?.token_limits_editable}
                     onChange={e => setAiForm({ ...aiForm, max_input_tokens: e.target.value === '' ? '' : Number(e.target.value) } as any)}
                     className="w-full mt-1 border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700 disabled:opacity-50" />
            </div>
            <div>
              <label className="text-xs mono flex items-center gap-1.5">Max output tokens
                {aiForm.max_output_tokens === 0 && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-violet-100 dark:bg-violet-950 text-violet-600 dark:text-violet-300">Unlimited</span>}
              </label>
              <input type="number" min={0} max={16000} value={aiForm.max_output_tokens} disabled={!settings?.ai?.token_limits_editable}
                     onChange={e => setAiForm({ ...aiForm, max_output_tokens: e.target.value === '' ? '' : Number(e.target.value) } as any)}
                     className="w-full mt-1 border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700 disabled:opacity-50" />
            </div>
          </div>
          <div className="flex items-end">
            <button onClick={saveAiDefault} disabled={aiSaving} className="w-full px-4 py-2.5 rounded-full bg-blue-600 text-white text-sm font-medium inline-flex items-center justify-center gap-2 disabled:opacity-50">
              <Save className="w-4 h-4" /> {aiSaving ? 'Saving…' : 'Save AI default'}
            </button>
          </div>
        </div>
        <div className="text-[11px] mono text-zinc-500 mt-2">
          Members inherit this default (key, model, budgets) unless they never see it — their requests simply use it. Per-workflow overrides below win over the default.
        </div>

        <div className="mt-4" data-testid="workflow-overrides">
          <div className="text-xs mono font-medium flex items-center gap-1"><Zap className="w-3.5 h-3.5" /> Per-workflow overrides <span className="text-zinc-400">(optional)</span></div>
          <p className="mt-1 text-[11px] text-zinc-500 dark:text-zinc-400">
            {savedOverrideIds.length === 0
              ? 'No overrides saved yet. Fill in any workflow below — blank fields inherit the default above.'
              : `${savedOverrideIds.length} override${savedOverrideIds.length === 1 ? '' : 's'} saved. Blank fields inherit the default above; leave a key blank to keep the saved one.`}
          </p>
          <WorkflowOverrides
            workflows={workflows}
            drafts={wfConfig}
            savedIds={savedOverrideIds}
            busyId={wfBusy}
            onChange={patchWf}
            onSave={wf => void saveWf(wf)}
            onClear={wf => void clearWf(wf)}
          />
        </div>

      </SectionCard>

      {/* ── Local decision engine (Laya) ──────────────────────────────────── */}
      <SectionCard title="Local decision engine (Laya)" icon={Cpu}
                   aside={
                     <span className={`text-[10px] px-2 py-0.5 rounded-full ${
                       settings?.laya?.installed
                         ? settings?.laya?.platform_enabled
                           ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300'
                           : 'bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300'
                         : 'bg-zinc-200 text-zinc-600 dark:bg-zinc-700 dark:text-zinc-300'}`}
                       data-testid="laya-status">
                       {!settings?.laya?.installed ? 'not installed'
                         : settings?.laya?.platform_enabled ? 'installed' : 'operator-disabled'}
                     </span>
                   }>
        <p className="text-xs text-zinc-500 dark:text-zinc-400 leading-relaxed">
          Laya is a self-hosted <b>typed-decision</b> model (Apache 2.0, one forward pass in
          milliseconds): it answers choice / yes-no / rubric-score questions and can never return
          malformed JSON — the failure mode that made ranking slow and unreliable on the AI gateway.
          It takes the decision-shaped work (job ranking, company-size classification, form-field
          mapping); anything that must <i>write</i> text (resumes, emails) stays on the AI gateway above.
        </p>
        <div className="mt-3 text-[11px] mono text-zinc-500" data-testid="laya-runtime">
          {!settings?.laya?.installed
            ? <>Not installed — <span className="mono">WITH_LAYA=1 ./run.sh</span> or <span className="mono">pip install -r backend/requirements-laya.txt</span> ships it alongside the app.</>
            : !settings?.laya?.platform_enabled
              ? 'The operator turned all Laya routing off (LAYA_ENABLED=false) — the AI gateway answers everything.'
              : `Installed — device ${settings?.laya?.device || 'auto'}, long-document budget ${settings?.laya?.max_len || 8192} tokens.`}
          {settings?.laya?.load_error ? ` Last load error: ${settings.laya.load_error}` : ''}
        </div>
        <div className="mt-3 grid md:grid-cols-2 gap-3">
          <div>
            <label className="text-xs mono font-medium">Routing mode</label>
            <select value={layaForm.mode} disabled={!settings?.laya?.platform_enabled}
                    onChange={e => setLayaForm({ ...layaForm, mode: e.target.value })}
                    className="w-full mt-1 border rounded-xl px-3 py-2.5 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700 disabled:opacity-50">
              <option value="auto">Auto — Laya first, AI escalates low-confidence answers</option>
              <option value="laya_only">Laya only — never fall back to the AI gateway</option>
              <option value="llm_only">AI only — never consult Laya</option>
            </select>
            <div className="mt-1 text-[11px] text-zinc-500 dark:text-zinc-400 leading-relaxed">
              Auto is the safe default: a shrug from the decision engine escalates to the AI gateway,
              and with Laya absent every decision simply goes where it went before.
            </div>
          </div>
          <div className="space-y-2">
            <label className="text-xs mono font-medium">Answer with Laya for</label>
            {([
              ['for_ranking', 'Job ranking & match scoring'],
              ['for_classification', 'Company-size classification (discovery)'],
              ['for_field_mapping', 'Form-field mapping (assisted/auto apply)'],
            ] as const).map(([key, label]) => (
              <label key={key} className="flex items-center gap-2 text-sm p-2 rounded-xl border dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-800">
                <input type="checkbox" checked={(layaForm as any)[key] !== false} disabled={!settings?.laya?.platform_enabled}
                       onChange={e => setLayaForm({ ...layaForm, [key]: e.target.checked })} />
                <span>{label}</span>
              </label>
            ))}
          </div>
          <div className="md:col-span-2">
            <button onClick={() => void saveLaya()} disabled={layaSaving || !settings?.laya?.platform_enabled}
                    className="px-4 py-2.5 rounded-full bg-blue-600 text-white text-sm font-medium inline-flex items-center gap-2 disabled:opacity-50">
              <Save className="w-4 h-4" /> {layaSaving ? 'Saving…' : 'Save decision engine settings'}
            </button>
          </div>
        </div>
      </SectionCard>

      {/* ── Health at a glance ────────────────────────────────────────────── */}
      <div className="grid lg:grid-cols-2 gap-4">
        <SectionCard title="Source health" icon={Globe}>
          {!sources.job_sources?.length ? (
            <EmptyState title="No sources registered" hint="Job sources appear here once the platform registers them." />
          ) : (
            <ul className="space-y-1.5 max-h-56 overflow-auto" data-testid="source-health">
              {sources.job_sources.map((s: any) => (
                <li key={s.id} className="flex items-center justify-between text-xs p-2 rounded-lg bg-zinc-50 dark:bg-zinc-800/60">
                  <span className="mono">{s.id}</span>
                  <span className="flex items-center gap-2">
                    {s.credentialed ? <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-300">key</span> : null}
                    <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${s.available ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300' : 'bg-zinc-200 text-zinc-600 dark:bg-zinc-700 dark:text-zinc-300'}`}>
                      {s.available ? 'available' : 'unavailable'}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          )}
          {sources.funding_providers?.length > 0 && (
            <div className="mt-3 text-[11px] mono text-zinc-500">
              Funding providers: {sources.funding_providers.map((p: any) => `${p.id || p.name || p.provider}: ${p.configured ? 'ok' : 'not configured'}`).join(' • ')}
            </div>
          )}
        </SectionCard>

        <SectionCard title="Queue health" icon={Layers}
                     aside={
                       <span className="flex gap-2">
                         <button onClick={() => void queueOp('/api/ops/queue/recover', 'recover')} disabled={busyOp === 'recover'}
                                 className="text-[11px] px-2 py-1 rounded-full border dark:border-zinc-700 disabled:opacity-50">
                           {busyOp === 'recover' ? '…' : 'Recover stalled'}
                         </button>
                         <button onClick={() => void queueOp('/api/ops/queue/retry-dead', 'retry')} disabled={busyOp === 'retry'}
                                 className="text-[11px] px-2 py-1 rounded-full border dark:border-zinc-700 disabled:opacity-50">
                           {busyOp === 'retry' ? '…' : 'Retry dead'}
                         </button>
                       </span>
                     }>
          {queues.totals ? (
            <>
              <div className="grid grid-cols-3 md:grid-cols-4 gap-2 text-center" data-testid="queue-health">
                {Object.entries(queues.totals).map(([status, n]) => (
                  <div key={status} className="p-2 rounded-xl bg-zinc-50 dark:bg-zinc-800/60">
                    <div className="text-lg font-semibold mono">{n as number}</div>
                    <div className="text-[10px] text-zinc-500">{status}</div>
                  </div>
                ))}
              </div>
              <div className="mt-2 text-xs mono text-zinc-500">
                Pipeline failure rate: <strong>{queues.failure_rate_percent}%</strong>
                {ops?.workers && <> • workers {ops.workers.tasks ? `${ops.workers.tasks.alive}/${ops.workers.tasks.expected}` : `concurrency ${ops.workers.concurrency}`}</>}
              </div>
            </>
          ) : <EmptyState title="Queue stats unavailable" />}
        </SectionCard>

        <SectionCard title="Usage & cost (30 days)" icon={DollarSign}>
          {usage.total_tokens === undefined ? (
            <EmptyState title="No usage recorded yet" hint="AI operations across the workspace appear here with their estimated cost." />
          ) : (
            <div data-testid="usage-cost">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-center">
                <div className="p-2 rounded-xl bg-zinc-50 dark:bg-zinc-800/60"><div className="text-lg font-semibold mono">{usage.total_tokens.toLocaleString()}</div><div className="text-[10px] text-zinc-500">tokens</div></div>
                <div className="p-2 rounded-xl bg-zinc-50 dark:bg-zinc-800/60"><div className="text-lg font-semibold mono">${usage.estimated_cost_usd}</div><div className="text-[10px] text-zinc-500">est. cost</div></div>
                <div className="p-2 rounded-xl bg-zinc-50 dark:bg-zinc-800/60"><div className="text-lg font-semibold mono">{usage.ai_operations}</div><div className="text-[10px] text-zinc-500">AI ops</div></div>
                <div className="p-2 rounded-xl bg-zinc-50 dark:bg-zinc-800/60"><div className="text-lg font-semibold mono">{usage.ai_failure_rate_percent}%</div><div className="text-[10px] text-zinc-500">AI failures</div></div>
              </div>
              {usage.by_workflow_month?.length > 0 && (
                <table className="mt-3 w-full text-xs mono">
                  <thead className="text-zinc-500"><tr><th className="text-left py-1">Workflow</th><th className="text-right">Ops</th><th className="text-right">Tokens</th><th className="text-right">Cost</th></tr></thead>
                  <tbody>
                    {usage.by_workflow_month.map((row: any) => (
                      <tr key={row.workflow} className="border-t dark:border-zinc-800">
                        <td className="py-1">{row.workflow}</td>
                        <td className="text-right">{row.operations}</td>
                        <td className="text-right">{row.tokens.toLocaleString()}</td>
                        <td className="text-right">${row.cost_usd}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </SectionCard>

        <SectionCard title="Failure rates" icon={Activity}>
          <div className="grid grid-cols-3 gap-2 text-center" data-testid="failure-rates">
            <div className="p-3 rounded-xl bg-zinc-50 dark:bg-zinc-800/60"><div className="text-xl font-semibold mono">{failures.pipeline_percent ?? 0}%</div><div className="text-[10px] text-zinc-500">all pipelines</div></div>
            <div className="p-3 rounded-xl bg-zinc-50 dark:bg-zinc-800/60"><div className="text-xl font-semibold mono">{failures.application_percent ?? 0}%</div><div className="text-[10px] text-zinc-500">applications</div></div>
            <div className="p-3 rounded-xl bg-zinc-50 dark:bg-zinc-800/60"><div className="text-xl font-semibold mono">{failures.ai_percent ?? 0}%</div><div className="text-[10px] text-zinc-500">AI calls</div></div>
          </div>
          {ops?.outbound && (
            <div className="mt-3 text-[11px] mono text-zinc-500">
              Outbound cache: {ops.outbound.http_cache?.entries ?? 0} entries • DNS {ops.outbound.dns_cache?.entries ?? 0} entries
            </div>
          )}
        </SectionCard>

        <SectionCard title="Application automation health" icon={TrendingUp}>
          <div className="grid grid-cols-3 gap-2 text-center">
            <div className="p-3 rounded-xl bg-zinc-50 dark:bg-zinc-800/60"><div className="text-xl font-semibold mono">{automation.active_users ?? 0}</div><div className="text-[10px] text-zinc-500">users with live work</div></div>
            <div className="p-3 rounded-xl bg-zinc-50 dark:bg-zinc-800/60"><div className="text-xl font-semibold mono">{(automation.application?.queued ?? 0) + (automation.application?.processing ?? 0)}</div><div className="text-[10px] text-zinc-500">in flight</div></div>
            <div className="p-3 rounded-xl bg-zinc-50 dark:bg-zinc-800/60"><div className="text-xl font-semibold mono">{automation.failure_rate_percent ?? 0}%</div><div className="text-[10px] text-zinc-500">failure rate</div></div>
          </div>
          <div className="mt-3 text-[11px] mono text-zinc-500">
            Accounts: {accounts.active_users ?? 0} active • {accounts.owners ?? 0} owner(s) • plans {Object.entries(accounts.plans || {}).map(([p, n]) => `${p}: ${n}`).join(' • ') || 'none yet'}
          </div>
        </SectionCard>

        <SectionCard title="Shared job pool" icon={Database}>
          {pool.enabled === false || pool.enabled === undefined ? (
            <EmptyState title="Pool disabled"
                        hint="JOB_POOL_ENABLED=false — discovery reads and writes nothing shared, exactly as before v2.3." />
          ) : (
            <div data-testid="job-pool">
              <div className="grid grid-cols-3 gap-2 text-center">
                <div className="p-3 rounded-xl bg-zinc-50 dark:bg-zinc-800/60">
                  <div className="text-xl font-semibold mono">{pool.live_entries ?? 0}</div>
                  <div className="text-[10px] text-zinc-500">live postings</div>
                </div>
                <div className="p-3 rounded-xl bg-zinc-50 dark:bg-zinc-800/60">
                  <div className="text-xl font-semibold mono">{pool.contributors ?? 0}</div>
                  <div className="text-[10px] text-zinc-500">contributors</div>
                </div>
                <div className="p-3 rounded-xl bg-zinc-50 dark:bg-zinc-800/60">
                  <div className="text-xl font-semibold mono">{pool.distinct_companies ?? 0}</div>
                  <div className="text-[10px] text-zinc-500">companies</div>
                </div>
              </div>
              <div className="mt-3 text-[11px] mono text-zinc-500">
                Retention {pool.retention_days ?? 7}d • added in {pool.window_days ?? 30}d {pool.added_in_window ?? 0}
                {(pool.by_source || []).length > 0 && <> • by source {(pool.by_source as any[]).slice(0, 6).map(x => `${x.source}: ${x.entries}`).join(' • ')}</>}
              </div>
              <div className="mt-3 text-xs font-medium">Gone — aggregated only</div>
              {(pool.gone?.all_time_by_reason || []).length === 0 ? (
                <div className="text-[11px] text-zinc-500 mt-1">Nothing pruned yet.</div>
              ) : (
                <ul className="mt-1 space-y-1 text-[11px] mono text-zinc-500" data-testid="job-pool-gone">
                  {(pool.gone.all_time_by_reason as any[]).map(row => (
                    <li key={row.reason}>
                      {row.reason}: {row.entries} posting(s) • seen {row.times_seen}× • up to {row.max_distinct_users} user(s)
                    </li>
                  ))}
                </ul>
              )}
              {(pool.gone?.companies || []).length > 0 && (
                <div className="mt-2 text-[11px] mono text-zinc-500">
                  Churn by company: {(pool.gone.companies as any[]).slice(0, 5).map(c => `${c.company}: ${c.entries}`).join(' • ')}
                </div>
              )}
              <div className="mt-3 text-[11px] text-zinc-500 dark:text-zinc-400 leading-relaxed">
                Content for pruned or withdrawn postings is deleted — what remains are counters, a hashed
                identity and the public company name, and it is only ever read here.
              </div>
            </div>
          )}
        </SectionCard>

        <SectionCard title="Global feature flags" icon={Gauge}>
          <FeatureFlagsCard flags={flags} onChanged={() => void load()} />
        </SectionCard>
      </div>
    </div>
  )
}
