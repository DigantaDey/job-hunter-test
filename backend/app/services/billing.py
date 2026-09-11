"""
Billing provider abstraction for Stripe and Razorpay.

Design:
- Interface BillingProvider with methods: create_customer, create_checkout, handle_webhook, etc.
- Manual provider for self-hosted / testing
- Webhook handlers are idempotent via BillingEvent dedupe
- All provider secrets via settings, never logged
"""

from __future__ import annotations

import hashlib
import hmac
import json
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import BillingEvent, Subscription, User

log = get_logger("app.billing")


class BillingProvider(ABC):
    @abstractmethod
    def name(self) -> str: ...

    def create_customer(self, user: User) -> Dict[str, Any]:
        return {"customer_id": f"cust_{user.id}"}

    def create_checkout_session(self, user: User, plan: str, success_url: str, cancel_url: str) -> Dict[str, Any]:
        # Stub: returns fake checkout URL
        return {"checkout_url": f"{success_url}?plan={plan}&user={user.id}", "session_id": f"sess_{user.id}_{plan}"}

    @abstractmethod
    def verify_webhook(self, payload: bytes, signature: str) -> bool: ...

    @abstractmethod
    def parse_event(self, payload: bytes) -> Dict[str, Any]: ...


class ManualProvider(BillingProvider):
    def name(self) -> str:
        return "manual"

    def verify_webhook(self, payload: bytes, signature: str) -> bool:
        # Manual provider: no signature verification, but check if token matches settings if set
        if settings.email_webhook_token:
            # Reuse webhook token as simple auth for manual
            return hmac.compare_digest(signature, settings.email_webhook_token)
        return True

    def parse_event(self, payload: bytes) -> Dict[str, Any]:
        try:
            data = json.loads(payload)
            return {
                "event_id": data.get("event_id") or f"manual_{hashlib.sha256(payload).hexdigest()[:12]}",
                "kind": data.get("kind") or "manual.update",
                "user_id": data.get("user_id"),
                "plan": data.get("plan"),
                "status": data.get("status", "active"),
                "provider_customer_id": data.get("customer_id", ""),
                "provider_subscription_id": data.get("subscription_id", ""),
            }
        except Exception as e:
            return {"event_id": f"manual_err_{datetime.utcnow().timestamp()}", "kind": "parse_error", "error": str(e)}


class StripeProvider(BillingProvider):
    def name(self) -> str:
        return "stripe"

    def verify_webhook(self, payload: bytes, signature: str) -> bool:
        # In production, verify Stripe signature using STRIPE_WEBHOOK_SECRET
        # For now, if secret not set, accept (with warning)
        secret = getattr(settings, "stripe_webhook_secret", "") or ""
        if not secret:
            log.warning("STRIPE_WEBHOOK_SECRET not set — skipping verification (dev only)")
            return True
        # Real verification would use stripe library; we do HMAC stub
        try:
            # Stripe signature format: t=timestamp,v1=signature
            # Simplified: just check HMAC
            expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
            # Extract v1 from header
            parts = dict(p.split("=") for p in signature.split(",") if "=" in p)
            v1 = parts.get("v1", "")
            return hmac.compare_digest(v1, expected)
        except Exception:
            return False

    def parse_event(self, payload: bytes) -> Dict[str, Any]:
        try:
            data = json.loads(payload)
            # Stripe event shape: {id, type, data: {object: {customer, subscription, etc}}}
            obj = data.get("data", {}).get("object", {}) or {}
            return {
                "event_id": data.get("id") or f"stripe_{hashlib.sha256(payload).hexdigest()[:12]}",
                "kind": data.get("type") or "stripe.unknown",
                "user_id": obj.get("metadata", {}).get("user_id") or obj.get("client_reference_id"),
                "plan": obj.get("metadata", {}).get("plan") or obj.get("plan", {}).get("id"),
                "status": obj.get("status") or "active",
                "provider_customer_id": obj.get("customer") or "",
                "provider_subscription_id": obj.get("subscription") or obj.get("id") or "",
                "raw": obj,
            }
        except Exception as e:
            return {"event_id": f"stripe_err_{datetime.utcnow().timestamp()}", "kind": "parse_error", "error": str(e)}


class RazorpayProvider(BillingProvider):
    def name(self) -> str:
        return "razorpay"

    def verify_webhook(self, payload: bytes, signature: str) -> bool:
        secret = getattr(settings, "razorpay_webhook_secret", "") or ""
        if not secret:
            log.warning("RAZORPAY_WEBHOOK_SECRET not set — skipping verification (dev only)")
            return True
        try:
            expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, signature)
        except Exception:
            return False

    def parse_event(self, payload: bytes) -> Dict[str, Any]:
        try:
            data = json.loads(payload)
            # Razorpay shape: {event, payload: {subscription: {entity: {...}}}}
            entity = data.get("payload", {}).get("subscription", {}).get("entity") or {}
            return {
                "event_id": data.get("event_id") or f"rzp_{hashlib.sha256(payload).hexdigest()[:12]}",
                "kind": data.get("event") or "razorpay.unknown",
                "user_id": entity.get("notes", {}).get("user_id"),
                "plan": entity.get("notes", {}).get("plan"),
                "status": entity.get("status") or "active",
                "provider_customer_id": entity.get("customer_id") or "",
                "provider_subscription_id": entity.get("id") or "",
                "raw": entity,
            }
        except Exception as e:
            return {"event_id": f"rzp_err_{datetime.utcnow().timestamp()}", "kind": "parse_error", "error": str(e)}


