from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os
import asyncio
from app.core.config import settings
from app.db import init_db
from app.api.routes import router
from app.services.ai_pipeline import ai_pipeline

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    os.makedirs(settings.upload_dir, exist_ok=True)
    os.makedirs(settings.generated_dir, exist_ok=True)
    # start AI pipeline worker
    worker = asyncio.create_task(ai_pipeline.worker_loop())
    print(f"✅ {settings.app_name} v{settings.version} started")
    print(f"   AI RPM: {settings.ai_rpm}, Base: {settings.ai_base_url}")
    yield
    worker.cancel()

app = FastAPI(title=settings.app_name, version=settings.version, docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")

@app.get("/api/health")
def health():
    return {"status": "ok", "version": settings.version}

@app.get("/api/meta")
def meta():
    return {
        "name": settings.app_name,
        "version": settings.version,
        "features": [
            "resume_layout_extraction",
            "ai_profile_extraction",
            "ai_keyword_extraction",       # NEW: context from profile+resume+user input
            "job_scraping",
            "three_fifo_pipelines",
            "vault_chrome_apple_export",
            "auto_fill",
            "resume_generation_jd_fact_guard",
            "scoring",
            "ai_rate_limiter",
            "company_classifier",
            "email_pipeline_smtp_2fa",
            "funding_radar_ai_context",     # NEW: fresh, context-driven, auto-refresh
            "dashboard",
            "error_logs",
            "dark_light_theme"
        ]
    }

# Serve frontend if built — SPA fallback so deep links (/funding, /jobs/…) work
frontend_dist = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist"))
if os.path.exists(frontend_dist):
    app.mount("/assets", StaticFiles(directory=os.path.join(frontend_dist, "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        candidate = os.path.join(frontend_dist, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(frontend_dist, "index.html"))
else:
    @app.get("/")
    def root():
        return {"message": f"{settings.app_name} API running. Frontend not built yet. See /api/docs"}
