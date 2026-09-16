"""
Central entitlement system for JobHunter SaaS.

Do NOT scatter `if user.plan == "pro"` throughout codebase.
Instead use capabilities: can_analyze_job, can_generate_resume, etc.

Plans:
- free: complete product with sensible limits
- pro: higher limits + advanced features
- pro_plus: highest limits + premium automation + intelligence

All features remain available in Free tier — limits differentiate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, Type

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.models import Job, Resume, Subscription, UsageCounter, VaultEntry

# --------------------------------------------------------------------------- #
# Plan definitions — single source of truth for limits
# --------------------------------------------------------------------------- #

PLANS: Dict[str, Dict[str, Any]] = {
    "free": {
        "label": "Free",
        "description": "Full JobHunter ecosystem with sensible limits",
        "price_monthly_usd": 0,
        "price_monthly_inr": 0,
        "price_yearly_usd": 0,
        "price_yearly_inr": 0,
        "limits": {
            # Discovery
            "jobs_discovered_per_month": 100,
            "jobs_discovered_per_day": 20,
            "funding_companies_per_month": 20,
            # AI
            "ai_operations_per_month": 50,
            "ai_operations_per_day": 15,
            "ai_credits_per_month": 50000,  # tokens
            #: Per-request output-token ceiling. A *cap*, not a quota: the
            #: gateway clamps every outgoing ``max_tokens`` (and the automatic
            #: escalation on truncation) to it. Free stays capped so a free
            #: account cannot make the platform's provider bill unbounded
            #: reasoning traces; ``0`` (unlimited) is what the paid tiers get.
            "ai_max_output_tokens": 16000,
            "resume_parses_per_month": 5,
            "job_analysis_per_month": 50,
            "advanced_matching": False,
            # Resume
            "tailored_resumes_per_month": 5,
            "cover_letters_per_month": 5,
            "resume_polish_per_month": 3,
            # Applications - core, free gets low limit not blocked
            "applications_per_month": 20,
            "automation_runs_per_month": 5,
            "autofill_enabled": True,
            # Outreach
            "outreach_per_month": 10,
            "outreach_per_day": 3,
            "contact_discovery_per_month": 20,
            # Analytics - basic for free, advanced for pro
            "advanced_analytics": False,
            "company_intel_per_month": 10,
            # Interview
            "interview_sessions_per_month": 2,
            # Storage
            "resumes_max": 10,
            "vault_entries_max": 20,
            "jobs_max": 500,
            # Notifications
            "advanced_alerts": False,
        },
        "capabilities": {
            "can_analyze_job": True,
            "can_generate_resume": True,
            "can_generate_cover_letter": True,
            "can_run_automation": True,
            "can_send_outreach": True,
            "can_use_advanced_matching": False,
            "can_access_analytics": True,
            "can_access_advanced_analytics": False,
            "can_use_company_intel": True,
            "can_use_interview_prep": True,
            "can_use_funding_radar": True,
            "can_use_autofill": True,
            "can_use_scheduled_workflows": False,
            "can_use_api_keys": False,
            "can_export_data": True,
        },
    },
    "pro": {
        "label": "Pro",
        "description": "Higher limits + advanced intelligence for active job seekers",
        "price_monthly_usd": 19,
        "price_monthly_inr": 999,
        "price_yearly_usd": 190,
        "price_yearly_inr": 9990,
        "limits": {
            "jobs_discovered_per_month": 1000,
            "jobs_discovered_per_day": 200,
            "funding_companies_per_month": 200,
            "ai_operations_per_month": 500,
            "ai_operations_per_day": 100,
            "ai_credits_per_month": 500000,
            #: Capped like free (see the note there); raise to 0 to unclamp.
            "ai_max_output_tokens": 16000,
            "resume_parses_per_month": 50,
            "job_analysis_per_month": 500,
            "advanced_matching": True,
            "tailored_resumes_per_month": 50,
            "cover_letters_per_month": 50,
            "resume_polish_per_month": 30,
            "applications_per_month": 200,
            "automation_runs_per_month": 50,
            "autofill_enabled": True,
            "outreach_per_month": 100,
            "outreach_per_day": 20,
            "contact_discovery_per_month": 200,
            "advanced_analytics": True,
            "company_intel_per_month": 100,
            "interview_sessions_per_month": 20,
            "resumes_max": 100,
            "vault_entries_max": 200,
            "jobs_max": 5000,
            "advanced_alerts": True,
        },
        "capabilities": {
            "can_analyze_job": True,
            "can_generate_resume": True,
            "can_generate_cover_letter": True,
            "can_run_automation": True,
            "can_send_outreach": True,
            "can_use_advanced_matching": True,
            "can_access_analytics": True,
            "can_access_advanced_analytics": True,
            "can_use_company_intel": True,
            "can_use_interview_prep": True,
            "can_use_funding_radar": True,
            "can_use_autofill": True,
            "can_use_scheduled_workflows": True,
            "can_use_api_keys": True,
            "can_export_data": True,
        },
    },
    "pro_plus": {
        "label": "Pro+",
        "description": "Highest limits + premium automation for power users",
        "price_monthly_usd": 49,
        "price_monthly_inr": 2499,
        "price_yearly_usd": 490,
        "price_yearly_inr": 24990,
        "limits": {
            "jobs_discovered_per_month": 5000,
            "jobs_discovered_per_day": 1000,
            "funding_companies_per_month": 1000,
            "ai_operations_per_month": 2000,
            "ai_operations_per_day": 500,
            #: **0 = unlimited.** Pro+ is the tier whose whole promise is "the
            #: model decides, not our cap": a 2M-token monthly quota still
            #: counted them down and the 16000-token output ceiling truncated
            #: long job descriptions into a `truncated_response` fallback
            #: ("Score 57 preliminary + AI pending"). Unlimited credits + an
            #: unclamped output budget mean the AI verdict is the verdict.
            "ai_credits_per_month": 0,
            #: 0 = no clamp — the provider's own per-model maximum applies.
            "ai_max_output_tokens": 0,
            "resume_parses_per_month": 200,
            "job_analysis_per_month": 2000,
            "advanced_matching": True,
            "tailored_resumes_per_month": 200,
            "cover_letters_per_month": 200,
            "resume_polish_per_month": 100,
            "applications_per_month": 1000,
            "automation_runs_per_month": 200,
            "autofill_enabled": True,
            "outreach_per_month": 500,
            "outreach_per_day": 100,
            "contact_discovery_per_month": 1000,
            "advanced_analytics": True,
            "company_intel_per_month": 500,
            "interview_sessions_per_month": 100,
            "resumes_max": 500,
            "vault_entries_max": 1000,
            "jobs_max": 20000,
            "advanced_alerts": True,
        },
        "capabilities": {
            "can_analyze_job": True,
            "can_generate_resume": True,
            "can_generate_cover_letter": True,
            "can_run_automation": True,
            "can_send_outreach": True,
            "can_use_advanced_matching": True,
            "can_access_analytics": True,
            "can_access_advanced_analytics": True,
            "can_use_company_intel": True,
            "can_use_interview_prep": True,
            "can_use_funding_radar": True,
            "can_use_autofill": True,
            "can_use_scheduled_workflows": True,
            "can_use_api_keys": True,
            "can_export_data": True,
        },
    },
}

# Map entitlements to limit keys
CAPABILITY_TO_LIMIT = {
    "can_analyze_job": "job_analysis_per_month",
    "can_generate_resume": "tailored_resumes_per_month",
    "can_generate_cover_letter": "cover_letters_per_month",
    "can_run_automation": "automation_runs_per_month",
    "can_send_outreach": "outreach_per_month",
    "can_use_advanced_matching": "advanced_matching",
    "can_access_analytics": "advanced_analytics",
    "can_use_company_intel": "company_intel_per_month",
    "can_use_interview_prep": "interview_sessions_per_month",
}

# Human-readable upgrade hints
UPGRADE_HINTS = {
    "jobs_discovered_per_month": "You've reached your monthly job discovery limit. Upgrade to Pro for 10x more.",
    "ai_operations_per_month": "You've reached your AI operations limit. Upgrade for more AI credits.",
    "tailored_resumes_per_month": "Resume tailoring limit reached. Pro gives you 50 per month.",
    "automation_runs_per_month": "Automation limit reached. Upgrade to Pro for 50 runs/month, Pro+ for 200.",
    "outreach_per_month": "Outreach limit reached. Upgrade for higher sending limits.",
    "advanced_matching": "Advanced matching is a Pro feature. Upgrade to see detailed breakdown.",
    "advanced_analytics": "Advanced analytics is Pro. Upgrade to see performance insights.",
    # Storage caps — enforced by *actual row count* (see STORAGE_LIMITS below),
    # so the hint offers the delete-out option that works on every plan.
    "resumes_max": "Resume storage is full — delete resumes you no longer need, or upgrade for more storage.",
    "vault_entries_max": "Credential vault is full — delete unused logins, or upgrade for more storage.",
    "jobs_max": "Your job board is full — clear jobs you no longer track, or upgrade for more space.",
}

#: Storage caps: limits that bound how many *rows* a user may hold.
#:
#: These are enforced by counting the rows, never by a ``UsageCounter``: a
#: counter for a storage cap is dead code by construction — nothing ever
#: incremented the ``resumes_max`` / ``vault_entries_max`` counters and
#: ``jobs_max`` was not enforced at all, so the ``enforce()`` calls in the
#: create endpoints never tripped and the free tier stored unlimited rows
#: while advertising 10/20/500. The row count is the usage: it goes up when a
#: row is created and down when one is deleted, with no bookkeeping to drift.
STORAGE_LIMITS: Dict[str, Type[Any]] = {
    "resumes_max": Resume,
    "vault_entries_max": VaultEntry,
    "jobs_max": Job,
}


def is_unlimited(limit: Any) -> bool:
    """``0`` or ``None`` means **unlimited** for every plan limit.

    The single predicate behind "this tier has no cap" — used by
    :func:`limit_for`, :func:`check_limit`, :func:`entitlements_snapshot` and
    the UI's *Unlimited* badge, so a plan can never disagree with itself about
    whether a limit exists. Booleans are never unlimited: they are
    feature flags, and ``False`` must stay a block.
    """
    if isinstance(limit, bool):
        return False  # feature flag: False must stay a block
    if limit is None:
        return True  # a blanked-out plan entry means "no cap", not a TypeError
    try:
        return int(limit) <= 0
    except (TypeError, ValueError):
        return True  # unparseable → fail open; a typo must not brick a tier


#: Plan limits that are *caps*, not consumable quotas. They belong in
#: ``limits`` (what the tier allows) but not in ``usage`` (what the user has
#: spent) — otherwise the billing grid renders a ceiling as "0 used".
NON_CONSUMABLE_LIMITS = frozenset({"ai_max_output_tokens"})


def current_period() -> str:
    return datetime.utcnow().strftime("%Y-%m")


# These are deliberately kept here, next to plan resolution, rather than in a
# billing router.  A provider can add a new status at any time; an entitlement
# reader must fail closed until the application knows what that status means.
KNOWN_SUBSCRIPTION_STATUSES = frozenset({
    "active",
    "trialing",
    "past_due",
    "canceled",
    "cancelled",
    "expired",
    "incomplete",
    "incomplete_expired",
    "unpaid",
    "paused",
})


def _naive_utc(moment: Optional[datetime] = None) -> datetime:
    """Return a comparable, naive UTC timestamp.

    Subscription columns are ``DateTime`` values stored as naive UTC.  SQLite
    accepts aware values in tests but returns them naive, while a PostgreSQL
    driver/configuration may hand an aware value back.  Normalising at the
    boundary avoids a timezone comparison turning an expiry check into a 500.
    """
    value = moment or datetime.utcnow()
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _subscription_has_expired(sub: Subscription, now: datetime) -> bool:
    """Apply date-based state transitions to one subscription row.

    Returns whether the row was changed.  A non-null ``trial_end`` is treated
    as an unpaid trial marker until the verified billing path clears it on an
    ``invoice.paid`` event.  That is important for older rows which were stored
    as ``active`` during a trial rather than ``trialing``.
    """
    status = str(sub.status or "").strip().lower()

    # ``past_due`` is intentionally first: the existing grace contract lets a
    # customer keep the plan through the grace window even if the provider's
    # period boundary has already passed.  Equality is expired, just as the
    # old get_user_plan implementation used a strict ``>`` comparison.
    if status == "past_due":
        if sub.grace_until is not None and _naive_utc(sub.grace_until) > now:
            return False
        if sub.status != "expired":
            sub.status = "expired"
            return True
        return False

    # Canceled/expired subscriptions retain their old grace behaviour.  A
    # grace window is a temporary entitlement, not a reason to reset the dates.
    if status in ("canceled", "cancelled", "expired"):
        if sub.grace_until is not None and _naive_utc(sub.grace_until) > now:
            return False
        if status != "expired":
            sub.status = "expired"
            return True
        return False

    # Incomplete (and other non-entitled known states) must remain non-entitled
    # until the verified invoice-paid path changes the row to active.  Do not
    # turn it into ``expired`` here: doing so would erase the useful distinction
    # between an unpaid checkout and a lapsed paid period.
    if status not in ("active", "trialing"):
        return False

    # A trial_end is only cleared by a verified payment renewal.  Therefore an
    # elapsed trial always expires, even when the old row says ``active``.
    if sub.trial_end is not None and _naive_utc(sub.trial_end) <= now:
        sub.status = "expired"
        # A grace window belongs to a payment failure/cancellation, not to an
        # unpaid trial.  Do not let stale data turn an expired trial back into a
        # paid entitlement through the canceled/expired grace rule.
        sub.grace_until = None
        return True

    # No provider renewal has been observed after this period boundary.  The
    # webhook code renews this date only for a verified payment event, never from
    # an arbitrary request/payload date.
    if sub.current_period_end is not None and _naive_utc(sub.current_period_end) <= now:
        sub.status = "expired"
        sub.grace_until = None
        return True

    return False


def effective_subscription(
    db: Session,
    user_id: int,
    *,
    now: Optional[datetime] = None,
    persist: bool = True,
) -> Optional[Subscription]:
    """Return a user's subscription after applying date-based expiry.

    This is the single state resolver used by request-time entitlement checks
    and by the background expiry sweep.  It performs one indexed subscription
    lookup and commits only when a date transition is needed, so ordinary calls
    do not write.  ``persist=False`` is for the nightly batch, which applies
    all flips and commits once at the end.

    The returned row is also useful to callers that need to display the
    subscription, but callers must still use :func:`get_user_plan` for the
    entitlement decision: unknown, incomplete, unpaid, paused, and invalid
    plans all resolve to free below.
    """
    sub = db.query(Subscription).filter(Subscription.user_id == user_id).first()
    if sub is None:
        return None

    changed = _subscription_has_expired(sub, _naive_utc(now))
    if changed and persist:
        db.commit()
    return sub


def expire_subscriptions(db: Session, *, now: Optional[datetime] = None) -> int:
    """Run the durable subscription expiry pass used by the worker/scheduler.

    It intentionally scans subscription rows rather than users: there is one
    row per user, the table is indexed by ``user_id``, and this keeps the pass
    independent of whether a user has enabled auto mode or has called the API.
    All due transitions are committed together.  The return value is an
    operator-friendly count of rows flipped during the pass.
    """
    moment = _naive_utc(now)
    subscriptions = db.query(Subscription).all()
    changed = 0
    for sub in subscriptions:
        before = str(sub.status or "")
        # Use the same resolver as request-time entitlement checks.  The batch
        # suppresses per-row commits and commits once below, but it must not
        # grow a second, subtly different expiry implementation.
        effective = effective_subscription(db, int(sub.user_id), now=moment, persist=False)
        if effective is not None and str(effective.status or "") != before:
            changed += 1
    if changed:
        db.commit()
    return changed


def get_user_plan(db: Session, user_id: int) -> str:
    """Resolve the effective plan, failing closed for unpaid/unknown states."""
    sub = effective_subscription(db, user_id)
    if not sub:
        return "free"

    status = str(sub.status or "").strip().lower()
    if status not in KNOWN_SUBSCRIPTION_STATUSES:
        return "free"
    if status in ("incomplete", "incomplete_expired", "unpaid", "paused"):
        return "free"
    if status in ("canceled", "cancelled", "expired", "past_due"):
        if not sub.grace_until or _naive_utc(sub.grace_until) <= _naive_utc():
            return "free"

    # A malformed plan must not accidentally become a paid entitlement either.
    plan = str(sub.plan or "free").strip().lower()
    return plan if plan in PLANS else "free"


def get_plan_config(plan: str) -> Dict[str, Any]:
    return PLANS.get(plan, PLANS["free"])


def get_subscription(db: Session, user_id: int) -> Optional[Subscription]:
    return db.query(Subscription).filter(Subscription.user_id == user_id).first()


def ensure_subscription(db: Session, user_id: int) -> Subscription:
    sub = get_subscription(db, user_id)
    if sub:
        return sub
    sub = Subscription(
        user_id=user_id,
        plan="free",
        status="active",
        provider="manual",
        current_period_start=datetime.utcnow(),
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def can(db: Session, user_id: int, capability: str) -> bool:
    """Check if user has capability per plan."""
    plan = get_user_plan(db, user_id)
    cfg = get_plan_config(plan)
    return bool(cfg["capabilities"].get(capability, False))


def limit_for(db: Session, user_id: int, limit_key: str) -> int:
    """The plan's numeric limit. ``0`` = unlimited (see :func:`is_unlimited`).

    ``None`` is accepted as another spelling of unlimited rather than blowing
    up on ``int(None)`` — a plan entry an operator blanks out means "no cap",
    not "TypeError at billing time".
    """
    plan = get_user_plan(db, user_id)
    cfg = get_plan_config(plan)
    raw = cfg["limits"].get(limit_key, 0)
    if isinstance(raw, bool):
        return int(raw)  # feature flag: 1 when on, 0 when off
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def usage_for(db: Session, user_id: int, capability_or_limit: str, period: Optional[str] = None) -> Tuple[int, int]:
    period = period or current_period()
    limit_key = CAPABILITY_TO_LIMIT.get(capability_or_limit, capability_or_limit)
    limit_val = limit_for(db, user_id, limit_key)
    if isinstance(limit_val, bool):
        return (0, 1) if limit_val else (0, 0)

    counter = (
        db.query(UsageCounter)
        .filter(
            UsageCounter.user_id == user_id,
            UsageCounter.period == period,
            UsageCounter.capability == limit_key,
        )
        .first()
    )
    used = counter.count if counter else 0
    return used, limit_val


def check_limit(db: Session, user_id: int, limit_key: str) -> Tuple[bool, int, int, str]:
    """``(allowed, used, limit, hint)``.

    A limit of ``0``/``None`` is **unlimited** — always allowed, never a block,
    and the returned limit stays ``0`` so callers (and the UI) can render it as
    *Unlimited* instead of "0 remaining". Boolean limits are feature flags: the
    flag itself decides.
    """
    used, lim = usage_for(db, user_id, limit_key)
    if lim <= 0:
        plan = get_user_plan(db, user_id)
        cfg = get_plan_config(plan)
        raw = cfg["limits"].get(limit_key)
        if isinstance(raw, bool):
            allowed = bool(raw)
            hint = UPGRADE_HINTS.get(limit_key, "Upgrade to unlock this feature")
            return allowed, used, 1 if allowed else 0, hint
        # 0 / None / anything non-numeric: unlimited, never a block.
        return True, used, 0, ""
    allowed = used < lim
    hint = UPGRADE_HINTS.get(limit_key, "") if not allowed else ""
    return allowed, used, lim, hint


def storage_count(db: Session, user_id: int, limit_key: str) -> int:
    """The number of rows the user actually holds for a storage cap.

    One indexed ``COUNT`` per cap — cheap even on a Pro+ board (20k rows).
    """
    model = STORAGE_LIMITS.get(limit_key)
    if model is None:
        raise ValueError(f"{limit_key!r} is not a storage cap")
    return int(db.query(func.count(model.id)).filter(model.user_id == user_id).scalar() or 0)


def check_storage(db: Session, user_id: int, limit_key: str) -> Tuple[bool, int, int, str]:
    """``(allowed, used, limit, hint)`` for a storage cap, by real row count.

    Unlike :func:`check_limit` this never consults ``UsageCounter``: the rows
    *are* the usage, so deleting a file frees a slot the same instant and the
    number the UI renders as "used" is the number that blocks. A ``0`` limit
    is unlimited, as everywhere else.
    """
    if limit_key not in STORAGE_LIMITS:
        raise ValueError(f"{limit_key!r} is not a storage cap")
    lim = limit_for(db, user_id, limit_key)
    used = storage_count(db, user_id, limit_key)
    if is_unlimited(lim):
        return True, used, 0, ""
    allowed = used < lim
    hint = UPGRADE_HINTS.get(limit_key, f"Storage limit for {limit_key} reached ({used}/{lim})") if not allowed else ""
    return allowed, used, lim, hint


def increment_usage(db: Session, user_id: int, capability: str, amount: int = 1) -> UsageCounter:
    """Increment usage counter atomically (SELECT FOR UPDATE)."""
    period = current_period()
    limit_key = CAPABILITY_TO_LIMIT.get(capability, capability)
    counter = (
        db.query(UsageCounter)
        .filter(
            UsageCounter.user_id == user_id,
            UsageCounter.period == period,
            UsageCounter.capability == limit_key,
        )
        .with_for_update()
        .first()
    )
    if counter:
        counter.count += amount
        counter.updated_at = datetime.utcnow()
    else:
        lim = limit_for(db, user_id, limit_key)
        if isinstance(lim, bool):
            lim = 1 if lim else 0
        counter = UsageCounter(
            user_id=user_id,
            period=period,
            capability=limit_key,
            count=amount,
            limit=lim,
        )
        db.add(counter)
    db.commit()
    db.refresh(counter)
    return counter


def enforce(db: Session, user_id: int, capability_or_limit: str):
    from fastapi import HTTPException

    if capability_or_limit in CAPABILITY_TO_LIMIT:
        cap = capability_or_limit
        if not can(db, user_id, cap):
            hint = UPGRADE_HINTS.get(CAPABILITY_TO_LIMIT[cap], "Upgrade to unlock")
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "upgrade_required",
                    "capability": cap,
                    "message": hint,
                },
            )
        limit_key = CAPABILITY_TO_LIMIT[cap]
    else:
        limit_key = capability_or_limit
        plan = get_user_plan(db, user_id)
        cfg = get_plan_config(plan)
        raw = cfg["limits"].get(limit_key)
        if isinstance(raw, bool) and not raw:
            hint = UPGRADE_HINTS.get(limit_key, "Upgrade to unlock")
            raise HTTPException(
                status_code=402,
                detail={"code": "upgrade_required", "limit": limit_key, "message": hint},
            )
        if capability_or_limit in get_plan_config("free")["capabilities"]:
            if not can(db, user_id, capability_or_limit):
                raise HTTPException(
                    status_code=402,
                    detail={
                        "code": "upgrade_required",
                        "capability": capability_or_limit,
                        "message": UPGRADE_HINTS.get(limit_key, "Upgrade required"),
                    },
                )
            return

    # Storage caps are enforced by the rows the user actually has, not by a
    # UsageCounter nothing writes — see STORAGE_LIMITS.
    if limit_key in STORAGE_LIMITS:
        allowed, used, lim, hint = check_storage(db, user_id, limit_key)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "limit_exceeded",
                    "limit": limit_key,
                    "used": used,
                    "limit_value": lim,
                    "message": hint or f"Storage limit for {limit_key} reached ({used}/{lim})",
                    "upgrade_required": True,
                },
            )
        return

    allowed, used, lim, hint = check_limit(db, user_id, limit_key)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "limit_exceeded",
                "limit": limit_key,
                "used": used,
                "limit_value": lim,
                "message": hint or f"Monthly limit for {limit_key} reached ({used}/{lim})",
                "upgrade_required": True,
            },
        )


def entitlements_snapshot(db: Session, user_id: int) -> Dict[str, Any]:
    plan = get_user_plan(db, user_id)
    cfg = get_plan_config(plan)
    sub = get_subscription(db, user_id)
    period = current_period()
    # ``remaining`` is None for an unlimited limit, hence the Any value type.
    usage: Dict[str, Dict[str, Any]] = {}
    for key in cfg["limits"].keys():
        if key in NON_CONSUMABLE_LIMITS:
            continue  # a cap, not a quota — see the constant
        if key in STORAGE_LIMITS:
            # A cap on stored rows: "used" is the real COUNT of the user's
            # rows, never the (unwritten) UsageCounter — the grid renders
            # "7/10 resumes" instead of a permanent "0 used".
            used = storage_count(db, user_id, key)
            lim = limit_for(db, user_id, key)
            if is_unlimited(lim):
                usage[key] = {"used": used, "limit": 0, "remaining": None, "unlimited": True}
            else:
                usage[key] = {"used": used, "limit": lim, "remaining": max(0, lim - used),
                              "unlimited": False}
            continue
        used, lim = usage_for(db, user_id, key, period=period)
        if isinstance(cfg["limits"][key], bool):
            usage[key] = {"allowed": bool(cfg["limits"][key]), "used": 0, "limit": 1 if cfg["limits"][key] else 0}
        elif is_unlimited(cfg["limits"][key]):
            # Unlimited: the counter still moves (billing/observability), but
            # there is no ceiling to run out of — and no invented "999999
            # remaining" that a UI would render as a real number.
            usage[key] = {"used": used, "limit": 0, "remaining": None, "unlimited": True}
        else:
            usage[key] = {"used": used, "limit": lim, "remaining": max(0, lim - used),
                          "unlimited": False}
    return {
        "plan": plan,
        "plan_label": cfg["label"],
        "plan_config": cfg,
        "subscription": {
            "status": sub.status if sub else "active",
            "provider": sub.provider if sub else "manual",
            "current_period_start": sub.current_period_start if sub else None,
            "current_period_end": sub.current_period_end if sub else None,
            "trial_end": sub.trial_end if sub else None,
            "cancel_at_period_end": sub.cancel_at_period_end if sub else False,
            "grace_until": sub.grace_until if sub else None,
        } if sub else None,
        "capabilities": cfg["capabilities"],
        "limits": cfg["limits"],
        "usage": usage,
        "period": period,
    }
