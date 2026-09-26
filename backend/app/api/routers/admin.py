"""
The owner/admin surface (``/api/admin/*``).

Every route in this router requires the owner role — enforced once, at the
router level, by ``dependencies=[Depends(require_owner)]``. A member token
hitting any path here gets ``403 owner_required`` before any handler runs;
there is no code path that relaxes it.

What lives here (and deliberately nowhere on the user surface):

* ``GET  /admin/overview``        — source health, queue health, usage & cost,
  failure rates, application-automation health, accounts and plan mix, and the
  shared job pool's aggregates (live entries + pruned/deleted counters).
* ``GET  /admin/audit``           — the cross-user audit trail (the self-scoped
  ``/api/account/audit`` stays available to every user for their own actions).
* ``GET/PUT /admin/flags``        — global feature flags and their consumers.
* ``GET  /admin/users``           — accounts, roles, plans (plan/entitlement controls).
* ``PUT  /admin/users/{id}/plan`` — set a user's plan (manual entitlement).
* ``PUT  /admin/users/{id}/role`` — grant/revoke the owner role.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import case, func

from app.api.deps import CurrentUser, DbSession
from app.core import audit, metrics
from app.core.auth import require_owner
from app.core.config import settings
from app.models.models import (
    AICreditLedger,
    AuditLog,
    PipelineJob,
    Subscription,
    User,
)
from app.services import ai_log, job_pool, net_guard, robots
from app.services import flags as feature_flags
from app.services import http as http_client
from app.services.ai_client import breaker_snapshot
from app.services.billing import create_or_update_subscription_manual
from app.services.funding_sources import provider_status
from app.services.job_queue import queue_stats
from app.services.sources import list_sources

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_owner)])

_PLANS = ("free", "pro", "pro_plus")
_ROLES = ("owner", "member")


@router.get("/overview")
def overview(user: CurrentUser, db: DbSession) -> Dict[str, Any]:
    """
    The operational single pane: source health, queue health, usage & cost,
    failure rates and application-automation health across the workspace.
    """
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # --- Usage & cost (owner-level accounting; never shown to end users) ---- #
    tokens_30d, cost_30d, ops_30d, failures_30d = (
        db.query(
            func.coalesce(func.sum(AICreditLedger.total_tokens), 0),
            func.coalesce(func.sum(AICreditLedger.estimated_cost_usd), 0.0),
            func.count(),
            func.coalesce(func.sum(case((AICreditLedger.success.is_(False), 1), else_=0)), 0),
        )
        .filter(AICreditLedger.created_at >= now - timedelta(days=30))
        .one()
    )
    by_workflow_rows = (
        db.query(
            AICreditLedger.workflow,
            func.count(),
            func.coalesce(func.sum(AICreditLedger.total_tokens), 0),
            func.coalesce(func.sum(AICreditLedger.estimated_cost_usd), 0.0),
        )
        .filter(AICreditLedger.created_at >= month_start)
        .group_by(AICreditLedger.workflow)
        .all()
    )

    # --- Queue health ------------------------------------------------------- #
    depths = queue_stats(db, user_id=None)
    totals: Dict[str, int] = dict.fromkeys(
        ("queued", "processing", "paused", "done", "failed", "dead", "needs_input"), 0
    )
    for buckets in depths.values():
        for key in totals:
            totals[key] += int(buckets.get(key, 0))
    terminal = totals["done"] + totals["failed"] + totals["dead"]
    failure_rate = round((totals["failed"] + totals["dead"]) / terminal * 100, 2) if terminal else 0.0

    # --- Application automation health -------------------------------------- #
    application = depths.get("application", {})
    apps_terminal = int(application.get("done", 0)) + int(application.get("failed", 0)) + int(application.get("dead", 0))
    apps_failure_rate = (
        round((int(application.get("failed", 0)) + int(application.get("dead", 0))) / apps_terminal * 100, 2)
        if apps_terminal else 0.0
    )
    active_automation_users = (
        db.query(func.count(func.distinct(PipelineJob.user_id)))
        .filter(
            PipelineJob.pipeline == "application",
            PipelineJob.status.in_(("queued", "processing", "paused", "needs_input")),
        )
        .scalar()
    )

    # --- Accounts & plans --------------------------------------------------- #
    user_count = db.query(User).filter(User.is_active.is_(True)).count()
    plan_rows = db.query(Subscription.plan, func.count(Subscription.id)).group_by(Subscription.plan).all()
    owners = db.query(User).filter(User.role == "owner").count()

    ai_failure_rate = round(float(failures_30d or 0) / int(ops_30d or 1) * 100, 2)
    return {
        "server_time": now.isoformat(),
        "version": settings.version,
        "environment": settings.environment,
        "sources": {
            "job_sources": list_sources(),
            "funding_providers": provider_status(),
            "outbound": {
                "http_cache": http_client.cache_stats(),
                "dns_cache": net_guard.dns_cache_stats(),
                "robots_cache": robots.cache_stats(),
            },
        },
        "queues": {
            "pipelines": depths,
            "totals": totals,
            "failure_rate_percent": failure_rate,
            "ai_breakers": breaker_snapshot(),
        },
        "usage_cost": {
            "window_days": 30,
            "total_tokens": int(tokens_30d or 0),
            "estimated_cost_usd": round(float(cost_30d or 0.0), 4),
            "ai_operations": int(ops_30d or 0),
            "ai_failures": int(failures_30d or 0),
            "ai_failure_rate_percent": ai_failure_rate,
            "by_workflow_month": [
                {"workflow": wf, "operations": int(n), "tokens": int(t), "cost_usd": round(float(c), 4)}
                for wf, n, t, c in by_workflow_rows
            ],
        },
        "failure_rates": {
            "pipeline_percent": failure_rate,
            "application_percent": apps_failure_rate,
            "ai_percent": ai_failure_rate,
            "metrics_snapshot": metrics.snapshot(),
        },
        "automation_health": {
            "application": application,
            "active_users": int(active_automation_users or 0),
            "failure_rate_percent": apps_failure_rate,
        },
        "accounts": {
            "active_users": int(user_count or 0),
            "owners": int(owners or 0),
            "plans": {plan: int(n) for plan, n in plan_rows},
        },
        # Shared job pool, owner-only: what the cross-user corpus currently
        # holds (live entries, sources, contributors) and what it no longer
        # holds (aggregate counters for pruned/deleted postings — the only
        # thing retention keeps). Route-level ``require_owner`` is the access
        # rule; nothing here is exposed on a user surface.
        "job_pool": job_pool.metrics_snapshot(db),
        # The two answers the owner console exists for, owner-only like
        # everything here: "what exactly did we send?" (a 24h rollup of the
        # call log — the bodies themselves are on /admin/ai/calls/{id}) and
        # "how is Laya doing?" (status mix, latency and confidence per task).
        "ai_log": ai_log.call_stats(db, window_hours=24),
        "laya": ai_log.laya_stats(db, window_hours=24),
        "flags": feature_flags.all_flags(db),
    }


@router.get("/audit")
def admin_audit(
    user: CurrentUser,
    db: DbSession,
    action: Optional[str] = Query(None, description="Exact action, e.g. auth.login_failed"),
    action_prefix: Optional[str] = Query(None, description="Prefix match, e.g. admin."),
    user_id: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    """The workspace-wide audit trail. Owner-only; the self log stays at /api/account/audit."""
    query = db.query(AuditLog)
    if action:
        query = query.filter(AuditLog.action == action)
    if action_prefix:
        query = query.filter(AuditLog.action.like(f"{action_prefix}%"))
    if user_id is not None:
        query = query.filter(AuditLog.user_id == user_id)
    rows = query.order_by(AuditLog.created_at.desc()).limit(limit).all()
    return {
        "items": [
            {
                "id": row.id,
                "action": row.action,
                "actor": row.actor,
                "user_id": row.user_id,
                "target": row.target,
                "detail": row.detail or {},
                "ip": row.ip,
                "request_id": row.request_id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
        "limit": limit,
    }


@router.get("/flags")
def get_flags(user: CurrentUser, db: DbSession):
    """Every registered global feature flag with its effective value."""
    return {"flags": feature_flags.all_flags(db)}


@router.put("/flags")
def put_flags(
    payload: Dict[str, bool],
    request: Request,
    owner: CurrentUser,
    db: DbSession,
):
    """Flip global feature flags. Unknown keys → 400; every change is audited."""
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(400, "Provide at least one {flag_key: enabled} entry")
    results = []
    for key, value in payload.items():
        if not feature_flags.is_registered(key):
            raise HTTPException(400, f"Unknown feature flag '{key}'")
        if not isinstance(value, bool):
            raise HTTPException(400, f"Flag '{key}' must be true or false")
        results.append(feature_flags.set_flag(db, key, value, actor=owner, request=request))
    return {"ok": True, "updated": results, "flags": feature_flags.all_flags(db)}


@router.get("/users")
def list_users(user: CurrentUser, db: DbSession, limit: int = Query(200, ge=1, le=500)):
    """Accounts with role + plan — the plan/entitlement control table."""
    rows = (
        db.query(User, Subscription)
        .outerjoin(Subscription, Subscription.user_id == User.id)
        .order_by(User.id.asc())
        .limit(limit)
        .all()
    )
    return {
        "items": [
            {
                "id": u.id,
                "email": u.email,
                "name": u.name or "",
                "role": u.role or "member",
                "is_active": bool(u.is_active),
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
                "plan": s.plan if s else "free",
                "plan_status": s.status if s else "none",
            }
            for u, s in rows
        ],
    }


@router.put("/users/{user_id}/plan")
def set_user_plan(
    user_id: int,
    payload: Dict[str, Any],
    request: Request,
    owner: CurrentUser,
    db: DbSession,
):
    """Plan/entitlement control: set any account's plan (manual entitlement)."""
    plan = str(payload.get("plan") or "").lower()
    if plan == "pro+":
        plan = "pro_plus"
    if plan not in _PLANS:
        raise HTTPException(400, f"plan must be one of {list(_PLANS)}")
    trial_days = int(payload.get("trial_days") or 0)
    if trial_days < 0 or trial_days > 60:
        raise HTTPException(400, "trial_days must be 0-60")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(404, "User not found")
    sub = create_or_update_subscription_manual(db, user_id, plan, status="active", trial_days=trial_days)
    audit.audit(db, "admin.plan_set", user=owner, target=f"user:{user_id}",
                detail={"plan": plan, "trial_days": trial_days, "email": target.email}, request=request)
    return {"ok": True, "user_id": user_id, "plan": sub.plan, "status": sub.status}


