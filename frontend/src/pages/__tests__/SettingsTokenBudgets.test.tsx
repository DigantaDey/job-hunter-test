/**
 * The token-budget form's reading of `0` — the round-trip half of the fix.
 *
 * `Max output tokens` is the field the operator uses to switch the clamp off,
 * and `0` is how "off" is spelled end to end (`AI_MAX_OUTPUT_TOKENS=0`,
 * `ai.max_output_tokens: 0`, `PLANS.pro_plus.ai_max_output_tokens: 0`). The form
 * used to run every read and every save through `Number(x) || 16000`, which
 * reads an explicit *Unlimited* as "unset" and quietly restores a ceiling:
 *
 *   user sets 0 → saves 16000 → reload shows 16000 → long JDs truncate again
 *
 * …with nothing on screen to say the setting had been changed under them. These
 * tests pin the two rules that prevent it: `0` survives both directions, and a
 * *genuinely empty* field falls back to the platform default instead of
 * switching the account to unlimited by accident.
 */
import { describe, expect, it } from 'vitest'
import { budgetValue } from '../Settings'

describe('Settings: token-budget form round-trip', () => {
  it('keeps 0 as 0 — unlimited is a value, not an empty field', () => {
    // The reported regression, in both directions.
    expect(budgetValue(0, 16000)).toBe(0)        // read back from GET /api/settings
    expect(budgetValue('0', 16000)).toBe(0)      // typed into the number input
    expect(budgetValue(0, 0)).toBe(0)
  })

  it('passes a real ceiling through untouched', () => {
    expect(budgetValue(16000, 0)).toBe(16000)
    expect(budgetValue(4000, 0)).toBe(4000)
    expect(budgetValue('4000', 0)).toBe(4000)
    expect(budgetValue(2400.9, 0)).toBe(2400)    // tokens are whole numbers
  })

  it('falls back to the platform default only when the field is truly empty', () => {
    // Clearing the input must not silently mean "unlimited" — that would switch
    // the account to an unmetered provider budget from a stray backspace.
    expect(budgetValue('', 16000)).toBe(16000)
    expect(budgetValue('   ', 16000)).toBe(16000)
    expect(budgetValue(undefined, 16000)).toBe(16000)
    expect(budgetValue(null, 16000)).toBe(16000)
  })

  it('falls back on nonsense rather than sending a garbage budget', () => {
    expect(budgetValue('lots', 16000)).toBe(16000)
    expect(budgetValue(NaN, 16000)).toBe(16000)
    expect(budgetValue(Infinity, 16000)).toBe(16000)
    expect(budgetValue(-5, 16000)).toBe(16000)   // the server also rejects < 0
  })

  it('treats an unlimited platform default as the fallback too', () => {
    // AI_MAX_OUTPUT_TOKENS=0 (the shipped default): an empty field means
    // unlimited, because that is what the platform is set to.
    expect(budgetValue('', 0)).toBe(0)
  })
})
