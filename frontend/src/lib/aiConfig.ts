/**
 * AI provider configuration helpers (owner console only).
 *
 * `budgetValue` used to live on the user Settings page; token budgets are an
 * owner-level control now, so the round-trip logic moved here next to the form
 * that uses it (pages/admin/AdminConsole.tsx).
 */

/**
 * Read a token-budget field back out of the form.
 *
 * **0 is a value, not an empty field** — it means *unlimited* (no clamp, the
 * provider's own per-model maximum), so it must survive the form round-trip.
 * `Number(x) || 16000` used to read a user's explicit "Unlimited" as unset and
 * silently restore a ceiling. A genuinely empty field falls back to the
 * platform default rather than switching the account to unlimited by accident.
 */
export function budgetValue(raw: unknown, fallback: number): number {
  const text = String(raw ?? '').trim()
  if (text === '') return fallback
  const parsed = Number(text)
  if (!Number.isFinite(parsed) || parsed < 0) return fallback
  return Math.trunc(parsed)
}

/**
 * The workflows that accept a per-workflow provider override (owner scope).
 *
 * Kept in lockstep with `WORKFLOWS` in `backend/app/services/ai_client.py`.
 * The admin console always renders an input row for each of these, even when
 * `GET /api/ai/config` has no saved overrides yet — an empty `overrides` map
 * is the common case, not a reason to hide the form.
 */
export const AI_WORKFLOWS = [
  'parse', 'keyword_extract', 'scoring', 'resume_gen', 'classify', 'email_gen',
  'form_detect', 'funding_scan', 'tagging', 'interview', 'company_intel', 'persona',
] as const

export type AIWorkflowId = (typeof AI_WORKFLOWS)[number]

/** Short labels for the override form. Descriptions come from the API when it has them. */
export const AI_WORKFLOW_LABELS: Record<string, string> = {
  parse: 'Resume parsing',
  keyword_extract: 'Keyword extraction',
  scoring: 'Match scoring',
  resume_gen: 'Resume generation',
  classify: 'Company classification',
  email_gen: 'Email drafting',
  form_detect: 'Form detection',
  funding_scan: 'Funding scan',
  tagging: 'Resume tagging',
  interview: 'Interview prep',
  company_intel: 'Company intelligence',
  persona: 'Persona reflection',
}

/** Fallback blurbs, used only when the config endpoint doesn't send its own. */
export const AI_WORKFLOW_DESCRIPTIONS: Record<string, string> = {
  parse: 'Resume parsing / profile extraction',
  keyword_extract: 'Search-context / keyword extraction',
  scoring: 'Profile ↔ job scoring',
  resume_gen: 'Tailored resume generation',
  classify: 'Company size classification',
  email_gen: 'Cold-email + decision-maker discovery',
  form_detect: 'Job-portal form-structure detection',
  funding_scan: 'Funding radar scan',
  tagging: 'Resume auto-tagging',
  interview: 'Interview preparation',
  company_intel: 'Company intelligence',
  persona: 'User-persona reflection / track suggestions',
}

/** Provider values the override endpoint accepts. `''` means inherit the default. */
export const AI_PROVIDER_OPTIONS = [
  { value: '', label: 'Inherit default' },
  { value: 'openai_compatible', label: 'OpenAI compatible' },
  { value: 'google', label: 'Google AI Studio (Gemini)' },
] as const

export type WorkflowOption = { id: string; description: string }

export type WorkflowOverrideDraft = {
  base_url?: string
  model?: string
  provider?: string
  api_key?: string
  api_key_set?: boolean
}

const MASKED_CHARS = new Set(['*', '•', '·', 'x', 'X'])

/** Mirror of the backend's masked-placeholder rejection (`***`, `••••`, …). */
export function looksMasked(value: string): boolean {
  const stripped = value.trim()
  if (stripped.length < 3) return false
  return [...stripped].every(ch => MASKED_CHARS.has(ch))
}

export function normalizeProvider(value: string): string {
  const text = value.trim().toLowerCase()
  if (text === 'google' || text === 'google_ai_studio' || text === 'google_ai') return 'google'
  if (text === 'openai' || text === 'openai-compatible' || text === 'openai_compatible') return 'openai_compatible'
  return value.trim()
}

