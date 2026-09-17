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

from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.core.config import (
    AI_MIN_INPUT_TOKENS,
    AI_MIN_OUTPUT_TOKENS,
    AI_TOKEN_UNLIMITED,
    ai_token_budget,
    settings,
)
from app.core.logging import get_logger
from app.core.security import decrypt_secret, encrypt_secret
from app.models.models import SettingsModel, User

log = get_logger("app.settings")

CATEGORY = "email"
SECRET_KEYS = {("email", "password"), ("ai", "api_key")}

#: Values that must never be persisted as a secret — they look like the masked
#: placeholders the API hands back to the UI. Accepting one would silently
#: overwrite a working key with the literal string ``***`` (and the provider
#: would then reject every request with 401 ``invalid_api_key``).
_MASKED_CHARS = {"*", "•", "·", "x", "X"}


def _looks_masked(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    stripped = value.strip()
    return len(stripped) >= 3 and set(stripped) <= _MASKED_CHARS


def _secret_scope(user_id: int) -> str:
    return f"user:{user_id}:settings"


#: Supported AI providers — canonical values stored in DB.
AI_PROVIDER_OPENAI = "openai_compatible"
AI_PROVIDER_GOOGLE = "google"
# Alias accepted from frontend/docs: google_ai_studio == google
AI_PROVIDERS = frozenset({AI_PROVIDER_OPENAI, AI_PROVIDER_GOOGLE, "google_ai_studio"})

def _normalize_provider(value) -> str:
    text = str(value or "").strip().lower()
    if text in ("google", "google_ai_studio", "google_ai"):
        return AI_PROVIDER_GOOGLE
    if text in ("openai_compatible", "openai", "openai-compatible", ""):
        return AI_PROVIDER_OPENAI
    return text

def _detect_provider_from_base_url(base_url: str) -> str:
    lowered = (base_url or "").lower()
    if "generativelanguage.googleapis.com" in lowered or "generativelanguage" in lowered:
        return AI_PROVIDER_GOOGLE
    return AI_PROVIDER_OPENAI


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
            "timeout": settings.ai_timeout,
            # Token budgets (single source of truth for the gateway + every
            # prompt-truncation site). Editable by the owner and all paid
            # tiers; the free tier keeps the safe defaults read-only.
            "max_input_tokens": settings.ai_max_input_tokens,
            "max_output_tokens": settings.ai_max_output_tokens,
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
        "automation": {
            # Auto mode (v2.2): the scheduler only enqueues work for users who
            # switched it on. Off by default — nothing runs automatically that
            # the user did not ask for.
            "auto_mode": False,
        },
    }


# Keys that may be written per category (everything else → 400).
WRITABLE_KEYS: Dict[str, set] = {
    "ai": {"base_url", "model", "rpm", "max_retries", "timeout", "api_key",
           "max_input_tokens", "max_output_tokens", "provider"},
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
    # `auto_mode` is writable on every tier (turning scheduling *off* is never
    # restricted); turning it *on* is tier-gated in ``apply_updates``.
    "automation": {"auto_mode"},
}


def _budget_or(value: Any, fallback: Any) -> int:
    """A token budget: ``value`` when it is set, else ``fallback``.

    **``0`` counts as set** — it means unlimited (provider default). The old
    ``value or fallback`` idiom read an explicit *Unlimited* as "unset" and
    quietly restored the platform ceiling, which is how a Pro+ user with
    unlimited AI kept hitting a 16000-token wall and getting
    ``truncated_response`` on long job descriptions.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return ai_token_budget(fallback)
    return ai_token_budget(value)


def coerce_bool(value: Any) -> Any:
    """Strict-ish boolean coercion for settings values: ``None`` when the value
    is not a boolean at all.

    A JSON client sending ``"false"`` (a *string*) must not silently switch
    auto mode on — ``bool("false")`` is True, and a user who asked for automation
    to stop would keep paying quota for work they refused. Truthy/falsy *words*
    are accepted (UI switches and env-style values both arrive here), anything
    else is rejected by the caller.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on", "enabled"}:
            return True
        if text in {"false", "0", "no", "off", "disabled", ""}:
            return False
    return None


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


