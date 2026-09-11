"""
Tests for monetization: entitlements, limits, AI credit ledger, billing webhooks idempotency, multi-user isolation.
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.models import Base, User, Subscription, BillingEvent, AICreditLedger, UsageCounter
from app.core.entitlements import (
    PLANS, get_plan_config, get_user_plan, can, enforce, check_limit, increment_usage,
    entitlements_snapshot, current_period,
)

CAP_JOBS = "jobs_discovered_per_month"
CAP_TAILORED = "tailored_resumes_per_month"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def make_user(db, email=None):
    u = User(
        email=email or f"user_{uuid.uuid4().hex[:6]}@test.com",
        password_hash="hashed",
        role="user",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_plans_have_required_limits():
    for plan_key in ["free", "pro", "pro_plus"]:
        cfg = get_plan_config(plan_key)
        assert cfg["limits"]["jobs_discovered_per_month"] > 0
        assert cfg["limits"]["ai_credits_per_month"] > 0
        assert cfg["limits"]["tailored_resumes_per_month"] > 0


def test_free_more_restricted_than_pro():
    free = get_plan_config("free")
    pro = get_plan_config("pro")
    pro_plus = get_plan_config("pro_plus")
    assert free["limits"]["jobs_discovered_per_month"] < pro["limits"]["jobs_discovered_per_month"]
    assert pro["limits"]["jobs_discovered_per_month"] <= pro_plus["limits"]["jobs_discovered_per_month"]
    assert free["limits"]["outreach_per_month"] < pro["limits"]["outreach_per_month"]


def test_entitlements_snapshot(db):
    user = make_user(db)
    ent = entitlements_snapshot(db, user.id)
    assert ent["plan"] == "free"
    assert "can_analyze_job" in ent["capabilities"]
    assert ent["limits"]["jobs_discovered_per_month"] == PLANS["free"]["limits"]["jobs_discovered_per_month"]
    # Pro user via subscription
    user2 = make_user(db)
    sub = Subscription(user_id=user2.id, plan="pro", status="active", provider="manual", current_period_start=datetime.now(timezone.utc))
    db.add(sub)
    db.commit()
    ent2 = entitlements_snapshot(db, user2.id)
    assert ent2["plan"] == "pro"
    assert ent2["capabilities"]["can_use_advanced_matching"] is True


def test_enforce_blocks_when_over_limit(db):
    user = make_user(db)
    period = current_period()
    counter = UsageCounter(user_id=user.id, period=period, capability=CAP_TAILORED, count=PLANS["free"]["limits"]["tailored_resumes_per_month"])
    db.add(counter)
    db.commit()

    with pytest.raises(Exception) as exc:
        enforce(db, user.id, CAP_TAILORED)
    assert getattr(exc.value, 'status_code', 0) in (402, 429)


def test_increment_usage_creates_counter(db):
    user = make_user(db)
    period = current_period()
    increment_usage(db, user.id, CAP_JOBS, amount=5)
    c = db.query(UsageCounter).filter_by(user_id=user.id, period=period, capability=CAP_JOBS).first()
    assert c is not None
    assert c.count == 5
    increment_usage(db, user.id, CAP_JOBS, amount=2)
    c2 = db.query(UsageCounter).filter_by(user_id=user.id, period=period, capability=CAP_JOBS).first()
    assert c2.count == 7


def test_billing_event_idempotency(db):
    user = make_user(db)
    sub = Subscription(user_id=user.id, plan="pro", status="active", provider="stripe", provider_customer_id="cus_123", provider_subscription_id="sub_123", current_period_start=datetime.now(timezone.utc), current_period_end=datetime.now(timezone.utc))
    db.add(sub)
    db.commit()

    ev = BillingEvent(
        provider="stripe",
        provider_event_id="evt_abc",
        kind="invoice.paid",
        user_id=user.id,
        payload={"id": "evt_abc"},
        processed=True,
    )
    db.add(ev)
    db.commit()

    from sqlalchemy.exc import IntegrityError
    ev2 = BillingEvent(
        provider="stripe",
        provider_event_id="evt_abc",
        kind="invoice.paid",
        user_id=user.id,
        payload={"id": "evt_abc"},
        processed=False,
    )
    db.add(ev2)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_multi_user_isolation_usage_counters(db):
    u1 = make_user(db, email="u1@test.com")
    u2 = make_user(db, email="u2@test.com")
    increment_usage(db, u1.id, CAP_JOBS, 20)
    increment_usage(db, u2.id, CAP_JOBS, 5)
    period = current_period()
    c1 = db.query(UsageCounter).filter_by(user_id=u1.id, period=period, capability=CAP_JOBS).first()
    c2 = db.query(UsageCounter).filter_by(user_id=u2.id, period=period, capability=CAP_JOBS).first()
    assert c1.count == 20
    assert c2.count == 5


def test_ai_credit_ledger_cost_tracking(db):
    user = make_user(db)
    ledger = AICreditLedger(
        user_id=user.id,
        workflow="job_match",
        model="gpt-4o-mini",
        prompt_tokens=500,
        completion_tokens=200,
        total_tokens=700,
        estimated_cost_usd=0.0014,
        latency_ms=1200,
        success=True,
    )
    db.add(ledger)
    db.commit()
    fetched = db.query(AICreditLedger).filter_by(user_id=user.id).first()
    assert fetched.estimated_cost_usd == 0.0014
    assert fetched.total_tokens == 700


def test_entitlements_free_cannot_use_pro_features(db):
    user = make_user(db)
    ent = entitlements_snapshot(db, user.id)
    assert ent["capabilities"]["can_use_advanced_matching"] is False
    assert ent["capabilities"]["can_use_autofill"] is False

    user2 = make_user(db)
    sub = Subscription(user_id=user2.id, plan="pro_plus", status="active", provider="manual", current_period_start=datetime.now(timezone.utc))
    db.add(sub)
    db.commit()
    ent_plus = entitlements_snapshot(db, user2.id)
    assert ent_plus["capabilities"]["can_use_advanced_matching"] is True
    assert ent_plus["capabilities"]["can_use_autofill"] is True
