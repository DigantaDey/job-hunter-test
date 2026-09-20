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

/** The workflows that accept a per-workflow provider override (owner scope). */
export const AI_WORKFLOWS = [
  'parse', 'keyword_extract', 'scoring', 'resume_gen', 'classify', 'email_gen',
  'form_detect', 'funding_scan', 'tagging', 'interview', 'company_intel',
] as const
