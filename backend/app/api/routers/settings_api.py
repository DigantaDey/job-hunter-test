"""Settings, AI configuration and platform status endpoints."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from app.api.deps import CurrentUser, DbSession
from app.core import audit
from app.core.config import settings
from app.core.rate_limiter import rate_limiter
from app.services.ai_client import (
    WORKFLOWS,
    breaker_snapshot,
    get_workflow_overrides,
    is_configured,
    ping,
    set_workflow_overrides,
    usage_snapshot,
)
from app.services.user_settings import WRITABLE_KEYS, apply_updates, grouped

router = APIRouter(tags=["settings"])


@router.get("/settings")
def get_settings(user: CurrentUser, db: DbSession):
    data = grouped(db, user)
    data["ai"]["configured"] = is_configured(db=db, user_id=user.id)
    data["_meta"] = {"writable": {category: sorted(keys) for category, keys in WRITABLE_KEYS.items()},
                     "environment": settings.environment}
    return data


@router.put("/settings")
def put_settings(payload: Dict[str, Any], request: Request, user: CurrentUser, db: DbSession):
    result = apply_updates(db, user, payload)
    if "ai" in payload and "rpm" in (payload["ai"] or {}):
        try:
            rate_limiter.update_rpm(int(payload["ai"]["rpm"]))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "ai.rpm must be an integer") from exc
    audit.audit(db, "settings.updated", user=user, target=",".join(payload.keys()),
                detail={"updated": result["updated"]}, request=request)
    return {"ok": True, **result}


@router.get("/settings/runtime")
def runtime(user: CurrentUser, db: DbSession):
    from app.services.autofill import autofill_available
    from app.services.funding_sources import provider_status
    from app.services.sources import list_sources

    return {
        "platform": settings.public_settings(),
        "autofill": autofill_available(),
        "funding_providers": provider_status(),
        "sources": list_sources(),
        "workflows": WORKFLOWS,
    }


@router.get("/settings/ai/status")
async def ai_status(user: CurrentUser, db: DbSession):
    # Per-user ping with owner fallback
    health = await ping(db=db, user_id=user.id)
    stats = rate_limiter.stats()
    # Resolve full config for user for UI display
    from app.services.user_settings import get_user_ai_config
    try:
        user_cfg = get_user_ai_config(db, user.id)
    except Exception:
        user_cfg = {"base_url": settings.ai_base_url, "model": settings.ai_model, "api_key_set": bool(settings.ai_api_key)}

    from app.services.ai_client import normalise_ping_reason

    # The probe speaks ``status_NNN``; the rest of the product speaks stable
    # reasons — normalise so the UI sees one vocabulary everywhere.
    reason = normalise_ping_reason(health.get("reason") or health.get("error"))
    hint = health.get("hint")
    stored_key_error = bool(user_cfg.get("api_key_error"))
    if stored_key_error and not health.get("online"):
        # A key is saved but cannot be decrypted — the app is currently using a
        # fallback (env/owner). Say so instead of a misleading auth failure.
        reason = "stored_key_unreadable"
        hint = ("Your saved API key can't be decrypted on this server (its ENCRYPTION_KEY changed). "
                "Re-enter the key below and save to fix it.")

    # The central three-state availability signal (v2.1) — the single value the
    # UI polls to decide between "AI paused — will resume automatically"
    # (transient_outage), "AI blocked — action needed" (blocked_needs_action)
    # and the green dot (online). It reflects what the NEXT call would
    # experience: the probe plus in-process breaker and daily budget.
    from app.services.ai_client import ai_availability
    from app.services.job_queue import paused_count

    availability = await ai_availability(db, int(user.id))
    return {
        "online": health.get("online", False),
        "state": availability.get("state"),
        "retry_after_hint": availability.get("retry_after_hint"),
        "paused_count": paused_count(db, user_id=int(user.id)),
        "configured": is_configured(db=db, user_id=user.id),
        "model": health.get("model") or user_cfg.get("model") or settings.ai_model,
        "base_url": health.get("base_url") or user_cfg.get("base_url") or settings.ai_base_url,
        "latency_ms": health.get("latency_ms"),
        "reason": reason,
        "hint": hint,
        "detail": health.get("detail"),
        "probe": health.get("probe"),
        "note": health.get("note"),
        "key_source": health.get("key_source") or user_cfg.get("key_source"),
        "key_preview": health.get("key_preview"),
        "stored_key_error": stored_key_error,
        "user_config": {
            "base_url": user_cfg.get("base_url"),
            "model": user_cfg.get("model"),
            "api_key_set": user_cfg.get("api_key_set"),
            "rpm": user_cfg.get("rpm"),
        },
        "is_owner": user.role == "owner",
        **stats,
        "breakers": breaker_snapshot(),
        "usage": usage_snapshot(),
        "overrides": sorted(get_workflow_overrides(user.id).keys()),
    }


@router.post("/settings/ai/resume")
async def resume_ai(user: CurrentUser, db: DbSession, request: Request):
    """One-click resume: re-queue this user's paused AI work right now.

    The watchdog already drains paused work automatically the moment the
    availability probe is green — this endpoint is for the impatient: it
    forces the drain immediately (the user explicitly asked). Idempotent and
    cheap when nothing is paused. The response includes the current
    availability state so the UI can explain why the work may pause again.
    """
    from app.services.ai_client import ai_availability
    from app.services.job_queue import drain_paused, paused_count

    paused_before = paused_count(db, user_id=int(user.id))
    resumed = drain_paused(db, user_id=int(user.id), force=True)
    # Probe after the drain so the "why did it pause again?" answer is current
    # (the probe result is cached ~60s — this is cheap).
    availability = await ai_availability(db, int(user.id))
    audit.audit(db, "ai.resume_requested", user=user, target="ai",
                detail={"resumed": resumed, "paused_before": paused_before,
                        "state": availability.get("state")},
                request=request)
    return {"ok": True, "resumed": resumed, "paused_count": paused_count(db, user_id=int(user.id)),
            "state": availability.get("state"),
            "retry_after_hint": availability.get("retry_after_hint")}


@router.get("/ai/config")
def get_ai_config(user: CurrentUser, db: DbSession):
    """Per-workflow AI overrides (API keys are never returned in full)."""
    from app.services.user_settings import read_workflow_override

    result: Dict[str, Any] = {}
    for workflow in WORKFLOWS:
        cfg = read_workflow_override(db, user.id, workflow)
        if not any(cfg.values()):
            continue
        result[workflow] = {
            "base_url": cfg.get("base_url", ""),
            "model": cfg.get("model", ""),
            "api_key": "",
            "api_key_set": bool(cfg.get("api_key")),
        }
    return {"workflows": WORKFLOWS, "overrides": result}


@router.post("/ai/config")
def update_ai_config(payload: Dict[str, Any], request: Request, user: CurrentUser, db: DbSession):
    """Configure a different base_url/model/key per workflow (blank = inherit)."""
    from app.services.user_settings import delete_workflow_override, read_workflow_override, write_workflow_override

    normalized: Dict[str, Dict[str, str]] = {}
    for workflow, config in (payload or {}).items():
        if workflow not in WORKFLOWS:
            raise HTTPException(400, f"Unknown workflow '{workflow}'")
        if not isinstance(config, dict):
            raise HTTPException(400, f"Workflow '{workflow}' must be an object")
        cleaned = {key: str(config.get(key) or "").strip() for key in ("base_url", "model", "api_key")}
        existing = read_workflow_override(db, user.id, workflow)
        # A blank api_key means "keep the stored one" (the UI never receives it).
        if not cleaned["api_key"] and existing.get("api_key"):
            cleaned["api_key"] = existing["api_key"]
        if not any(cleaned.values()):
            delete_workflow_override(db, user.id, workflow)
        else:
            # Stores the key encrypted at rest (same per-user scope as the default key).
            write_workflow_override(db, user.id, workflow, cleaned)
        normalized[workflow] = cleaned

    db.commit()
    set_workflow_overrides(normalized, user_id=user.id)
    audit.audit(db, "ai.config_updated", user=user, target=",".join(normalized),
                detail={"workflows": list(normalized.keys())}, request=request)
    return {"ok": True, "workflows": list(normalized.keys())}


@router.delete("/ai/config/{workflow}")
def clear_ai_config(workflow: str, user: CurrentUser, db: DbSession):
    from app.services.user_settings import delete_workflow_override

    if workflow not in WORKFLOWS:
        raise HTTPException(400, f"Unknown workflow '{workflow}'")
    delete_workflow_override(db, user.id, workflow)
    db.commit()
    set_workflow_overrides({workflow: {"base_url": "", "api_key": "", "model": ""}}, user_id=user.id)
    return {"ok": True}