#: Validation ranges for the per-user token budgets (sane, model-aware lower
#: and upper bounds — see the over-context decision in the CHANGELOG).
#:
#: ``0`` is always accepted for both and means **unlimited**: no clamp, the
#: provider's own per-model maximum applies (the shipped default, and what Pro+
#: gets). A *positive* value must sit inside the range — a ceiling under the
#: floor is not a cost cap, it is a guarantee that every answer truncates.
TOKEN_LIMIT_RANGES = {
    "max_input_tokens": (AI_MIN_INPUT_TOKENS, 200000),
    "max_output_tokens": (AI_MIN_OUTPUT_TOKENS, 16000),
}


def can_edit_token_limits(db: Session, user: "User") -> bool:
    """Owner (the operator) and all paid tiers may edit the token budgets.

    The free tier keeps fixed safe defaults — enforced server-side here (a
    free-tier PUT is rejected), not merely hidden in the UI.
    """
    if user.role == "owner":
        return True
    from app.core.entitlements import get_user_plan

    return get_user_plan(db, int(user.id)) != "free"


def get_secret_setting(db: Session, user_id: int, category: str, key: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Read a secret column. Returns ``(value, error)``:

    * ``(None, None)`` — nothing stored;
    * ``(value, None)`` — decrypted successfully;
    * ``(None, "unreadable")`` — a value IS stored but cannot be decrypted
      (typically the server's ``ENCRYPTION_KEY`` changed after the secret was
      saved). The caller must surface this instead of silently falling back —
      otherwise the app keeps sending some *other* key while the UI still
      claims a key is configured.
    """
    try:
        row = (
            db.query(SettingsModel)
            .filter(SettingsModel.user_id == user_id, SettingsModel.category == category, SettingsModel.key == key)
            .first()
        )
    except Exception as exc:  # e.g. a malformed JSON cell — treat as unreadable, never 500
        log.warning("settings row %s.%s for user %s could not be read: %s", category, key, user_id, exc)
        return None, "unreadable"
    if row is None or row.value is None:
        return None, None
    if (category, key) not in SECRET_KEYS:
        value = row.value
        return (value if value is not None else None), None
    try:
        return decrypt_secret(str(row.value), _secret_scope(user_id)), None
    except Exception as exc:
        log.warning(
            "stored secret settings.%s.%s for user %s could not be decrypted: %s — "
            "it will be ignored until re-entered (ENCRYPTION_KEY changed?)",
            category, key, user_id, exc,
        )
        return None, "unreadable"


def get_setting(db: Session, user_id: int, category: str, key: str, default: Any = None) -> Any:
    value, _error = get_secret_setting(db, user_id, category, key)
    return default if value is None else value


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


# --------------------------------------------------------------------------- #
# Per-workflow AI overrides (category "ai_workflows", key = workflow name)
#
# The value is a JSON object {base_url, model, api_key}. The api_key is
# encrypted at rest with the same per-user scope as the default AI key.
# Rows written by older versions stored the key in plaintext; readers accept
# both (``read_workflow_override``) and writers always encrypt.
# --------------------------------------------------------------------------- #
_OVERRIDE_KEYS = ("base_url", "model", "api_key", "provider")


def read_workflow_override(db: Session, user_id: int, workflow: str) -> Dict[str, str]:
    """Decrypted, trimmed override for one workflow (empty dict when unset)."""
    try:
        row = (
            db.query(SettingsModel)
            .filter(SettingsModel.user_id == user_id, SettingsModel.category == "ai_workflows", SettingsModel.key == workflow)
            .first()
        )
    except Exception as exc:  # malformed JSON cell — treat as no override, never 500
        log.warning("workflow override row for user %s/%s could not be read: %s", user_id, workflow, exc)
        return {}
    if row is None or not isinstance(row.value, dict):
        return {}
    raw = row.value
    cfg = {k: str(raw.get(k) or "").strip() for k in _OVERRIDE_KEYS}
    stored_key = str(raw.get("api_key") or "")
    if stored_key.startswith("gAAAA"):  # Fernet token — decrypt
        try:
            cfg["api_key"] = decrypt_secret(stored_key, _secret_scope(user_id))
        except Exception as exc:
            log.warning("workflow override api_key for user %s/%s could not be decrypted: %s", user_id, workflow, exc)
            cfg["api_key"] = ""
    # Normalize provider; auto-detect from base_url if not set
    provider_raw = (raw.get("provider") or "").strip()
    if provider_raw:
        cfg["provider"] = _normalize_provider(provider_raw)
    else:
        cfg["provider"] = _detect_provider_from_base_url(cfg.get("base_url") or "") if cfg.get("base_url") else ""
    return cfg


def write_workflow_override(db: Session, user_id: int, workflow: str, cfg: Dict[str, Any]) -> Dict[str, str]:
    """Persist an override (api_key encrypted at rest). Returns the cleaned, decrypted view."""
    cleaned = {k: str((cfg or {}).get(k) or "").strip() for k in _OVERRIDE_KEYS}
    # Normalize provider
    if cleaned.get("provider"):
        cleaned["provider"] = _normalize_provider(cleaned["provider"])
    elif cleaned.get("base_url"):
        cleaned["provider"] = _detect_provider_from_base_url(cleaned["base_url"])
    else:
        cleaned["provider"] = ""
    if cleaned["api_key"] and _looks_masked(cleaned["api_key"]):
        from fastapi import HTTPException

        raise HTTPException(
            400,
            {"code": "masked_api_key",
             "message": "That looks like a masked placeholder — paste the full API key (it is never displayed back in full)."},
        )
    # Validate provider if set
    if cleaned.get("provider") and cleaned["provider"] not in AI_PROVIDERS:
        from fastapi import HTTPException
        raise HTTPException(400, {"code": "invalid_provider", "message": f"provider must be one of {sorted(AI_PROVIDERS)}"})
    stored = dict(cleaned)
    if cleaned["api_key"]:
        stored["api_key"] = encrypt_secret(cleaned["api_key"], _secret_scope(user_id))
    row = (
        db.query(SettingsModel)
        .filter(SettingsModel.user_id == user_id, SettingsModel.category == "ai_workflows", SettingsModel.key == workflow)
        .first()
    )
    if row is None:
        db.add(SettingsModel(user_id=user_id, category="ai_workflows", key=workflow, value=stored))
    else:
        row.value = stored
    return cleaned


def delete_workflow_override(db: Session, user_id: int, workflow: str) -> None:
    db.query(SettingsModel).filter(
        SettingsModel.user_id == user_id, SettingsModel.category == "ai_workflows", SettingsModel.key == workflow
    ).delete(synchronize_session=False)


def get_user_ai_config(db: Session, user_id: int) -> Dict[str, Any]:
    """
    Resolve per-user AI config from DB (owner fallback as global default).
    Returns dict with base_url, model, api_key (decrypted), api_key_set bool,
    key_source ("user" | "owner" | "env") and api_key_error (set when a stored
    key exists but cannot be decrypted).
    Resolution order:
    1. Current user's DB settings
    2. Owner's DB settings (first owner user) as global default
    3. Env defaults
    """
    # Current user
    base_url = get_setting(db, user_id, "ai", "base_url", None)
    model = get_setting(db, user_id, "ai", "model", None)
    api_key, api_key_error = get_secret_setting(db, user_id, "ai", "api_key")
    provider_raw = get_setting(db, user_id, "ai", "provider", None)
    provider = _normalize_provider(provider_raw) if provider_raw else ""
    key_source: Optional[str] = "user" if api_key else None

    # Token budgets: the user's own value, else the owner's (the operator's
    # global default), else the env default. Same resolution order as the key.
    max_input_tokens = get_setting(db, user_id, "ai", "max_input_tokens", None)
    max_output_tokens = get_setting(db, user_id, "ai", "max_output_tokens", None)

    # If current user doesn't have api_key, try owner fallback
    owner_provider = None
    if not api_key:
        try:
            owner = db.query(User).filter(User.role == "owner").order_by(User.id.asc()).first()
            if owner and owner.id != user_id:
                owner_key, _owner_err = get_secret_setting(db, owner.id, "ai", "api_key")
                if owner_key:
                    api_key = owner_key
                    base_url = base_url or get_setting(db, owner.id, "ai", "base_url", None)
                    model = model or get_setting(db, owner.id, "ai", "model", None)
                    if not provider:
                        owner_provider = get_setting(db, owner.id, "ai", "provider", None)
                    key_source = "owner"
                if max_input_tokens is None:
                    max_input_tokens = get_setting(db, int(owner.id), "ai", "max_input_tokens", None)
                if max_output_tokens is None:
                    max_output_tokens = get_setting(db, int(owner.id), "ai", "max_output_tokens", None)
                if not provider and owner_provider:
                    provider = _normalize_provider(owner_provider)
        except Exception:
            pass

    if not api_key and settings.ai_api_key:
        api_key = settings.ai_api_key
        key_source = "env"

    # Resolve provider: explicit -> owner fallback -> auto-detect from base_url -> default
    final_base = base_url or settings.ai_base_url
    if not provider:
        provider = _detect_provider_from_base_url(final_base)
    # Normalize google alias
    provider = _normalize_provider(provider)
    if provider not in AI_PROVIDERS:
        provider = AI_PROVIDER_OPENAI

    return {
        "base_url": final_base,
        "model": model or settings.ai_model,
        "api_key": api_key or "",
        "api_key_set": bool(api_key),
        "key_source": key_source,
        "api_key_error": api_key_error,
        "rpm": get_setting(db, user_id, "ai", "rpm", settings.ai_rpm),
        "max_retries": get_setting(db, user_id, "ai", "max_retries", settings.ai_max_retries),
        "timeout": get_setting(db, user_id, "ai", "timeout", settings.ai_timeout),
        # ``0`` = unlimited, so these must be first-value-set, never
        # ``value or default`` — that silently turned an explicit "unlimited"
        # back into the env ceiling.
        "max_input_tokens": _budget_or(max_input_tokens, settings.ai_max_input_tokens),
        "max_output_tokens": _budget_or(max_output_tokens, settings.ai_max_output_tokens),
        "provider": provider,
    }


def grouped(db: Session, user: User, *, reveal_secrets: bool = False) -> Dict[str, Dict[str, Any]]:
    """Return all settings for a user, defaults filled in, secrets redacted."""
    result = defaults()

    # Secrets go through the decrypting accessor so the UI reflects what the
    # app can actually use (a stored-but-undecryptable key is reported, not
    # silently reported as "set").
    ai_key, ai_key_error = get_secret_setting(db, user.id, "ai", "api_key")
    email_password, _email_error = get_secret_setting(db, user.id, "email", "password")

    try:
        rows = db.query(SettingsModel).filter(SettingsModel.user_id == user.id).all()
    except Exception as exc:  # a single malformed cell must not take the page down
        log.warning("settings rows for user %s could not be listed: %s", user.id, exc)
        rows = []
    for row in rows:
        if row.category == "ai_workflows":
            continue
        bucket = result.setdefault(row.category, {})
        if (row.category, row.key) in SECRET_KEYS:
            continue  # handled above from the decrypted values
        bucket[row.key] = row.value

    # Provider — respect stored value or auto-detect from base_url
    provider_raw = get_setting(db, user.id, "ai", "provider", None)
    # grouped is per-user; owner fallback for display is same as get_user_ai_config
    # so show the user's own provider, or the owner's when they have no key.
    try:
        from app.services.user_settings import get_user_ai_config as _g
        # Use helper to resolve provider with fallback logic
        cfg_for_provider = get_user_ai_config(db, user.id)
        result["ai"]["provider"] = cfg_for_provider.get("provider", "openai_compatible")
    except Exception:
        result["ai"]["provider"] = _normalize_provider(provider_raw) if provider_raw else _detect_provider_from_base_url(str(result["ai"].get("base_url") or ""))
    result["ai"]["openai_compatible"] = result["ai"]["provider"] == AI_PROVIDER_OPENAI
    result["ai"]["api_key_set"] = bool(ai_key or settings.ai_api_key)
    # ``0`` on a token budget means unlimited (no clamp — the provider's own
    # maximum). Spelled out for the UI so it can render an "Unlimited" badge
    # instead of a bare 0, and so the platform default is visible next to it.
    result["ai"]["max_output_tokens"] = ai_token_budget(result["ai"].get("max_output_tokens"))
    result["ai"]["max_input_tokens"] = ai_token_budget(result["ai"].get("max_input_tokens"))
    result["ai"]["max_output_tokens_unlimited"] = result["ai"]["max_output_tokens"] <= 0
    result["ai"]["max_input_tokens_unlimited"] = result["ai"]["max_input_tokens"] <= 0
    result["ai"]["platform_max_output_tokens"] = settings.ai_max_output_tokens
    result["ai"]["platform_max_input_tokens"] = settings.ai_max_input_tokens
    if ai_key:
        result["ai"]["api_key_masked"] = "***"
    if ai_key_error:
        result["ai"]["api_key_error"] = "unreadable"
    if reveal_secrets and ai_key is not None:
        result["ai"]["api_key"] = ai_key

    result["email"]["password_set"] = bool(email_password)
    if reveal_secrets and email_password is not None:
        result["email"]["password"] = email_password

    # If owner, also expose global defaults info
    if user.role == "owner":
        result["ai"]["is_owner"] = True
        result["ai"]["is_global_default"] = True
    else:
        result["ai"]["is_owner"] = False
    # The UI disables the token-limit fields (with the upgrade reason) exactly
    # when the server would reject a PUT — one predicate, both sides.
    result["ai"]["token_limits_editable"] = can_edit_token_limits(db, user)
    if not result["ai"]["token_limits_editable"]:
        result["ai"]["token_limits_locked_reason"] = "upgrade to adjust token limits"

    # Auto mode (v2.2), same rule: the switch is disabled in the UI exactly when
    # the PUT gate would reject it, and the cadence the card displays is read
    # from the very table the scheduler sweeps on — the schedule a user is shown
    # is the schedule they get, never a copy that can drift.
    from app.core.entitlements import can as capability_can
    from app.core.entitlements import get_user_plan
    from app.services.auto_scheduler import plan_cadence

    automation = result.setdefault("automation", {})
    automation["can_use"] = bool(capability_can(db, int(user.id), "can_use_scheduled_workflows"))
    automation["cadence"] = plan_cadence(get_user_plan(db, int(user.id)))
    if not automation["can_use"]:
        automation["locked_reason"] = "Locked on the free tier — upgrade to Pro / Pro+"

    # Show if using owner fallback
    try:
        owner = db.query(User).filter(User.role == "owner").order_by(User.id.asc()).first()
        if owner:
            owner_has_key = bool(get_setting(db, owner.id, "ai", "api_key", None))
            if owner_has_key and not ai_key and not settings.ai_api_key:
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

    # Server-side tier gate for the token budgets: the free tier keeps fixed
    # safe defaults — a PUT that touches them is rejected (not just hidden).
    if "ai" in payload:
        token_keys = [k for k in (payload["ai"] or {}) if k in TOKEN_LIMIT_RANGES]
        if token_keys and not can_edit_token_limits(db, user):
            from fastapi import HTTPException

            raise HTTPException(
                403,
                {
                    "code": "upgrade_required",
                    "message": "Token limits are editable on paid tiers — upgrade to adjust token limits.",
                    "fields": sorted(token_keys),
                },
            )

    # Server-side tier gate for auto mode (v2.2), same shape as the gate above.
    # The entitlement it enforces (`can_use_scheduled_workflows`) used to be a
    # flag nothing consumed; the scheduler now only enqueues for users whose plan
    # has it. Switching auto mode *off* is never gated — a lock that only blocks
    # enabling can never trap a user who wants out.
    if "automation" in payload and "auto_mode" in (payload["automation"] or {}):
        if coerce_bool((payload["automation"] or {}).get("auto_mode")):
            from app.core.entitlements import can as capability_can

            if not capability_can(db, int(user.id), "can_use_scheduled_workflows"):
                raise HTTPException(
                    403,
                    {
                        "code": "upgrade_required",
                        "capability": "can_use_scheduled_workflows",
                        "message": "Scheduled auto mode is a Pro / Pro+ feature — upgrade to enable it.",
                        "fields": ["auto_mode"],
                    },
                )

    # Validate provider if present
    if "ai" in payload and "provider" in (payload["ai"] or {}):
        prov = _normalize_provider((payload["ai"] or {}).get("provider"))
        if prov not in AI_PROVIDERS:
            raise HTTPException(400, {"code": "invalid_provider", "message": f"provider must be one of {sorted(AI_PROVIDERS)} — got {prov!r}"})
        # Normalize in place so stored value is canonical
        (payload["ai"] or {})["provider"] = prov
        # When provider is google, base_url defaults to Google endpoint if not provided
        if prov == AI_PROVIDER_GOOGLE and not (payload["ai"] or {}).get("base_url"):
            # Allow auto-detect; no default injection here — client will default, but validate that model looks like gemini if provided
            pass

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
            if (category, key) == ("automation", "auto_mode"):
                # Coerce strictly. Storing the *string* "false" would be truthy
                # to the scheduler, i.e. a user who turned auto mode off would
                # keep consuming quota on a switch that looks off.
                coerced = coerce_bool(value)
                if coerced is None:
                    raise HTTPException(
                        400,
                        {"code": "invalid_setting_value",
                         "message": "'automation.auto_mode' must be true or false",
                         "fields": ["auto_mode"]},
                    )
                value = coerced
            if key in {"rpm", "port", "freshness_hours", "daily_limit", "max_retries",
                       "daily_application_limit", "rate_limit_per_day"}:
                try:
                    value = int(value)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(400, f"'{category}.{key}' must be an integer") from exc
            if category == "ai" and key == "timeout":
                # Generous but bounded: heavy reasoning models need minutes,
                # but an unbounded wait would hang HTTP requests forever.
                try:
                    value = float(value)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(400, "'ai.timeout' must be a number of seconds") from exc
                if not 5 <= value <= 1800:
                    raise HTTPException(400, "'ai.timeout' must be between 5 and 1800 seconds")
            if category == "ai" and key in TOKEN_LIMIT_RANGES:
                # Sane ranges; over-context behaviour is clamping-with-warning
                # (the gateway truncates/clamps and flags it — it never
                # rejects a save just because a model's window is smaller).
                #
                # ``0`` is the escape hatch, not an out-of-range value: it means
                # unlimited (no clamp — the provider's own maximum). A negative
                # value is a typo for "off" and is stored as 0 rather than
                # becoming a 1-token cap that truncates every answer.
                lo, hi = TOKEN_LIMIT_RANGES[key]
                try:
                    value = int(value)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(400, f"'{category}.{key}' must be an integer number of tokens") from exc
                if value < 0:
                    value = AI_TOKEN_UNLIMITED
                elif value != AI_TOKEN_UNLIMITED and not lo <= value <= hi:
                    raise HTTPException(
                        400,
                        f"'{category}.{key}' must be 0 (unlimited — the provider's own maximum) "
                        f"or between {lo} and {hi} tokens",
                    )
            # Hygiene for AI connection fields: paste artifacts (trailing
            # whitespace/newlines) and masked placeholders are the two classic
            # ways a *valid* key ends up rejected by the provider.
            if category == "ai" and key in ("base_url", "model", "api_key", "provider") and value is not None:
                value = str(value).strip()
                if key == "provider" and value:
                    value = _normalize_provider(value)
                    if value not in AI_PROVIDERS:
                        raise HTTPException(400, {"code": "invalid_provider", "message": f"provider must be one of {sorted(AI_PROVIDERS)}"})
                if key == "api_key" and value and _looks_masked(value):
                    raise HTTPException(
                        400,
                        {"code": "masked_api_key",
                         "message": "That looks like a masked placeholder — paste the full API key (it is never displayed back in full)."},
                    )
            if (category, key) in SECRET_KEYS and _looks_masked(value):
                raise HTTPException(
                    400,
                    {"code": "masked_secret",
                     "message": "That looks like a masked placeholder — paste the full value (secrets are never displayed back in full)."},
                )
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
