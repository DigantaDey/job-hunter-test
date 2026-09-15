"""
Billing, subscription, plans and usage endpoints.

Provides:
- Plans listing (public pricing)
- Current subscription & entitlements
- Checkout session creation (Stripe/Razorpay/manual)
- Webhook handling with mandatory signature verification and idempotency
- Usage & credit ledger
- Upgrade/downgrade/cancel (manual for dev, real via provider)

Webhook trust boundaries (all three routes):
- Provider secrets / the manual token are mandatory. Unconfigured means the
  route answers 503 (``webhook_not_configured``) in production — and the
  manual route in every environment — instead of silently skipping auth or
  signature verification.
- Signatures are verified over the raw request body; identity is resolved from
  the provider subscription/customer id stored on the Subscription row and
  the plan is derived from the event's price/plan id via server-side maps.
  An event body can never name a user or a plan.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request

from app.api.deps import CurrentUser, DbSession, client_ip
from app.core import audit
from app.core.config import settings
from app.core.entitlements import (
    PLANS,
    current_period,
    entitlements_snapshot,
)
from app.core.logging import get_logger
from app.core.security import constant_time_equals
from app.models.models import AICreditLedger, BillingEvent, Subscription
from app.services.billing import (
    MANUAL_WEBHOOK_STATUSES,
    create_or_update_subscription_manual,
    get_provider,
    handle_billing_event,
    resolve_plan_from_price,
    resolve_user_id,
    verify_razorpay_signature,
    verify_stripe_signature,
)

log = get_logger("app.billing")

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/plans")
def list_plans():
    """Public pricing — no auth needed for landing page, but we allow auth too."""
    return {
        "plans": {
            key: {
                "id": key,
                "label": cfg["label"],
                "description": cfg["description"],
                "price_monthly_usd": cfg["price_monthly_usd"],
                "price_monthly_inr": cfg["price_monthly_inr"],
                "price_yearly_usd": cfg["price_yearly_usd"],
                "price_yearly_inr": cfg["price_yearly_inr"],
                "limits": cfg["limits"],
                "capabilities": cfg["capabilities"],
            }
            for key, cfg in PLANS.items()
        },
        "provider": settings.billing_provider,
        "trial_days": settings.trial_days,
    }


@router.get("/subscription")
def get_subscription_endpoint(user: CurrentUser, db: DbSession):
    snap = entitlements_snapshot(db, user.id)
    return snap


@router.get("/usage")
def get_usage(user: CurrentUser, db: DbSession, period: Optional[str] = Query(None)):
    period = period or current_period()
    snap = entitlements_snapshot(db, user.id)
    # Recent AI ledger
    ledger = (
        db.query(AICreditLedger)
        .filter(AICreditLedger.user_id == user.id)
        .order_by(AICreditLedger.created_at.desc())
        .limit(50)
        .all()
    )
    return {
        "period": period,
        "plan": snap["plan"],
        "usage": snap["usage"],
        "limits": snap["limits"],
        "capabilities": snap["capabilities"],
        "recent_ai_ops": [
            {
                "id": r.id,
                "workflow": r.workflow,
                "model": r.model,
                "total_tokens": r.total_tokens,
                "cost_usd": r.estimated_cost_usd,
                "success": r.success,
                "created_at": r.created_at,
            }
            for r in ledger
        ],
    }


@router.get("/credits")
def get_credits(user: CurrentUser, db: DbSession, limit: int = Query(100, le=500)):
    rows = (
        db.query(AICreditLedger)
        .filter(AICreditLedger.user_id == user.id)
        .order_by(AICreditLedger.created_at.desc())
        .limit(limit)
        .all()
    )
    total_tokens = sum(r.total_tokens for r in rows)
    total_cost = sum(r.estimated_cost_usd for r in rows)
    # Monthly aggregation
    period = current_period()
    monthly = (
        db.query(AICreditLedger)
        .filter(AICreditLedger.user_id == user.id, AICreditLedger.created_at >= datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0))
        .all()
    )
    monthly_tokens = sum(r.total_tokens for r in monthly)
    monthly_cost = sum(r.estimated_cost_usd for r in monthly)

    return {
        "period": period,
        "monthly_tokens": monthly_tokens,
        "monthly_cost_usd": round(monthly_cost, 4),
        "total_tokens_sample": total_tokens,
        "total_cost_sample_usd": round(total_cost, 4),
        "ledger": [
            {
                "id": r.id,
                "workflow": r.workflow,
                "model": r.model,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "cost_usd": r.estimated_cost_usd,
                "success": r.success,
                "latency_ms": r.latency_ms,
                "error": r.error,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


@router.post("/checkout")
def create_checkout(
    user: CurrentUser,
    db: DbSession,
    payload: Dict[str, Any],
    request: Request,
):
    """
    Create a checkout session for upgrading.
    Payload: {plan: "pro" | "pro_plus", billing_cycle: "monthly" | "yearly", success_url?, cancel_url?}
    """
    plan = str(payload.get("plan") or "").lower()
    if plan not in ("pro", "pro_plus", "pro+"):
        raise HTTPException(400, "Invalid plan — choose pro or pro_plus")
    if plan == "pro+":
        plan = "pro_plus"
    billing_cycle = str(payload.get("billing_cycle") or "monthly").lower()
    success_url = str(payload.get("success_url") or settings.billing_success_url)
    cancel_url = str(payload.get("cancel_url") or settings.billing_cancel_url)

    provider = get_provider(settings.billing_provider)
    try:
        session = provider.create_checkout_session(user, plan, success_url, cancel_url)
    except Exception as exc:
        raise HTTPException(500, f"Failed to create checkout: {exc}") from exc

    audit.audit(db, "billing.checkout_created", user=user, target=plan, detail={"provider": provider.name(), "cycle": billing_cycle}, request=request)
    return {
        "plan": plan,
        "billing_cycle": billing_cycle,
        "provider": provider.name(),
        "checkout_url": session.get("checkout_url"),
        "session_id": session.get("session_id"),
        "message": "Redirect to checkout_url to complete payment" if provider.name() != "manual" else "Manual provider — use /billing/upgrade to simulate",
    }


@router.post("/upgrade")
def manual_upgrade(user: CurrentUser, db: DbSession, payload: Dict[str, Any], request: Request):
    """
    Manual upgrade for self-hosted / dev. In production with Stripe/Razorpay,
    this would be admin-only or disabled.
    Payload: {plan: "free" | "pro" | "pro_plus", trial_days?: int}
    """
    # Allow any user to self-upgrade in manual mode, or owner in any mode for testing
    plan = str(payload.get("plan") or "").lower()
    if plan == "pro+":
        plan = "pro_plus"
    if plan not in ("free", "pro", "pro_plus"):
        raise HTTPException(400, "Invalid plan")
    trial_days = int(payload.get("trial_days") or 0)
    if trial_days < 0 or trial_days > 60:
        raise HTTPException(400, "trial_days must be 0-60")

    # If not manual provider, only allow downgrade to free or owner can override for testing
    if settings.billing_provider != "manual" and plan != "free" and user.role != "owner":
        raise HTTPException(403, "Use checkout flow for paid plans when billing provider is not manual")

    sub = create_or_update_subscription_manual(db, user.id, plan, status="active", trial_days=trial_days)
    audit.audit(db, "billing.upgraded", user=user, target=plan, detail={"manual": True, "trial_days": trial_days}, request=request)
    return {
        "ok": True,
        "plan": sub.plan,
        "status": sub.status,
        "trial_end": sub.trial_end,
        "message": f"Upgraded to {plan}" + (f" with {trial_days} day trial" if trial_days else ""),
    }


@router.post("/cancel")
def cancel_subscription(user: CurrentUser, db: DbSession, request: Request):
    sub = db.query(Subscription).filter(Subscription.user_id == user.id).first()
    if not sub:
        raise HTTPException(404, "No subscription found")
    if sub.plan == "free":
        raise HTTPException(400, "Free plan cannot be canceled")
    sub.cancel_at_period_end = True
    sub.grace_until = datetime.utcnow() + timedelta(days=3)
    db.commit()
    audit.audit(db, "billing.canceled", user=user, target=sub.plan, request=request)
    return {"ok": True, "message": "Subscription will cancel at period end", "grace_until": sub.grace_until}


@router.post("/downgrade")
def downgrade_to_free(user: CurrentUser, db: DbSession, request: Request):
    sub = db.query(Subscription).filter(Subscription.user_id == user.id).first()
    if not sub or sub.plan == "free":
        return {"ok": True, "plan": "free", "message": "Already on free plan"}
    sub.plan = "free"
    sub.status = "active"
    sub.cancel_at_period_end = False
    sub.grace_until = None
    db.commit()
    audit.audit(db, "billing.downgraded", user=user, target="free", request=request)
    return {"ok": True, "plan": "free"}


@router.get("/invoices")
def list_invoices(user: CurrentUser, db: DbSession):
    events = (
        db.query(BillingEvent)
        .filter(BillingEvent.user_id == user.id)
        .order_by(BillingEvent.created_at.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "id": e.id,
            "provider": e.provider,
            "event_id": e.provider_event_id,
            "kind": e.kind,
            "processed": e.processed,
            "created_at": e.created_at,
            "payload": {k: v for k, v in (e.payload or {}).items() if k not in ("raw",)}  # hide raw if large
        }
        for e in events
    ]


# --------------------------------------------------------------------------- #
# Webhooks — signature/token verified (never skippable), idempotent, no user auth
# --------------------------------------------------------------------------- #
def _require_provider_webhook_secret(provider: str) -> str:
    """Return the provider webhook secret, or refuse to serve (fail closed).

    A missing secret is a hard configuration error, not a dev convenience:
    without it there is nothing to verify a signature against, and an open
    "apply any event" endpoint is a subscription forge. Production answers
    503 (mirrors the tracking provider webhook) so the provider retries;
    non-production answers 400 so a misconfigured dev deploy fails loudly
    instead of silently accepting unsigned events.
    """
    if provider == "stripe":
        secret = (settings.stripe_webhook_secret or "").strip()
        env_var = "STRIPE_WEBHOOK_SECRET"
    else:
        secret = (settings.razorpay_webhook_secret or "").strip()
        env_var = "RAZORPAY_WEBHOOK_SECRET"
    if secret:
        return secret
    message = (
        f"Set {env_var} to enable this endpoint — unsigned {provider} events are "
        f"never accepted (env={settings.environment})"
    )
    if settings.is_production:
        log.error("refusing to serve /billing/webhooks/%s: %s is unset (webhook_not_configured)", provider, env_var)
        raise HTTPException(503, {"code": "webhook_not_configured", "message": message})
    log.error("/billing/webhooks/%s is inert: %s is unset — verification is mandatory, refusing event (400)", provider, env_var)
    raise HTTPException(400, {"code": "webhook_not_configured", "message": message})


def _reject_unverified(provider: str, reason: str, request: Request) -> None:
    """Uniform, log-then-raise path for signature failures (no payload echoes)."""
    log.warning("%s webhook rejected: %s (ip=%s)", provider, reason, client_ip(request))
    status = 401 if reason == "signature_mismatch" else 400
    raise HTTPException(status, {"code": "invalid_signature", "message": f"{provider} webhook signature check failed: {reason}"})


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: DbSession, stripe_signature: str = Header(default="", alias="Stripe-Signature")):
    """Stripe events, HMAC-verified over the raw body (no auth — signature IS the auth)."""
    secret = _require_provider_webhook_secret("stripe")
    payload_bytes = await request.body()
    ok, reason = verify_stripe_signature(secret, payload_bytes, stripe_signature)
    if not ok:
        _reject_unverified("stripe", reason, request)
    provider = get_provider("stripe")
    parsed = provider.parse_event(payload_bytes)
    if parsed.get("kind") == "parse_error":
        raise HTTPException(400, "Failed to parse Stripe event")

    event = handle_billing_event(
        db,
        provider_name="stripe",
        provider_event_id=parsed["event_id"],
        kind=parsed["kind"],
        payload=parsed,
    )
    response = {"ok": True, "processed": event.processed, "event_id": event.provider_event_id}
    if not event.user_id:
        # Signature valid, event stored for audit, nothing applied: no
        # subscription carries this customer/subscription id. 200 keeps
        # Stripe from hammering retries for an event we will never map.
        log.warning(
            "stripe webhook %s: customer/subscription id matches no stored subscription — event recorded, nothing applied",
            event.provider_event_id,
        )
        response["ignored"] = "unmapped_customer"
    return response


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request, db: DbSession, x_razorpay_signature: str = Header(default="", alias="X-Razorpay-Signature")):
    """Razorpay events, HMAC-verified over the raw body (no auth — signature IS the auth)."""
    secret = _require_provider_webhook_secret("razorpay")
    payload_bytes = await request.body()
    ok, reason = verify_razorpay_signature(secret, payload_bytes, x_razorpay_signature)
    if not ok:
        _reject_unverified("razorpay", reason, request)
    provider = get_provider("razorpay")
    parsed = provider.parse_event(payload_bytes)
    if parsed.get("kind") == "parse_error":
        raise HTTPException(400, "Failed to parse Razorpay event")

    event = handle_billing_event(
        db,
        provider_name="razorpay",
        provider_event_id=parsed["event_id"],
        kind=parsed["kind"],
        payload=parsed,
    )
    response = {"ok": True, "processed": event.processed, "event_id": event.provider_event_id}
    if not event.user_id:
        log.warning(
            "razorpay webhook %s: subscription id matches no stored subscription — event recorded, nothing applied",
            event.provider_event_id,
        )
        response["ignored"] = "unmapped_customer"
    return response


@router.post("/webhooks/manual")
async def manual_webhook(request: Request, db: DbSession, x_webhook_token: str = Header(default="")):
    """
    Manual webhook for testing self-hosted billing.

    Trust boundaries (unlike the old behaviour, all hard):
    - ``EMAIL_WEBHOOK_TOKEN`` is required in EVERY environment: unset → 503
      ``webhook_not_configured`` (mirrors the tracking provider webhook);
      wrong → 401, compared with ``constant_time_equals``.
    - The body can never set user identity or plan. The event must reference a
      ``subscription_id``/``customer_id`` already stored on a Subscription row
      (unknown mapping → logged and rejected with 404), and the plan is
      derived from the event's price/plan id via the server-side configured
      maps (STRIPE_PRICE_* / RAZORPAY_PLAN_*).
    - ``event_id`` is required so the BillingEvent unique constraint dedupes
      retries.

    Payload: {event_id, kind?, status?, price?, customer_id?, subscription_id?}
    """
    if not (settings.email_webhook_token or "").strip():
        log.error("manual billing webhook refused: EMAIL_WEBHOOK_TOKEN is unset (env=%s)", settings.environment)
        raise HTTPException(
            503,
            {"code": "webhook_not_configured",
             "message": "Set EMAIL_WEBHOOK_TOKEN to enable the manual billing webhook"},
        )
    if not constant_time_equals(x_webhook_token, settings.email_webhook_token):
        log.warning("manual billing webhook rejected: invalid token (ip=%s)", client_ip(request))
        raise HTTPException(401, "Invalid webhook token")

    payload_bytes = await request.body()
    try:
        data = json.loads(payload_bytes) if payload_bytes else {}
    except Exception:
        raise HTTPException(400, "Manual webhook body must be valid JSON") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "Manual webhook body must be a JSON object")

    event_id = str(data.get("event_id") or "").strip()
    if not event_id:
        raise HTTPException(400, {"code": "event_id_required",
                                  "message": "event_id is required so retries are applied exactly once"})
    kind = str(data.get("kind") or "manual.update")
    status = str(data.get("status") or "active").strip().lower()
    if status not in MANUAL_WEBHOOK_STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(sorted(MANUAL_WEBHOOK_STATUSES))}")
    customer_id = str(data.get("customer_id") or "").strip()
    subscription_id = str(data.get("subscription_id") or "").strip()

    # Identity ONLY from the stored mapping — `user_id` in the body is not a
    # supported field, so a forged event can never pick its victim.
    user_id = resolve_user_id(db, {"provider_subscription_id": subscription_id,
                                   "provider_customer_id": customer_id})
    if not user_id:
        log.warning(
            "manual billing webhook rejected: subscription_id=%r customer_id=%r matches no stored subscription "
            "(body user_id is ignored) — event NOT applied",
            subscription_id or None, customer_id or None,
        )
        raise HTTPException(
            404,
            {"code": "user_not_found",
             "message": "No subscription carries the supplied subscription_id/customer_id. "
                        "Manual events may only target users with a stored provider mapping; "
                        "identity fields in the body are ignored."},
        )

    payload = {
        "event_id": event_id,
        "kind": kind,
        "status": status,
        # Only the price/plan id — the internal plan is resolved server-side.
        "price_id": str(data.get("price") or data.get("price_id") or "").strip(),
        "provider_customer_id": customer_id,
        "provider_subscription_id": subscription_id,
    }
    event = handle_billing_event(
        db,
        provider_name="manual",
        provider_event_id=event_id,
        kind=kind,
        payload=payload,
        user_id=user_id,
    )
    return {
        "ok": True,
        "processed": event.processed,
        "event_id": event.provider_event_id,
        "plan": resolve_plan_from_price(payload["price_id"]) or "unchanged",
    }

