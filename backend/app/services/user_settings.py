"""
Per-user settings (tenant-scoped, whitelisted, secrets encrypted at rest).

The settings surface is intentionally a *schema* (known categories + keys with
defaults) rather than a free-form key/value store: the API rejects unknown keys,
so a typo can never silently disable a safety control.
"""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decrypt_secret, encrypt_secret
from app.models.models import SettingsModel, User

CATEGORY = "email"
SECRET_KEYS = {("email", "password")}


def _secret_scope(user_id: int) -> str:
    return f"user:{user_id}:settings"


def defaults() -> Dict[str, Dict[str, Any]]:
    """Factory for the default settings document of a user."""
    return {
        "ai": {
            "base_url": settings.ai_base_url,
            "model": settings.ai_model,
            "rpm": settings.ai_rpm,
            "api_key_set": bool(settings.ai_api_key),
            "max_retries": settings.ai_max_retries,
        },
        "scraping": {
            "keywords": settings.default_keywords,
            # Which adapters may be used. Sources marked `requires_account` in
            # the registry stay off until the user opts in explicitly.
            "sources": ["greenhouse", "lever", "ashby", "smartrecruiters", "workable",
                        "remoteok", "jobicy", "himalayas", "arbeitnow", "remotive"],
            "live_enabled": settings.live_scraping_enabled,
            "live_sources": ["arbeitnow", "remotive"],
            "freshness_hours": settings.default_freshness_hours,
        },
        "general": {
            "strict_skeleton": False,
            "auto_approve_email": False,
            "default_resume_format": "ai_generated",
            "require_resume_approval": True,
            "reuse_similarity_threshold": 0.85,
            "generate_min_score": 65,
        },
        "application": {
            "auto_create_credentials": True,
            "notify_unknown_fields": True,
            "autofill_enabled": settings.autofill_enabled,
            "autofill_dry_run": settings.autofill_dry_run,
            "allow_auto_submit": False,   # gated behind explicit consent
            "daily_application_limit": 25,
        },
        "email": {
            "host": settings.email_smtp_host,
            "port": settings.email_smtp_port,
            "username": settings.email_smtp_username,
            "use_tls": settings.email_smtp_use_tls,
            "from_name": settings.email_from_name,
            "postal_address": settings.email_postal_address,
            "daily_limit": settings.email_daily_limit,
            "dry_run": not settings.email_real_sending,
            "tracking_enabled": settings.email_tracking_enabled,
            "password_set": bool(settings.email_smtp_password),
        },
        "funding": {
            "context_notes": "",
            "industries": "",
            "freshness_days": settings.funding_freshness_days,
            "provider": settings.funding_provider,
            "include_unverified": settings.allow_synthetic_funding_data,
        },
        "compliance": {
            "automation_acknowledged": False,
            "outreach_acknowledged": False,
            "rate_limit_per_day": 40,
        },
        "workflows": {
            "ai_for_discovery": True,
            "ai_for_scoring": True,
            "ai_for_resume": True,
            "ai_for_emails": True,
        },
    }


# Keys that may be written per category (everything else → 400).
WRITABLE_KEYS: Dict[str, set] = {
    "ai": {"base_url", "model", "rpm", "max_retries"},
    "scraping": {"keywords", "sources", "live_enabled", "live_sources", "freshness_hours"},
    "general": {"strict_skeleton", "auto_approve_email", "default_resume_format",
                "require_resume_approval", "reuse_similarity_threshold", "generate_min_score"},
    "application": {"auto_create_credentials", "notify_unknown_fields", "autofill_enabled",
                    "autofill_dry_run", "allow_auto_submit", "daily_application_limit"},
    "email": {"host", "port", "username", "password", "use_tls", "from_name", "postal_address",
              "daily_limit", "dry_run", "tracking_enabled"},
    "funding": {"context_notes", "industries", "freshness_days", "provider", "include_unverified"},
    "compliance": {"automation_acknowledged", "outreach_acknowledged", "rate_limit_per_day"},
    "workflows": {"ai_for_discovery", "ai_for_scoring", "ai_for_resume", "ai_for_emails"},
}


def get_setting(db: Session, user_id: int, category: str, key: str, default: Any = None) -> Any:
    row = (
        db.query(SettingsModel)
        .filter(SettingsModel.user_id == user_id, SettingsModel.category == category, SettingsModel.key == key)
        .first()
    )
    if row is None or row.value is None:
        return default
    if (category, key) in SECRET_KEYS:
        try:
            return decrypt_secret(str(row.value), _secret_scope(user_id))
        except Exception:
            return ""
    return row.value


def set_setting(db: Session, user_id: int, category: str, key: str, value: Any) -> SettingsModel:
    stored = value
    if (category, key) in SECRET_KEYS and isinstance(value, str) and value:
        stored = encrypt_secret(value, _secret_scope(user_id))
    row = (
        db.query(SettingsModel)
        .filter(SettingsModel.user_id == user_id, SettingsModel.category == category, SettingsModel.key == key)
        .first()
    )
    if row is None:
        row = SettingsModel(user_id=user_id, category=category, key=key, value=stored)
        db.add(row)
    else:
        row.value = stored
    return row


def grouped(db: Session, user: User, *, reveal_secrets: bool = False) -> Dict[str, Dict[str, Any]]:
    """Return all settings for a user, defaults filled in, secrets redacted."""
    result = defaults()
    rows = db.query(SettingsModel).filter(SettingsModel.user_id == user.id).all()
    for row in rows:
        if row.category == "ai_workflows":
            continue  # exposed through /api/ai/config
        bucket = result.setdefault(row.category, {})
        if (row.category, row.key) in SECRET_KEYS:
            bucket["password_set"] = bool(row.value)
            if reveal_secrets:
                try:
                    bucket[row.key] = decrypt_secret(str(row.value), _secret_scope(row.user_id))
                except Exception:
                    bucket[row.key] = ""
            continue
        bucket[row.key] = row.value
    result["email"]["password_set"] = bool(result["email"].get("password_set"))
    result["ai"]["api_key_set"] = bool(settings.ai_api_key) or result["ai"].get("api_key_set", False)
    return result


def apply_updates(db: Session, user: User, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate + persist a settings payload ``{category: {key: value}}``.

    Returns ``{"updated": {...}, "skipped": {...}}`` so the caller can report
    exactly which keys were written (unknown keys are rejected loudly).
    """
    from fastapi import HTTPException

    updated: Dict[str, Any] = {}
    skipped: Dict[str, Any] = {}
    for category, values in (payload or {}).items():
        if not isinstance(values, dict):
            raise HTTPException(400, f"Category '{category}' must be an object")
        allowed = WRITABLE_KEYS.get(category)
        if allowed is None:
            raise HTTPException(400, f"Unknown settings category '{category}'")
        for key, value in values.items():
            if key not in allowed:
                raise HTTPException(400, f"Unknown settings key '{category}.{key}'")
            if key in {"rpm", "port", "freshness_hours", "daily_limit", "max_retries",
                       "daily_application_limit", "rate_limit_per_day"}:
                try:
                    value = int(value)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(400, f"'{category}.{key}' must be an integer") from exc
            set_setting(db, user.id, category, key, value)
            updated.setdefault(category, {})[key] = "***" if (category, key) in SECRET_KEYS and value else value
    db.commit()
    return {"updated": updated, "skipped": skipped}