function fallbackCatalog(): WorkflowOption[] {
  return AI_WORKFLOWS.map(id => ({ id, description: AI_WORKFLOW_DESCRIPTIONS[id] || '' }))
}

/**
 * Workflows the override form should render.
 *
 * Prefer the server's list (`GET /api/ai/config` → `workflows`, a name→description
 * map or a list). Fall back to `AI_WORKFLOWS` when that payload is missing or
 * empty — that is exactly the "no overrides yet" response, and hiding the
 * inputs then is what left the section with nothing to type into.
 * Any saved override whose id isn't in the list is appended so it stays editable.
 */
export function workflowCatalog(payload: unknown): WorkflowOption[] {
  const data = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {}
  const raw = data.workflows
  const fromServer: WorkflowOption[] = []
  if (Array.isArray(raw)) {
    for (const item of raw) {
      if (typeof item === 'string' && item.trim()) {
        const id = item.trim()
        fromServer.push({ id, description: AI_WORKFLOW_DESCRIPTIONS[id] || '' })
      } else if (item && typeof item === 'object') {
        const row = item as { id?: unknown; description?: unknown }
        if (typeof row.id === 'string' && row.id.trim()) {
          fromServer.push({ id: row.id.trim(), description: typeof row.description === 'string' ? row.description : '' })
        }
      }
    }
  } else if (raw && typeof raw === 'object') {
    for (const [id, description] of Object.entries(raw as Record<string, unknown>)) {
      if (!id.trim()) continue
      fromServer.push({ id, description: typeof description === 'string' ? description : (AI_WORKFLOW_DESCRIPTIONS[id] || '') })
    }
  }
  const catalog = fromServer.length ? fromServer : fallbackCatalog()
  const known = new Set(catalog.map(row => row.id))
  const overrides = data.overrides
  if (overrides && typeof overrides === 'object' && !Array.isArray(overrides)) {
    for (const id of Object.keys(overrides as object)) {
      if (!known.has(id)) {
        catalog.push({ id, description: AI_WORKFLOW_DESCRIPTIONS[id] || '' })
        known.add(id)
      }
    }
  }
  return catalog
}

export function workflowLabel(id: string): string {
  return AI_WORKFLOW_LABELS[id] || id.replace(/_/g, ' ')
}

/** The body `POST /api/ai/config` expects for one workflow. Blank api_key = keep the stored key. */
export function overrideSavePayload(draft: WorkflowOverrideDraft): {
  base_url: string
  model: string
  provider: string
  api_key: string
} {
  return {
    base_url: String(draft.base_url || '').trim(),
    model: String(draft.model || '').trim(),
    provider: normalizeProvider(String(draft.provider || '')),
    api_key: String(draft.api_key || '').trim(),
  }
}

/**
 * Client-side gate before POST. Returns a human error, or null when the draft
 * is safe to send.
 *
 * A provider on its own is not an override — the API deletes a workflow whose
 * base URL, model and key are all blank — so saving that would look successful
 * and then vanish on reload.
 */
export function overrideSaveError(draft: WorkflowOverrideDraft): string | null {
  const payload = overrideSavePayload(draft)
  if (payload.base_url && !/^https?:\/\//i.test(payload.base_url)) {
    return 'Base URL must start with http:// or https:// (for example https://api.openai.com/v1).'
  }
  if (payload.model && payload.model.length < 2) {
    return 'Model must be at least 2 characters (for example gpt-4o-mini).'
  }
  if (payload.api_key && looksMasked(payload.api_key)) {
    return 'That looks like a masked placeholder — paste the full API key. It is never shown back in full.'
  }
  if (payload.provider && !AI_PROVIDER_OPTIONS.some(option => option.value === payload.provider)) {
    return 'Provider must be OpenAI compatible, Google AI Studio, or left as “inherit default”.'
  }
  const hasMaterial = Boolean(payload.base_url || payload.model || payload.api_key || draft.api_key_set)
  if (!hasMaterial) {
    return 'Set a base URL, model, or API key. A provider alone is not saved — blank fields inherit the default above.'
  }
  return null
}
