"""
Billing provider abstraction for Stripe and Razorpay.

Design:
- Interface BillingProvider with methods: create_customer, create_checkout,
  verify_webhook, parse_event.
- Manual provider for self-hosted / testing.
- Webhook verification is **mandatory**: ``verify_webhook`` fails closed when
  the provider secret is unset (it never "skips with a warning"), signatures
  are computed over the *raw request body* and compared in constant time.
- Webhook identity and plan are never taken from the event body: the user is
  resolved from the provider subscription/customer id stored on the
  Subscription row, and the plan is derived from the event's price/plan id via
  the server-side configured maps (STRIPE_PRICE_* / RAZORPAY_PLAN_*).
- Webhook handlers are idempotent under concurrent retries: the BillingEvent
  row is inserted (and committed) *before* any side effect, so the unique
  (provider, provider_event_id) constraint is the idempotency gate.
- All provider secrets via settings, never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import constant_time_equals
from app.models.models import BillingEvent, Subscription, User

log = get_logger("app.billing")

#: Rejection window for Stripe webhook timestamps (seconds). Mirrors the
#: ``stripe listen`` / stripe-python default so replays stop being useful
#: while modest clock skew is tolerated.
STRIPE_SIGNATURE_TOLERANCE_SECONDS = 300

#: Statuses a manual (self-hosted) billing event may report. Anything else is
#: a malformed event, not a subscription change.
MANUAL_WEBHOOK_STATUSES = {
    "active",
    "trialing",
    "incomplete",
    "incomplete_expired",
    "past_due",
    "unpaid",
    "paused",
    "canceled",
    "cancelled",
    "expired",
}


# --------------------------------------------------------------------------- #
# Signature verification (raw-body HMAC, constant-time compare, fail closed)
# --------------------------------------------------------------------------- #
def parse_stripe_signature_header(header: str) -> Tuple[str, List[str]]:
    """Extract the ``t=`` timestamp and the list of ``v1=`` signatures.

    Stripe sends ``t=<unix-seconds>,v1=<hex>[,v1=<hex>...]``; multiple ``v1``
    entries exist during secret rotation, so they are collected in order.
    """
    timestamp = ""
    candidates: List[str] = []
    for part in (header or "").split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "t" and not timestamp:
            timestamp = value
        elif key == "v1" and value:
            candidates.append(value)
    return timestamp, candidates


def compute_stripe_signature(secret: str, timestamp: str, payload: bytes) -> str:
    """HMAC-SHA256 over ``"{timestamp}.{raw_body}"`` — exactly Stripe's scheme.

    Binding the timestamp into the signed message is what makes a captured
    webhook useless after the tolerance window; signing the raw body (not
    re-serialised JSON) is what makes the check meaningful at all.
    """
    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    return hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()


def verify_stripe_signature(
    secret: str,
    payload: bytes,
    signature_header: str,
    *,
    tolerance_seconds: int = STRIPE_SIGNATURE_TOLERANCE_SECONDS,
    now: Optional[float] = None,
) -> Tuple[bool, str]:
    """Verify a ``Stripe-Signature`` header. Returns ``(ok, reason)``.

    Fails closed on every path — most importantly when ``secret`` is unset:
    an unconfigured webhook secret is a configuration error (the route turns
    it into 400/503), never a licence to accept unsigned events.
    """
    if not (secret or "").strip():
        return False, "webhook_secret_not_configured"
    timestamp, candidates = parse_stripe_signature_header(signature_header)
    if not timestamp or not candidates:
        return False, "malformed_signature_header"
    try:
        signed_at = int(timestamp)
    except ValueError:
        return False, "malformed_signature_timestamp"
    current = int(time.time() if now is None else now)
    if abs(current - signed_at) > max(int(tolerance_seconds), 0):
        return False, "timestamp_outside_tolerance"
    expected = compute_stripe_signature(secret, timestamp, payload)
    # Compare the latest v1 entry (the canonical stripe-python behaviour).
    candidate = candidates[-1]
    if not hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8")):
        return False, "signature_mismatch"
    return True, ""


def verify_razorpay_signature(secret: str, payload: bytes, signature: str) -> Tuple[bool, str]:
    """Verify Razorpay's ``X-Razorpay-Signature``: HMAC-SHA256 of the raw body.

    Fails closed when the webhook secret is unset — never "skip with warning".
    """
    if not (secret or "").strip():
        return False, "webhook_secret_not_configured"
    if not (signature or "").strip():
        return False, "missing_signature"
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.strip().encode("utf-8"), expected.encode("utf-8")):
        return False, "signature_mismatch"
    return True, ""


# --------------------------------------------------------------------------- #
# Server-side plan mapping — the body can name a *price id*, never a plan
# --------------------------------------------------------------------------- #
def configured_plan_ids() -> Dict[str, str]:
    """Provider price/plan ids configured on this server → internal plan.

    Single source of truth for what a webhook event's price/plan id means.
    Deployment config (STRIPE_PRICE_*, RAZORPAY_PLAN_*) decides; the event body
    only supplies a lookup key. An empty mapping means "no price is recognised"
    — which is fail-safe: no plan change, not "trust whatever the body says".
    """
    mapping: Dict[str, str] = {}
    pairs = (
        (settings.stripe_price_pro_monthly, "pro"),
        (settings.stripe_price_pro_plus_monthly, "pro_plus"),
        (settings.stripe_price_pro_yearly, "pro"),
        (settings.stripe_price_pro_plus_yearly, "pro_plus"),
        (settings.razorpay_plan_pro_monthly, "pro"),
        (settings.razorpay_plan_pro_plus_monthly, "pro_plus"),
    )
    for price_id, plan in pairs:
        key = (price_id or "").strip()
        if key:
            mapping[key] = plan
    return mapping


def resolve_plan_from_price(price_id: Optional[str]) -> Optional[str]:
    """Map a webhook price/plan id to an internal plan (``None`` = not configured)."""
    key = str(price_id or "").strip()
    if not key:
        return None
    return configured_plan_ids().get(key)


class BillingProvider(ABC):
    @abstractmethod
    def name(self) -> str: ...

    def create_customer(self, user: User) -> Dict[str, Any]:
        return {"customer_id": f"cust_{user.id}"}

    def create_checkout_session(self, user: User, plan: str, success_url: str, cancel_url: str) -> Dict[str, Any]:
        # Stub: returns fake checkout URL
        return {"checkout_url": f"{success_url}?plan={plan}&user={user.id}", "session_id": f"sess_{user.id}_{plan}"}

    @abstractmethod
    def verify_webhook(self, payload: bytes, signature: str) -> Tuple[bool, str]:
        """Verify the raw webhook body against the provider signature.

        ``(False, reason)`` whenever verification is impossible — including
        when the shared secret is unset. Never "True, with a warning".
        """

    @abstractmethod
    def parse_event(self, payload: bytes) -> Dict[str, Any]:
        """Normalise a *verified* provider payload into an internal event.

        Output never carries ``user_id`` or ``plan``: identity is resolved
        from stored provider ids and the plan from the configured price map.
        """


class ManualProvider(BillingProvider):
    def name(self) -> str:
        return "manual"

    def verify_webhook(self, payload: bytes, signature: str) -> Tuple[bool, str]:
        # Manual provider: token auth (EMAIL_WEBHOOK_TOKEN) is mandatory in
        # every environment — unset token fails closed, configured token is
        # compared in constant time.
        if not (settings.email_webhook_token or "").strip():
            return False, "webhook_token_not_configured"
        if not constant_time_equals(signature, settings.email_webhook_token):
            return False, "invalid_webhook_token"
        return True, ""

    def parse_event(self, payload: bytes) -> Dict[str, Any]:
        try:
            data = json.loads(payload) if payload else {}
            if not isinstance(data, dict):
                raise ValueError("manual webhook body must be a JSON object")
            return {
                "event_id": str(data.get("event_id") or f"manual_{hashlib.sha256(payload).hexdigest()[:12]}"),
                "kind": str(data.get("kind") or "manual.update"),
                "status": str(data.get("status") or "active").strip().lower(),
                "price_id": str(data.get("price") or data.get("price_id") or ""),
                "provider_customer_id": str(data.get("customer_id") or ""),
                "provider_subscription_id": str(data.get("subscription_id") or ""),
            }
        except Exception as e:
            return {"event_id": f"manual_err_{datetime.utcnow().timestamp()}", "kind": "parse_error", "error": str(e)}


class StripeProvider(BillingProvider):
    def name(self) -> str:
        return "stripe"

    def verify_webhook(self, payload: bytes, signature: str) -> Tuple[bool, str]:
        return verify_stripe_signature(settings.stripe_webhook_secret, payload, signature)

    def parse_event(self, payload: bytes) -> Dict[str, Any]:
        try:
            data = json.loads(payload)
            # Stripe event shape: {id, type, data: {object: {...}}}. The object
            # is echoed *as Stripe signed it*, so ids/status/price here are
            # trustworthy — identity and plan still never are (see below).
            obj = (data.get("data") or {}).get("object") or {}
            event_type = str(data.get("type") or "")
            return {
                "event_id": str(data.get("id") or f"stripe_{hashlib.sha256(payload).hexdigest()[:12]}"),
                "kind": event_type or "stripe.unknown",
                "status": _stripe_status(event_type, obj),
                # Only the price/plan *id*; the internal plan is resolved
                # server-side from the configured maps. Metadata user_id is
                # deliberately not read — stored subscription mappings own identity.
                "price_id": _stripe_price_id(obj),
                "provider_customer_id": str(obj.get("customer") or ""),
                "provider_subscription_id": str(obj.get("subscription") or "")
                or (str(obj.get("id")) if event_type.startswith("customer.subscription.") else ""),
            }
        except Exception as e:
            return {"event_id": f"stripe_err_{datetime.utcnow().timestamp()}", "kind": "parse_error", "error": str(e)}


def _stripe_status(event_type: str, obj: Dict[str, Any]) -> str:
    """Normalise the verified event into a subscription status."""
    if event_type.endswith(".deleted"):
        return "canceled"
    if event_type == "customer.subscription.paused":
        return "paused"
    if event_type in ("invoice.payment_failed",):
        return "past_due"
    if event_type in ("invoice.paid", "invoice.payment_succeeded", "checkout.session.completed"):
        return "active"
    return str(obj.get("status") or "active").strip().lower()


def _stripe_price_id(obj: Dict[str, Any]) -> str:
    """Best-effort price id from the objects Stripe sends on billing events."""
    try:  # subscription.*: items.data[0].price.id
        return str(obj["items"]["data"][0]["price"]["id"])
    except (KeyError, IndexError, TypeError):
        pass
    try:  # invoice.*: lines.data[0].pricing.price_data.price (or legacy plan.id)
        line = obj["lines"]["data"][0]
        pricing = line.get("pricing") or {}
        return str(pricing.get("price_data", {}).get("price") or (line.get("plan") or {}).get("id") or "")
    except (KeyError, IndexError, TypeError):
        pass
    try:  # bare plan object (checkout session / subscription.created variants)
        return str(obj["plan"]["id"])
    except (KeyError, IndexError, TypeError):
        return ""


class RazorpayProvider(BillingProvider):
    def name(self) -> str:
        return "razorpay"

    def verify_webhook(self, payload: bytes, signature: str) -> Tuple[bool, str]:
        return verify_razorpay_signature(settings.razorpay_webhook_secret, payload, signature)

    def parse_event(self, payload: bytes) -> Dict[str, Any]:
        try:
            data = json.loads(payload)
            # Razorpay shape: {event, payload: {subscription: {entity: {...}}}}.
            # ``notes`` used to be read for user_id/plan — removed: identity
            # comes from the stored mapping and plan from the configured id.
            entity = ((data.get("payload") or {}).get("subscription") or {}).get("entity") or {}
            event = str(data.get("event") or "")
            return {
                "event_id": str(data.get("event_id") or f"rzp_{hashlib.sha256(payload).hexdigest()[:12]}"),
                "kind": event or "razorpay.unknown",
                "status": _razorpay_status(event, entity),
                "price_id": str(entity.get("plan_id") or ""),
                "provider_customer_id": str(entity.get("customer_id") or ""),
                "provider_subscription_id": str(entity.get("id") or ""),
            }
        except Exception as e:
            return {"event_id": f"rzp_err_{datetime.utcnow().timestamp()}", "kind": "parse_error", "error": str(e)}


def _razorpay_status(event: str, entity: Dict[str, Any]) -> str:
    lowered = str(entity.get("status") or "").strip().lower()
    if event in ("subscription.cancelled", "subscription.expired") or lowered in ("cancelled", "expired", "halted"):
        return "canceled" if lowered != "halted" else "past_due"
    if event in ("subscription.paused", "subscription.halted"):
        return "past_due"
    if event in ("subscription.activated", "subscription.authenticated", "subscription.charge.succeeded"):
        return "active"
    if lowered in ("authenticated", "active"):
        return "active"
    if lowered in ("created", "pending"):
        return "incomplete"
    return lowered or "active"


def get_provider(name: str) -> BillingProvider:
    name = (name or "manual").lower()
    if name == "stripe":
        return StripeProvider()
    if name == "razorpay":
        return RazorpayProvider()
    return ManualProvider()


# --------------------------------------------------------------------------- #
# Identity — stored mappings only, never the body
# --------------------------------------------------------------------------- #
def resolve_user_id(db: Session, payload: Dict[str, Any]) -> Optional[int]:
    """Resolve the event's user from provider ids *stored on a Subscription*.

    This is the only identity channel for webhooks: the event must reference a
    provider_subscription_id or provider_customer_id that an existing
    subscription already carries (written when the checkout/subscription was
    created server-side). An event that names a user in any other way is
    ignored here — and the route turns the miss into a logged rejection.
    """
    payload = payload or {}
    prov_sub = str(payload.get("provider_subscription_id") or "").strip()
    if prov_sub:
        sub = db.query(Subscription).filter(Subscription.provider_subscription_id == prov_sub).first()
        if sub:
            return sub.user_id
    prov_cust = str(payload.get("provider_customer_id") or "").strip()
    if prov_cust:
        sub = db.query(Subscription).filter(Subscription.provider_customer_id == prov_cust).first()
        if sub:
            return sub.user_id
    return None


def handle_billing_event(
    db: Session,
    provider_name: str,
    provider_event_id: str,
    kind: str,
    payload: Dict[str, Any],
    user_id: Optional[int] = None,
) -> BillingEvent:
    """Record a webhook event and apply its side effect exactly once.

    Idempotent under concurrent retries: the BillingEvent row is inserted and
    committed *before* anything touches the subscription, so the unique
    (provider, provider_event_id) constraint is the gate. A second delivery —
    or a losing race between provider retries — gets an ``IntegrityError`` and
    returns the stored row (an ok response) instead of applying side effects
    twice. A stored-but-unprocessed event (a crash between insert and apply)
    is re-applied on delivery, so provider retries can still recover it.
    """
    # Identity via stored mappings only (never payload["user_id"]).
    if not user_id:
        user_id = resolve_user_id(db, payload)

    event = BillingEvent(
        user_id=user_id,
        provider=provider_name,
        provider_event_id=provider_event_id,
        kind=kind,
        payload=payload,
        processed=False,
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        # Duplicate delivery (sequential retry or lost race): never re-apply.
        db.rollback()
        existing = (
            db.query(BillingEvent)
            .filter(BillingEvent.provider == provider_name, BillingEvent.provider_event_id == provider_event_id)
            .first()
        )
        if existing is None:  # pragma: no cover - the row vanished mid-race
            raise
        if not existing.processed and (existing.user_id or user_id):
            if not existing.user_id and user_id:
                existing.user_id = user_id
                db.commit()
            return _apply_side_effects(db, existing)
        return existing

    if user_id:
        return _apply_side_effects(db, event)

    log.warning(
        "billing event %s (%s) matches no stored subscription mapping — recorded, not applied",
        provider_event_id,
        provider_name,
    )
    return event


def _apply_side_effects(db: Session, event: BillingEvent) -> BillingEvent:
    """Apply subscription side effects and mark the stored event processed."""
    if event.user_id is None:
        # handle_billing_event only reaches this with a resolved user (it logs
        # and skips otherwise); a stored row that predates resolution is left
        # unprocessed rather than crashed against the NOT NULL constraint.
        event.processed = False
        event.error = "no user resolved for event"
        db.commit()
        log.error("billing event %s has no user; not applying", event.provider_event_id)
        return event
    try:
        apply_subscription_change(db, event.user_id, event.payload or {}, event.provider)
        event.processed = True
        event.error = ""
        db.commit()
    except Exception as exc:  # noqa: BLE001 - the event row records the failure
        db.rollback()
        event.processed = False
        event.error = str(exc)[:2000]
        db.commit()
        log.error("billing event %s failed: %s", event.provider_event_id, exc)
    return event


def apply_subscription_change(
    db: Session,
    user_id: int,
    payload: Dict[str, Any],
    provider_name: str,
) -> Optional[Subscription]:
    """Apply a webhook's subscription state change.

    Side effects are set-from-event (plan, status, provider ids) and
    re-applyable: replaying a retried event produces the same row state, so
    the idempotency gate in :func:`handle_billing_event` is what limits
    *when* this runs, and nothing here depends on running exactly once.

    The plan is derived from the event's price/plan id through the server's
    configured maps — a body-supplied ``plan`` string is never consulted. An
    unrecognised price id leaves the current plan untouched (fail-safe).
    """
    plan = resolve_plan_from_price(payload.get("price_id"))
    status = str(payload.get("status") or "active").strip().lower()
    customer_id = str(payload.get("provider_customer_id") or "")
    sub_id = str(payload.get("provider_subscription_id") or "")

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
        if status in ("canceled", "cancelled", "expired"):
            sub.cancel_at_period_end = True
            sub.grace_until = datetime.utcnow() + timedelta(days=3)
        elif status in ("past_due", "unpaid"):
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


__all__ = [
    "BillingProvider",
    "ManualProvider",
    "StripeProvider",
    "RazorpayProvider",
    "get_provider",
    "handle_billing_event",
    "apply_subscription_change",
    "create_or_update_subscription_manual",
    "verify_stripe_signature",
    "verify_razorpay_signature",
    "compute_stripe_signature",
    "parse_stripe_signature_header",
    "configured_plan_ids",
    "resolve_plan_from_price",
    "resolve_user_id",
    "STRIPE_SIGNATURE_TOLERANCE_SECONDS",
    "MANUAL_WEBHOOK_STATUSES",
]
