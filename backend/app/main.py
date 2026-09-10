"""
FastAPI application factory.

Wiring, in order of execution per request:

    RequestContext → CORS → SecurityHeaders → RateLimit → BodySizeLimit → routes

Startup applies migrations, seeds runtime config, and (optionally) runs the
pipeline worker in-process. Shutdown is graceful: workers stop, the queue's
leases expire safely, and the HTTP client pool is closed.
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.routes import router
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics import inc
from app.core.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.db import SessionLocal, init_db
from app.services.ai_client import set_workflow_overrides
from app.services.ai_pipeline import ai_pipeline
from app.services.job_queue import recover_stalled
from app.worker import Worker

log = get_logger("app.main")
STARTED_AT = time.time()


def _load_workflow_overrides() -> None:
    """Restore per-workflow AI config from the DB into the runtime registry."""
    try:
        db = SessionLocal()
        try:
            from app.models.models import SettingsModel
            rows = db.query(SettingsModel).filter(SettingsModel.category == "ai_workflows").all()
            set_workflow_overrides({row.key: row.value for row in rows if isinstance(row.value, dict)})
        finally:
            db.close()
    except Exception as exc:  # pragma: no cover - startup best effort
        log.warning("could not load AI workflow overrides: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    init_db()
    for path in (settings.upload_dir, settings.generated_dir, settings.screenshot_dir):
        os.makedirs(path, exist_ok=True)
    _load_workflow_overrides()

    db = SessionLocal()
    try:
        recovered = recover_stalled(db)
        if recovered:
            log.warning("recovered %s stalled queue item(s) at startup", recovered)
    finally:
        db.close()

    worker: Worker | None = None
    if settings.run_worker_in_api:
        worker = Worker()
        app.state.worker = worker
        app.state.worker_task = asyncio.create_task(worker.start())
    else:
        log.info("RUN_WORKER_IN_API=false — run `python -m app.worker` for pipeline processing")

    log.info(
        "%s v%s started (env=%s, db=%s, migrations=%s, worker_in_api=%s)",
        settings.app_name, settings.version, settings.environment,
        settings.database_url.split("://")[0], settings.auto_migrate, settings.run_worker_in_api,
    )
    try:
        yield
    finally:
        if worker is not None:
            await worker.stop()
            task = getattr(app.state, "worker_task", None)
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        ai_pipeline.running = False
        from app.services.http import close_client

        await close_client()
        log.info("shutdown complete")


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description=(
            "Autonomous job-search platform: discovery, tailored resumes with a fact guard, "
            "credential vault, application automation and compliant outreach."
        ),
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    # --- middleware (added inside-out: last added is outermost) ---
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_upload_mb * 1024 * 1024 + (2 * 1024 * 1024))
    app.add_middleware(RateLimitMiddleware, limit_per_minute=settings.api_rate_limit_per_minute,
                       exempt_paths={"/api/health", "/api/health/live", "/api/health/ready", "/api/metrics"})
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials="*" not in settings.cors_origin_list,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID", "X-Webhook-Token"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )
    if settings.allowed_host_list != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(router, prefix="/api")

    # --- error handling: one JSON shape, always with a request id ---
    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        inc("jobhunter_http_errors_total", kind="validation")
        return JSONResponse(
            status_code=422,
            content={"detail": "Validation error", "errors": exc.errors()[:20],
                     "request_id": getattr(request.state, "request_id", "")},
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):  # pragma: no cover - safety net
        inc("jobhunter_http_errors_total", kind="unhandled")
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        detail: Dict[str, Any] = {"detail": "Internal server error",
                                  "request_id": getattr(request.state, "request_id", "")}
        if settings.debug:
            detail["error"] = f"{type(exc).__name__}: {exc}"
        return JSONResponse(status_code=500, content=detail)

    @app.get("/api/meta", include_in_schema=False)
    def meta():
        return {
            "name": settings.app_name,
            "version": settings.version,
            "environment": settings.environment,
            "uptime_seconds": round(time.time() - STARTED_AT, 1),
            "features": [
                "multi_user_auth_rbac", "tenant_isolation", "per_user_vault_encryption",
                "ai_keyword_extraction", "ai_scoring_calibrated", "ai_email_drafting",
                "durable_pipeline_queue", "real_job_sources", "robots_txt_compliance",
                "resume_fact_guard_plus_verification", "resume_diff_and_polish",
                "application_dry_run_autofill", "playwright_optional",
                "compliant_outreach_unsubscribe_suppression", "email_open_tracking_webhooks",
                "funding_radar_real_providers", "audit_trail", "gdpr_export_delete",
                "alembic_migrations", "structured_logging", "prometheus_metrics",
                "health_live_ready", "rate_limiting",
            ],
        }

    # --- frontend (built SPA) ---
    frontend_dist = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist"))
    if os.path.exists(frontend_dist):
        assets = os.path.join(frontend_dist, "assets")
        if os.path.isdir(assets):
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            candidate = os.path.join(frontend_dist, full_path)
            if full_path and os.path.isfile(candidate) and os.path.abspath(candidate).startswith(frontend_dist):
                return FileResponse(candidate)
            return FileResponse(os.path.join(frontend_dist, "index.html"))
    else:
        @app.get("/", include_in_schema=False)
        def root():
            return {"service": settings.app_name, "version": settings.version,
                    "docs": "/api/docs", "message": "Frontend not built — run `npm run build` in frontend/"}

    return app


app = create_app()
