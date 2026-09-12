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
import json
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
from app.metrics_server import start_metrics_server
from app.services.ai_client import set_workflow_overrides
from app.services.ai_pipeline import ai_pipeline
from app.services.job_queue import recover_stalled
from app.worker import Worker

log = get_logger("app.main")
STARTED_AT = time.time()


def _load_workflow_overrides() -> None:
    """
    Warm the per-workflow AI override cache and migrate legacy rows.

    Rows written before the keys were encrypted at rest hold plaintext
    ``api_key`` values; they are re-encrypted in place on first boot. Per-user
    overrides are resolved fresh from the DB on each AI call, so this cache is
    only a warm start — staleness can never outlive a restart.
    """
    try:
        db = SessionLocal()
        try:
            from app.core.security import encrypt_secret
            from app.models.models import SettingsModel
            from app.services.user_settings import _secret_scope, read_workflow_override

            rows = db.query(SettingsModel).filter(SettingsModel.category == "ai_workflows").all()
            restored = migrated = 0
            for row in rows:
                if not isinstance(row.value, dict):
                    continue
                cfg = read_workflow_override(db, row.user_id, row.key)  # decrypts, legacy plaintext passthrough
                stored_key = str(row.value.get("api_key") or "")
                if cfg.get("api_key") and stored_key and not stored_key.startswith("gAAAA"):
                    try:  # legacy plaintext → encrypted at rest
                        row.value = {**row.value, "api_key": encrypt_secret(cfg["api_key"], _secret_scope(row.user_id))}
                        db.commit()
                        migrated += 1
                    except Exception:
                        db.rollback()
                set_workflow_overrides({row.key: cfg}, user_id=row.user_id)
                restored += 1
            if restored or migrated:
                log.info("loaded %d AI workflow override(s)%s", restored, f", migrated {migrated} to encrypted storage" if migrated else "")
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

    metrics_listener = start_metrics_server()

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
        if metrics_listener is not None:
            metrics_listener.stop()
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
    # HSTS is only correct once the deployment is actually served over https —
    # sending it on a local/plain-http install makes the origin unreachable.
    hsts = settings.is_production or settings.public_base_url.startswith("https://")
    app.add_middleware(SecurityHeadersMiddleware, enable_hsts=hsts)
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
    from app.services.ai_client import AIClientError
    from app.services.ai_guardrails import AIUnavailableError, GuardrailError, describe_ai_error

    @app.exception_handler(AIUnavailableError)
    async def ai_unavailable_handler(request: Request, exc: AIUnavailableError):
        """AI offline is never a silent degradation — the UI gets the reason."""
        inc("jobhunter_http_errors_total", kind="ai_unavailable", reason=exc.reason)
        log.warning("AI unavailable for %s (%s): %s", exc.workflow, exc.reason, exc.detail)
        return JSONResponse(
            status_code=503,
            content={"detail": exc.message, "request_id": getattr(request.state, "request_id", ""),
                     **exc.payload()},
        )

    @app.exception_handler(AIClientError)
    async def ai_client_error_handler(request: Request, exc: AIClientError):
        """A raw gateway failure that a service did not wrap itself.

        Same contract as the AIUnavailableError handler: a typed, pausable 503
        (transient) or a needs-action 503 (blocked) — never a generic 500,
        never a guessed result.
        """
        outage = describe_ai_error(exc)
        inc("jobhunter_http_errors_total", kind="ai_unavailable", reason=outage.reason)
        log.warning("AI failure on %s %s (%s): %s", request.method, request.url.path,
                    outage.reason, outage.detail)
        return JSONResponse(
            status_code=503,
            content={"detail": outage.message, "request_id": getattr(request.state, "request_id", ""),
                     **outage.payload()},
        )

    @app.exception_handler(GuardrailError)
    async def guardrail_handler(request: Request, exc: GuardrailError):
        """The model answered but the answer is not safe to ship."""
        inc("jobhunter_http_errors_total", kind="guardrail_failed", workflow=exc.workflow)
        log.warning("guardrail rejected %s output: %s", exc.workflow, exc.issues[:4])
        return JSONResponse(
            status_code=422,
            content={"detail": exc.payload()["message"], "request_id": getattr(request.state, "request_id", ""),
                     **exc.payload()},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        inc("jobhunter_http_errors_total", kind="validation")
        # ``exc.errors()`` carries live exception objects in ``ctx`` (pydantic
        # puts the original ValueError there), which are not JSON serialisable —
        # stringify anything unrepresentable instead of 500-ing on the error path.
        try:
            errors = json.loads(json.dumps(exc.errors()[:20], default=str))
        except Exception:  # pragma: no cover - defensive
            errors = []
        # A bare "Validation error" is useless in the UI: surface the first
        # human-readable message (and where it came from) alongside the details.
        message = "Validation error"
        if errors:
            field = ".".join(str(part) for part in errors[0].get("loc", ()) if part != "body")
            message = str(errors[0].get("msg") or message)
            if field:
                message = f"{field}: {message}"
        return JSONResponse(
            status_code=422,
            content={"detail": message, "message": message, "errors": errors,
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
        async def spa(request: Request, full_path: str):
            # Unknown /api/* paths must answer JSON: an HTML 200 here sends API
            # clients (and their error handling) down a confusing path.
            if full_path.startswith("api/") or full_path == "api":
                return JSONResponse(
                    status_code=404,
                    content={"detail": f"No API route /{full_path}",
                             "request_id": getattr(request.state, "request_id", "")},
                )
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
