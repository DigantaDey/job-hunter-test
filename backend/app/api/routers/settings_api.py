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
    data["ai"]["configured"] = is_configured()
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
async def ai_status(user: CurrentUser):
    health = await ping()
    stats = rate_limiter.stats()
    return {
        "online": health.get("online", False),
        "configured": is_configured(),
        "model": health.get("model") or settings.ai_model,
        "base_url": health.get("base_url") or settings.ai_base_url,
        "latency_ms": health.get("latency_ms"),
        "reason": health.get("reason") or health.get("error"),
        **stats,
        "breakers": breaker_snapshot(),
        "usage": usage_snapshot(),
        "overrides": sorted(get_workflow_overrides().keys()),
    }


@router.get("/ai/config")
def get_ai_config(user: CurrentUser, db: DbSession):
    """Per-workflow AI overrides (API keys are never returned in full)."""
    from app.models.models import SettingsModel

    rows = db.query(SettingsModel).filter(
        SettingsModel.user_id == user.id, SettingsModel.category == "ai_workflows"
    ).all()
    result: Dict[str, Any] = {}
    for row in rows:
        value = dict(row.value or {})
        if value.get("api_key"):
            value["api_key_set"] = True
            value["api_key"] = ""
        result[row.key] = value
    return {"workflows": WORKFLOWS, "overrides": result}


@router.post("/ai/config")
def update_ai_config(payload: Dict[str, Any], request: Request, user: CurrentUser, db: DbSession):
    """Configure a different base_url/model/key per workflow (blank = inherit)."""
    from app.models.models import SettingsModel

    normalized: Dict[str, Dict[str, str]] = {}
    for workflow, config in (payload or {}).items():
        if workflow not in WORKFLOWS:
            raise HTTPException(400, f"Unknown workflow '{workflow}'")
        if not isinstance(config, dict):
            raise HTTPException(400, f"Workflow '{workflow}' must be an object")
        cleaned = {key: str(config.get(key) or "") for key in ("base_url", "model", "api_key")}
        row = db.query(SettingsModel).filter(
            SettingsModel.user_id == user.id, SettingsModel.category == "ai_workflows", SettingsModel.key == workflow
        ).first()
        existing = dict(row.value or {}) if row else {}
        # A blank api_key means "keep the stored one" (the UI never receives it).
        if not cleaned["api_key"] and existing.get("api_key"):
            cleaned["api_key"] = existing["api_key"]
        if row:
            row.value = cleaned
        else:
            db.add(SettingsModel(user_id=user.id, category="ai_workflows", key=workflow, value=cleaned))
        normalized[workflow] = cleaned

    db.commit()
    set_workflow_overrides(normalized)
    audit.audit(db, "ai.config_updated", user=user, target=",".join(normalized),
                detail={"workflows": list(normalized.keys())}, request=request)
    return {"ok": True, "workflows": list(normalized.keys())}


@router.delete("/ai/config/{workflow}")
def clear_ai_config(workflow: str, user: CurrentUser, db: DbSession):
    from app.models.models import SettingsModel

    if workflow not in WORKFLOWS:
        raise HTTPException(400, f"Unknown workflow '{workflow}'")
    db.query(SettingsModel).filter(
        SettingsModel.user_id == user.id, SettingsModel.category == "ai_workflows", SettingsModel.key == workflow
    ).delete(synchronize_session=False)
    db.commit()
    set_workflow_overrides({workflow: {"base_url": "", "api_key": "", "model": ""}})
    return {"ok": True}
