import { useEffect, useRef, useState } from 'react'
import client, { apiError } from '../../api/client'
import {
  Activity, AlertTriangle, Bot, CheckCircle, Cpu, DollarSign, Eye, EyeOff, Gauge, Globe, Key,
  Layers, RefreshCw, Save, ShieldCheck, ToggleRight, TrendingUp, Zap,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { budgetValue, AI_WORKFLOWS } from '../../lib/aiConfig'
import { EmptyState, ErrorState, LoadingBlock, SectionCard, SectionSkeleton } from '../../components/states'

/**
 * The owner console (v2.3) — infrastructure and controls, deliberately out of
 * the end-user surface. Everything here is server-enforced owner-only:
 * `GET /api/admin/overview`, `GET /api/ops/status`, `GET /api/settings/ai/status`,
 * `GET|POST /api/ai/config` and `PUT /api/admin/flags` all 403 a member token.
 *
 * Sections: AI provider configuration (default + per-workflow overrides + token
 * budgets), source health, queue health, usage & cost, failure rates,
 * application-automation health, and the global feature flags.
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
  const [wfConfig, setWfConfig] = useState<any>({})
  const [flags, setFlags] = useState<any[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [msg, setMsg] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [aiSaving, setAiSaving] = useState(false)
  const [busyOp, setBusyOp] = useState<string | null>(null)
  const [aiForm, setAiForm] = useState({ base_url: '', model: '', api_key: '', rpm: 60, timeout: 300, max_input_tokens: 0, max_output_tokens: 0, provider: 'openai_compatible' })
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
      if (cf.status === 'fulfilled') setWfConfig(cf.value.data.overrides || {})
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

  const saveAiDefault = async () => {
    setAiSaving(true); setMsg('')
    try {
      if (!aiForm.base_url || !aiForm.base_url.startsWith('http')) {
        setMsg('base_url must be a valid URL like https://api.openai.com/v1'); return
      }
      if (!aiForm.model || aiForm.model.length < 2) {
        setMsg('model must be at least 2 chars like gpt-4o-mini'); return
      }
      const timeoutSecs = Number(aiForm.timeout) || 300
      if (timeoutSecs < 5 || timeoutSecs > 1800) {
        setMsg('timeout must be between 5 and 1800 seconds'); return
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
      setMsg('AI provider saved ✓')
      setAiForm({ ...aiForm, api_key: '' })
      setTimeout(() => setMsg(''), 4000)
      void load()
    } catch (e) { setMsg(apiError(e)) } finally { setAiSaving(false) }
  }

  const saveWf = async (wf: string) => {
    setMsg('')
    try {
      await client.post('/api/ai/config', { [wf]: wfConfig[wf] || {} })
      setMsg(`Saved override for "${wf}" ✓`)
      setTimeout(() => setMsg(''), 3000)
      void load()
    } catch (e) { setMsg(apiError(e)) }
  }

  const queueOp = async (path: string, key: string) => {
    setBusyOp(key)
    try {
      await client.post(path)
      setMsg(key === 'recover' ? 'Stalled queue items recovered ✓' : 'Dead items re-queued ✓')
      setTimeout(() => setMsg(''), 3000)
      void load()
    } catch (e) { setMsg(apiError(e)) } finally { setBusyOp(null) }
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
          <Link to="/admin/audit" className="text-xs px-3 py-2 rounded-full border dark:border-zinc-700">Audit log</Link>
          <Link to="/admin/plans" className="text-xs px-3 py-2 rounded-full border dark:border-zinc-700">Plans & users</Link>
        </div>
      </div>
      {msg && <div className="text-sm mono p-3 rounded-xl bg-emerald-50 dark:bg-emerald-950 border border-emerald-200 dark:border-emerald-800 text-emerald-700 dark:text-emerald-300">{msg}</div>}

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

        <div className="mt-4">
          <div className="text-xs mono font-medium flex items-center gap-1"><Zap className="w-3.5 h-3.5" /> Per-workflow overrides <span className="text-zinc-400">(optional)</span></div>
          {Object.keys(wfConfig).length === 0 ? (
            <div className="mt-2 text-[11px] mono text-zinc-500">No overrides set — every workflow uses the default above.</div>
          ) : (
            <div className="mt-2 grid md:grid-cols-2 gap-2 max-h-64 overflow-auto">
              {Object.keys(wfConfig).map(wf => {
                const cfg = wfConfig[wf] || {}
                return (
                  <div key={wf} className="border dark:border-zinc-700 rounded-lg p-2 bg-white dark:bg-zinc-900">
                    <div className="flex items-center justify-between">
                      <span className="text-[11px] mono font-medium">{wf}</span>
                      <button onClick={() => void saveWf(wf)} className="text-[11px] px-2 py-1 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900">Save</button>
                    </div>
                    <div className="mt-1.5 grid grid-cols-4 gap-1.5">
                      <input value={cfg.base_url || ''} onChange={e => setWfConfig({ ...wfConfig, [wf]: { ...cfg, base_url: e.target.value } })} placeholder="Base URL" className="border rounded-lg px-2 py-1 text-[11px] mono bg-white dark:bg-zinc-800 dark:border-zinc-700" />
                      <input value={cfg.model || ''} onChange={e => setWfConfig({ ...wfConfig, [wf]: { ...cfg, model: e.target.value } })} placeholder="Model" className="border rounded-lg px-2 py-1 text-[11px] mono bg-white dark:bg-zinc-800 dark:border-zinc-700" />
                      <input value={cfg.provider || ''} onChange={e => setWfConfig({ ...wfConfig, [wf]: { ...cfg, provider: e.target.value } })} placeholder="Provider" className="border rounded-lg px-2 py-1 text-[11px] mono bg-white dark:bg-zinc-800 dark:border-zinc-700" />
                      <input value={cfg.api_key || ''} onChange={e => setWfConfig({ ...wfConfig, [wf]: { ...cfg, api_key: e.target.value } })} type="password" placeholder="API key" className="border rounded-lg px-2 py-1 text-[11px] mono bg-white dark:bg-zinc-800 dark:border-zinc-700" />
                    </div>
                  </div>
                )
              })}
            </div>
          )}
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

        <SectionCard title="Global feature flags" icon={Gauge}>
          <FeatureFlagsCard flags={flags} onChanged={() => void load()} />
        </SectionCard>
      </div>
    </div>
  )
}
