"""
Unified OpenAI-compatible AI gateway.

Guarantees (in order of importance):

1. **Rate limit first.** Every call acquires a token from the global limiter
   before touching the wire, so parallel pipelines can never exceed the
   configured RPM.
2. **Bounded retries.** 429/5xx/network errors are retried with exponential
   backoff + jitter, honouring ``Retry-After``. 4xx auth/validation errors are
   not retried (they will not fix themselves).
3. **Circuit breaker.** After N consecutive failures the gateway fails fast for a
   cooldown window instead of queueing doomed requests; callers fall back to
   heuristics immediately.
4. **Observability.** Latency, status, token usage and breaker state are exported
   as metrics and surfaced through ``/api/settings/ai/status``.
5. **Per-workflow config.** Different base_url/model/key per workflow, resolved
   from the DB overrides with env defaults.
6. **Per-user credit tracking.** Every successful call can be attributed to a
   user for billing, limits and cost control.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.config import settings
from app.core.logging import get_logger, user_id_var
from app.core.metrics import inc, observe, set_gauge
from app.core.rate_limiter import rate_limiter

log = get_logger("app.ai")


#: Stable machine-readable reasons. Every AI failure the product can hit maps to
#: one of these so the UI can explain *why* the model is offline instead of
#: silently substituting a degraded result.
REASON_NO_API_KEY = "no_api_key"
REASON_INVALID_API_KEY = "invalid_api_key"
REASON_STORED_KEY_UNREADABLE = "stored_key_unreadable"
REASON_UNREACHABLE = "unreachable"
REASON_TIMEOUT = "timeout"
REASON_PROVIDER_STATUS = "provider_error"
REASON_RATE_LIMITED = "rate_limited"
REASON_QUOTA = "quota_exceeded"
REASON_CIRCUIT_OPEN = "circuit_open"
REASON_BUDGET = "budget_exhausted"
REASON_ENTITLEMENT = "limit_exceeded"
REASON_MALFORMED = "malformed_response"
REASON_INVALID_JSON = "invalid_json"
REASON_MODEL_UNAVAILABLE = "model_unavailable"
REASON_UNKNOWN = "unknown"

_REASON_PREFIXES = {
    "no_api_key": REASON_NO_API_KEY,
    "circuit_open": REASON_CIRCUIT_OPEN,
    "budget_exhausted": REASON_BUDGET,
    "limit_exceeded": REASON_ENTITLEMENT,
    "invalid_json": REASON_INVALID_JSON,
    "malformed_response": REASON_MALFORMED,
    "http_error": REASON_UNREACHABLE,
    "stored_key_unreadable": REASON_STORED_KEY_UNREADABLE,
}


def reason_from_message(message: str, status: Optional[int] = None) -> str:
    """Best-effort stable reason code for an error string."""
    text = (message or "").strip()
    head = text.split(":", 1)[0].strip().lower()
    if head in _REASON_PREFIXES:
        return _REASON_PREFIXES[head]
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered or "readtimeout" in lowered:
        return REASON_TIMEOUT
    if status in (401, 403) or "incorrect api key" in lowered or "invalid api key" in lowered:
        return REASON_INVALID_API_KEY
    if status == 429:
        return REASON_RATE_LIMITED
    if status in (402, 429) or "quota" in lowered or "insufficient_quota" in lowered or "billing" in lowered:
        return REASON_QUOTA
    if status == 404 or "model_not_found" in lowered or "does not exist" in lowered:
        return REASON_MODEL_UNAVAILABLE
    if status and status >= 500:
        return REASON_PROVIDER_STATUS
    if "connect" in lowered or "name or service not known" in lowered or "ssl" in lowered:
        return REASON_UNREACHABLE
    return REASON_UNKNOWN


class AIClientError(Exception):
    """Raised when the AI layer cannot produce a result (unconfigured, timeout, HTTP error…)."""

    def __init__(self, message: str, *, status: Optional[int] = None, retryable: bool = False,
                 reason: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.reason = reason or reason_from_message(message, status)

    def diagnostics(self) -> Dict[str, Any]:
        return {"reason": self.reason, "status": self.status, "retryable": self.retryable,
                "detail": str(self)[:500]}


WORKFLOWS = {
    "parse": "resume parsing / profile extraction",
    "keyword_extract": "search-context / keyword extraction",
    "scoring": "profile ↔ JD scoring",
    "resume_gen": "tailored resume generation",
    "classify": "company size classification",
    "email_gen": "cold-email + decision-maker discovery",
    "form_detect": "job-portal form-structure detection",
    "funding_scan": "funding radar scan",
    "tagging": "resume auto-tagging",
    "interview": "interview preparation",
    "company_intel": "company intelligence",
    "persona": "user-persona reflection / track suggestions",
}

_workflow_overrides: Dict[Tuple[int, str], Dict[str, str]] = {}
_semaphore: Optional[asyncio.Semaphore] = None


@dataclass
class Breaker:
    failures: int = 0
    opened_at: float = 0.0
    last_error: str = ""

    def is_open(self) -> bool:
        if self.failures < settings.ai_breaker_failures:
            return False
        return (time.monotonic() - self.opened_at) < settings.ai_breaker_cooldown_seconds

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = 0.0
        self.last_error = ""

    def record_failure(self, error: str) -> None:
        self.failures += 1
        self.last_error = error
        if self.failures >= settings.ai_breaker_failures:
            self.opened_at = time.monotonic()

    def state(self) -> Dict[str, Any]:
        return {
            "failures": self.failures,
            "open": self.is_open(),
            "cooldown_remaining": max(0.0, settings.ai_breaker_cooldown_seconds - (time.monotonic() - self.opened_at))
            if self.failures >= settings.ai_breaker_failures else 0.0,
            "last_error": self.last_error,
        }


_breakers: Dict[str, Breaker] = {}
_usage: Dict[str, Dict[str, int]] = {}
_budget_day: str = ""
_budget_used: int = 0


def _spend_tokens(count: int) -> None:
    """Track the daily token spend used by the budget guard."""
    global _budget_day, _budget_used
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if today != _budget_day:
        _budget_day, _budget_used = today, 0
    _budget_used += max(0, int(count))
    if settings.ai_daily_token_budget:
        set_gauge("jobhunter_ai_tokens_today", _budget_used)


def budget_snapshot() -> Dict[str, Any]:
    return {"day": _budget_day or time.strftime("%Y-%m-%d", time.gmtime()),
            "used": _budget_used, "limit": settings.ai_daily_token_budget}


def budget_exhausted() -> bool:
    limit = settings.ai_daily_token_budget
    if not limit:
        return False
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if today != _budget_day:
        return False
    return _budget_used >= limit


def _breaker(workflow: str) -> Breaker:
    return _breakers.setdefault(workflow or "default", Breaker())


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, settings.ai_max_concurrency))
    return _semaphore


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def set_workflow_overrides(overrides: Dict[str, Dict[str, str]], user_id: int = 0) -> None:
    """Update the in-memory override cache (bucket 0 = system-level legacy path).

    Per-user overrides are resolved fresh from the DB by
    ``resolve_config_for_user`` — this cache only short-circuits the env-only
    ``resolve_config`` path and the status listing.
    """
    for workflow, cfg in (overrides or {}).items():
        if workflow not in WORKFLOWS:
            continue
        if isinstance(cfg, dict):
            cleaned = {k: (str(v).strip() if v else "") for k, v in cfg.items() if k in ("base_url", "api_key", "model")}
            if any(cleaned.values()):
                _workflow_overrides[(user_id, workflow)] = cleaned
            else:
                _workflow_overrides.pop((user_id, workflow), None)


def get_workflow_overrides(user_id: Optional[int] = None) -> Dict[str, Dict[str, str]]:
    if user_id is None:
        return {k[1]: dict(v) for k, v in _workflow_overrides.items()}
    return {k[1]: dict(v) for k, v in _workflow_overrides.items() if k[0] == user_id}


def resolve_config(workflow: Optional[str] = None) -> Dict[str, str]:
    """Env-level config (no user context). Includes the legacy system bucket."""
    override = _workflow_overrides.get((0, workflow or ""), {}) if workflow else {}
    api_key = (override.get("api_key") or settings.ai_api_key or "").strip()
    return {
        "base_url": (override.get("base_url") or settings.ai_base_url).strip().rstrip("/"),
        "api_key": api_key,
        "model": (override.get("model") or settings.ai_model).strip(),
        "key_source": "workflow_override" if override.get("api_key") else ("env" if api_key else None),
    }


def resolve_config_for_user(db, user_id: int, workflow: Optional[str] = None) -> Dict[str, str]:
    """
    Resolve AI config for a specific user, including per-user DB settings and owner fallback.
    Resolution order:
    1. Per-workflow override (from DB ai_workflows rows — tenant-scoped, read
       fresh so multi-worker deployments never serve stale keys)
    2. Per-user default AI settings (from SettingsModel category=ai)
    3. Owner's default AI settings as global fallback
    4. Env defaults
    All are OpenAI compatible: base_url, model, api_key
    """
    key_source: Optional[str] = None
    key_error: Optional[str] = None
    try:
        from app.services.user_settings import get_user_ai_config
        user_cfg = get_user_ai_config(db, user_id)
        base_url = user_cfg.get("base_url") or settings.ai_base_url
        model = user_cfg.get("model") or settings.ai_model
        api_key = user_cfg.get("api_key") or settings.ai_api_key
        key_source = user_cfg.get("key_source") or ("env" if api_key else None)
        key_error = user_cfg.get("api_key_error")
    except Exception as exc:
        log.warning("per-user AI config resolution failed for user %s, using env defaults: %s", user_id, exc)
        base_url = settings.ai_base_url
        model = settings.ai_model
        api_key = settings.ai_api_key
        key_source = "env" if api_key else None

    # Apply per-workflow override if present (overrides per-user and env)
    if workflow:
        try:
            from app.services.user_settings import read_workflow_override
            override = read_workflow_override(db, user_id, workflow)
            if override:
                base_url = override.get("base_url") or base_url
                api_key = override.get("api_key") or api_key
                model = override.get("model") or model
                if override.get("api_key"):
                    key_source = "workflow_override"
                    key_error = None
        except Exception as exc:
            log.warning("could not read workflow override for user %s/%s: %s", user_id, workflow, exc)

    return {
        "base_url": (base_url or "").strip().rstrip("/"),
        "api_key": (api_key or "").strip(),
        "model": (model or "").strip(),
        "key_source": key_source,
        "key_error": key_error,
    }


def _ambient_user_id() -> Optional[int]:
    """User id bound to the current request / worker job by the auth layer.

    ``get_current_user`` sets it for every API request and the worker's
    ``LogContext`` sets it per queue item, so AI calls made deep inside
    pipelines (which historically had no user context) can still resolve the
    *user's* configured key instead of falling back to env-only config.
    """
    raw = user_id_var.get()
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


@dataclass
class _ResolvedAI:
    cfg: Dict[str, str]
    db: Optional[Any] = None
    user_id: Optional[int] = None
    owned_session: bool = False  # caller must close db (ambient lookup)


def _resolve_ai_config(workflow: Optional[str], db, user_id: Optional[int]) -> _ResolvedAI:
    """Explicit (db, user_id) → per-user config; else ambient request/worker user; else env."""
    if db is not None and user_id is not None:
        try:
            return _ResolvedAI(resolve_config_for_user(db, user_id, workflow), db, user_id, False)
        except Exception as exc:
            log.warning("AI config resolution failed, falling back: %s", exc)
    ambient = _ambient_user_id()
    if ambient is not None:
        from app.db import SessionLocal

        session = SessionLocal()
        try:
            cfg = resolve_config_for_user(session, ambient, workflow)
            return _ResolvedAI(cfg, session, ambient, True)
        except Exception as exc:
            log.warning("ambient AI config resolution failed for user %s: %s", ambient, exc)
            session.close()
    return _ResolvedAI(resolve_config(workflow), None, None, False)


def is_configured(workflow: Optional[str] = None, db=None, user_id: Optional[int] = None) -> bool:
    if db is not None and user_id is not None:
        try:
            return bool(resolve_config_for_user(db, user_id, workflow)["api_key"])
        except Exception:
            pass
    if _ambient_user_id() is not None:
        resolved = _resolve_ai_config(workflow, None, None)
        try:
            return bool(resolved.cfg["api_key"])
        finally:
            if resolved.owned_session:
                resolved.db.close()
    return bool(resolve_config(workflow)["api_key"])


def usage_snapshot() -> Dict[str, Dict[str, int]]:
    return {k: dict(v) for k, v in _usage.items()}


def breaker_snapshot() -> Dict[str, Dict[str, Any]]:
    return {k: v.state() for k, v in _breakers.items()}


# --------------------------------------------------------------------------- #
# Per-user AI credit tracking (SaaS)
# --------------------------------------------------------------------------- #
MODEL_COSTS = {
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-4o": {"input": 0.005, "output": 0.015},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    "claude-3-opus": {"input": 0.015, "output": 0.075},
    "claude-3-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
    # free/open models via aggregators (OpenRouter, z-ai, etc.)
    "glm": {"input": 0.0001, "output": 0.0003},
    "z-ai": {"input": 0.0001, "output": 0.0003},
    "default": {"input": 0.001, "output": 0.002},
}

def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    m = (model or "").lower()
    cost_entry = MODEL_COSTS["default"]
    for key, val in MODEL_COSTS.items():
        if key in m and key != "default":
            cost_entry = val
            break
    return (prompt_tokens / 1000.0) * cost_entry["input"] + (completion_tokens / 1000.0) * cost_entry["output"]


def _estimate_tokens_from_text(prompt: str, content: str) -> int:
    """Very rough token estimate when provider omits usage — ~4 chars per token."""
    try:
        return max(1, (len(prompt or "") + len(content or "")) // 4)
    except Exception:
        return 0


def _extract_json_lenient(text: str):
    """Best-effort JSON extraction for providers that wrap JSON in markdown fences or prose."""
    if not isinstance(text, str):
        return None
    import re as _re
    candidate = text.strip()
    # fenced block ```json { ... } ``` or ``` { ... } ```
    m = _re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", candidate, _re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    try:
        return json.loads(candidate)
    except Exception:
        pass
    start_candidates = [i for i in (candidate.find("{"), candidate.find("[")) if i >= 0]
    if not start_candidates:
        return None
    start = min(start_candidates)
    end = max(candidate.rfind("}"), candidate.rfind("]"))
    if end > start:
        try:
            return json.loads(candidate[start:end+1])
        except Exception:
            return None
    return None

def track_ai_usage(
    db=None,
    user_id: Optional[int] = None,
    workflow: str = "",
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    success: bool = True,
    latency_ms: int = 0,
    error: str = "",
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    if total_tokens == 0:
        total_tokens = prompt_tokens + completion_tokens
    cost = estimate_cost(model or "default", prompt_tokens, completion_tokens)
    _spend_tokens(total_tokens)

    if db is None or user_id is None:
        return
    try:
        from app.core.entitlements import increment_usage
        from app.models.models import AICreditLedger, utcnow

        ledger = AICreditLedger(
            user_id=user_id,
            workflow=workflow,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=cost,
            success=success,
            latency_ms=latency_ms,
            error=(error or "")[:2000],
            meta=meta or {},
            created_at=utcnow(),
        )
        db.add(ledger)
        db.commit()
        try:
            increment_usage(db, user_id, "ai_operations_per_month", 1)
            increment_usage(db, user_id, "ai_credits_per_month", total_tokens)
            if workflow == "scoring":
                increment_usage(db, user_id, "job_analysis_per_month", 1)
            elif workflow == "resume_gen":
                increment_usage(db, user_id, "tailored_resumes_per_month", 1)
            elif workflow == "parse":
                increment_usage(db, user_id, "resume_parses_per_month", 1)
            elif workflow == "email_gen":
                increment_usage(db, user_id, "outreach_per_month", 1)
        except Exception:
            pass
    except Exception as exc:
        log.warning("failed to track AI usage: %s", exc)


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #
async def chat_completion(
    workflow: str,
    prompt: str,
    *,
    temperature: float = 0.2,
    timeout: Optional[int] = None,
    json_mode: bool = True,
    max_tokens: Optional[int] = None,
    ai_config: Optional[Dict[str, str]] = None,
    system: Optional[str] = None,
    db=None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Send a chat completion. Raises ``AIClientError`` on failure."""
    # Resolution order: explicit ai_config > per-user DB config (explicit or
    # ambient request/worker user, with owner fallback) > env defaults.
    resolved = _resolve_ai_config(workflow, db, user_id)
    cfg = resolved.cfg
    db, user_id = resolved.db, resolved.user_id
    try:
        if ai_config:
            cfg = {
                "base_url": (ai_config.get("base_url") or cfg["base_url"]).rstrip("/"),
                "api_key": (ai_config.get("api_key") or cfg["api_key"]).strip(),
                "model": (ai_config.get("model") or cfg["model"]).strip(),
                "key_source": "explicit" if ai_config.get("api_key") else cfg.get("key_source"),
                "key_error": cfg.get("key_error"),
            }
        if not cfg["api_key"]:
            if cfg.get("key_error"):
                raise AIClientError(
                    "stored_key_unreadable: the saved AI API key could not be decrypted on this server "
                    "(its ENCRYPTION_KEY changed since the key was saved) — re-enter it in Settings → AI API",
                    reason=REASON_STORED_KEY_UNREADABLE,
                )
            raise AIClientError(
                "no_api_key: AI is not configured (set AI_API_KEY in Settings or per-workflow override — OpenAI compatible format: base_url like https://api.openai.com/v1, model like gpt-4o-mini)",
                reason=REASON_NO_API_KEY,
            )

        # Per-user entitlement pre-check if db/user_id provided
        if db is not None and user_id is not None:
            try:
                from app.core.entitlements import enforce
                enforce(db, user_id, "ai_operations_per_month")
                enforce(db, user_id, "ai_credits_per_month")
            except Exception as e:
                # If enforcement fails (limit exceeded), raise as client error
                # to be caught and fallback to heuristic
                raise AIClientError(f"limit_exceeded: {e}", retryable=False) from e

        if budget_exhausted():
            inc("jobhunter_ai_requests_total", workflow=workflow, status="budget_exhausted")
            raise AIClientError(
                f"budget_exhausted: daily AI token budget of {settings.ai_daily_token_budget} reached",
                retryable=True,
            )

        breaker = _breaker(workflow)
        if breaker.is_open():
            inc("jobhunter_ai_requests_total", workflow=workflow, status="breaker_open")
            raise AIClientError(f"circuit_open: {breaker.last_error}", retryable=True)

        messages: List[Dict[str, str]] = []
        if system:
            # Sanitize system prompt — treat external data as untrusted
            safe_system = system.replace("```", "")[:2000]
            messages.append({"role": "system", "content": safe_system})
        # Sanitize user prompt: remove potential injection
        safe_prompt = prompt.replace("SYSTEM:", "").replace("Ignore previous", "")[:12000]
        messages.append({"role": "user", "content": safe_prompt})

        payload: Dict[str, Any] = {"model": cfg["model"], "messages": messages, "temperature": temperature}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        payload["max_tokens"] = max_tokens or settings.ai_max_output_tokens

        headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
        url = f"{cfg['base_url']}/chat/completions"
        attempts = max(1, settings.ai_max_retries)
        last_error: Optional[str] = None
        json_mode_active = json_mode

        for attempt in range(1, attempts + 1):
            await rate_limiter.wait_and_acquire(1)
            started = time.perf_counter()
            try:
                async with _sem():
                    async with httpx.AsyncClient(timeout=timeout or settings.ai_timeout) as client:
                        response = await client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                last_error = f"http_error: {exc}"
                inc("jobhunter_ai_requests_total", workflow=workflow, status="network_error")
                if attempt < attempts:
                    await _sleep_backoff(attempt)
                    continue
                breaker.record_failure(last_error)
                if db and user_id:
                    track_ai_usage(db=db, user_id=user_id, workflow=workflow, model=cfg["model"], success=False, error=last_error)
                raise AIClientError(last_error, retryable=True) from exc
            finally:
                observe("jobhunter_ai_latency_seconds", time.perf_counter() - started, workflow=workflow)

            if response.status_code == 200:
                breaker.record_success()
                break

            body = response.text[:400]
            last_error = f"status {response.status_code}: {body}"
            # Some providers (e.g. z-ai via aggregators) don't support json_object response_format
            # and return 400 mentioning response_format. Retry once without it.
            if response.status_code == 400 and json_mode_active and "response_format" in body.lower():
                log.warning("AI %s: provider rejected json_object response_format, retrying without it", workflow)
                payload.pop("response_format", None)
                json_mode_active = False
                if attempt < attempts:
                    continue
                # fall through to retry logic below

            retryable = response.status_code == 429 or 500 <= response.status_code < 600
            inc("jobhunter_ai_requests_total", workflow=workflow, status=str(response.status_code))
            if retryable and attempt < attempts:
                delay = _retry_delay(response, attempt)
                log.warning("AI %s -> %s, retrying in %.1fs", workflow, response.status_code, delay)
                await asyncio.sleep(delay)
                continue
            if not retryable:
                breaker.record_failure(last_error) if response.status_code in (401, 403) else None
                if db and user_id:
                    track_ai_usage(db=db, user_id=user_id, workflow=workflow, model=cfg["model"], success=False, error=last_error, latency_ms=int((time.perf_counter() - started)*1000))
                raise AIClientError(last_error, status=response.status_code, retryable=False)

            breaker.record_failure(last_error)
            if db and user_id:
                track_ai_usage(db=db, user_id=user_id, workflow=workflow, model=cfg["model"], success=False, error=last_error)
            raise AIClientError(last_error, status=response.status_code, retryable=True)
        else:  # pragma: no cover - loop always breaks or raises
            raise AIClientError(last_error or "unknown error", retryable=True)

        try:
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            if content is None:
                content = ""
            elif not isinstance(content, str):
                # Some providers return structured content
                content = json.dumps(content)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            breaker.record_failure(f"malformed_response: {exc}")
            if db and user_id:
                track_ai_usage(db=db, user_id=user_id, workflow=workflow, model=cfg["model"], success=False, error=str(exc))
            raise AIClientError(f"malformed_response: {exc}") from exc

        # Extract usage — handle many provider variants (prompt_tokens / input_tokens / etc.)
        raw_usage = data.get("usage") or data.get("usage_metadata") or {}
        def _int_field(*keys: str) -> int:
            for k in keys:
                v = raw_usage.get(k)
                if isinstance(v, int) and v > 0:
                    return v
                if isinstance(v, str) and v.isdigit():
                    return int(v)
            return 0
        prompt_tokens = _int_field("prompt_tokens", "input_tokens", "promptTokens", "inputTokens", "prompt_tokens_count")
        completion_tokens = _int_field("completion_tokens", "output_tokens", "completionTokens", "outputTokens", "candidatesTokenCount")
        total_tokens = _int_field("total_tokens", "totalTokens")
        if total_tokens == 0:
            total_tokens = (prompt_tokens + completion_tokens) if (prompt_tokens or completion_tokens) else 0
        # Fallback estimate when provider omits usage entirely (common on free tiers)
        if total_tokens == 0 and content:
            estimated = _estimate_tokens_from_text(prompt, str(content))
            # don't pretend it's precise — mark as estimate in meta but still bill something
            if estimated > 10:
                total_tokens = estimated
                if prompt_tokens == 0:
                    prompt_tokens = len(prompt) // 4
                if completion_tokens == 0:
                    completion_tokens = max(1, total_tokens - prompt_tokens)
        # Some providers nest usage differently
        if total_tokens == 0 and isinstance(raw_usage, dict) and raw_usage:
            # try any numeric field
            for v in raw_usage.values():
                if isinstance(v, int) and v > 5:
                    total_tokens = v
                    break

        latency_ms = int((time.perf_counter() - started) * 1000) if 'started' in locals() else 0

        _spend_tokens(total_tokens)
        bucket = _usage.setdefault(workflow, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0})
        bucket["calls"] += 1
        bucket["prompt_tokens"] += prompt_tokens
        bucket["completion_tokens"] += completion_tokens
        bucket["total_tokens"] += total_tokens
        set_gauge("jobhunter_ai_tokens_total", bucket["total_tokens"], workflow=workflow)
        inc("jobhunter_ai_requests_total", workflow=workflow, status="200")

        # Track per-user — success path
        if db and user_id:
            track_ai_usage(
                db=db,
                user_id=user_id,
                workflow=workflow,
                model=cfg["model"],
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                success=True,
                latency_ms=latency_ms,
            )

        if not json_mode:
            return {"content": content, "raw": data, "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": total_tokens}}

        # Lenient JSON parsing — many models wrap JSON in markdown fences or add preamble
        text_content = str(content or "")
        parsed = None
        try:
            parsed = json.loads(text_content)
        except (json.JSONDecodeError, TypeError):
            parsed = _extract_json_lenient(text_content)

        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list) and parsed:
            # Some models return a list with one object
            if isinstance(parsed[0], dict):
                return parsed[0]

        # Still not JSON — record ledger with real token counts before failing
        if db and user_id:
            track_ai_usage(db=db, user_id=user_id, workflow=workflow, model=cfg["model"],
                           prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                           total_tokens=total_tokens, success=False,
                           error=f"invalid_json: {str(text_content)[:200]}", latency_ms=latency_ms)
        raise AIClientError(f"invalid_json: model did not return valid JSON (preview: {text_content[:300]!r})")
    finally:
        if resolved.owned_session and resolved.db is not None:
            try:
                resolved.db.close()
            except Exception:
                pass


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return min(30.0, float(retry_after))
        except ValueError:
            pass
    return min(20.0, settings.ai_backoff_base ** attempt) * (0.7 + random.random() * 0.6)


