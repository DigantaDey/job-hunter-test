"""Operations endpoints: health, readiness, metrics, logs, dashboard, queue admin — with SaaS."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from sqlalchemy import func

from app.api.deps import CurrentUser, DbSession
from app.api.routers.auth import login_throttle_state
from app.core import metrics, middleware
from app.core.auth import require_owner
from app.core.config import settings
from app.core.entitlements import entitlements_snapshot
from app.core.logging import get_logger
from app.core.rate_limiter import rate_limiter
from app.core.security import constant_time_equals, key_cache_stats
from app.db import check_db_health, migration_state
from app.metrics_server import TEXT_CONTENT_TYPE
from app.models.models import (
    AICreditLedger,
    Email,
    ErrorLog,
    Job,
    Notification,
    PipelineJob,
    Resume,
    UserInputRequest,
    VaultEntry,
)
from app.schemas.schemas import ErrorLogOut
from app.services import http as http_client
from app.services import net_guard
from app.services.ai_client import breaker_snapshot, is_configured, ping
from app.services.auto_scheduler import brief as auto_scheduler_brief
from app.services.job_queue import queue_stats, recover_stalled

router = APIRouter(tags=["ops"])
log = get_logger("app.ops_api")


def _users_with_key() -> List[int]:
    """User ids that have a (decryptable) AI key stored in the DB.

    Readiness is unauthenticated and therefore cannot resolve a specific
    user's config — but reporting ``ai_configured: false`` while the owner
    saved a key through the UI is exactly the kind of lie that sends people
    debugging the wrong problem.
    """
    try:
        from app.db import SessionLocal
        from app.models.models import SettingsModel
        from app.services.user_settings import get_secret_setting

        db = SessionLocal()
        try:
            rows = db.query(SettingsModel).filter(
                SettingsModel.category == "ai", SettingsModel.key == "api_key"
            ).all()
            return [row.user_id for row in rows if get_secret_setting(db, row.user_id, "ai", "api_key")[0]]
        finally:
            db.close()
    except Exception:
        return []


@router.get("/health")
def health():
    return {"status": "ok", "version": settings.version, "environment": settings.environment}


@router.get("/health/live")
def live():
    """Process liveness — never touches external systems."""
    return {"status": "alive", "version": settings.version}


@router.get("/health/ready")
async def ready(response: Response):
    """Readiness: database reachable + schema applied (+ AI probe when configured)."""
    db_health = check_db_health()
    db_ok = db_health.get("database") == "ok"
    migrations = migration_state()
    configured_users = _users_with_key()
    checks = {
        "database": db_health,
        "migrations": migrations,
        "ai_configured": is_configured() or bool(configured_users),
        "workers_in_api": settings.run_worker_in_api,
        "version": settings.version,
        "environment": settings.environment,
    }
    if checks["ai_configured"]:
        # Probe with a configured user's key when no env key exists, so the
        # readiness signal matches what the app actually uses.
        from app.db import SessionLocal

        if not settings.ai_api_key and configured_users:
            db = SessionLocal()
            try:
                checks["ai"] = await ping(timeout=3, db=db, user_id=configured_users[0])
            finally:
                db.close()
        else:
            checks["ai"] = await ping(timeout=3)
    healthy = db_ok
    if not healthy:
        response.status_code = 503
    return {"status": "ready" if healthy else "degraded", "checks": checks, "timestamp": datetime.utcnow().isoformat()}


@router.get("/metrics")
def prometheus(
    metrics_token: Optional[str] = None,
    authorization: Optional[str] = Header(default=None, description="Bearer <METRICS_TOKEN>"),
):
    """Prometheus exposition. Protected when METRICS_TOKEN is set."""
    if settings.metrics_port:
        raise HTTPException(404, {"code": "metrics_moved", "message": f"Metrics are served on port {settings.metrics_port} ({settings.metrics_host}) at /metrics"})
    if not settings.metrics_enabled:
        raise HTTPException(404, "Metrics are disabled")
    if settings.metrics_token:
        bearer = (authorization or "").removeprefix("Bearer ").strip()
        supplied = metrics_token or bearer
        if not constant_time_equals(supplied, settings.metrics_token):
            raise HTTPException(401, "Metrics token required")
    payload = metrics.metrics_text(version=settings.version, environment=settings.environment)
    return Response(content=payload, media_type=TEXT_CONTENT_TYPE)


@router.get("/logs", response_model=List[ErrorLogOut])
def logs(user: CurrentUser, db: DbSession, pipeline: Optional[str] = None, level: Optional[str] = None, job_id: Optional[int] = None, limit: int = Query(100, le=1000)):
    query = db.query(ErrorLog).filter(ErrorLog.user_id == user.id)
    if pipeline:
        query = query.filter(ErrorLog.pipeline == pipeline)
    if level:
        query = query.filter(ErrorLog.level == level)
    if job_id:
        query = query.filter(ErrorLog.job_id == job_id)
    return query.order_by(ErrorLog.timestamp.desc()).limit(limit).all()


@router.delete("/logs")
def clear_logs(user: CurrentUser, db: DbSession, level: Optional[str] = None):
    query = db.query(ErrorLog).filter(ErrorLog.user_id == user.id)
    if level:
        query = query.filter(ErrorLog.level == level)
    deleted = query.delete(synchronize_session=False)
    db.commit()
    return {"ok": True, "deleted": deleted}


@router.get("/dashboard/summary")
def dashboard(user: CurrentUser, db: DbSession):
    today = datetime.utcnow() - timedelta(hours=24)
    week_ago = datetime.utcnow() - timedelta(days=7)
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    job_counts = {
        status: db.query(Job).filter(Job.user_id == user.id, Job.status == status).count()
        for status in ("discovered", "queued", "needs_input", "preparing", "ready_to_apply", "applied", "failed", "skipped")
    }

    # Automation stats
    pipeline_stats = queue_stats(db, user_id=user.id)
    automation_running = pipeline_stats.get("application", {}).get("processing", 0) + pipeline_stats.get("application", {}).get("queued", 0)
    automation_completed = pipeline_stats.get("application", {}).get("done", 0)
    automation_failed = pipeline_stats.get("application", {}).get("failed", 0) + pipeline_stats.get("application", {}).get("dead", 0)

    # Outreach
    outreach_sent = db.query(Email).filter(Email.user_id == user.id, Email.status == "sent").count()
    outreach_pending = db.query(Email).filter(Email.user_id == user.id, Email.status == "pending_approval").count()
    outreach_replies = db.query(Email).filter(Email.user_id == user.id, Email.opens > 0).count()

    # AI usage: aggregate in SQL rather than materialising every ledger row for
    # the month.  ``COUNT(*)`` preserves the operations metric without loading
    # the rows themselves into Python.
    ai_tokens_used, ai_cost, ai_operations_month = (
        db.query(
            func.coalesce(func.sum(AICreditLedger.total_tokens), 0),
            func.coalesce(func.sum(AICreditLedger.estimated_cost_usd), 0.0),
            func.count(),
        )
        .filter(AICreditLedger.user_id == user.id, AICreditLedger.created_at >= month_start)
        .one()
    )
    ai_tokens_used = int(ai_tokens_used or 0)
    ai_cost = float(ai_cost or 0.0)
    ai_operations_month = int(ai_operations_month or 0)

    # Performance
    total_jobs = db.query(Job).filter(Job.user_id == user.id).count()
    applied = job_counts.get("applied", 0)
    high_match = db.query(Job).filter(Job.user_id == user.id, Job.score >= 75, Job.status == "discovered").count()
    interview_estimate = db.query(Job).filter(Job.user_id == user.id, Job.score >= 80, Job.status == "applied").count()
    response_rate = round(interview_estimate / max(1, applied) * 100, 1) if applied else 0

    # Notifications
    unread_notifications = db.query(Notification).filter(Notification.user_id == user.id, Notification.read.is_(False)).count()

    # Entitlements
    entitlements = entitlements_snapshot(db, user.id)

    # Auto mode (v2.2). Read from the scheduler's own state so the dashboard and
    # the Settings card can never disagree about whether work is scheduled. The
    # block is optional decoration on the front door: on a database that has not
    # been migrated yet it reports "off" instead of blanking the dashboard.
    try:
        auto_mode = auto_scheduler_brief(db, int(user.id))
    except Exception as exc:  # noqa: BLE001 - decorative, never fatal
        log.warning("auto-mode state unavailable for user %s: %s", user.id, exc)
        auto_mode = {"auto_mode": False, "active": False, "next_run": None, "blocking": ["state_unavailable"]}

    return {
        "jobs": {
            "total": total_jobs,
            "last_24h": db.query(Job).filter(Job.user_id == user.id, Job.discovered_at >= today).count(),
            "last_7d": db.query(Job).filter(Job.user_id == user.id, Job.discovered_at >= week_ago).count(),
            "applied": applied,
            "high_match": high_match,
            "new_matches": db.query(Job).filter(Job.user_id == user.id, Job.status == "discovered", Job.discovered_at >= week_ago).count(),
            "saved": db.query(Job).filter(Job.user_id == user.id, Job.status == "discovered").count(),
            **job_counts,
        },
        "applications": {
            "prepared": job_counts.get("queued", 0) + job_counts.get("ready_to_apply", 0),
            "submitted": applied,
            "interviews": interview_estimate,
            "rejected": db.query(Job).filter(Job.user_id == user.id, Job.status == "rejected").count(),
            "offers": 0,  # future
            "response_rate": response_rate,
        },
        "automation": {
            "running": automation_running,
            "completed": automation_completed,
            "failed": automation_failed,
            "needs_input": job_counts.get("needs_input", 0),
            "queues": pipeline_stats,
            # v2.2 (additive): auto mode. ``auto_mode`` is the user's switch,
            # ``next_run`` the earliest moment any workflow of theirs is due
            # again (``null`` when it is off, blocked or a run is in flight) —
            # both read from the scheduler's own state, never recomputed here.
            "auto_mode": auto_mode["auto_mode"],
            "auto_mode_active": auto_mode["active"],
            "next_run": auto_mode["next_run"],
        },
        "outreach": {
            "sent": outreach_sent,
            "pending_approval": outreach_pending,
            "replies": outreach_replies,
            "follow_ups": 0,
            "total": db.query(Email).filter(Email.user_id == user.id).count(),
        },
        "ai_usage": {
            "credits_used": ai_tokens_used,
            "cost_usd": round(ai_cost, 4),
            "remaining": entitlements["usage"].get("ai_credits_per_month", {}).get("remaining", 0) if entitlements else 0,
            "limit": entitlements["usage"].get("ai_credits_per_month", {}).get("limit", 0) if entitlements else 0,
            "operations_month": ai_operations_month,
        },
        "performance": {
            "applications": applied,
            "interviews": interview_estimate,
            "response_rate": response_rate,
            "high_priority_jobs": high_match,
        },
        "emails": {
            "pending_approval": outreach_pending,
            "sent": outreach_sent,
            "total": db.query(Email).filter(Email.user_id == user.id).count(),
        },
        "resumes": {
            "total": db.query(Resume).filter(Resume.user_id == user.id).count(),
            "pending_approval": db.query(Resume).filter(Resume.user_id == user.id, Resume.status == "pending").count(),
        },
        "vault": db.query(VaultEntry).filter(VaultEntry.user_id == user.id).count(),
        "user_input": db.query(UserInputRequest).filter(UserInputRequest.user_id == user.id, UserInputRequest.status == "pending").count(),
        "notifications": {"unread": unread_notifications},
        "pipelines": pipeline_stats,
        "rate_limiter": rate_limiter.stats(),
        "entitlements": entitlements,
    }


@router.get("/ops/status")
async def ops_status(request: Request, user: CurrentUser, db: DbSession):
    """Single pane of glass for the Ops page. Owner-only — worker, cache and
    edge internals are infrastructure status, not user-facing product."""
    require_owner(user)
    db_health = check_db_health()
    ai = await ping(timeout=3, db=db, user_id=user.id) if is_configured(db=db, user_id=user.id) else {"online": False, "reason": "no_api_key"}
    depths = queue_stats(db, user_id=None)
    worker = getattr(request.app.state, "worker", None)
    worker_tasks: Dict[str, Any] = (
        worker.task_health()
        if worker is not None
        else {"expected": settings.worker_concurrency, "alive": 0, "last_crash": None}
    )
    return {
        "version": settings.version,
        "environment": settings.environment,
        "database": {**db_health, "migrations": migration_state()},
        "ai": {**ai, "breakers": breaker_snapshot(), "rate_limiter": rate_limiter.stats()},
        "queues": depths,
        "workers": {
            "in_api": settings.run_worker_in_api,
            "concurrency": settings.worker_concurrency,
            "lease_seconds": settings.worker_lease_seconds,
            # Self-healing slots (v2.1.1): expected vs actually alive, plus
            # the most recent crash the supervisor logged, if any.
            "tasks": worker_tasks,
        },
        "metrics": metrics.snapshot(),
        # Bounded in-process caches (v2.2.11): entries/evictions/hits for the
        # outbound GET cache, the per-host politeness state and the SSRF guard's
        # DNS verdicts. A worker that sweeps thousands of distinct URLs must not
        # grow any of these — ``entries`` sitting at ``max_entries`` with a
        # rising ``evictions`` is the healthy shape.
        "outbound": {
            "http_cache": http_client.cache_stats(),
            "dns_cache": net_guard.dns_cache_stats(),
        },
        # Bounded *edge* state (v2.2.12): the rate limiter's key log, the body
        # cap and the metrics registry itself. All three are keyed on data a
        # caller controls, so all three are capped — and the caps are only
        # useful if the shape is observable. ``keys`` at ``max_keys`` with a
        # rising ``evictions`` means someone is flooding distinct identities (or
        # that RATE_LIMIT_MAX_KEYS is too small for the traffic);
        # ``registry.evicted`` rising means a caller is labelling metrics on
        # unbounded values.
        "edge": {
            "rate_limiter": middleware.rate_limit_state(),
            "body_limit": middleware.body_limit_state(),
            "registry": metrics.stats(),
            # The pre-auth login window is caller-keyed too (email addresses), so
            # it is a bounded LRU with the same shape of counters.
            "login_throttle": login_throttle_state(),
            "client_ip": {
                "trusted_proxies": list(settings.trusted_proxies),
                "forwarded_allow_ips": settings.forwarded_allow_ips,
                "resolution": "rightmost_untrusted_hop" if settings.trusted_proxies else "peer",
            },
            "workers": int(settings.web_concurrency or 1),
        },
        # Derived per-scope vault keys. One entry per user vault, so this is a
        # bounded LRU like the rest — and ``expirations``/``evictions`` rising is
        # what makes a key-rotation staleness window visible instead of silent.
        "key_cache": key_cache_stats(),
    }


@router.post("/ops/queue/recover")
def recover(request: Request, user: CurrentUser, db: DbSession):
    owner = require_owner(user)
    count = recover_stalled(db)
    return {"ok": True, "recovered": count, "by": owner.email}


@router.post("/ops/queue/retry-dead")
def retry_dead(request: Request, user: CurrentUser, db: DbSession):
    """Re-queue every dead item with a **fresh budget on both counters**.

    ``attempts`` (handler failures) and ``payload.paused_count`` (AI-outage
    pauses) are separate budgets — see :mod:`app.services.job_queue`. Resetting
    only ``attempts`` meant an item that had dead-lettered on the pause cap was
    re-queued with ``AI_PAUSE_MAX + 1`` pauses already banked, so its very next
    outage killed it again and the operator's retry looked like a no-op.
    """
    owner = require_owner(user)
    dead = db.query(PipelineJob).filter(PipelineJob.status == "dead").all()
    for item in dead:
        item.status = "queued"
        item.attempts = 0
        item.scheduled_at = datetime.utcnow()
        item.error = ""
        payload = item.payload
        if isinstance(payload, dict) and "paused_count" in payload:
            # Reassign (not mutate): a JSON column only persists on assignment.
            item.payload = {k: v for k, v in payload.items() if k != "paused_count"}
    db.commit()
    return {"ok": True, "requeued": len(dead), "by": owner.email}
