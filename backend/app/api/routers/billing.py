"""
Billing, subscription, plans and usage endpoints.

Provides:
- Plans listing (public pricing)
- Current subscription & entitlements
- Checkout session creation (Stripe/Razorpay/manual)
- Webhook handling with idempotency
- Usage & credit ledger
- Upgrade/downgrade/cancel (manual for dev, real via provider)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Query
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession
from app.core import audit
from app.core.config import settings
from app.core.entitlements import (
    PLANS,
    entitlements_snapshot,
    get_user_plan,
    get_plan_config,
    usage_for,
    current_period,
)
from app.models.models import AICreditLedger, BillingEvent, Subscription, UsageCounter
from app.services.billing import (
    get_provider,
    handle_billing_event,
    create_or_update_subscription_manual,
)

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
# Webhooks — idempotent, no auth (signature verified)
# --------------------------------------------------------------------------- #
@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: DbSession, stripe_signature: str = Header(default="", alias="Stripe-Signature")):
    payload_bytes = await request.body()
    provider = get_provider("stripe")
    if not provider.verify_webhook(payload_bytes, stripe_signature):
        raise HTTPException(401, "Invalid Stripe signature")
    try:
        parsed = provider.parse_event(payload_bytes)
    except Exception as exc:
        raise HTTPException(400, f"Failed to parse Stripe event: {exc}") from exc

    event = handle_billing_event(
        db,
        provider_name="stripe",
        provider_event_id=parsed.get("event_id") or "unknown",
        kind=parsed.get("kind") or "stripe.unknown",
        payload=parsed,
        user_id=parsed.get("user_id") if isinstance(parsed.get("user_id"), int) else None,
    )
    return {"ok": True, "processed": event.processed, "event_id": event.provider_event_id}


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request, db: DbSession, x_razorpay_signature: str = Header(default="", alias="X-Razorpay-Signature")):
    payload_bytes = await request.body()
    provider = get_provider("razorpay")
    if not provider.verify_webhook(payload_bytes, x_razorpay_signature):
        raise HTTPException(401, "Invalid Razorpay signature")
    try:
        parsed = provider.parse_event(payload_bytes)
    except Exception as exc:
        raise HTTPException(400, f"Failed to parse Razorpay event: {exc}") from exc

    event = handle_billing_event(
        db,
        provider_name="razorpay",
        provider_event_id=parsed.get("event_id") or "unknown",
        kind=parsed.get("kind") or "razorpay.unknown",
        payload=parsed,
        user_id=parsed.get("user_id") if isinstance(parsed.get("user_id"), int) else None,
    )
    return {"ok": True, "processed": event.processed, "event_id": event.provider_event_id}


@router.post("/webhooks/manual")
async def manual_webhook(request: Request, db: DbSession, x_webhook_token: str = Header(default="")):
    """
    Manual webhook for testing self-hosted billing.
    Requires EMAIL_WEBHOOK_TOKEN or any token in dev.
    Payload: {event_id, kind, user_id, plan, status, customer_id, subscription_id}
    """
    payload_bytes = await request.body()
    # Simple auth
    if settings.email_webhook_token and x_webhook_token != settings.email_webhook_token:
        if settings.is_production:
            raise HTTPException(401, "Invalid webhook token")
    try:
        data = json.loads(payload_bytes) if payload_bytes else {}
    except Exception:
        data = {}
    provider = get_provider("manual")
    parsed = provider.parse_event(payload_bytes)

    event = handle_billing_event(
        db,
        provider_name="manual",
        provider_event_id=parsed.get("event_id") or f"manual_{datetime.utcnow().timestamp()}",
        kind=parsed.get("kind") or "manual.update",
        payload=parsed,
        user_id=data.get("user_id") if isinstance(data.get("user_id"), int) else None,
    )
    return {"ok": True, "processed": event.processed, "event_id": event.provider_event_id}
