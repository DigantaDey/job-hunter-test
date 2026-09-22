import { useState } from 'react'
import { Eye, EyeOff, Save } from 'lucide-react'
import {
  AI_PROVIDER_OPTIONS,
  overrideSavePayload,
  workflowLabel,
  type WorkflowOption,
  type WorkflowOverrideDraft,
} from '../lib/aiConfig'

/**
 * Per-workflow provider overrides.
 *
 * Always renders an input row per workflow. The config endpoint only returns
 * workflows that already have a saved override, so keying the form off that
 * map left this section with a "nothing set" sentence and no way to create one.
 */

const inputClass =
  'w-full min-w-0 mt-1 border rounded-xl px-3 py-2 text-sm mono bg-white dark:bg-zinc-900 dark:border-zinc-700 min-h-[44px]'

export function WorkflowOverrides({
  workflows,
  drafts,
  savedIds,
  busyId,
  onChange,
  onSave,
  onClear,
}: {
  workflows: WorkflowOption[]
  drafts: Record<string, WorkflowOverrideDraft>
  savedIds: string[]
  busyId: string | null
  onChange: (workflow: string, patch: Partial<WorkflowOverrideDraft>) => void
  onSave: (workflow: string) => void
  onClear: (workflow: string) => void
}) {
  const [revealed, setRevealed] = useState<string | null>(null)
  const saved = new Set(savedIds)

  return (
    <div className="mt-3 space-y-3" data-testid="workflow-override-list">
      {workflows.map(workflow => {
        const draft = drafts[workflow.id] || {}
        const provider = overrideSavePayload(draft).provider
        const providerKnown = AI_PROVIDER_OPTIONS.some(option => option.value === provider)
        const keySet = Boolean(draft.api_key_set)
        const isSaved = saved.has(workflow.id)
        const busy = busyId === workflow.id
        return (
          <fieldset
            key={workflow.id}
            data-testid={`workflow-override-${workflow.id}`}
            aria-label={`${workflowLabel(workflow.id)} override`}
            className="border dark:border-zinc-700 rounded-xl p-3 bg-white dark:bg-zinc-900 min-w-0"
          >
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="text-sm font-medium">
                {workflowLabel(workflow.id)}
                <span className="ml-2 text-[11px] mono font-normal text-zinc-400">{workflow.id}</span>
                {isSaved && (
                  <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300 align-middle">
                    override saved
                  </span>
                )}
              </div>
              <div className="flex gap-2">
                {isSaved && (
                  <button
                    type="button"
                    onClick={() => onClear(workflow.id)}
                    disabled={busy}
                    aria-label={`Clear override for ${workflow.id}`}
                    className="text-[11px] px-3 py-1.5 rounded-full border dark:border-zinc-700 disabled:opacity-50"
                  >
                    {busy ? '…' : 'Clear'}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => onSave(workflow.id)}
                  disabled={busy}
                  aria-label={`Save override for ${workflow.id}`}
                  className="text-[11px] px-3 py-1.5 rounded-full bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 inline-flex items-center gap-1 disabled:opacity-50"
                >
                  <Save className="w-3 h-3" /> {busy ? 'Saving…' : 'Save'}
                </button>
              </div>
            </div>
            {workflow.description && (
              <p className="mt-1 text-[11px] text-zinc-500 dark:text-zinc-400">{workflow.description}</p>
            )}
            <div className="mt-2 grid sm:grid-cols-2 gap-2">
              <label className="block text-[11px] mono text-zinc-500 min-w-0">
                Base URL
                <input
                  value={draft.base_url || ''}
                  onChange={e => onChange(workflow.id, { base_url: e.target.value })}
                  placeholder="https://api.openai.com/v1"
                  aria-label={`Base URL for ${workflow.id}`}
                  autoComplete="off"
                  spellCheck={false}
                  className={inputClass}
                />
              </label>
              <label className="block text-[11px] mono text-zinc-500 min-w-0">
                Model
                <input
                  value={draft.model || ''}
                  onChange={e => onChange(workflow.id, { model: e.target.value })}
                  placeholder="gpt-4o-mini"
                  aria-label={`Model for ${workflow.id}`}
                  autoComplete="off"
                  spellCheck={false}
                  className={inputClass}
                />
              </label>
              <label className="block text-[11px] mono text-zinc-500 min-w-0">
                Provider
                <select
                  value={provider}
                  onChange={e => onChange(workflow.id, { provider: e.target.value })}
                  aria-label={`Provider for ${workflow.id}`}
                  className={inputClass}
                >
                  {AI_PROVIDER_OPTIONS.map(option => (
                    <option key={option.value || 'inherit'} value={option.value}>{option.label}</option>
                  ))}
                  {!providerKnown && provider && <option value={provider}>{provider}</option>}
                </select>
              </label>
              <label className="block text-[11px] mono text-zinc-500 min-w-0">
                API key
                <span className="relative block">
                  <input
                    value={draft.api_key || ''}
                    onChange={e => onChange(workflow.id, { api_key: e.target.value })}
                    type={revealed === workflow.id ? 'text' : 'password'}
                    placeholder={keySet ? '•••• (set) — leave blank to keep' : 'sk-… (optional)'}
                    aria-label={`API key for ${workflow.id}`}
                    name={`ai-key-${workflow.id}`}
                    autoComplete="new-password"
                    spellCheck={false}
                    className={`${inputClass} pr-10`}
                  />
                  <button
                    type="button"
                    onClick={() => setRevealed(current => current === workflow.id ? null : workflow.id)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 p-1.5 rounded-full hover:bg-zinc-100 dark:hover:bg-zinc-800"
                    aria-label={revealed === workflow.id ? `Hide API key for ${workflow.id}` : `Show API key for ${workflow.id}`}
                  >
                    {revealed === workflow.id ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                  </button>
                </span>
                {keySet && !draft.api_key && (
                  <span className="mt-1 block text-[10px] text-emerald-600 dark:text-emerald-400">A key is saved for this workflow.</span>
                )}
              </label>
            </div>
          </fieldset>
        )
      })}
    </div>
  )
}
