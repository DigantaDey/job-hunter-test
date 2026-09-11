"""
Per-user settings (tenant-scoped, whitelisted, secrets encrypted at rest).

The settings surface is intentionally a *schema* (known categories + keys with
defaults) rather than a free-form key/value store: the API rejects unknown keys,
so a typo can never silently disable a safety control.

Owner account can set default AI API details (OpenAI compatible) via settings:
base_url, model, api_key — all stored encrypted per-user, with owner fallback
as global default.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decrypt_secret, encrypt_secret
from app.models.models import SettingsModel, User

CATEGORY = "email"
SECRET_KEYS = {("email", "password"), ("ai", "api_key")}


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
            "api_key_masked": "***" if settings.ai_api_key else "",
            "max_retries": settings.ai_max_retries,
            "provider": "openai_compatible",
            "openai_compatible": True,
        },
        "scraping": {
            "keywords": settings.default_keywords,
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
            "allow_auto_submit": False,
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
    "ai": {"base_url", "model", "rpm", "max_retries", "api_key"},
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


def validate_openai_compatible(base_url: str, model: str, api_key: str = "") -> Optional[str]:
    """
    Validate OpenAI compatible format.
    Returns error message if invalid, None if valid.
    OpenAI compatible requires:
    - base_url: valid https URL, typically ending with /v1, e.g. https://api.openai.com/v1 or https://api.groq.com/openai/v1 or custom
    - model: non-empty string, e.g. gpt-4o-mini, gpt-4o, claude-3, etc.
    - api_key: if provided, should be non-empty, typically starts with sk- or similar but we allow any non-empty for compatibility with local models (Ollama etc)
    """
    if base_url:
        try:
            parsed = urlparse(base_url)
            if parsed.scheme not in ("https", "http"):
                return "base_url must be a valid URL starting with https:// (e.g. https://api.openai.com/v1)"
            if not parsed.netloc:
                return "base_url must be a valid URL (e.g. https://api.openai.com/v1)"
        except Exception:
            return "base_url must be a valid URL"
    if model is not None and model != "":
        if len(model.strip()) < 2:
            return "model must be at least 2 characters (e.g. gpt-4o-mini)"
    return None


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


def get_user_ai_config(db: Session, user_id: int) -> Dict[str, Any]:
    """
    Resolve per-user AI config from DB (owner fallback as global default).
    Returns dict with base_url, model, api_key (decrypted), api_key_set bool.
    Resolution order:
    1. Current user's DB settings
    2. Owner's DB settings (first owner user) as global default
    3. Env defaults
    """
    # Current user
    base_url = get_setting(db, user_id, "ai", "base_url", None)
    model = get_setting(db, user_id, "ai", "model", None)
    api_key = get_setting(db, user_id, "ai", "api_key", None)

    # If current user doesn't have api_key, try owner fallback
    if not api_key:
        try:
            owner = db.query(User).filter(User.role == "owner").order_by(User.id.asc()).first()
            if owner and owner.id != user_id:
                owner_key = get_setting(db, owner.id, "ai", "api_key", None)
                owner_base = get_setting(db, owner.id, "ai", "base_url", None)
                owner_model = get_setting(db, owner.id, "ai", "model", None)
                if owner_key:
                    api_key = api_key or owner_key
                    base_url = base_url or owner_base
                    model = model or owner_model
        except Exception:
            pass

    return {
        "base_url": base_url or settings.ai_base_url,
        "model": model or settings.ai_model,
        "api_key": api_key or settings.ai_api_key,
        "api_key_set": bool(api_key or settings.ai_api_key),
        "rpm": get_setting(db, user_id, "ai", "rpm", settings.ai_rpm),
        "max_retries": get_setting(db, user_id, "ai", "max_retries", settings.ai_max_retries),
    }


def grouped(db: Session, user: User, *, reveal_secrets: bool = False) -> Dict[str, Dict[str, Any]]:
    """Return all settings for a user, defaults filled in, secrets redacted."""
    result = defaults()
    rows = db.query(SettingsModel).filter(SettingsModel.user_id == user.id).all()
    has_ai_key_in_db = False
    for row in rows:
        if row.category == "ai_workflows":
            continue
        bucket = result.setdefault(row.category, {})
        if (row.category, row.key) in SECRET_KEYS:
            if row.category == "ai" and row.key == "api_key":
                has_ai_key_in_db = bool(row.value)
                bucket["api_key_set"] = has_ai_key_in_db or bool(settings.ai_api_key)
                if has_ai_key_in_db:
                    bucket["api_key_masked"] = "***"
                if reveal_secrets:
                    try:
                        bucket[row.key] = decrypt_secret(str(row.value), _secret_scope(row.user_id))
                    except Exception:
                        bucket[row.key] = ""
                continue
            else:
                bucket["password_set"] = bool(row.value)
                if reveal_secrets:
                    try:
                        bucket[row.key] = decrypt_secret(str(row.value), _secret_scope(row.user_id))
                    except Exception:
                        bucket[row.key] = ""
                continue
        bucket[row.key] = row.value

    # Ensure api_key_set reflects DB or env
    if not has_ai_key_in_db:
        result["ai"]["api_key_set"] = bool(settings.ai_api_key)
    else:
        result["ai"]["api_key_set"] = True

    result["email"]["password_set"] = bool(result["email"].get("password_set"))

    # If owner, also expose global defaults info
    if user.role == "owner":
        result["ai"]["is_owner"] = True
        result["ai"]["is_global_default"] = True
    else:
        # Show if using owner fallback
        try:
            owner = db.query(User).filter(User.role == "owner").order_by(User.id.asc()).first()
            if owner:
                owner_has_key = bool(get_setting(db, owner.id, "ai", "api_key", None))
                if owner_has_key and not has_ai_key_in_db and not settings.ai_api_key:
                    result["ai"]["using_owner_default"] = True
                    result["ai"]["owner_base_url"] = get_setting(db, owner.id, "ai", "base_url", settings.ai_base_url)
                    result["ai"]["owner_model"] = get_setting(db, owner.id, "ai", "model", settings.ai_model)
        except Exception:
            pass

    return result


def apply_updates(db: Session, user: User, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate + persist a settings payload {category: {key: value}}.
    Returns {updated, skipped} so caller can report exactly which keys were written.
    Includes OpenAI compatible validation for ai category.
    """
    from fastapi import HTTPException

    updated: Dict[str, Any] = {}
    skipped: Dict[str, Any] = {}

    # Pre-validate OpenAI compatible format if ai category present
    if "ai" in payload:
        ai_payload = payload["ai"] or {}
        base_url = ai_payload.get("base_url")
        model = ai_payload.get("model")
        api_key = ai_payload.get("api_key")
        # Only validate if at least one of base_url/model/api_key is being updated
        if any(k in ai_payload for k in ("base_url", "model", "api_key")):
            # Get current values for validation context
            current_base = get_setting(db, user.id, "ai", "base_url", settings.ai_base_url) if base_url is None else base_url
            current_model = get_setting(db, user.id, "ai", "model", settings.ai_model) if model is None else model
            err = validate_openai_compatible(
                base_url if base_url is not None else current_base,
                model if model is not None else current_model,
                api_key if api_key is not None else ""
            )
            if err:
                raise HTTPException(400, {"code": "invalid_ai_config", "message": err, "hint": "OpenAI compatible format: base_url like https://api.openai.com/v1, model like gpt-4o-mini, api_key like sk-..."})

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
            # Empty api_key means keep existing (UI never receives full key)
            if (category, key) in SECRET_KEYS and key == "api_key" and (value == "" or value is None):
                # Skip if empty — keep existing
                existing = get_setting(db, user.id, category, key, None)
                if existing:
                    continue
                # If no existing and empty, allow clearing? For ai api_key, empty means clear only if explicitly wanted
                # We treat empty as no-op unless user wants to clear via explicit null
                if value == "":
                    continue
            set_setting(db, user.id, category, key, value)
            updated.setdefault(category, {})[key] = "***" if (category, key) in SECRET_KEYS and value else value
    db.commit()
    return {"updated": updated, "skipped": skipped}