async def _sleep_backoff(attempt: int) -> None:
    await asyncio.sleep(min(20.0, settings.ai_backoff_base ** attempt) * (0.7 + random.random() * 0.6))


async def chat_text(workflow: str, prompt: str, **kwargs) -> str:
    result = await chat_completion(workflow, prompt, json_mode=False, **kwargs)
    return str(result.get("content", ""))


def _key_preview(key: str) -> str:
    """Safe display form of a key (never the full secret)."""
    if not key:
        return ""
    if len(key) <= 12:
        return f"{key[:3]}***"
    return f"{key[:6]}…{key[-4:]}"


def _provider_error(response: Optional[httpx.Response]) -> str:
    """Best-effort human-readable error from a provider response."""
    if response is None:
        return ""
    try:
        data = response.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or "")[:200]
        if err:
            return str(err)[:200]
    except Exception:
        pass
    return (response.text or "")[:200]


# The status probe is polled every ~15s by the SPA; cache results so a
# provider without a /models endpoint doesn't trigger a billable chat probe
# on every poll. Re-saving a (different) key/base_url/model changes the cache
# key, so fixing config still reflects immediately.
_PING_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_PING_TTL_SECONDS = 60.0


async def ping(workflow: Optional[str] = None, timeout: int = 5, db=None, user_id: Optional[int] = None) -> Dict[str, Any]:
    """Health probe for the green/red status dot. Never raises.

    Two-stage probe: ``GET /models`` is cheap and works on most providers, but
    a 401/403/404 there does NOT prove the key is invalid — some providers
    restrict or don't implement the models listing while chat completions work
    fine. Only a rejected *chat completion* is reported as ``invalid_api_key``.
    """
    resolved = _resolve_ai_config(workflow, db, user_id)
    try:
        return await _ping_with_config(resolved.cfg, timeout)
    finally:
        if resolved.owned_session and resolved.db is not None:
            try:
                resolved.db.close()
            except Exception:
                pass