def get_provider(name: str) -> BillingProvider:
    name = (name or "manual").lower()
    if name == "stripe":
        return StripeProvider()
    if name == "razorpay":
        return RazorpayProvider()
    return ManualProvider()


def handle_billing_event(db: Session, provider_name: str, provider_event_id: str, kind: str, payload: Dict[str, Any], user_id: Optional[int] = None) -> BillingEvent:
    """
    Idempotent handling: if event already processed, return existing.
    Otherwise create BillingEvent and apply subscription change.
    """
    # Check idempotency
    existing = (
        db.query(BillingEvent)
        .filter(BillingEvent.provider == provider_name, BillingEvent.provider_event_id == provider_event_id)
        .first()
    )
    if existing:
        return existing

    # Determine user_id
    resolved_user_id = user_id
    if not resolved_user_id:
        # Try to find from payload
        uid = payload.get("user_id")
        try:
            resolved_user_id = int(uid) if uid else None
        except Exception:
            resolved_user_id = None
        # Also try to find subscription by provider ids
        if not resolved_user_id:
            prov_sub = payload.get("provider_subscription_id") or payload.get("raw", {}).get("id")
            if prov_sub:
                sub = db.query(Subscription).filter(Subscription.provider_subscription_id == prov_sub).first()
                if sub:
                    resolved_user_id = sub.user_id
            prov_cust = payload.get("provider_customer_id")
            if not resolved_user_id and prov_cust:
                sub = db.query(Subscription).filter(Subscription.provider_customer_id == prov_cust).first()
                if sub:
                    resolved_user_id = sub.user_id

    event = BillingEvent(
        user_id=resolved_user_id,
        provider=provider_name,
        provider_event_id=provider_event_id,
        kind=kind,
        payload=payload,
        processed=False,
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    # Apply subscription logic
    try:
        if resolved_user_id:
            apply_subscription_change(db, resolved_user_id, payload, provider_name)
        event.processed = True
        db.commit()
    except Exception as exc:
        event.error = str(exc)[:2000]
        event.processed = False
        db.commit()
        log.error("billing event %s failed: %s", provider_event_id, exc)

    return event


def apply_subscription_change(db: Session, user_id: int, payload: Dict[str, Any], provider_name: str) -> Optional[Subscription]:
    """
    Apply subscription change from webhook payload.
    Payload expected to have plan, status, customer_id, subscription_id.
    """
    plan = payload.get("plan")
    status = payload.get("status", "active")
    customer_id = payload.get("provider_customer_id") or ""
    sub_id = payload.get("provider_subscription_id") or ""

    # Normalize plan
    if plan:
        plan = str(plan).lower()
        # Map provider plan ids to internal
        plan_map = {
            "free": "free",
            "pro": "pro",
            "pro_plus": "pro_plus",
            "pro+": "pro_plus",
            "pro-plus": "pro_plus",
            "price_pro_monthly": "pro",
            "price_pro_plus_monthly": "pro_plus",
            # Razorpay plan ids could be mapped here
        }
        plan = plan_map.get(plan, plan)
        if plan not in ("free", "pro", "pro_plus"):
            # If unknown, keep existing
            plan = None

    sub = db.query(Subscription).filter(Subscription.user_id == user_id).first()
    if not sub:
        sub = Subscription(
            user_id=user_id,
            plan=plan or "free",
            status=status,
            provider=provider_name,
            provider_customer_id=customer_id,
            provider_subscription_id=sub_id,
            current_period_start=datetime.utcnow(),
            current_period_end=datetime.utcnow() + timedelta(days=30),
        )
        db.add(sub)
    else:
        if plan:
            sub.plan = plan
        sub.status = status
        sub.provider = provider_name
        if customer_id:
            sub.provider_customer_id = customer_id
        if sub_id:
            sub.provider_subscription_id = sub_id
        # Handle cancellation
        if status in ("canceled", "cancelled"):
            sub.cancel_at_period_end = True
            sub.grace_until = datetime.utcnow() + timedelta(days=3)
        elif status == "past_due":
            sub.grace_until = datetime.utcnow() + timedelta(days=3)
        elif status == "active":
            sub.grace_until = None
            sub.cancel_at_period_end = False
            sub.current_period_start = datetime.utcnow()
            sub.current_period_end = datetime.utcnow() + timedelta(days=30)

    db.commit()
    db.refresh(sub)
    log.info("subscription updated user=%s plan=%s status=%s provider=%s", user_id, sub.plan, sub.status, provider_name)
    return sub


def create_or_update_subscription_manual(db: Session, user_id: int, plan: str, status: str = "active", trial_days: int = 0) -> Subscription:
    """Manual upgrade/downgrade for testing or admin."""
    if plan not in ("free", "pro", "pro_plus"):
        raise ValueError(f"unknown plan {plan}")
    sub = db.query(Subscription).filter(Subscription.user_id == user_id).first()
    now = datetime.utcnow()
    if not sub:
        sub = Subscription(
            user_id=user_id,
            plan=plan,
            status=status,
            provider="manual",
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
            trial_end=now + timedelta(days=trial_days) if trial_days else None,
        )
        db.add(sub)
    else:
        sub.plan = plan
        sub.status = status
        sub.provider = "manual"
        sub.current_period_start = now
        sub.current_period_end = now + timedelta(days=30)
        if trial_days:
            sub.trial_end = now + timedelta(days=trial_days)
    db.commit()
    db.refresh(sub)
    return sub
