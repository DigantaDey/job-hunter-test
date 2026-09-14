/**
 * Plan limits & AI credits — the shared reading of "this plan has no ceiling".
 *
 * The backend encodes **unlimited as `0`** (`entitlements.PLANS`: Pro+ has
 * `ai_credits_per_month: 0` and `ai_max_output_tokens: 0`; `is_unlimited()` on
 * the server treats `0`/`None` the same way). Every surface that renders a
 * limit has to read `0` as *unlimited*, not as "no headroom left":
 *
 *   Pro+ • 222757/0 credits     ← what the sidebar used to show: a quota spent
 *   Pro+ • 222757 credits used • Unlimited
 *
 * Three pages used to make that decision inline, in three slightly different
 * ways, and one of them (`v.limit || '∞'`) was truthiness-based — exactly the
 * bug that made `0` look like a used-up allowance. These helpers are the single
 * reading of the contract, so the sidebar, the billing grid and the dashboard
 * cannot disagree, and the rule is testable without rendering a whole page.
 *
 * A *missing* limit is not unlimited: it means the payload did not say, and
 * claiming "Unlimited" from a hole in the data would be as false as claiming
 * "0 left". Those cases render the usage alone, with no ceiling claim.
 */

/** One entry of `entitlements_snapshot()["usage"][key]`. */
export interface UsageEntry {
  used?: number | null
  limit?: number | null
  /** `null` on the server when the limit is unlimited (never an invented 999999). */
  remaining?: number | null
  /** Spelled out by the server for unlimited limits. */
  unlimited?: boolean
  /** Feature-flag entries carry this instead of a counter. */
  allowed?: boolean
}

/** The limit as a finite number, or `null` when the payload did not state one. */
export function limitNumber(limit: unknown): number | null {
  if (limit === null || limit === undefined || limit === '') return null
  const parsed = Number(limit)
  return Number.isFinite(parsed) ? parsed : null
}

/** `true` only when a limit is *stated* as unlimited (`0`, or the server's flag). */
export function isUnlimited(limit: unknown): boolean {
  const parsed = limitNumber(limit)
  return parsed !== null && parsed <= 0
}

/** Unlimited per the entry's own flag first, then its `limit`. */
export function entryIsUnlimited(entry: UsageEntry | null | undefined): boolean {
  // A feature-flag entry (`{allowed, used, limit: 1|0}`) is not a quota: `limit: 0`
  // there means "this tier does not have the feature", which is the opposite of
  // unlimited and must never earn the badge.
  if (entry && typeof entry.allowed === 'boolean') return false
  if (entry?.unlimited === true) return true
  return isUnlimited(entry?.limit)
}

/**
 * The one-line credit readout.
 *
 * * capped   → `20000/50000 credits`
 * * unlimited→ `222757 credits used • Unlimited`
 * * unstated → `222757 credits used` (no ceiling claim either way)
 */
export function creditLabel(used: unknown, limit: unknown): string {
  const spent = Number(used) || 0
  if (isUnlimited(limit)) return `${spent} credits used • Unlimited`
  const cap = limitNumber(limit)
  if (cap === null || cap <= 0) return `${spent} credits used`
  return `${spent}/${cap} credits`
}

/** Meter fill (0–100). An unlimited or unstated limit has no meter to draw. */
export function usagePercent(used: unknown, limit: unknown): number | null {
  const cap = limitNumber(limit)
  if (cap === null || cap <= 0) return null
  const spent = Number(used) || 0
  return Math.min(100, Math.max(0, (spent / cap) * 100))
}
