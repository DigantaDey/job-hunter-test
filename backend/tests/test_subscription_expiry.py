"""Regression coverage for date-aware subscription entitlements."""

import uuid
from datetime import datetime, timedelta

import pytest

from app.core.entitlements import expire_subscriptions, get_user_plan
from app.models.models import Subscription, User
from app.services.auto_scheduler import AutoScheduler


def _user(db):
    user = User(
        email=f"expiry-{uuid.uuid4().hex}@example.test",
        password_hash="test-hash",
        role="member",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _subscription(db, user, **values):
    attributes = {
        "user_id": user.id,
        "plan": "pro_plus",
        "status": "active",
        "current_period_start": datetime.utcnow() - timedelta(days=8),
        "current_period_end": datetime.utcnow() + timedelta(days=22),
    }
    attributes.update(values)
    sub = Subscription(**attributes)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def test_expired_trial_is_downgraded_on_entitlement_read_and_persisted(db):
    user = _user(db)
    sub = _subscription(
        db,
        user,
        status="trialing",
        trial_end=datetime.utcnow() - timedelta(seconds=1),
    )

    assert get_user_plan(db, user.id) == "free"
    db.refresh(sub)
    assert sub.status == "expired"


@pytest.mark.asyncio
async def test_nightly_scheduler_pass_expires_trial_without_api_read(db):
    user = _user(db)
    sub = _subscription(
        db,
        user,
        status="trialing",
        trial_end=datetime.utcnow() - timedelta(days=1),
    )

    # No auto-mode setting is needed: subscription maintenance is deliberately
    # independent of the list of users who opted into scheduled workflows.
    await AutoScheduler(interval_seconds=300).sweep()

    db.refresh(sub)
    assert sub.status == "expired"
    assert sub.plan == "pro_plus"  # status is the durable expiry marker


def test_incomplete_and_unknown_statuses_are_free(db):
    for status in ("incomplete", "provider_added_a_new_status"):
        user = _user(db)
        _subscription(db, user, status=status)
        assert get_user_plan(db, user.id) == "free"


def test_past_due_grace_is_strictly_bounded(db):
    user = _user(db)
    sub = _subscription(
        db,
        user,
        status="past_due",
        current_period_end=datetime.utcnow() - timedelta(days=1),
        grace_until=datetime.utcnow() + timedelta(hours=1),
    )

    assert get_user_plan(db, user.id) == "pro_plus"

    sub.grace_until = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    assert get_user_plan(db, user.id) == "free"
    db.refresh(sub)
    assert sub.status == "expired"


def test_expiry_batch_returns_flipped_row_count(db):
    user = _user(db)
    sub = _subscription(db, user, current_period_end=datetime.utcnow() - timedelta(seconds=1))

    assert expire_subscriptions(db) == 1
    db.refresh(sub)
    assert sub.status == "expired"
