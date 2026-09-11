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

from datetime import datetime
from typing import Dict, Any, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import Subscription, UsageCounter, User

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
            "ai_credits_per_month": 2000000,
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
}


def current_period() -> str:
    return datetime.utcnow().strftime("%Y-%m")


def get_user_plan(db: Session, user_id: int) -> str:
    """Resolve user's current plan, default free."""
    sub = db.query(Subscription).filter(Subscription.user_id == user_id).first()
    if not sub:
        return "free"
    if sub.status in ("canceled", "expired"):
        if sub.grace_until and sub.grace_until > datetime.utcnow():
            return sub.plan
        return "free"
    if sub.status == "past_due":
        if sub.grace_until and sub.grace_until > datetime.utcnow():
            return sub.plan
        return "free"
    return sub.plan or "free"


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
    plan = get_user_plan(db, user_id)
    cfg = get_plan_config(plan)
    return int(cfg["limits"].get(limit_key, 0))


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
    used, lim = usage_for(db, user_id, limit_key)
    if lim == 0:
        plan = get_user_plan(db, user_id)
        cfg = get_plan_config(plan)
        raw = cfg["limits"].get(limit_key)
        if isinstance(raw, bool):
            allowed = bool(raw)
            hint = UPGRADE_HINTS.get(limit_key, "Upgrade to unlock this feature")
            return allowed, used, 1 if allowed else 0, hint
        if raw == 0:
            return True, used, 0, ""
    allowed = used < lim if lim > 0 else True
    hint = UPGRADE_HINTS.get(limit_key, "") if not allowed else ""
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
    usage = {}
    for key in cfg["limits"].keys():
        used, lim = usage_for(db, user_id, key, period=period)
        if isinstance(cfg["limits"][key], bool):
            usage[key] = {"allowed": bool(cfg["limits"][key]), "used": 0, "limit": 1 if cfg["limits"][key] else 0}
        else:
            usage[key] = {"used": used, "limit": lim, "remaining": max(0, lim - used) if lim else 999999}
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
