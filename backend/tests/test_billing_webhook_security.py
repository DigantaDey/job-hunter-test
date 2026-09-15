"""
Payment-webhook trust boundaries (backend/app/api/routers/billing.py).

Proves the four acceptance properties:
  (a) unsigned / forged Stripe and Razorpay events are rejected (4xx) when a
      webhook secret IS configured — signatures are real HMAC-SHA256 over the
      raw body, constant-time compared, timestamp-tolerant (Stripe);
  (b) every billing webhook route fails CLOSED when its secret/token is
      unset: 503 webhook_not_configured in production (and for the manual
      route in every environment), 400 webhook_not_configured in dev —
      verification is never "skipped with a warning";
  (c) a duplicate event_id applied twice has its side effect applied once
      (insert-first BillingEvent row + IntegrityError → stored 200-ok),
      including under a concurrent retry race;
  (d) the manual webhook cannot set an arbitrary user's plan: identity comes
      only from the provider ids stored on a Subscription row (unknown →
      logged 404), the plan only from the server-side configured price map —
      user_id/plan/status fields in the body are inert.

Also pins the config audit: the .env.example may not declare env vars the
Settings object never reads (the STRIPE_SECRET_KEY/STRIPE_API_KEY class of
bug), and startup validation flags a selected provider without a webhook
secret.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time

import pytest

from app.core.config import REPO_DIR, Settings, settings
from app.db import SessionLocal
from app.models.models import BillingEvent, Subscription, User
from app.services import billing as billing_service
from app.services.billing import (
    STRIPE_SIGNATURE_TOLERANCE_SECONDS,
    compute_stripe_signature,
    parse_stripe_signature_header,
    resolve_plan_from_price,
    verify_razorpay_signature,
    verify_stripe_signature,
)

STRIPE_SECRET = "whsec_unit_test_secret_0f2a"
RAZORPAY_SECRET = "rzp_whsec_unit_test_9c31"
MANUAL_TOKEN = "manual-webhook-token-42"

PRICE_PRO = "price_test_pro_monthly"
PRICE_PRO_PLUS = "price_test_pro_plus_monthly"
RZP_PLAN_PRO = "plan_test_pro_monthly"

MANUAL_HEADERS = {"X-Webhook-Token": MANUAL_TOKEN}


@pytest.fixture
def secrets(monkeypatch):
    """All three webhook credentials configured (acceptance (a)/(c)/(d) baseline)."""
    monkeypatch.setattr(settings, "stripe_webhook_secret", STRIPE_SECRET)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", RAZORPAY_SECRET)
    monkeypatch.setattr(settings, "email_webhook_token", MANUAL_TOKEN)
    monkeypatch.setattr(settings, "stripe_price_pro_monthly", PRICE_PRO)
    monkeypatch.setattr(settings, "stripe_price_pro_plus_monthly", PRICE_PRO_PLUS)
    monkeypatch.setattr(settings, "razorpay_plan_pro_monthly", RZP_PLAN_PRO)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def sign_stripe(body: bytes, secret: str = STRIPE_SECRET, *, timestamp: int | None = None) -> str:
    ts = str(int(time.time()) if timestamp is None else timestamp)
    return f"t={ts},v1={compute_stripe_signature(secret, ts, body)}"


def sign_razorpay(body: bytes, secret: str = RAZORPAY_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def make_user(db, email: str) -> User:
    user = User(email=email, password_hash="not-a-real-hash", role="member", name=email.split("@")[0])
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_sub(db, user: User, *, plan="free", customer_id="", subscription_id="", provider="stripe"):
    sub = Subscription(user_id=user.id, plan=plan, status="active", provider=provider,
                       provider_customer_id=customer_id, provider_subscription_id=subscription_id)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def plan_of(db, user_id: int) -> str:
    sub = db.query(Subscription).filter(Subscription.user_id == user_id).first()
    return sub.plan if sub else "<none>"


def stripe_subscription_event(event_id: str, *, customer="cus_500", sub_id="sub_500",
                              price=PRICE_PRO, status="active") -> bytes:
    """A well-formed Stripe customer.subscription.updated event.

    Deliberately carries metadata user_id/plan pointing at another user on a
    better plan — every route must ignore that channel entirely.
    """
    obj = {
        "id": sub_id,
        "object": "customer.subscription",
        "customer": customer,
        "status": status,
        "items": {"data": [{"price": {"id": price}}]},
        "metadata": {"user_id": "999999", "plan": "pro_plus"},
    }
    return json.dumps({"id": event_id, "type": "customer.subscription.updated",
                       "data": {"object": obj}}).encode()


def razorpay_subscription_event(event_id: str, *, sub_id="sub_rzp_1", plan_id=RZP_PLAN_PRO,
                                status="authenticated") -> bytes:
    body = {
        "event": "subscription.charge.succeeded",
        "event_id": event_id,
        "payload": {"subscription": {"entity": {
            "id": sub_id,
            "status": status,
            "plan_id": plan_id,
            "customer_id": "cus_rzp_1",
            "notes": {"user_id": 999999, "plan": "pro_plus"},  # ignored by design
        }}},
    }
    return json.dumps(body).encode()


def billing_event_count(db, event_id: str) -> int:
    return db.query(BillingEvent).filter(BillingEvent.provider_event_id == event_id).count()


@pytest.fixture
def apply_spy(monkeypatch):
    """Count apply_subscription_change invocations (the side-effect gate)."""
    original = billing_service.apply_subscription_change
    calls = []

    def spy(db, user_id, payload, provider_name):
        calls.append(user_id)
        return original(db, user_id, payload, provider_name)

    monkeypatch.setattr(billing_service, "apply_subscription_change", spy)
    return calls


# --------------------------------------------------------------------------- #
# (a) Unsigned / forged provider events are rejected — 4xx, never applied
# --------------------------------------------------------------------------- #
def test_stripe_unsigned_and_forged_events_rejected(client, db, secrets):
    victim = make_user(db, "victim@stripe.test")
    make_sub(db, victim, plan="free", customer_id="cus_500", subscription_id="sub_500")
    body = stripe_subscription_event("evt_bad_1")

    def assert_rejected(response, expected_status, reason):
        assert response.status_code == expected_status, response.text
        assert response.json()["detail"]["code"] == "invalid_signature"
        assert reason in response.json()["detail"]["message"]
        assert plan_of(db, victim.id) == "free"

    # 1. Completely unsigned — no Stripe-Signature header at all.
    assert client.post("/api/billing/webhooks/stripe", content=body).status_code == 400
    # 2. Garbage header.
    r = client.post("/api/billing/webhooks/stripe", content=body,
                    headers={"Stripe-Signature": "not-even-close"})
    assert_rejected(r, 400, "malformed_signature_header")
    # 3. Signed against the body WITHOUT the timestamp prefix (the old broken
    #    HMAC) — proves the "{timestamp}.{raw_body}" construction is enforced.
    naive = hmac.new(STRIPE_SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert client.post("/api/billing/webhooks/stripe", content=body,
                       headers={"Stripe-Signature": f"t={int(time.time())},v1={naive}"}).status_code == 401
    # 4. Right shape, wrong secret (forgery).
    assert client.post("/api/billing/webhooks/stripe", content=body,
                       headers={"Stripe-Signature": sign_stripe(body, secret="whsec_wrong")}).status_code == 401
    # 5. Correctly signed but replayed outside the 300s tolerance window.
    stale = sign_stripe(body, timestamp=int(time.time()) - STRIPE_SIGNATURE_TOLERANCE_SECONDS - 60)
    r = client.post("/api/billing/webhooks/stripe", content=body, headers={"Stripe-Signature": stale})
    assert_rejected(r, 400, "timestamp_outside_tolerance")
    # 6. Signed legitimately, then the body is swapped for an escalated
    #    price mid-flight. Raw-body binding is what catches this.
    escalated = body.replace(PRICE_PRO.encode(), PRICE_PRO_PLUS.encode())
    header_of_original = sign_stripe(body)
    r = client.post("/api/billing/webhooks/stripe", content=escalated,
                    headers={"Stripe-Signature": header_of_original})
    assert r.status_code == 401, r.text

    assert plan_of(db, victim.id) == "free"
    # No event from any rejected attempt was recorded — nothing got that far.
    assert db.query(BillingEvent).count() == 0


def test_razorpay_unsigned_and_forged_events_rejected(client, db, secrets):
    victim = make_user(db, "victim@razorpay.test")
    make_sub(db, victim, plan="free", customer_id="cus_rzp_1", subscription_id="sub_rzp_1")
    body = razorpay_subscription_event("evt_rzp_bad")

    # Unsigned.
    assert client.post("/api/billing/webhooks/razorpay", content=body).status_code == 400
    # Forged (wrong secret) → 401.
    r = client.post("/api/billing/webhooks/razorpay", content=body,
                    headers={"X-Razorpay-Signature": sign_razorpay(body, secret="wrong-secret")})
    assert r.status_code == 401
    assert r.json()["detail"]["message"].endswith("signature_mismatch")
    # Signed, then the body is swapped.
    honest_sig = sign_razorpay(body)
    tampered = body.replace(b"sub_rzp_1", b"sub_evil_9")
    assert client.post("/api/billing/webhooks/razorpay", content=tampered,
                       headers={"X-Razorpay-Signature": honest_sig}).status_code == 401

    assert plan_of(db, victim.id) == "free"
    assert db.query(BillingEvent).count() == 0


def test_validly_signed_events_apply_only_configured_plan_mapping(client, db, secrets, apply_spy):
    """Positive control for (a): a genuine signature DOES work — and the plan
    comes from the configured price id, never from the (ignored) metadata."""
    user = make_user(db, "payer@stripe.test")
    victim = make_user(db, "other@stripe.test")
    make_sub(db, user, plan="free", customer_id="cus_500", subscription_id="sub_500")
    make_sub(db, victim, plan="free", customer_id="cus_other")

    body = stripe_subscription_event("evt_good_1")
    r = client.post("/api/billing/webhooks/stripe", content=body,
                    headers={"Stripe-Signature": sign_stripe(body)})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert plan_of(db, user.id) == "pro"           # from PRICE_PRO via the server map
    assert plan_of(db, victim.id) == "free"        # metadata user_id=999999 ignored

    # Razorpay, same story via plan_id.
    rz_user = make_user(db, "payer@razorpay.test")
    make_sub(db, rz_user, plan="free", subscription_id="sub_rzp_1", provider="razorpay")
    rbody = razorpay_subscription_event("evt_rzp_good")
    r = client.post("/api/billing/webhooks/razorpay", content=rbody,
                    headers={"X-Razorpay-Signature": sign_razorpay(rbody)})
    assert r.status_code == 200, r.text
    assert plan_of(db, rz_user.id) == "pro"
    assert len(apply_spy) == 2  # one side effect per fresh event


# --------------------------------------------------------------------------- #
# (b) Unconfigured secrets/tokens → fail closed everywhere
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("env", ["test", "production"])
def test_provider_webhooks_fail_closed_without_secrets(client, db, monkeypatch, env):
    monkeypatch.setattr(settings, "environment", env)
    monkeypatch.setattr(settings, "stripe_webhook_secret", "")
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "")
    monkeypatch.setattr(settings, "email_webhook_token", "")
    expected = 503 if env == "production" else 400

    body = stripe_subscription_event("evt_nosecret")
    # Even a header that would be structurally "correct" is refused — there is
    # no secret to verify against, so nothing can ever be trusted.
    r = client.post("/api/billing/webhooks/stripe", content=body,
                    headers={"Stripe-Signature": f"t={int(time.time())},v1=whatever"})
    assert r.status_code == expected
    assert r.json()["detail"]["code"] == "webhook_not_configured"

    r = client.post("/api/billing/webhooks/razorpay", content=body,
                    headers={"X-Razorpay-Signature": "whatever"})
    assert r.status_code == expected
    assert r.json()["detail"]["code"] == "webhook_not_configured"

    # Manual: the token is required in ALL environments — 503 even in dev.
    r = client.post("/api/billing/webhooks/manual", json={"event_id": "x"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "webhook_not_configured"

    assert db.query(BillingEvent).count() == 0


def test_wrong_manual_token_is_401_in_every_environment(client, monkeypatch):
    for env in ("test", "production"):
        monkeypatch.setattr(settings, "environment", env)
        monkeypatch.setattr(settings, "email_webhook_token", "the-real-token")
        r = client.post("/api/billing/webhooks/manual", json={"event_id": "x"},
                        headers={"X-Webhook-Token": "guessed"})
        assert r.status_code == 401, (env, r.text)


# --------------------------------------------------------------------------- #
# (c) Duplicate event_id: applied exactly once
# --------------------------------------------------------------------------- #
def test_duplicate_event_id_applied_once(client, db, secrets, apply_spy):
    user = make_user(db, "payer@test")
    make_sub(db, user, plan="free", customer_id="cus_500", subscription_id="sub_500")
    body = stripe_subscription_event("evt_once")
    headers = {"Stripe-Signature": sign_stripe(body)}

    first = client.post("/api/billing/webhooks/stripe", content=body, headers=headers)
    assert first.status_code == 200 and first.json()["processed"] is True
    assert plan_of(db, user.id) == "pro"
    assert len(apply_spy) == 1

    # Provider retry: identical event, identical signature.
    replay = client.post("/api/billing/webhooks/stripe", content=body, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["ok"] is True
    assert replay.json()["event_id"] == "evt_once"
    assert len(apply_spy) == 1                      # side effect ran exactly once
    assert billing_event_count(db, "evt_once") == 1  # and was recorded exactly once


def test_duplicate_event_never_reapplies_even_with_different_payload(db, secrets, apply_spy):
    """Service level: the second delivery is rejected at the unique-constraint
    insert itself — a forged same-id/different-content event changes nothing."""
    user = make_user(db, "payer2@test")
    make_sub(db, user, plan="free", subscription_id="sub_500")
    payload_pro = {"status": "active", "price_id": PRICE_PRO,
                   "provider_subscription_id": "sub_500", "provider_customer_id": ""}
    first = billing_service.handle_billing_event(db, "manual", "evt_race", "manual.update", payload_pro,
                                                  user_id=user.id)
    assert first.processed is True and plan_of(db, user.id) == "pro"

    # A second writer (separate session) with the SAME event_id but an
    # escalated price must bounce off the constraint and return the stored row.
    other = SessionLocal()
    try:
        payload_plus = {"status": "active", "price_id": PRICE_PRO_PLUS,
                        "provider_subscription_id": "sub_500", "provider_customer_id": ""}
        second = billing_service.handle_billing_event(other, "manual", "evt_race", "manual.update",
                                                       payload_plus, user_id=user.id)
        assert second.id == first.id          # the stored event, not a new one
    finally:
        other.close()
    assert plan_of(db, user.id) == "pro"      # escalation never applied
    assert len(apply_spy) == 1                # side effect executed once
    assert billing_event_count(db, "evt_race") == 1


def test_concurrent_retries_single_row(client, db, secrets, apply_spy):
    """True race: two threads insert the same event simultaneously. The losing
    commit gets IntegrityError and returns the stored 200-ok row instead of
    500-ing or double-applying; the final state shows the effect once."""
    user = make_user(db, "racer@test")
    make_sub(db, user, plan="free", subscription_id="sub_race")
    payload = {"status": "active", "price_id": PRICE_PRO,
               "provider_subscription_id": "sub_race", "provider_customer_id": ""}

    barrier = threading.Barrier(2)
    results: dict[int, object] = {}
    errors: list[BaseException] = []

    def worker(index: int):
        session = SessionLocal()
        try:
            barrier.wait(timeout=15)
            results[index] = billing_service.handle_billing_event(
                session, "manual", "evt_concurrent", "manual.update", payload, user_id=user.id)
        except BaseException as exc:  # noqa: BLE001 - surfaced as a failure below
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    # Both deliveries acknowledged, both reference the one stored event, and
    # the plan reflects the event — applied, but recorded exactly once.
    assert {getattr(ev, "provider_event_id", None) for ev in results.values()} == {"evt_concurrent"}
    assert all(ev.processed for ev in results.values())
    assert billing_event_count(db, "evt_concurrent") == 1
    assert plan_of(db, user.id) == "pro"


# --------------------------------------------------------------------------- #
# (d) The manual webhook cannot set an arbitrary user's plan
# --------------------------------------------------------------------------- #
def test_manual_webhook_cannot_target_unmapped_users(client, db, secrets, apply_spy, caplog):
    victim = make_user(db, "victim@test")
    make_sub(db, victim, plan="pro", subscription_id="sub_victim", provider="manual")
    attacker_target = make_user(db, "target@test")
    make_sub(db, attacker_target, plan="free", subscription_id="sub_target", provider="manual")

    with caplog.at_level(logging.WARNING, logger="app.billing"):
        # Attack 1: name the victim's user_id + plan directly (the old API).
        r = client.post("/api/billing/webhooks/manual", headers=MANUAL_HEADERS, json={
            "event_id": "manual_atk_1", "user_id": attacker_target.id, "plan": "pro_plus",
            "status": "active", "subscription_id": "sub_does_not_exist",
        })
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "user_not_found"
        # Attack 2: omit any provider mapping entirely.
        assert client.post("/api/billing/webhooks/manual", headers=MANUAL_HEADERS,
                           json={"event_id": "manual_atk_2", "user_id": victim.id,
                                 "plan": "free", "status": "canceled"}).status_code == 404
        # Attack 3: no token at all (token is set in this fixture).
        assert client.post("/api/billing/webhooks/manual",
                           json={"event_id": "manual_atk_3", "subscription_id": "sub_victim"}).status_code == 401

    assert plan_of(db, victim.id) == "pro"          # nobody downgraded
    assert plan_of(db, attacker_target.id) == "free"  # nobody upgraded
    assert len(apply_spy) == 0
    assert db.query(BillingEvent).count() == 0
    messages = " | ".join(r.message for r in caplog.records)
    assert "manual billing webhook rejected" in messages   # rejections are logged
    assert "invalid token" in messages                      # including the auth failure


def test_manual_webhook_identity_and_plan_channels_are_structural(client, db, secrets, apply_spy):
    """Positive control for (d): identity ONLY via the stored mapping, plan ONLY
    via a configured price id. The body may shout plan/user_id all it likes."""
    user = make_user(db, "mapped@test")
    make_sub(db, user, plan="free", customer_id="cus_mapped", subscription_id="sub_mapped",
             provider="manual")
    bystander = make_user(db, "bystander@test")
    make_sub(db, bystander, plan="free", subscription_id="sub_bystander", provider="manual")

    # Body carries hostile user_id/plan; the event references user's stored
    # subscription and a configured price → user goes to pro (not what the
    # body asked for, and not the bystander).
    r = client.post("/api/billing/webhooks/manual", headers=MANUAL_HEADERS, json={
        "event_id": "manual_ok_1",
        "subscription_id": "sub_mapped",
        "price": PRICE_PRO_PLUS,          # server map decides: pro_plus
        "plan": "free",                   # ignored
        "user_id": bystander.id,          # ignored
        "status": "active",
    })
    assert r.status_code == 200, r.text
    assert plan_of(db, user.id) == "pro_plus"
    assert plan_of(db, bystander.id) == "free"
    assert len(apply_spy) == 1

    # Unknown price id → fail-safe: the event is accepted and recorded (valid
    # token, mapped user) but the plan is untouched — no "pro_plus" default.
    r = client.post("/api/billing/webhooks/manual", headers=MANUAL_HEADERS, json={
        "event_id": "manual_ok_2", "subscription_id": "sub_mapped",
        "price": "price_malicious", "plan": "pro_plus", "status": "active",
    })
    assert r.status_code == 200, r.text
    assert r.json()["plan"] == "unchanged"
    assert plan_of(db, user.id) == "pro_plus"

    # Customer-id mapping is the second supported channel.
    r = client.post("/api/billing/webhooks/manual", headers=MANUAL_HEADERS, json={
        "event_id": "manual_ok_3", "customer_id": "cus_mapped",
        "price": PRICE_PRO, "status": "active",
    })
    assert r.status_code == 200, r.text
    assert plan_of(db, user.id) == "pro"

    # Body hygiene: event_id is mandatory (dedupe), status is a closed set.
    assert client.post("/api/billing/webhooks/manual", headers=MANUAL_HEADERS,
                       json={"subscription_id": "sub_mapped"}).status_code == 400
    bad = client.post("/api/billing/webhooks/manual", headers=MANUAL_HEADERS,
                      json={"event_id": "manual_ok_4", "subscription_id": "sub_mapped",
                            "status": "whatever_i_want"})
    assert bad.status_code == 400

    # Replays of the accepted manual event dedupe the side effect (c, again,
    # on the manual route specifically).
    before = len(apply_spy)
    for _ in range(2):
        dup = client.post("/api/billing/webhooks/manual", headers=MANUAL_HEADERS, json={
            "event_id": "manual_ok_3", "customer_id": "cus_mapped",
            "price": "price_ignored", "status": "canceled",
        })
        assert dup.status_code == 200
    assert len(apply_spy) == before
    assert plan_of(db, user.id) == "pro"  # canceled status of a replay never applied


# --------------------------------------------------------------------------- #
# Signature primitives (unit) — construction details the routes rely on
# --------------------------------------------------------------------------- #
def test_stripe_signature_construction_and_latest_v1():
    body = json.dumps({"id": "evt_x", "type": "customer.subscription.created",
                       "data": {"object": {"status": "active", "customer": "cus_1"}}}).encode()
    ts = str(int(time.time()))
    good = compute_stripe_signature(STRIPE_SECRET, ts, body)

    # Round trip.
    ok, reason = verify_stripe_signature(STRIPE_SECRET, body, f"t={ts},v1={good}")
    assert ok and reason == ""

    # Header parsing collects every v1 in order.
    assert parse_stripe_signature_header(f"t={ts},v1=aaa,v1=bbb,v1={good}") == (ts, ["aaa", "bbb", good])
    # "compare the latest v1": a stale-rotation candidate first is fine, the
    # LAST entry is the one that must match.
    assert verify_stripe_signature(STRIPE_SECRET, body, f"t={ts},v1={good},v1=stale-rotation")[0] is False
    assert verify_stripe_signature(STRIPE_SECRET, body, f"t={ts},v1=stale-rotation,v1={good}")[0] is True

    # Failures: wrong secret, wrong body, non-numeric timestamp, and — crucially —
    # an unset secret NEVER verifies.
    assert verify_stripe_signature("other-secret", body, f"t={ts},v1={good}")[0] is False
    assert verify_stripe_signature(STRIPE_SECRET, body + b" ", f"t={ts},v1={good}")[0] is False
    assert verify_stripe_signature(STRIPE_SECRET, body, "t=abc,v1=x") == (False, "malformed_signature_timestamp")
    ok, reason = verify_stripe_signature("", body, f"t={ts},v1={good}")
    assert ok is False and reason == "webhook_secret_not_configured"


def test_razorpay_signature_primitives_fail_closed():
    body = b'{"event":"subscription.cancelled"}'
    sig = sign_razorpay(body)
    assert verify_razorpay_signature(RAZORPAY_SECRET, body, sig)[0] is True
    assert verify_razorpay_signature(RAZORPAY_SECRET, body, "f" * 64)[0] is False
    assert verify_razorpay_signature(RAZORPAY_SECRET, body, "")[0] is False
    ok, reason = verify_razorpay_signature("", body, sig)
    assert ok is False and reason == "webhook_secret_not_configured"


def test_parse_events_do_not_emit_identity_or_plan_channels():
    stripe = billing_service.StripeProvider().parse_event(stripe_subscription_event("evt_p"))
    rzp = billing_service.RazorpayProvider().parse_event(razorpay_subscription_event("evt_p2"))
    manual = billing_service.ManualProvider().parse_event(
        json.dumps({"event_id": "evt_p3", "user_id": 7, "plan": "pro_plus",
                    "subscription_id": "sub_x"}).encode())
    for parsed in (stripe, rzp, manual):
        assert "user_id" not in parsed
        assert "plan" not in parsed
    assert stripe["price_id"] == PRICE_PRO and stripe["provider_customer_id"] == "cus_500"
    assert rzp["status"] == "active" and rzp["price_id"] == RZP_PLAN_PRO
    assert manual["price_id"] == ""  # body "plan" is not even a lookup key — only "price" is
    assert manual["provider_subscription_id"] == "sub_x"


def test_resolve_plan_only_from_server_configuration(monkeypatch):
    monkeypatch.setattr(settings, "stripe_price_pro_monthly", PRICE_PRO)
    monkeypatch.setattr(settings, "stripe_price_pro_plus_monthly", PRICE_PRO_PLUS)
    monkeypatch.setattr(settings, "razorpay_plan_pro_monthly", "")
    assert resolve_plan_from_price(PRICE_PRO) == "pro"
    assert resolve_plan_from_price(PRICE_PRO_PLUS) == "pro_plus"
    assert resolve_plan_from_price("pro_plus") is None      # a raw plan name is not a price id
    assert resolve_plan_from_price("") is None and resolve_plan_from_price(None) is None
    monkeypatch.setattr(settings, "stripe_price_pro_monthly", "")
    assert resolve_plan_from_price(PRICE_PRO) is None       # no config → nothing recognized


# --------------------------------------------------------------------------- #
# Startup config validation + the .env.example audit
# --------------------------------------------------------------------------- #
def test_startup_flags_provider_without_webhook_secret():
    s = Settings(environment="development", billing_provider="stripe", stripe_webhook_secret="",
                 email_webhook_token="t")
    problems = s.billing_webhook_problems()
    assert any("STRIPE_WEBHOOK_SECRET" in p for p in problems)
    assert any("webhook_not_configured" in p for p in problems)
    # Dev warnings surface the same problem (the router still refuses).
    assert any("STRIPE_WEBHOOK_SECRET" in w for w in s.warnings())
    assert s.stripe_webhook_secret_configured is False

    fixed = Settings(environment="development", billing_provider="stripe", stripe_webhook_secret="whsec_real")
    assert fixed.billing_webhook_problems() == []
    assert fixed.stripe_webhook_secret_configured is True

    rz = Settings(environment="development", billing_provider="razorpay", razorpay_webhook_secret="")
    assert any("RAZORPAY_WEBHOOK_SECRET" in p for p in rz.billing_webhook_problems())

    # manual needs no provider secret; case/whitespace in BILLING_PROVIDER are
    # normalised so `STRIPE` is not silently treated as `manual`.
    assert Settings(environment="development", billing_provider="manual").billing_webhook_problems() == []
    shouty = Settings(environment="development", billing_provider="STRIPE ", stripe_webhook_secret="")
    assert shouty.billing_provider == "stripe"
    assert shouty.billing_webhook_problems()


def test_production_boots_but_refuses_webhook_routes_not_configured():
    """Missing webhook secrets do not brick boot (the routes are the boundary)."""
    s = Settings(
        environment="production",
        secret_key="a-very-long-and-unique-production-secret-key-1234567890",
        encryption_key="another-long-and-unique-encryption-key-1234567890",
        database_url="postgresql+psycopg2://user:pass@db:5432/jobhunter",
        cors_origins="https://app.example.com",
        allowed_hosts="app.example.com",
        metrics_token="metrics-token",
        public_base_url="https://app.example.com",
        billing_provider="stripe",
        stripe_webhook_secret="",
    )
    assert s.is_production and s.billing_webhook_problems()


def test_env_example_declares_only_real_settings_fields():
    """Audit: every active KEY in .env.example maps to a Settings field.

    The old file shipped STRIPE_SECRET_KEY while the app read STRIPE_API_KEY —
    a name the loader never consults, silently dropped (extra="ignore"). One
    class of typo this test pins out of existence.
    """
    path = os.path.join(REPO_DIR, ".env.example")
    text = open(path, encoding="utf-8").read()
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]*)\s*=", text, re.M))
    fields = {name.upper() for name in Settings.model_fields}
    unknown = sorted(declared - fields)
    assert not unknown, f".env.example declares {unknown}, which Settings never reads"
    # The corrected name is present — and the dead alias is gone.
    assert "STRIPE_API_KEY" in declared
    assert "STRIPE_SECRET_KEY" not in declared
    # And every field the billing routes depend on is documented.
    for key in ("STRIPE_WEBHOOK_SECRET", "RAZORPAY_WEBHOOK_SECRET", "EMAIL_WEBHOOK_TOKEN",
                "STRIPE_PRICE_PRO_MONTHLY", "RAZORPAY_PLAN_PRO_MONTHLY"):
        assert key in declared
