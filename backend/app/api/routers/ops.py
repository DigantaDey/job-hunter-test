"""Operations endpoints: health, readiness, metrics, logs, dashboard, queue admin."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response

from app.api.deps import CurrentUser, DbSession
from app.core import metrics
from app.core.auth import require_owner
from app.core.config import settings
from app.core.rate_limiter import rate_limiter
from app.core.security import constant_time_equals
from app.db import check_db_health, migration_state
from app.metrics_server import TEXT_CONTENT_TYPE
from app.models.models import Email, ErrorLog, Job, PipelineJob, Resume, UserInputRequest, VaultEntry
from app.schemas.schemas import ErrorLogOut
from app.services.ai_client import breaker_snapshot, is_configured, ping
from app.services.job_queue import queue_stats, recover_stalled

router = APIRouter(tags=["ops"])


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
    checks = {
        "database": db_health,
        "migrations": migrations,
        "ai_configured": is_configured(),
        "workers_in_api": settings.run_worker_in_api,
        "version": settings.version,
        "environment": settings.environment,
    }
    if is_configured():
        checks["ai"] = await ping(timeout=3)
    healthy = db_ok
    if not healthy:
        response.status_code = 503
    return {"status": "ready" if healthy else "degraded", "checks": checks,
            "timestamp": datetime.utcnow().isoformat()}


@router.get("/metrics")
def prometheus(
    metrics_token: Optional[str] = None,
    authorization: Optional[str] = Header(default=None, description="Bearer <METRICS_TOKEN>"),
):
    """Prometheus exposition. Protected when METRICS_TOKEN is set."""
    if settings.metrics_port:
        # Metrics live on their own (internal) port — see METRICS_PORT.
        raise HTTPException(404, {"code": "metrics_moved",
                                  "message": f"Metrics are served on port {settings.metrics_port} "
                                             f"({settings.metrics_host}) at /metrics"})
    if not settings.metrics_enabled:
        raise HTTPException(404, "Metrics are disabled")
    if settings.metrics_token:
        # Two accepted shapes: `Authorization: Bearer <token>` (preferred — a
        # query string ends up in access logs, proxy logs and browser history)
        # and `?metrics_token=` for scrapers that cannot set headers.
        bearer = (authorization or "").removeprefix("Bearer ").strip()
        supplied = metrics_token or bearer
        if not constant_time_equals(supplied, settings.metrics_token):
            raise HTTPException(401, "Metrics token required")
    payload = metrics.metrics_text(version=settings.version, environment=settings.environment)
    return Response(content=payload, media_type=TEXT_CONTENT_TYPE)


@router.get("/logs", response_model=List[ErrorLogOut])
def logs(user: CurrentUser, db: DbSession, pipeline: Optional[str] = None, level: Optional[str] = None,
         job_id: Optional[int] = None, limit: int = Query(100, le=1000)):
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
    job_counts = {
        status: db.query(Job).filter(Job.user_id == user.id, Job.status == status).count()
        for status in ("discovered", "queued", "needs_input", "preparing", "ready_to_apply", "applied", "failed", "skipped")
    }
    return {
        "jobs": {
            "total": db.query(Job).filter(Job.user_id == user.id).count(),
            "last_24h": db.query(Job).filter(Job.user_id == user.id, Job.discovered_at >= today).count(),
            "applied": job_counts.get("applied", 0),
            "high_match": db.query(Job).filter(Job.user_id == user.id, Job.score >= 75,
                                               Job.status == "discovered").count(),
            **job_counts,
        },
        "emails": {
            "pending_approval": db.query(Email).filter(Email.user_id == user.id,
                                                       Email.status == "pending_approval").count(),
            "sent": db.query(Email).filter(Email.user_id == user.id, Email.status == "sent").count(),
            "total": db.query(Email).filter(Email.user_id == user.id).count(),
        },
        "resumes": {
            "total": db.query(Resume).filter(Resume.user_id == user.id).count(),
            "pending_approval": db.query(Resume).filter(Resume.user_id == user.id,
                                                        Resume.status == "pending").count(),
        },
        "vault": db.query(VaultEntry).filter(VaultEntry.user_id == user.id).count(),
        "user_input": db.query(UserInputRequest).filter(UserInputRequest.user_id == user.id,
                                                        UserInputRequest.status == "pending").count(),
        "pipelines": queue_stats(db, user_id=user.id),
        "rate_limiter": rate_limiter.stats(),
    }


@router.get("/ops/status")
async def ops_status(user: CurrentUser, db: DbSession):
    """Single pane of glass for the Ops page."""
    db_health = check_db_health()
    ai = await ping(timeout=3) if is_configured() else {"online": False, "reason": "no_api_key"}
    depths = queue_stats(db, user_id=None)
    return {
        "version": settings.version,
        "environment": settings.environment,
        "database": {**db_health, "migrations": migration_state()},
        "ai": {**ai, "breakers": breaker_snapshot(), "rate_limiter": rate_limiter.stats()},
        "queues": depths,
        "workers": {"in_api": settings.run_worker_in_api, "concurrency": settings.worker_concurrency,
                    "lease_seconds": settings.worker_lease_seconds},
        "metrics": metrics.snapshot(),
    }


@router.post("/ops/queue/recover")
def recover(request: Request, user: CurrentUser, db: DbSession):
    owner = require_owner(user)
    count = recover_stalled(db)
    return {"ok": True, "recovered": count, "by": owner.email}


@router.post("/ops/queue/retry-dead")
def retry_dead(request: Request, user: CurrentUser, db: DbSession):
    owner = require_owner(user)
    dead = db.query(PipelineJob).filter(PipelineJob.status == "dead").all()
    for item in dead:
        item.status = "queued"
        item.attempts = 0
        item.scheduled_at = datetime.utcnow()
        item.error = ""
    db.commit()
    return {"ok": True, "requeued": len(dead), "by": owner.email}
