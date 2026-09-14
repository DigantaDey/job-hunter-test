/**
 * `limit === 0` → **Unlimited** — the rendering half of the token-budget fix.
 *
 * The backend encodes "this plan has no ceiling" as `0`
 * (`entitlements.PLANS["pro_plus"].ai_credits_per_month = 0`, and
 * `entitlements_snapshot` answers `{"used": n, "limit": 0, "remaining": null,
 * "unlimited": true}`). Three surfaces render that number, and before the fix
 * the sidebar showed the Pro+ account from the bug report as
 * `Pro+ • 222757/0 credits` — a quota that reads as *spent*, on the very plan
 * whose promise is that it never runs out.
 *
 * These tests render the real components (`PlanCreditBanner` from the sidebar,
 * `UsageCell` from the billing grid) with the exact payload shapes the API
 * serves, so a truthiness slip (`limit || …`, `limit ? … : …`) fails here
 * instead of shipping.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { PlanCreditBanner } from '../Layout'
import { UsageCell } from '../../pages/Billing'
import {
  creditLabel,
  entryIsUnlimited,
  isUnlimited,
  limitNumber,
  usagePercent,
} from '../../lib/credits'

// Vitest runs without globals, so RTL cannot register its own auto-cleanup:
// without this, each render leaks into the next and queries find duplicates.
afterEach(cleanup)

/** The entitlement payload the sidebar gets from `GET /api/billing/subscription`. */
const proPlusUnlimited = {
  plan: 'pro_plus',
  plan_label: 'Pro+',
  limits: { ai_credits_per_month: 0, ai_max_output_tokens: 0 },
  usage: { ai_credits_per_month: { used: 222757, limit: 0, remaining: null, unlimited: true } },
}

const freeCapped = {
  plan: 'free',
  plan_label: 'Free',
  limits: { ai_credits_per_month: 50000 },
  usage: { ai_credits_per_month: { used: 20000, limit: 50000, remaining: 30000, unlimited: false } },
}

describe('credits: reading a limit', () => {
  it('treats 0 as unlimited, and a positive number as a cap', () => {
    expect(isUnlimited(0)).toBe(true)
    expect(isUnlimited('0')).toBe(true)
    expect(isUnlimited(50000)).toBe(false)
    expect(isUnlimited(16000)).toBe(false)
  })

  it('never claims "unlimited" from a limit the payload did not state', () => {
    // A hole in the data is not a plan promise — that would be as false as
    // claiming "0 left".
    expect(isUnlimited(undefined)).toBe(false)
    expect(isUnlimited(null)).toBe(false)
    expect(isUnlimited('')).toBe(false)
    expect(isUnlimited('lots')).toBe(false)
    expect(limitNumber(undefined)).toBeNull()
    expect(limitNumber(0)).toBe(0)
    expect(limitNumber(50000)).toBe(50000)
  })

  it('does not let a locked feature flag earn the unlimited badge', () => {
    // Feature flags come back as {allowed, used: 0, limit: 1|0}: limit 0 there
    // means "this tier does not have the feature", the opposite of unlimited.
    expect(entryIsUnlimited({ allowed: false, used: 0, limit: 0 })).toBe(false)
    expect(entryIsUnlimited({ allowed: true, used: 0, limit: 1 })).toBe(false)
    expect(entryIsUnlimited({ used: 222757, limit: 0, remaining: null, unlimited: true })).toBe(true)
  })

  it('says the ceiling in words instead of printing /0', () => {
    expect(creditLabel(222757, 0)).toBe('222757 credits used • Unlimited')
    expect(creditLabel(20000, 50000)).toBe('20000/50000 credits')
    expect(creditLabel(0, 50000)).toBe('0/50000 credits')
    expect(creditLabel(222757, undefined)).toBe('222757 credits used')
    // Never the reported regression:
    expect(creditLabel(222757, 0)).not.toContain('222757/0')
  })

  it('draws a meter only when there is a ceiling to draw against', () => {
    expect(usagePercent(20000, 50000)).toBe(40)
    expect(usagePercent(60000, 50000)).toBe(100)  // clamped, never >100%
    expect(usagePercent(222757, 0)).toBeNull()    // unlimited → no meter
    expect(usagePercent(222757, undefined)).toBeNull()
  })
})

describe('sidebar plan banner', () => {
  it('shows Unlimited for Pro+ instead of 222757/0 credits', () => {
    render(<PlanCreditBanner ent={proPlusUnlimited} />)

    const banner = screen.getByText(/Pro\+/)
    expect(banner.textContent).toContain('222757 credits used • Unlimited')
    expect(banner.textContent).not.toContain('/0')
    expect(banner.textContent).not.toContain('0 credits')
  })

  it('still shows the cap for a metered plan', () => {
    render(<PlanCreditBanner ent={freeCapped} />)

    const banner = screen.getByText(/Free/)
    expect(banner.textContent).toContain('20000/50000 credits')
    expect(banner.textContent).not.toContain('Unlimited')
  })

  it('renders nothing before the entitlements arrive', () => {
    const { container } = render(<PlanCreditBanner ent={null} />)
    // No jest-dom in this repo, so assert on the DOM directly: an unknown plan
    // must not produce a banner that claims any ceiling at all.
    expect(container.innerHTML).toBe('')
  })
})

describe('billing usage tile', () => {
  it('badges an unlimited limit and hides the meter', () => {
    const { container } = render(
      <UsageCell label="ai_credits_per_month"
                 value={{ used: 222757, limit: 0, remaining: null, unlimited: true }} />
    )

    expect(screen.getByText('Unlimited')).toBeTruthy()
    expect(screen.getByText('222757 used')).toBeTruthy()
    // No "0 left" and no meter drawn against a 0 ceiling.
    expect(screen.queryByText(/left/)).toBeNull()
    expect(container.querySelector('[style*="width"]')).toBeNull()
  })

  it('shows remaining headroom and a meter for a capped limit', () => {
    const { container } = render(
      <UsageCell label="ai_credits_per_month"
                 value={{ used: 20000, limit: 50000, remaining: 30000, unlimited: false }} />
    )

    expect(screen.getByText('20000/50000')).toBeTruthy()
    expect(screen.getByText('30000 left')).toBeTruthy()
    expect(screen.queryByText('Unlimited')).toBeNull()
    const meter = container.querySelector('[style*="width"]') as HTMLElement
    expect(meter).not.toBeNull()
    expect(meter.style.width).toBe('40%')
  })

  it('does not badge a locked feature flag as unlimited', () => {
    render(<UsageCell label="advanced_matching" value={{ allowed: false, used: 0, limit: 0 }} />)

    expect(screen.queryByText('Unlimited')).toBeNull()
    expect(screen.getByText('0 left')).toBeTruthy()
  })

  it('humanises the label', () => {
    render(<UsageCell label="ai_credits_per_month"
                      value={{ used: 1, limit: 5, remaining: 4, unlimited: false }} />)
    expect(screen.getByText('ai credits per month')).toBeTruthy()
  })
})