@router.put("/users/{user_id}/role")
def set_user_role(
    user_id: int,
    payload: Dict[str, Any],
    request: Request,
    owner: CurrentUser,
    db: DbSession,
):
    """Grant or revoke the owner role. The last owner cannot demote themselves."""
    role = str(payload.get("role") or "").lower()
    if role not in _ROLES:
        raise HTTPException(400, f"role must be one of {list(_ROLES)}")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(404, "User not found")
    if target.id == owner.id and role != "owner":
        owners = db.query(User).filter(User.role == "owner").count()
        if owners <= 1:
            raise HTTPException(400, "You are the only owner — grant the owner role to someone else first.")
    previous = target.role
    target.role = role
    db.commit()
    audit.audit(db, "admin.role_set", user=owner, target=f"user:{user_id}",
                detail={"from": previous, "to": role, "email": target.email}, request=request)
    return {"ok": True, "user_id": user_id, "role": role, "previous": previous}


# --------------------------------------------------------------------------- #
# Owner-only AI log: the exact request sent, and how Laya is doing
#
# Both surfaces are diagnostic and both are ``require_owner`` at the router
# level above — there is no per-user or public variant of either. Everything
# below is read-only; nothing here can change what a call sends.
# --------------------------------------------------------------------------- #
@router.get("/ai/calls")
def admin_ai_calls(
    user: CurrentUser,
    db: DbSession,
    workflow: Optional[str] = Query(None, description="Exact workflow, e.g. scoring"),
    status: Optional[str] = Query(None, description="ok | error"),
    user_id: Optional[int] = Query(None, description="Only calls made on behalf of this account"),
    before_id: Optional[int] = Query(None, description="Page: records with a smaller id"),
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """Newest-first page of logged AI calls — metadata plus a prompt preview.

    The full request bodies for one call come from ``/admin/ai/calls/{id}``.
    """
    items = ai_log.recent_calls(db, limit=limit, workflow=workflow, status=status,
                                user_id=user_id, before_id=before_id)
    return {
        "items": items,
        "limit": limit,
        "next_before_id": items[-1]["id"] if len(items) == limit and items else None,
        "enabled": ai_log.enabled(),
        "retention_days": ai_log.retention_days(),
    }


@router.get("/ai/calls/{record_id}")
def admin_ai_call_detail(record_id: int, user: CurrentUser, db: DbSession) -> Dict[str, Any]:
    """One call with the exact bodies sent (per attempt) and the answer excerpt."""
    record = ai_log.get_call(db, record_id)
    if record is None:
        raise HTTPException(404, "No AI call logged with that id")
    return record


@router.get("/ai/stats")
def admin_ai_stats(
    user: CurrentUser,
    db: DbSession,
    window_hours: int = Query(24, ge=1, le=720),
) -> Dict[str, Any]:
    """Volume, failures-by-reason, tokens, cost and latency over a window."""
    return ai_log.call_stats(db, window_hours=window_hours)


@router.get("/laya/decisions")
def admin_laya_decisions(
    user: CurrentUser,
    db: DbSession,
    task: Optional[str] = Query(None, description="ranking | classification | field_mapping"),
    status: Optional[str] = Query(None, description="ok | low_confidence | timeout | error | parked"),
    user_id: Optional[int] = Query(None),
    before_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """Newest-first page of local-engine forward passes, with the verdicts."""
    items = ai_log.recent_laya_decisions(db, limit=limit, task=task, status=status,
                                         user_id=user_id, before_id=before_id)
    return {
        "items": items,
        "limit": limit,
        "next_before_id": items[-1]["id"] if len(items) == limit and items else None,
        "statuses": ["ok", ai_log.LAYA_LOW_CONFIDENCE, ai_log.LAYA_TIMEOUT,
                     ai_log.LAYA_ERROR, ai_log.LAYA_PARKED],
        "enabled": ai_log.enabled(),
        "retention_days": ai_log.retention_days(),
    }


@router.get("/laya/stats")
def admin_laya_stats(
    user: CurrentUser,
    db: DbSession,
    window_hours: int = Query(24, ge=1, le=720),
) -> Dict[str, Any]:
    """How the engine is doing: per-task status mix, latency, confidence."""
    return ai_log.laya_stats(db, window_hours=window_hours)