async def _ping_with_config(cfg: Dict[str, str], timeout: int) -> Dict[str, Any]:
    key = (cfg.get("api_key") or "").strip()
    base = (cfg.get("base_url") or "").strip().rstrip("/")
    model = (cfg.get("model") or "").strip()
    meta = {
        "base_url": base,
        "model": model,
        "key_source": cfg.get("key_source"),
        "key_preview": _key_preview(key),
    }

    if cfg.get("key_error"):
        return {"online": False, "reason": "stored_key_unreadable", "latency_ms": None,
                "hint": "The saved API key could not be decrypted on this server (its ENCRYPTION_KEY changed since the key was saved). Re-enter the key in Settings → AI API to fix it.",
                **meta}
    if not key:
        return {"online": False, "reason": "no_api_key", "latency_ms": None,
                "hint": "Set API key in Settings → AI API (OpenAI compatible: base_url like https://api.openai.com/v1)",
                **meta}

    cache_key = f"{base}|{model}|{hashlib.sha256(key.encode()).hexdigest()[:16]}"
    now = time.monotonic()
    cached = _PING_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    started = time.perf_counter()

    # Stage 1 — cheap models listing (sufficient on most providers).
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{base}/models", headers=headers)
    except httpx.HTTPError as exc:
        # Transient network errors are NOT cached — connectivity can recover
        # at any moment, unlike a definitive auth rejection.
        return {"online": False, "reason": "unreachable", "error": str(exc), "latency_ms": None,
                "hint": f"Could not reach {base} — check the base URL and network connectivity.", **meta}

    if response.status_code == 200:
        result = {"online": True, "latency_ms": int((time.perf_counter() - started) * 1000),
                  "status": 200, "probe": "models", **meta}
        _PING_CACHE[cache_key] = (now + _PING_TTL_SECONDS, result)
        return result

    # Stage 2 — /models did not answer 200. The key may still be perfectly
    # valid for chat completions (restricted/unimplemented models endpoint),
    # so verify with the endpoint the product actually uses. A minimal
    # request keeps the cost at ~1 token.
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            probe = await client.post(
                f"{base}/chat/completions",
                headers=headers,
                json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
            )
    except httpx.HTTPError:
        # Chat endpoint unreachable: report what /models told us.
        result = {"online": False, "latency_ms": int((time.perf_counter() - started) * 1000),
                  "status": response.status_code, "reason": f"status_{response.status_code}",
                  "detail": _provider_error(response), "probe": "models", **meta}
        _PING_CACHE[cache_key] = (now + _PING_TTL_SECONDS, result)
        return result

    latency_ms = int((time.perf_counter() - started) * 1000)
    if probe.status_code == 200:
        result = {"online": True, "latency_ms": latency_ms, "status": 200, "probe": "chat_completions",
                  "note": "models endpoint unavailable/restricted — chat completions verified working",
                  **meta}
    elif probe.status_code in (401, 403):
        result = {"online": False, "reason": "invalid_api_key", "status": probe.status_code,
                  "latency_ms": latency_ms, "probe": "chat_completions",
                  "detail": _provider_error(probe) or _provider_error(response),
                  "hint": f"The provider rejected this key for {base}. Double-check the key, and that base_url + model belong to the same provider.",
                  **meta}
    else:
        result = {"online": False, "reason": f"status_{probe.status_code}", "status": probe.status_code,
                  "latency_ms": latency_ms, "probe": "chat_completions",
                  "detail": _provider_error(probe), **meta}
    _PING_CACHE[cache_key] = (now + _PING_TTL_SECONDS, result)
    return result
