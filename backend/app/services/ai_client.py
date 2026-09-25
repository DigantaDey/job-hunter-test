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
   cooldown window instead of queueing doomed requests. Callers never substitute
   a guessed value: a transient failure surfaces as a *pausable* outcome (the
   product pauses and re-queues the work), a blocked failure (bad/missing key,
   quota) surfaces as an explicit needs-action outcome.
4. **Observability.** Latency, status, token usage and breaker state are exported
   as metrics and surfaced through ``/api/settings/ai/status`` — including the
   central three-state availability signal (:func:`ai_availability`):
   ``online``, ``transient_outage`` (safe to retry later), ``blocked_needs_action``
   (retrying is pointless until the user acts).
5. **Per-workflow config.** Different base_url/model/key per workflow, resolved
   from the DB overrides with env defaults.
6. **Per-user credit tracking.** Every successful call can be attributed to a
   user for billing, limits and cost control.
7. **User-controlled token budgets.** The per-user ``ai.max_input_tokens`` /
   ``ai.max_output_tokens`` settings (owner + paid tiers; fixed safe defaults on
   free) are the single source of truth for prompt truncation and the output
   ceiling. **``0`` means unlimited** (the shipped default): the gateway sends
   the task's own starting budget, clamps nothing, truncates no prompt part, and
   lets the escalation on truncation grow to the provider's own maximum — which
   is what keeps a long job description on a reasoning model from coming back
   ``truncated_response``. A positive ceiling still clamps every outgoing
   ``max_tokens`` *and* the escalation to it.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.config import ai_token_budget, settings
from app.core.logging import get_logger, user_id_var
from app.core.metrics import inc, observe, set_gauge
from app.core.rate_limiter import rate_limiter
from app.core.redaction import SECRET_PATTERNS
from app.services.http import get_client as get_http_client

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
REASON_EMPTY_RESPONSE = "empty_response"
REASON_TRUNCATED = "truncated_response"
REASON_CONTENT_FILTER = "content_filter"
REASON_MODEL_UNAVAILABLE = "model_unavailable"
REASON_UNKNOWN = "unknown"

_REASON_PREFIXES = {
    "no_api_key": REASON_NO_API_KEY,
    "circuit_open": REASON_CIRCUIT_OPEN,
    "budget_exhausted": REASON_BUDGET,
    "limit_exceeded": REASON_ENTITLEMENT,
    "invalid_json": REASON_INVALID_JSON,
    "empty_response": REASON_EMPTY_RESPONSE,
    "truncated_response": REASON_TRUNCATED,
    "content_filter": REASON_CONTENT_FILTER,
    "malformed_response": REASON_MALFORMED,
    "http_error": REASON_UNREACHABLE,
    "timeout": REASON_TIMEOUT,
    "stored_key_unreadable": REASON_STORED_KEY_UNREADABLE,
}


def reason_from_message(message: str, status: Optional[int] = None) -> str:
    """Best-effort stable reason code for an error string.

    A wait problem must never be reported as a connectivity failure: timeout
    keywords win over the ``http_error`` prefix (a read timeout means the
    endpoint *was* reached and is still generating). Genuine connect-phase
    failures keep the ``unreachable`` reason.
    """
    text = (message or "").strip()
    lowered = text.lower()
    # Timeout first — before the prefix map. "http_error: timed out" is a wait
    # problem, not a DNS/TLS/connection failure. A connect-phase timeout
    # ("connection timed out", "connect timeout") means the endpoint was never
    # reached, so it stays unreachable.
    if "timed out" in lowered or "timeout" in lowered or "readtimeout" in lowered:
        if "connect" not in lowered:
            return REASON_TIMEOUT
    head = text.split(":", 1)[0].strip().lower()
    if head in _REASON_PREFIXES:
        return _REASON_PREFIXES[head]
    if status in (401, 403) or "incorrect api key" in lowered or "invalid api key" in lowered or "api key not valid" in lowered or "api_key_invalid" in lowered:
        return REASON_INVALID_API_KEY
    if status == 429:
        return REASON_RATE_LIMITED
    if status in (402, 429) or "quota" in lowered or "insufficient_quota" in lowered or "billing" in lowered or "resource_exhausted" in lowered:
        return REASON_QUOTA
    if status == 404 or "model_not_found" in lowered or "does not exist" in lowered or "model not found" in lowered or "not found" in lowered and "model" in lowered:
        return REASON_MODEL_UNAVAILABLE
    if status and status >= 500:
        return REASON_PROVIDER_STATUS
    if "connect" in lowered or "name or service not known" in lowered or "ssl" in lowered:
        return REASON_UNREACHABLE
    return REASON_UNKNOWN


# --------------------------------------------------------------------------- #
# Central AI-availability signal — three states, one classifier
# --------------------------------------------------------------------------- #
#: The product pauses (and re-queues) work on ``transient_outage`` and demands
#: user action on ``blocked_needs_action``. Every stable reason maps to exactly
#: one state; the mapping is the single source of truth for the UI banner, the
#: queue pause/resume decision and the watchdog's drain condition.
STATE_ONLINE = "online"
STATE_TRANSIENT = "transient_outage"
STATE_BLOCKED = "blocked_needs_action"

#: Safe to retry later — nothing the user can fix in this moment; waiting works.
#: * timeout / unreachable / 5xx: provider-side, recovers on its own;
#: * rate_limited (429): a rate *window* that resets automatically;
#: * circuit_open: opened BY transient failures — it resets after the cooldown;
#: * budget_exhausted: the daily token budget resets at the next UTC day.
TRANSIENT_REASONS = frozenset({
    REASON_TIMEOUT, REASON_UNREACHABLE, REASON_PROVIDER_STATUS, REASON_RATE_LIMITED,
    REASON_CIRCUIT_OPEN, REASON_BUDGET,
})

#: Retrying is pointless until the user acts (fix the key, add credit, upgrade).
#:
#: **Where ``quota_exceeded`` belongs — and why:** it is BLOCKED, not transient.
#: A 429 rate limit is a *time window*: the provider resets it on its own, so
#: retrying later is meaningful. A quota/billing rejection (HTTP 402,
#: ``insufficient_quota``) is *account-level*: the provider account has no
#: credits left, and no amount of waiting changes that — only topping up the
#: account does. Retrying a hard-quota account burns nothing but retries, so
#: the product treats it as "needs action" and points the user at their
#: provider billing instead of pausing work forever.
BLOCKED_REASONS = frozenset({
    REASON_NO_API_KEY, REASON_INVALID_API_KEY, REASON_STORED_KEY_UNREADABLE,
    REASON_QUOTA, REASON_MODEL_UNAVAILABLE, REASON_ENTITLEMENT,
})


def normalise_ping_reason(reason: Optional[str]) -> str:
    """Map the probe's generic ``status_NNN`` verdicts to the stable reasons.

    ``ping`` reports non-auth probe verdicts as ``status_NNN`` (the probe does
    not know the product's vocabulary) — the availability signal, the UI and
    the watchdog must all speak the stable reason language instead.
    """
    if not reason or not str(reason).startswith("status_"):
        return reason or REASON_UNKNOWN
    try:
        code = int(str(reason).split("_", 1)[1])
    except (ValueError, IndexError):
        return str(reason)
    if code == 429:
        return REASON_RATE_LIMITED
    if code in (401, 403):
        return REASON_INVALID_API_KEY
    if code == 404:
        return REASON_MODEL_UNAVAILABLE
    return REASON_PROVIDER_STATUS  # 5xx and other 4xx probe verdicts


def classify_reason(reason: Optional[str]) -> str:
    """Map a stable reason to the three-state availability signal.

    Per-call answer-quality problems (``invalid_json``, ``empty_response``,
    ``truncated_response``, ``malformed_response``, ``content_filter``,
    ``unknown``) are classified ``transient_outage``: they are retryable later
    (which is exactly what the queue/worker does) and none of them indicates
    that the *provider connection or account* needs user action — so they must
    never flip the global signal to ``blocked_needs_action``.
    """
    if reason in TRANSIENT_REASONS:
        return STATE_TRANSIENT
    if reason in BLOCKED_REASONS:
        return STATE_BLOCKED
    return STATE_TRANSIENT


def retry_hint_for_reason(reason: Optional[str], *, workflow: str = "") -> Optional[float]:
    """Best-effort ``retry_after_hint`` (seconds) for a failure reason.

    ``None`` means "do not retry on a schedule" (blocked states). Values are
    advisory: the watchdog re-probes on its own interval regardless.
    """
    if reason in BLOCKED_REASONS:
        return None
    if reason == REASON_CIRCUIT_OPEN:
        breaker = _breaker(workflow) if workflow else None
        remaining = breaker.state().get("cooldown_remaining") if breaker else None
        return float(remaining) if remaining else float(settings.ai_breaker_cooldown_seconds)
    if reason == REASON_BUDGET:
        # Daily budget resets at the next UTC midnight.
        import datetime as _dt

        now = _dt.datetime.utcnow()
        tomorrow = (now + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return float((tomorrow - now).total_seconds())
    if reason == REASON_RATE_LIMITED:
        return 30.0  # a typical rate window; Retry-After is not always present
    return float(settings.ai_backoff_base) ** max(1, settings.ai_max_retries) * 5  # ~34s at defaults


class AIClientError(Exception):
    """Raised when the AI layer cannot produce a result (unconfigured, timeout, HTTP error…)."""

    def __init__(self, message: str, *, status: Optional[int] = None, retryable: bool = False,
                 reason: Optional[str] = None, meta: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.reason = reason or reason_from_message(message, status)
        #: v2.0.5 accounting carried through to the response/ledger: attempts
        #: spent, whether a billed-but-unseen generation may exist, …
        self.meta: Dict[str, Any] = dict(meta or {})

    def diagnostics(self) -> Dict[str, Any]:
        return {"reason": self.reason, "status": self.status, "retryable": self.retryable,
                "detail": str(self)[:500]}

    @property
    def state(self) -> str:
        """Three-state availability classification of this failure."""
        return classify_reason(self.reason)


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

#: Supported AI providers
AI_PROVIDER_OPENAI = "openai_compatible"
AI_PROVIDER_GOOGLE = "google"
AI_VALID_PROVIDERS = frozenset({AI_PROVIDER_OPENAI, AI_PROVIDER_GOOGLE, "google_ai_studio"})

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

def _is_google_provider(cfg: Dict[str, Any]) -> bool:
    prov = _normalize_provider(cfg.get("provider"))
    if prov == AI_PROVIDER_GOOGLE:
        return True
    # Auto-detect from base_url when provider not explicitly set
    if not cfg.get("provider") and cfg.get("base_url"):
        return _detect_provider_from_base_url(str(cfg["base_url"])) == AI_PROVIDER_GOOGLE
    return False

def _build_openai_url(cfg: Dict[str, Any]) -> str:
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    return f"{base}/chat/completions"

def _build_google_url(cfg: Dict[str, Any], stream: bool = False) -> str:
    base = str(cfg.get("base_url") or "https://generativelanguage.googleapis.com").strip().rstrip("/")
    # Normalise base that may already contain /v1beta or /v1
    # For tests, allow localhost/127.0.0.1 as fake Google endpoint — do not override to real host
    # nosec B104: this is URL host *matching* for the test-endpoint allowlist,
    # not a socket bind — nothing here listens on an interface.
    is_local = any(h in base for h in ("127.0.0.1", "localhost", "0.0.0.0"))  # nosec B104
    if "generativelanguage.googleapis.com" not in base and not is_local:
        # If user set provider=google but base_url is still openai style or empty, default to google host
        # Keep any explicit base (e.g. custom proxy) if it looks like a URL, otherwise default
        if not base or base.startswith("https://api.openai.com") or base.startswith("https://api.groq.com") or base == "" or "/chat/completions" in base:
            base = "https://generativelanguage.googleapis.com"
        # If base is still a non-google custom URL, keep it as-is and just ensure version
        elif "http" in base and "generativelanguage" not in base:
            # If it's a custom/test URL, keep it; don't force google host
            pass
        else:
            base = "https://generativelanguage.googleapis.com"
    # Ensure /v1beta
    if "/v1beta" not in base and "/v1" not in base:
        base = f"{base}/v1beta"
    elif base.endswith("/v1"):
        base = base[:-3] + "/v1beta"
    model = str(cfg.get("model") or "gemini-1.5-flash").strip()
    action = "streamGenerateContent" if stream else "generateContent"
    # Use :streamGenerateContent?alt=sse for streaming per spec, else :generateContent
    if stream:
        return f"{base}/models/{model}:{action}?alt=sse"
    return f"{base}/models/{model}:{action}"

def _translate_to_google_payload(messages, system: Optional[str], temperature: float, max_tokens: int, json_mode: bool) -> Dict[str, Any]:
    # Translate OpenAI messages -> Google contents
    contents = []
    system_instruction = None
    for msg in messages or []:
        role = msg.get("role")
        content = msg.get("content") or ""
        if role == "system":
            # Collect system as systemInstruction
            if content.strip():
                if system_instruction is None:
                    system_instruction = {"parts": [{"text": content}]}
                else:
                    # Append
                    system_instruction["parts"][0]["text"] += "\n\n" + content
            continue
        # user/assistant -> user/model
        g_role = "model" if role == "assistant" else "user"
        contents.append({"role": g_role, "parts": [{"text": str(content)}]})
    # Also add explicit system param if provided and not already in messages
    if system and not system_instruction:
        system_instruction = {"parts": [{"text": system.replace("```", "").strip()}]}
    payload: Dict[str, Any] = {"contents": contents}
    if system_instruction:
        payload["systemInstruction"] = system_instruction
    gen_config: Dict[str, Any] = {}
    if temperature is not None:
        gen_config["temperature"] = float(temperature)
    if max_tokens:
        gen_config["maxOutputTokens"] = int(max_tokens)
    if json_mode:
        gen_config["responseMimeType"] = "application/json"
    if gen_config:
        payload["generationConfig"] = gen_config
    return payload

def _translate_from_google_response(data: Dict[str, Any]) -> Dict[str, Any]:
    # Google -> OpenAI-like unified shape for _extract_content
    candidates = data.get("candidates") or []
    candidate = candidates[0] if candidates else {}
    content_obj = candidate.get("content") or {}
    parts = content_obj.get("parts") or []
    text = ""
    if parts and isinstance(parts, list):
        texts = [p.get("text") or "" for p in parts if isinstance(p, dict)]
        text = "\n".join(t for t in texts if t)
    finish_reason = candidate.get("finishReason")
    # Map finishReason to OpenAI style
    if finish_reason == "MAX_TOKENS":
        finish_reason = "length"
    elif finish_reason == "SAFETY":
        finish_reason = "content_filter"
    elif finish_reason == "STOP":
        finish_reason = "stop"
    # Build OpenAI-compatible wrapper
    return {
        "choices": [{"message": {"content": text}, "finish_reason": finish_reason}],
        "usage": data.get("usageMetadata") or data.get("usage") or {},
        "_google_raw": data,
    }

def _translate_from_google_stream_chunk(chunk: Dict[str, Any]) -> Dict[str, Any]:
    # Each SSE data is already a candidates chunk
    candidates = chunk.get("candidates") or []
    candidate = candidates[0] if candidates else {}
    content_obj = candidate.get("content") or {}
    parts = content_obj.get("parts") or []
    text = ""
    if parts and isinstance(parts, list):
        texts = [p.get("text") or "" for p in parts if isinstance(p, dict)]
        text = "".join(t for t in texts if t)
    finish_reason = candidate.get("finishReason")
    if finish_reason == "MAX_TOKENS":
        finish_reason = "length"
    elif finish_reason == "SAFETY":
        finish_reason = "content_filter"
    elif finish_reason == "STOP":
        finish_reason = "stop"
    usage = chunk.get("usageMetadata") or {}
    return {"content": text, "reasoning": "", "finish_reason": finish_reason, "usage": usage}

_workflow_overrides: Dict[Tuple[int, str], Dict[str, str]] = {}
_semaphore: Optional[asyncio.Semaphore] = None
_semaphore_loop: Optional[asyncio.AbstractEventLoop] = None


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


def slice_failure_baseline(workflow: str) -> int:
    """Consecutive failures on *workflow*'s breaker right now.

    Half of the bounded-parallel accounting pair: a caller that runs N verdicts
    in flight takes this before the slice and hands it to
    :func:`collapse_slice_failures` if the slice fails.
    """
    return int(_breaker(workflow).failures)


def collapse_slice_failures(workflow: str, baseline: int) -> None:
    """Count one failed parallel slice as **one** consecutive failure.

    A batch that issues N verdicts in flight records N failures when the
    provider is down — so the breaker opens after ``AI_BREAKER_FAILURES``/N
    failed batches instead of after N of them. That is not a theoretical
    difference: an open breaker makes :func:`ai_availability` report
    ``transient_outage`` regardless of a green probe, and the watchdog then
    refuses to drain *paused* work until the cooldown elapses — a fresh outage
    would delay every user's resumed run by up to ``AI_BREAKER_COOLDOWN_SECONDS``.

    The sequential slice this replaced recorded exactly one failure per batch
    (the first candidate's — the loop stopped there), so the parallel one keeps
    that accounting: the breaker is left as it was, plus one failure for the
    attempt that failed.
    """
    breaker = _breaker(workflow)
    if breaker.failures > baseline + 1:
        breaker.failures = baseline + 1


def _sem() -> asyncio.Semaphore:
    global _semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop or _semaphore_loop.is_closed():
        _semaphore = asyncio.Semaphore(max(1, settings.ai_max_concurrency))
        _semaphore_loop = loop
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
            cleaned = {k: (str(v).strip() if v else "") for k, v in cfg.items() if k in ("base_url", "api_key", "model", "provider")}
            if cleaned.get("provider"):
                cleaned["provider"] = _normalize_provider(cleaned["provider"])
            elif cleaned.get("base_url"):
                cleaned["provider"] = _detect_provider_from_base_url(cleaned["base_url"])
            if any(v for k,v in cleaned.items() if k != "provider"):  # provider alone does not create an override
                _workflow_overrides[(user_id, workflow)] = cleaned
            else:
                _workflow_overrides.pop((user_id, workflow), None)


def get_workflow_overrides(user_id: Optional[int] = None) -> Dict[str, Dict[str, str]]:
    if user_id is None:
        return {k[1]: dict(v) for k, v in _workflow_overrides.items()}
    return {k[1]: dict(v) for k, v in _workflow_overrides.items() if k[0] == user_id}


def resolve_config(workflow: Optional[str] = None) -> Dict[str, Any]:
    """Env-level config (no user context). Includes the legacy system bucket."""
    override = _workflow_overrides.get((0, workflow or ""), {}) if workflow else {}
    api_key = (override.get("api_key") or settings.ai_api_key or "").strip()
    base_url = (override.get("base_url") or settings.ai_base_url).strip().rstrip("/")
    provider_raw = (override.get("provider") or "").strip()
    provider = _normalize_provider(provider_raw) if provider_raw else _detect_provider_from_base_url(base_url)
    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": (override.get("model") or settings.ai_model).strip(),
        "provider": provider,
        "key_source": "workflow_override" if override.get("api_key") else ("env" if api_key else None),
        "timeout": settings.ai_timeout,
        "max_retries": settings.ai_max_retries,
        "max_input_tokens": settings.ai_max_input_tokens,
        "max_output_tokens": settings.ai_max_output_tokens,
    }


def resolve_config_for_user(db, user_id: int, workflow: Optional[str] = None) -> Dict[str, Any]:
    """
    Resolve AI config for a specific user, including per-user DB settings and owner fallback.
    Resolution order:
    1. Per-workflow override (from DB ai_workflows rows — tenant-scoped, read
       fresh so multi-worker deployments never serve stale keys)
    2. Per-user default AI settings (from SettingsModel category=ai)
    3. Owner's default AI settings as global fallback
    4. Env defaults
    All are OpenAI compatible: base_url, model, api_key. The wait budget
    (timeout) and retry count also resolve per user so a slow reasoning model
    can be given more time in Settings → AI API without touching env.
    """
    key_source: Optional[str] = None
    key_error: Optional[str] = None
    user_timeout: Any = settings.ai_timeout
    user_max_retries: Any = settings.ai_max_retries
    user_max_input_tokens: Any = settings.ai_max_input_tokens
    user_max_output_tokens: Any = settings.ai_max_output_tokens
    provider: str = AI_PROVIDER_OPENAI
    try:
        from app.services.user_settings import get_user_ai_config
        user_cfg = get_user_ai_config(db, user_id)
        base_url = user_cfg.get("base_url") or settings.ai_base_url
        model = user_cfg.get("model") or settings.ai_model
        api_key = user_cfg.get("api_key") or settings.ai_api_key
        key_source = user_cfg.get("key_source") or ("env" if api_key else None)
        key_error = user_cfg.get("api_key_error")
        provider = _normalize_provider(user_cfg.get("provider")) if user_cfg.get("provider") else _detect_provider_from_base_url(base_url)
        user_timeout = user_cfg.get("timeout", settings.ai_timeout)
        user_max_retries = user_cfg.get("max_retries", settings.ai_max_retries)
        user_max_input_tokens = user_cfg.get("max_input_tokens", settings.ai_max_input_tokens)
        user_max_output_tokens = user_cfg.get("max_output_tokens", settings.ai_max_output_tokens)
    except Exception as exc:
        log.warning("per-user AI config resolution failed for user %s, using env defaults: %s", user_id, exc)
        base_url = settings.ai_base_url
        model = settings.ai_model
        api_key = settings.ai_api_key
        provider = _detect_provider_from_base_url(base_url)
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
                if override.get("provider"):
                    provider = _normalize_provider(override["provider"])
                elif override.get("base_url"):
                    provider = _detect_provider_from_base_url(override["base_url"])
                if override.get("api_key"):
                    key_source = "workflow_override"
                    key_error = None
        except Exception as exc:
            log.warning("could not read workflow override for user %s/%s: %s", user_id, workflow, exc)

    # The plan's output ceiling is a tenant-level cap, applied last so it can
    # only ever tighten the resolved budget: Pro+ (0) is genuinely unlimited,
    # free/pro keep their cap whatever the operator's env says.
    plan_cap = plan_output_ceiling(db, user_id)
    resolved_output = _first_budget((user_max_output_tokens,), settings.ai_max_output_tokens)
    if plan_cap > 0:
        resolved_output = plan_cap if resolved_output <= 0 else min(resolved_output, plan_cap)

    # Final provider auto-detect if still empty or inconsistent with base_url
    if not provider or provider not in AI_VALID_PROVIDERS:
        provider = _detect_provider_from_base_url(base_url or "")
    provider = _normalize_provider(provider)
    return {
        "base_url": (base_url or "").strip().rstrip("/"),
        "api_key": (api_key or "").strip(),
        "model": (model or "").strip(),
        "provider": provider,
        "key_source": key_source,
        "key_error": key_error,
        "timeout": user_timeout,
        "max_retries": user_max_retries,
        "max_input_tokens": _first_budget((user_max_input_tokens,), settings.ai_max_input_tokens),
        "max_output_tokens": resolved_output,
        "plan_max_output_tokens": plan_cap,
    }


def plan_output_ceiling(db, user_id: int) -> int:
    """The plan's per-request output ceiling (``0`` = unlimited).

    The monetisation boundary for token budgets: Pro+ has no ceiling, free/pro
    keep one — which is why a free account still gets ``truncated_response`` on
    a long job description while Pro+ gets the real AI verdict.

    The operator's own account is exempt: their setting/env value *is* the
    platform default, and clamping it to the free plan would put the operator
    straight back on the truncated-response bug ``AI_MAX_OUTPUT_TOKENS=0``
    exists to fix. A failed plan lookup also fails open — an entitlement read
    must never be the reason AI is unavailable.
    """
    try:
        from app.core.entitlements import limit_for
        from app.models.models import User

        user = db.query(User).filter(User.id == user_id).first()
        if user is not None and user.role == "owner":
            return 0  # unlimited — the operator decides
        return limit_for(db, int(user_id), "ai_max_output_tokens")
    except Exception as exc:
        log.warning("plan output ceiling could not be resolved for user %s: %s", user_id, exc)
        return 0


def _ambient_user_id() -> Optional[int]:
    """User id bound to the current request / worker job by the auth layer.

    ``get_current_user`` sets it for every API request and the worker's
    ``LogContext`` sets it per queue item, so AI calls made deep inside
    pipelines (which historically had no user context) can still resolve the
    *user's* configured key instead of falling back to env-only config.

    The value is an ``int`` at every writer site and ``ContextFilter`` coerces
    it, so the ``int()`` below is belt-and-braces rather than a repair: it keeps
    a future caller that binds a numeric string from silently losing the user's
    key, and it still returns ``None`` for anything that is not a number.
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
    cfg: Dict[str, Any]
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
            if resolved.owned_session and resolved.db is not None:
                resolved.db.close()
    return bool(resolve_config(workflow)["api_key"])


def usage_snapshot() -> Dict[str, Dict[str, int]]:
    return {k: dict(v) for k, v in _usage.items()}


def breaker_snapshot() -> Dict[str, Dict[str, Any]]:
    return {k: v.state() for k, v in _breakers.items()}


def _first_budget(values: Tuple[Any, ...], fallback: Any) -> int:
    """First value that is actually set — **``0`` counts as set** (it means
    unlimited, so a ``value or default`` chain would silently re-introduce a
    cap the operator switched off). Unset/``None``/unparseable → ``fallback``.
    """
    for value in values:
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        return ai_token_budget(value)
    return ai_token_budget(fallback)


def resolve_output_ceiling(*values: Any) -> int:
    """The effective OUTPUT-token ceiling for a call. ``0`` = **unlimited**.

    Resolution is "first value that is set" (an explicit ``0`` wins over
    anything after it), so a user who chose *Unlimited* in Settings → AI API is
    never clamped by the env default, and vice versa.
    """
    return _first_budget(values, settings.ai_max_output_tokens)


def resolve_input_budget(*values: Any) -> int:
    """The effective INPUT-token budget for a call. ``0`` = **unlimited**."""
    return _first_budget(values, settings.ai_max_input_tokens)


def escalation_ceiling(output_ceiling: int) -> int:
    """Highest value the automatic ``max_tokens`` escalation may reach.

    A user ceiling is a hard stop (the escalation can never exceed it). With an
    unlimited ceiling the escalation is still bounded — by
    :data:`_MAX_OUTPUT_TOKENS_HARD_STOP`, past every shipping model's maximum
    completion — so "unlimited" can never turn a retry loop into an unbounded
    bill.
    """
    return output_ceiling if output_ceiling > 0 else _MAX_OUTPUT_TOKENS_HARD_STOP


def input_budget_chars(db=None, user_id: Optional[int] = None,
                       ai_config: Optional[Dict[str, Any]] = None) -> int:
    """Character budget for ONE dynamic prompt part, from the user's input budget.

    Services cap every document they splice into a prompt (JD, profile JSON,
    resume text…) with this instead of hardcoded slices; the gateway then caps
    the *total* prompt the same way, so the user's ``ai.max_input_tokens`` is
    the single source of truth end to end.

    ``0`` (unlimited — the shipped default) means **no client-side cap**: every
    caller passes the value straight to :func:`fit_prompt_part`, which treats
    ``0`` as "do not truncate", so a long job description reaches the model
    whole. It used to collapse to the 1000-char floor, which truncated every
    document on the unlimited path.
    """
    raw: Any = None
    if isinstance(ai_config, dict):
        raw = ai_config.get("max_input_tokens")
    if raw is None and db is not None and user_id is not None:
        try:
            raw = resolve_config_for_user(db, user_id).get("max_input_tokens")
        except Exception:
            raw = None
    if raw is None:
        raw = settings.ai_max_input_tokens
    budget = ai_token_budget(raw)
    if budget <= 0:
        return 0  # unlimited — fit_prompt_part truncates nothing
    return max(1000, budget * CHARS_PER_TOKEN)


def fit_prompt_part(text: str, budget_chars: int, *, label: str = "") -> Tuple[str, bool]:
    """Cap one dynamic prompt part at the input budget. Returns ``(text, truncated)``.

    Truncation is *clamping with a warning* — not rejection: a budget smaller
    than a document must still produce a (shorter) honest request, and the
    caller is told truncation happened (see ``input_truncated`` in the ledger
    meta). The provider-side ``max_tokens`` 400 fix-up remains the last line of
    defence for output budgets a specific model does not support.
    """
    text = (text or "").strip()
    if budget_chars <= 0 or len(text) <= budget_chars:
        return text, False
    if label:
        log.warning("AI prompt part '%s' truncated %d -> %d chars (user input budget)",
                    label, len(text), budget_chars)
    return text[:budget_chars], True


async def ai_availability(db=None, user_id: Optional[int] = None,
                          *, probe_timeout: int = 6) -> Dict[str, Any]:
    """The central three-state AI-availability signal.

    Combines the live probe (:func:`ping`, cached — polling is cheap) with the
    in-process breaker and daily budget, so the state reflects what the NEXT
    call would actually experience, not just whether the endpoint answers:

    * ``online``                — the probe succeeded AND nothing in-process
                                  blocks the next call;
    * ``transient_outage``      — timeout / unreachable / 429 / 5xx / breaker
                                  open / daily budget used up: safe to retry
                                  later, so paused work is re-queued (drained
                                  by the watchdog);
    * ``blocked_needs_action``  — no key / invalid key / undecryptable key /
                                  provider quota / unknown model / plan limit:
                                  retrying is pointless until the user acts.

    ``retry_after_hint`` (seconds, ``None`` when blocked) tells the UI and the
    watchdog when it is worth looking again.
    """
    try:
        health = await ping(db=db, user_id=user_id, timeout=probe_timeout)
    except Exception as exc:  # the signal itself must never raise
        health = {"online": False, "reason": "unreachable", "error": str(exc)}

    if health.get("online"):
        # Probe is green — but an open breaker or an exhausted daily budget
        # still blocks the next call in this process.
        any_breaker_open = any(b.is_open() for b in _breakers.values())
        if any_breaker_open:
            reason = REASON_CIRCUIT_OPEN
        elif budget_exhausted():
            reason = REASON_BUDGET
        else:
            return {"state": STATE_ONLINE, "online": True, "reason": None, "hint": None,
                    "detail": "", "retry_after_hint": None,
                    "latency_ms": health.get("latency_ms"), "base_url": health.get("base_url"),
                    "model": health.get("model"), "key_source": health.get("key_source")}
    else:
        reason = normalise_ping_reason(str(health.get("reason") or "unknown"))

    state = classify_reason(reason)
    try:
        from app.services.ai_guardrails import DIAGNOSIS  # local import: ai_guardrails imports this module
        _fix = DIAGNOSIS.get(reason, (None, None))[1]
    except Exception:
        _fix = ""
    return {
        "state": state,
        "online": False,
        "reason": reason,
        "hint": health.get("hint") or _fix,
        "detail": str(health.get("detail") or health.get("error") or "")[:500],
        "retry_after_hint": retry_hint_for_reason(reason) if state != STATE_ONLINE else None,
        "latency_ms": health.get("latency_ms"),
        "base_url": health.get("base_url"),
        "model": health.get("model"),
        "key_source": health.get("key_source"),
    }


def is_transient_ai_error(exc: BaseException) -> bool:
    """True when an AI-layer failure is a *transient outage* (pause + re-queue),
    as opposed to a needs-action failure (do not retry) or non-AI error."""
    from app.services.ai_guardrails import AIUnavailableError

    if isinstance(exc, AIUnavailableError):
        return getattr(exc, "state", None) == STATE_TRANSIENT
    if isinstance(exc, AIClientError):
        return exc.state == STATE_TRANSIENT
    return False


def is_ai_error(exc: BaseException) -> bool:
    from app.services.ai_guardrails import AIUnavailableError

    return isinstance(exc, (AIUnavailableError, AIClientError))


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
    "gemini-1.5-flash": {"input": 0.000075, "output": 0.0003},
    "gemini-1.5-pro": {"input": 0.00125, "output": 0.005},
    "gemini-2.0-flash": {"input": 0.0001, "output": 0.0004},
    "gemini": {"input": 0.0001, "output": 0.0004},
    "default": {"input": 0.001, "output": 0.002},
}

def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    m = (model or "").lower()
    cost_entry = MODEL_COSTS.get(m)
    if cost_entry is None:
        # Prefer the most specific family for dated/provider model variants.
        # Substring matching would price gpt-4o-mini as gpt-4.
        cost_entry = next(
            (MODEL_COSTS[key] for key in sorted(MODEL_COSTS, key=len, reverse=True)
             if key != "default" and m.startswith(key)),
            MODEL_COSTS["default"],
        )
    return (prompt_tokens / 1000.0) * cost_entry["input"] + (completion_tokens / 1000.0) * cost_entry["output"]


def _estimate_tokens_from_text(prompt: str, content: str) -> int:
    """Very rough token estimate when provider omits usage — ~4 chars per token."""
    try:
        return max(1, (len(prompt or "") + len(content or "")) // 4)
    except Exception:
        return 0


#: Invisible characters some providers wrap answers in (BOM, zero-width joiners).
#: They break ``json.loads`` while being undetectable in logs.
_INVISIBLES_TABLE = str.maketrans("", "", "\ufeff\u200b\u200c\u200d\u2060")
#: How many opening-bracket positions to probe when salvaging the model's last
#: JSON draft out of a reasoning trace.
_MAX_JSON_SALVAGE_TRIES = 32
#: Hard safety stop for the automatic ``max_tokens`` escalation when the
#: resolved output ceiling is UNLIMITED (``0`` = provider default).
#:
#: "Unlimited" means *we* do not clamp — it does not mean the retry loop may
#: bill an unbounded budget: the escalation still stops here, which is past
#: every shipping model's maximum completion (8k–100k), so a runaway retry can
#: never ask a provider for millions of output tokens. A user who sets an
#: explicit ceiling gets that ceiling instead (and the escalation can never
#: exceed it).
_MAX_OUTPUT_TOKENS_HARD_STOP = 128000
#: Starting output budget when a task does not state its own (per-call
#: ``max_tokens`` argument is the task's own starting budget; this is the
#: fallback — matching the legacy AI_MAX_OUTPUT_TOKENS default so out-of-the-box
#: behaviour is unchanged).
_DEFAULT_OUTPUT_START = 4000
#: Rough chars-per-token factor used to turn the user's input-token budget
#: into a character budget for prompt parts (4 chars/token is the industry
#: planning figure for English prose/JSON mixes).
CHARS_PER_TOKEN = 4
#: ```json { ... } ``` or ``` { ... } ``` blocks (findall → every block, so the
#: last one — the model's final answer — can be tried first).
_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


def _extract_json_lenient(text: str):
    """Best-effort JSON extraction for providers that wrap JSON in markdown
    fences, prose or reasoning traces.

    Tries, in order: the whole text → fenced blocks (last first — the final
    block is the model's answer) → the span from the first opening bracket to
    the last closing bracket → balanced objects scanned from the end (the last
    complete draft a reasoning model produced before its budget ran out).
    """
    if not isinstance(text, str):
        return None
    candidate = text.translate(_INVISIBLES_TABLE).strip()
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except (ValueError, TypeError):
        pass
    # fenced block ```json { ... } ``` or ``` { ... } ``` — several blocks may be
    # present (repair loops, drafts); the LAST one is the model's final answer.
    for block in reversed(_FENCED_JSON.findall(candidate)):
        try:
            return json.loads(block)
        except (ValueError, TypeError):
            continue
    # span from the first opening bracket to the last closing bracket
    starts = [i for i in (candidate.find("{"), candidate.find("[")) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    end = max(candidate.rfind("}"), candidate.rfind("]"))
    if end > start:
        try:
            return json.loads(candidate[start:end + 1])
        except (ValueError, TypeError):
            pass
    # Last resort: the model drafted the JSON inside its reasoning trace —
    # try every object that *ends* at the last closing brackets, opening
    # brackets from the end backwards. Only a complete, valid object parses.
    ends = sorted((i for i in (candidate.rfind("}"), candidate.rfind("]")) if i > 0), reverse=True)
    for end in ends:
        opens = [i for i, ch in enumerate(candidate[:end]) if ch in "{["]
        for start in reversed(opens[-_MAX_JSON_SALVAGE_TRIES:]):
            try:
                return json.loads(candidate[start:end + 1])
            except (ValueError, TypeError):
                continue
    return None


def _parse_model_json(content: str):
    """Strict parse first, lenient extraction (fences/prose) as a fallback."""
    if not isinstance(content, str) or not content.strip():
        return None
    try:
        return json.loads(content)
    except (ValueError, TypeError):
        return _extract_json_lenient(content)


def _extract_content(data: Any) -> Dict[str, Any]:
    """
    Pull the answer out of an OpenAI-compatible chat completion body without
    ever raising. Also handles Google generateContent shape (candidates[0].content.parts[0].text).

    The wild is messy: ``content`` arrives as ``None``, as a list of parts
    (``[{"type": "text", "text": ...}]``), or empty with the real answer in
    ``reasoning_content`` (reasoning models whose thinking phase consumed the
    whole ``max_tokens`` budget). Legacy completions proxies put the text in
    ``choices[0].text``. All of those are normalised here. Google returns
    ``candidates[0].content.parts[0].text`` with ``finishReason`` and
    ``usageMetadata``.
    """
    empty: Dict[str, Any] = {"content": "", "reasoning": "", "finish_reason": None,
                             "message": {}, "well_formed": False}
    if not isinstance(data, dict):
        return empty
    # Google shape: translate first
    if "candidates" in data and "choices" not in data:
        try:
            data = _translate_from_google_response(data)
        except Exception:
            pass
    choices = data.get("choices") if isinstance(data, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(choice, dict):
        return empty
    message = choice.get("message")
    if not isinstance(message, dict):
        message = {}
    finish_reason = choice.get("finish_reason")
    if not isinstance(finish_reason, str):
        finish_reason = None

    content = message.get("content")
    if isinstance(content, list):
        # Multimodal / parts-style content: [{"type": "text", "text": "..."}]
        parts = [p.get("text") if isinstance(p, dict) else str(p) for p in content]
        content = "\n".join(str(p) for p in parts if p)
    if content is None:
        content = ""
    elif not isinstance(content, str):
        content = json.dumps(content) if isinstance(content, (dict, list)) else str(content)

    reasoning = ""
    for alt_key in ("reasoning_content", "reasoning"):
        alt = message.get(alt_key)
        if isinstance(alt, str) and alt.strip():
            reasoning = alt
            break
    if not content.strip() and not reasoning:
        # Legacy completions-style proxy: the answer sits on the choice itself.
        legacy = choice.get("text")
        if isinstance(legacy, str) and legacy.strip():
            content = legacy
    return {"content": content, "reasoning": reasoning, "finish_reason": finish_reason,
            "message": message, "well_formed": True}


def _read_usage(data: Dict[str, Any], prompt: str, content: str) -> Tuple[int, int, int]:
    """Extract token usage, handling the many provider field variants (OpenAI + Google)."""
    raw_usage = data.get("usage") or data.get("usage_metadata") or data.get("usageMetadata") or {}
    # Google also nests usage at top level _google_raw
    if not raw_usage and isinstance(data.get("_google_raw"), dict):
        raw_usage = data["_google_raw"].get("usageMetadata") or {}
    if not isinstance(raw_usage, dict):
        raw_usage = {}

    def _int_field(*keys: str) -> int:
        for k in keys:
            v = raw_usage.get(k)
            if isinstance(v, int) and v > 0:
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        return 0

    prompt_tokens = _int_field("prompt_tokens", "input_tokens", "promptTokens", "promptTokenCount", "inputTokens", "prompt_tokens_count")
    completion_tokens = _int_field("completion_tokens", "output_tokens", "completionTokens", "candidatesTokenCount", "outputTokens", "candidatesTokenCount")
    total_tokens = _int_field("total_tokens", "totalTokens", "totalTokenCount")
    if total_tokens == 0:
        total_tokens = (prompt_tokens + completion_tokens) if (prompt_tokens or completion_tokens) else 0
    # Fallback estimate when the provider omits usage entirely (common on free tiers)
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
    if total_tokens == 0 and raw_usage:
        # try any numeric field
        for v in raw_usage.values():
            if isinstance(v, int) and v > 5:
                total_tokens = v
                break
    return prompt_tokens, completion_tokens, total_tokens


def _json_mode_rejected(body_lowered: str) -> bool:
    """True when a 400 names json/structured-output mode as unsupported.

    OpenAI-style errors mention ``response_format``; Google mentions
    ``responseMimeType``. Hugging Face's OpenAI-compatible router reports
    ``does not support feature: structured-outputs`` without naming the
    request field, so that phrase must also drop json mode.
    """
    return any(token in (body_lowered or "") for token in (
        "response_format",
        "responsemimetype",
        "response_mime_type",
        "structured-outputs",
        "structured_outputs",
        "structured outputs",
        "json_object",
        "json_schema",
    ))


def _json_retry_action(content: str, finish_reason: Optional[str], *, json_mode_active: bool,
                       plain_retries: int) -> Optional[str]:
    """Decide how to recover from an empty/invalid JSON answer.

    Returns one of ``raise_max_tokens_big`` (the thinking phase consumed the
    budget), ``raise_max_tokens`` (the JSON was cut mid-way),
    ``drop_response_format`` (provider accepted json_object but emitted
    nothing), ``plain_retry`` (transient flake) or ``None`` (do not retry).
    """
    if finish_reason == "content_filter":
        return None  # the same prompt will be blocked again
    empty = not (content or "").strip()
    if finish_reason == "length":
        return "raise_max_tokens_big" if empty else "raise_max_tokens"
    if empty:
        if json_mode_active:
            # The provider accepted response_format=json_object but emitted an
            # empty answer — retrying without it is the targeted fix.
            return "drop_response_format"
        return "plain_retry" if plain_retries < 1 else None
    # Non-empty text that is not JSON — one same-payload retry (a temperature
    # flake or a fenced answer the lenient parser still missed).
    return "plain_retry" if plain_retries < 1 else None


def _invalid_json_error(content: str, finish_reason: Optional[str], current_max_tokens: int) -> "AIClientError":
    """A precise, actionable error instead of a blanket "invalid_json"."""
    if finish_reason == "content_filter":
        return AIClientError(
            "content_filter: the provider blocked the answer (finish_reason=content_filter)",
            reason=REASON_CONTENT_FILTER, retryable=False)
    empty = not (content or "").strip()
    if empty:
        if finish_reason == "length":
            return AIClientError(
                f"truncated_response: the model spent all {current_max_tokens} output tokens before writing "
                "an answer (finish_reason=length — a reasoning model's thinking phase consumed the budget). "
                "Set Max output tokens to 0 (unlimited — the provider's own maximum) in Settings → AI API "
                "or AI_MAX_OUTPUT_TOKENS=0, or use a model that does not burn the budget on reasoning",
                reason=REASON_TRUNCATED, retryable=True)
        return AIClientError(
            f"empty_response: the model returned an empty answer (finish_reason={finish_reason or 'unknown'}) "
            "— free-tier and reasoning models do this intermittently; retry or switch model",
            reason=REASON_EMPTY_RESPONSE, retryable=True)
    if finish_reason == "length":
        return AIClientError(
            f"truncated_response: the JSON was cut off at max_tokens={current_max_tokens} "
            "(finish_reason=length) — raise the output ceiling (Settings → AI API → Max output tokens, "
            "or AI_MAX_OUTPUT_TOKENS; 0 = unlimited) or use a model with a larger output limit",
            reason=REASON_TRUNCATED, retryable=True)
    return AIClientError(
        f"invalid_json: model did not return valid JSON (preview: {content[:300]!r})",
        reason=REASON_INVALID_JSON, retryable=True)

#: How many attempts' request bodies the owner-only log keeps per call.
_MAX_LOGGED_ATTEMPTS = 3


def _request_envelope(url: str, sent: List[Dict[str, Any]], *, temperature: float,
                      json_mode: bool, stream: bool, max_tokens: int,
                      attempts: int) -> Dict[str, Any]:
    """The owner-facing shape of one call's outbound request(s)."""
    return {
        "url": str(url or ""),
        "params": {"temperature": temperature, "json_mode": bool(json_mode),
                   "stream": bool(stream), "max_tokens": int(max_tokens or 0),
                   "attempts": int(attempts or 1)},
        "attempts": list(sent),
    }


def _answer_excerpt(text: Optional[str], parsed: Optional[Dict[str, Any]]) -> str:
    """A bounded excerpt of the answer, whichever shape it arrived in."""
    if text:
        return text
    if parsed is not None:
        try:
            return json.dumps(parsed, default=str, ensure_ascii=False)
        except Exception:  # pragma: no cover
            return str(parsed)
    return ""


def _log_ai_call(db, *, user_id: Optional[int], workflow: str, model: str, provider: str,
                 base_url: str, success: bool, error: str, latency_ms: int,
                 prompt_tokens: int, completion_tokens: int, total_tokens: int,
                 estimated_cost_usd: float, meta: Optional[Dict[str, Any]],
                 request: Optional[Dict[str, Any]], response: str) -> None:
    """Hand one finished call to the owner-only log (never raises)."""
    try:
        from app.services import ai_log  # noqa: PLC0415 - keeps the import graph flat

        info: Dict[str, Any] = dict(meta or {})
        http_status = info.get("http_status")
        try:
            attempts = max(1, int(info["attempts"])) if info.get("attempts") is not None else 1
        except (TypeError, ValueError):
            attempts = 1
        ai_log.record_call(
            db,
            user_id=user_id,
            workflow=workflow,
            status=ai_log.STATUS_OK if success else ai_log.STATUS_ERROR,
            model=model,
            provider=provider,
            base_url=base_url,
            reason=str(info.get("reason") or ("ok" if success else "error")),
            http_status=int(http_status) if isinstance(http_status, int) else None,
            attempts=attempts,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost_usd,
            request=request,
            response=response,
            error=error,
        )
    except Exception as exc:  # pragma: no cover - observability must never fail a call
        log.debug("ai_client: call log write failed: %s", exc)


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
    request: Optional[Dict[str, Any]] = None,
    response: str = "",
    provider: str = "",
    base_url: str = "",
) -> None:
    """Record every outcome; only successful calls consume AI entitlements.

    Workflow quotas belong to completed business units, not provider calls (a
    business unit can involve retries or multiple guarded calls). Observed
    tokens, including failed attempts, spend the daily provider safety budget.

    ``request`` (the bodies actually sent, one per attempt) and ``response`` feed
    the owner-only call log (``app.services.ai_log``) — the ledger answers "how
    much", the log answers "what exactly". Both writes are best-effort and
    bounded: see that module's docstring.
    """
    if total_tokens == 0:
        total_tokens = prompt_tokens + completion_tokens
    cost = estimate_cost(model or "default", prompt_tokens, completion_tokens)
    _spend_tokens(total_tokens)

    if db is None or user_id is None:
        return
    _log_ai_call(
        db, user_id=user_id, workflow=workflow, model=model, provider=provider,
        base_url=base_url, success=success, error=error, latency_ms=latency_ms,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        total_tokens=total_tokens, estimated_cost_usd=cost, meta=meta,
        request=request, response=response,
    )
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
        if success:
            increment_usage(db, user_id, "ai_operations_per_month", 1)
            increment_usage(db, user_id, "ai_credits_per_month", total_tokens)
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
    timeout: Optional[float] = None,
    json_mode: bool = True,
    max_tokens: Optional[int] = None,
    ai_config: Optional[Dict[str, str]] = None,
    system: Optional[str] = None,
    db=None,
    user_id: Optional[int] = None,
    stream: bool = False,
) -> Dict[str, Any]:
    """Send a chat completion. Raises AIClientError on failure.

    Streaming: when stream=True, uses http_client.stream with Timeout(1800, connect 10, read 60, write 60, pool 60) + SSE parsing.
    """
    resolved = _resolve_ai_config(workflow, db, user_id)
    cfg = resolved.cfg
    db, user_id = resolved.db, resolved.user_id
    call_started = time.perf_counter()
    usage_totals: Dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    failure_meta: Dict[str, Any] = {}
    #: Diagnostics for the owner-only call log. Declared *before* the try so a
    #: preflight failure (no key, breaker open, budget exhausted) — which never
    #: reaches the payload build below — is still logged as a call that has an
    #: endpoint-less request body and no answer, rather than as an unbound name.
    url: str = ""
    sent_requests: List[Dict[str, Any]] = []
    last_content: str = ""
    attempt = 0
    current_max_tokens = 0
    json_mode_active = json_mode
    try:
        if ai_config:
            merged_provider = ai_config.get("provider") or cfg.get("provider")
            if not merged_provider and ai_config.get("base_url"):
                merged_provider = _detect_provider_from_base_url(ai_config["base_url"])
            cfg = {
                "base_url": (ai_config.get("base_url") or cfg["base_url"]).rstrip("/"),
                "api_key": (ai_config.get("api_key") or cfg["api_key"]).strip(),
                "model": (ai_config.get("model") or cfg["model"]).strip(),
                "provider": _normalize_provider(merged_provider) if merged_provider else cfg.get("provider"),
                "key_source": "explicit" if ai_config.get("api_key") else cfg.get("key_source"),
                "key_error": cfg.get("key_error"),
                "timeout": cfg.get("timeout"),
                "max_retries": cfg.get("max_retries"),
                "max_input_tokens": _first_budget(
                    (ai_config.get("max_input_tokens"), cfg.get("max_input_tokens")),
                    settings.ai_max_input_tokens),
                "max_output_tokens": _first_budget(
                    (ai_config.get("max_output_tokens"), cfg.get("max_output_tokens")),
                    settings.ai_max_output_tokens),
            }
        if not cfg.get("provider") or cfg.get("provider") not in AI_VALID_PROVIDERS:
            cfg["provider"] = _detect_provider_from_base_url(cfg.get("base_url") or "")
        cfg["provider"] = _normalize_provider(cfg.get("provider"))
        is_google = _is_google_provider(cfg)
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
        if db is not None and user_id is not None:
            try:
                from app.core.entitlements import enforce
                enforce(db, user_id, "ai_operations_per_month")
                enforce(db, user_id, "ai_credits_per_month")
            except Exception as e:
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
            raise AIClientError(f"circuit_open: {breaker.last_error}", retryable=True,
                                meta={"retry_after_hint": retry_hint_for_reason(REASON_CIRCUIT_OPEN, workflow=workflow)})
        output_ceiling = resolve_output_ceiling(cfg.get("max_output_tokens"))
        escalation_stop = escalation_ceiling(output_ceiling)
        resolved_input = resolve_input_budget(cfg.get("max_input_tokens"))
        input_budget_chars_val = 0 if resolved_input <= 0 else max(1000, resolved_input * CHARS_PER_TOKEN)
        input_truncated = False
        messages: List[Dict[str, str]] = []
        if system:
            safe_system, _system_truncated = fit_prompt_part(system.replace("```", ""), input_budget_chars_val,
                                                             label=f"{workflow}.system")
            input_truncated = input_truncated or _system_truncated
            messages.append({"role": "system", "content": safe_system})
        safe_prompt, _prompt_truncated = fit_prompt_part(
            _scrub_prompt_secrets(prompt.replace("SYSTEM:", "").replace("Ignore previous", "")),
            input_budget_chars_val,
            label=f"{workflow}.prompt")
        input_truncated = input_truncated or _prompt_truncated
        messages.append({"role": "user", "content": safe_prompt})
        json_mode_active = json_mode
        try:
            starting_budget = max(1, int(max_tokens or _DEFAULT_OUTPUT_START))
        except (TypeError, ValueError):
            starting_budget = _DEFAULT_OUTPUT_START
        if output_ceiling > 0 and starting_budget > output_ceiling:
            log.warning("AI %s: max_tokens=%d clamped to the user's output ceiling %d",
                        workflow, starting_budget, output_ceiling)
            starting_budget = output_ceiling
        current_max_tokens = starting_budget
        max_tokens_key = "max_tokens"
        payload: Dict[str, Any] = {}
        headers: Dict[str, str] = {}
        if is_google:
            payload = _translate_to_google_payload(messages, system, temperature, current_max_tokens, json_mode)
            url = _build_google_url(cfg, stream=stream)
            headers = {"Content-Type": "application/json", "x-goog-api-key": cfg["api_key"]}
        else:
            payload = {"model": cfg["model"], "messages": messages, "temperature": temperature}
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            payload[max_tokens_key] = current_max_tokens
            headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
            url = _build_openai_url(cfg)
            if stream:
                payload["stream"] = True
                payload["stream_options"] = {"include_usage": True}
        try:
            cfg_timeout = float(cfg.get("timeout") or settings.ai_timeout)
        except (TypeError, ValueError):
            cfg_timeout = settings.ai_timeout
        effective_timeout = float(timeout) if timeout is not None else cfg_timeout
        if not effective_timeout or effective_timeout <= 0:
            effective_timeout = settings.ai_timeout
        if effective_timeout > 1800:
            effective_timeout = 1800
        try:
            attempts = max(1, int(cfg.get("max_retries") or settings.ai_max_retries))
        except (TypeError, ValueError):
            attempts = max(1, settings.ai_max_retries)
        connect_timeout = min(settings.ai_connect_timeout, effective_timeout)
        client_timeout = httpx.Timeout(effective_timeout, connect=connect_timeout)
        # Streaming uses idle timeout (read 60s) while total respects per-call/per-user budget
        # Effective read is min(60, effective_timeout) so a 0.4s budget still times out promptly
        read_timeout = min(60, effective_timeout) if effective_timeout and effective_timeout > 0 else 60
        streaming_timeout = httpx.Timeout(effective_timeout if effective_timeout <= 1800 else 1800, connect=10, read=read_timeout, write=60, pool=60)
        last_error: Optional[str] = None
        http_client = await get_http_client()
        # The payload dict is mutated in place by the escalation/fix-up paths,
        # so ``_capture_request`` stores a deep copy at send time; only the last
        # :data:`_MAX_LOGGED_ATTEMPTS` are kept, which is enough to show what a
        # retry changed without storing an unbounded list of near-identical
        # prompts.
        def _capture_request() -> None:
            try:
                sent_requests.append({"attempt": attempt, "body": copy.deepcopy(payload)})
                del sent_requests[:-_MAX_LOGGED_ATTEMPTS]
            except Exception:  # pragma: no cover - capture is best-effort
                pass
        parsed_result: Optional[Dict[str, Any]] = None
        text_result: Optional[str] = None
        final_error: Optional[AIClientError] = None
        data: Dict[str, Any] = {}
        plain_retries = 0
        fixups = 0
        attempt = 0
        # ``last_content`` (the last answer the model produced, used as the
        # failure record's excerpt) is declared above the try.
        while attempt < attempts:
            attempt += 1
            await rate_limiter.wait_and_acquire(1)
            started = time.perf_counter()
            if is_google:
                if "generationConfig" not in payload:
                    payload["generationConfig"] = {}
                payload["generationConfig"]["maxOutputTokens"] = int(current_max_tokens)
                if json_mode_active:
                    payload["generationConfig"]["responseMimeType"] = "application/json"
                elif "responseMimeType" in payload["generationConfig"]:
                    payload["generationConfig"].pop("responseMimeType", None)
                url = _build_google_url(cfg, stream=stream)
            else:
                payload[max_tokens_key] = current_max_tokens
                if json_mode_active and "response_format" not in payload:
                    payload["response_format"] = {"type": "json_object"}
                elif not json_mode_active and "response_format" in payload:
                    payload.pop("response_format", None)
                if stream and "stream" not in payload:
                    payload["stream"] = True
            try:
                if stream:
                    async with _sem():
                        _capture_request()
                        try:
                            async with http_client.stream("POST", url, headers=headers, json=payload, timeout=streaming_timeout) as response:
                                if response.status_code != 200:
                                    body_bytes = await response.aread()
                                    body = body_bytes.decode(errors="ignore")[:400]
                                    last_error = f"status {response.status_code}: {body}"
                                    lowered = body.lower()
                                    if response.status_code == 400 and fixups < 3:
                                        fixup_applied = False
                                        if json_mode_active and _json_mode_rejected(lowered):
                                            log.warning("AI %s: provider rejected json mode, retrying without it", workflow)
                                            json_mode_active = False
                                            if is_google and "generationConfig" in payload:
                                                payload["generationConfig"].pop("responseMimeType", None)
                                            else:
                                                payload.pop("response_format", None)
                                            fixup_applied = True
                                        if (not fixup_applied and max_tokens_key in payload and "max_completion_tokens" in lowered and "unsupported" in lowered):
                                            log.warning("AI %s: provider wants max_completion_tokens, renaming the parameter", workflow)
                                            payload["max_completion_tokens"] = payload.pop(max_tokens_key)
                                            max_tokens_key = "max_completion_tokens"
                                            fixup_applied = True
                                        if (not fixup_applied and "temperature" in payload and "temperature" in lowered and ("unsupported" in lowered or "not supported" in lowered)):
                                            log.warning("AI %s: provider rejected temperature, retrying without it", workflow)
                                            payload.pop("temperature", None)
                                            if is_google and "generationConfig" in payload:
                                                payload["generationConfig"].pop("temperature", None)
                                            fixup_applied = True
                                        if (not fixup_applied and (max_tokens_key in payload or (is_google and "generationConfig" in payload)) and "max_tokens" in lowered and any(w in lowered for w in ("too large", "too high", "at most", "maximum", "exceeds"))):
                                            clamped = max(256, current_max_tokens // 2)
                                            if clamped < current_max_tokens:
                                                log.warning("AI %s: max_tokens=%d exceeds the model limit, retrying with %d",
                                                            workflow, current_max_tokens, clamped)
                                                current_max_tokens = clamped
                                                if is_google:
                                                    payload["generationConfig"]["maxOutputTokens"] = clamped
                                                else:
                                                    payload[max_tokens_key] = clamped
                                                fixup_applied = True
                                        if fixup_applied:
                                            fixups += 1
                                            attempt -= 1
                                            raise _StreamFixupNeeded()
                                    retryable = response.status_code == 429 or 500 <= response.status_code < 600
                                    http_reason = reason_from_message(last_error, response.status_code)
                                    inc("jobhunter_ai_requests_total", workflow=workflow, status=str(response.status_code))
                                    if retryable and attempt < attempts:
                                        delay = _retry_delay_stream(body, response, attempt)
                                        log.warning("AI %s -> %s, retrying in %.1fs", workflow, response.status_code, delay)
                                        await asyncio.sleep(delay)
                                        continue
                                    if not retryable:
                                        if response.status_code in (401, 403):
                                            breaker.record_failure(last_error)
                                        failure_meta.update({"attempts": attempt})
                                        raise AIClientError(last_error, status=response.status_code, retryable=False,
                                                            meta={"attempts": attempt})
                                    breaker.record_failure(last_error)
                                    failure_meta.update({"attempts": attempt})
                                    raise AIClientError(last_error, status=response.status_code, retryable=True,
                                                        meta={"attempts": attempt, "retry_after_hint": retry_hint_for_reason(http_reason, workflow=workflow)})
                                inc("jobhunter_ai_requests_total", workflow=workflow, status="200")
                                # If provider returned JSON instead of SSE (e.g. legacy test handler not supporting streaming), fallback to JSON
                                content_type = response.headers.get("content-type", "")
                                if "application/json" in content_type:
                                    # Read as JSON directly — streaming request got a non-stream response
                                    body_bytes = await response.aread()
                                    try:
                                        data = json.loads(body_bytes.decode())
                                    except Exception as exc:
                                        last_error = f"malformed_response: provider returned a non-JSON body ({exc})"
                                        if plain_retries < 1:
                                            plain_retries += 1
                                            continue
                                        breaker.record_failure(last_error)
                                        failure_meta.update({"attempts": attempt})
                                        raise AIClientError(last_error, retryable=True) from exc
                                    extracted = _extract_content(data)
                                    content = extracted["content"]
                                    last_content = content or last_content
                                    finish_reason = extracted["finish_reason"]
                                    reasoning = extracted["reasoning"]
                                    p_tok, c_tok, t_tok = _read_usage(data, prompt, content)
                                    usage_totals["prompt_tokens"] += p_tok
                                    usage_totals["completion_tokens"] += c_tok
                                    usage_totals["total_tokens"] += t_tok
                                    if not extracted["well_formed"]:
                                        last_error = ("malformed_response: provider returned 200 without a usable choices[0].message")
                                        if plain_retries < 1:
                                            plain_retries += 1
                                            continue
                                        breaker.record_failure(last_error)
                                        failure_meta.update({"attempts": attempt})
                                        raise AIClientError(last_error, retryable=True)
                                    if not json_mode:
                                        if content.strip() and finish_reason != "length":
                                            text_result = content
                                            break
                                        action = _json_retry_action(content, finish_reason, json_mode_active=False, plain_retries=plain_retries)
                                    else:
                                        parsed = None
                                        if content.strip():
                                            parsed = _parse_model_json(content)
                                        if parsed is None and reasoning.strip():
                                            parsed = _parse_model_json(reasoning)
                                        if isinstance(parsed, dict):
                                            parsed_result = parsed
                                            break
                                        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                                            parsed_result = parsed[0]
                                            break
                                        action = _json_retry_action(content, finish_reason, json_mode_active=json_mode_active, plain_retries=plain_retries)
                                    if action is not None and attempt >= attempts:
                                        action = None
                                    if action == "raise_max_tokens_big" or action == "raise_max_tokens":
                                        factor, floor = (4, 4000) if action == "raise_max_tokens_big" else (2, 2400)
                                        new_max = min(max(current_max_tokens * factor, floor), escalation_stop)
                                        if new_max > current_max_tokens:
                                            what = "the thinking phase used the whole budget" if not content.strip() else "the JSON was cut off"
                                            log.warning("AI %s: %s at %d tokens (finish_reason=%s) — retrying with %d tokens",
                                                        workflow, what, current_max_tokens, finish_reason, new_max)
                                            current_max_tokens = new_max
                                            if is_google:
                                                payload["generationConfig"]["maxOutputTokens"] = new_max
                                            else:
                                                payload[max_tokens_key] = new_max
                                            continue
                                        action = None
                                    elif action == "drop_response_format":
                                        log.warning("AI %s: model returned an empty answer with response_format=json_object — retrying without it", workflow)
                                        json_mode_active = False
                                        if is_google and "generationConfig" in payload:
                                            payload["generationConfig"].pop("responseMimeType", None)
                                        else:
                                            payload.pop("response_format", None)
                                        continue
                                    elif action == "plain_retry":
                                        log.warning("AI %s: model returned an %s answer (finish_reason=%s) — retrying",
                                                    workflow, "empty" if not content.strip() else "unparseable", finish_reason)
                                        plain_retries += 1
                                        continue
                                    final_error = _invalid_json_error(content, finish_reason, current_max_tokens)
                                    break
                                content_parts: List[str] = []
                                reasoning_parts: List[str] = []
                                finish_reason = None
                                usage_data: Dict[str, Any] = {}
                                # Fallback: if SSE yields nothing, try to read remaining as json
                                had_sse = False
                                async for line in response.aiter_lines():
                                    if not line:
                                        continue
                                    stripped = line.strip()
                                    if stripped.startswith("data:"):
                                        had_sse = True
                                    if not stripped.startswith("data:"):
                                        # If not SSE at all, treat as raw json fallback
                                        try:
                                            fallback_data = json.loads(stripped)
                                            # If we haven't had SSE, this might be raw JSON body
                                            if not had_sse:
                                                data = fallback_data
                                                extracted = _extract_content(data)
                                                content = extracted["content"]
                                                last_content = content or last_content
                                                finish_reason = extracted["finish_reason"]
                                                reasoning = extracted["reasoning"]
                                                p_tok, c_tok, t_tok = _read_usage(data, prompt, content)
                                                usage_totals["prompt_tokens"] += p_tok
                                                usage_totals["completion_tokens"] += c_tok
                                                usage_totals["total_tokens"] += t_tok
                                                # handle validation similar to above but simplified: if well_formed etc.
                                                if extracted["well_formed"]:
                                                    if not json_mode:
                                                        if content.strip() and finish_reason != "length":
                                                            text_result = content
                                                            break
                                                    else:
                                                        parsed = _parse_model_json(content) if content.strip() else None
                                                        if parsed is None and reasoning.strip():
                                                            parsed = _parse_model_json(reasoning)
                                                        if isinstance(parsed, dict):
                                                            parsed_result = parsed
                                                            break
                                                        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                                                            parsed_result = parsed[0]
                                                            break
                                                # Fall through to SSE handling if not matched
                                        except Exception:
                                            pass
                                        continue
                                    data_str = stripped[5:].strip()
                                    if data_str == "[DONE]" or not data_str:
                                        break
                                    try:
                                        chunk = json.loads(data_str)
                                    except Exception:
                                        continue
                                    if is_google:
                                        parsed = _translate_from_google_stream_chunk(chunk)
                                        if parsed.get("content"):
                                            content_parts.append(parsed["content"])
                                        if parsed.get("reasoning"):
                                            reasoning_parts.append(parsed["reasoning"])
                                        if parsed.get("finish_reason"):
                                            finish_reason = parsed["finish_reason"]
                                        if parsed.get("usage"):
                                            usage_data = parsed["usage"]
                                        if "usageMetadata" in chunk:
                                            usage_data = chunk["usageMetadata"]
                                    else:
                                        choices = chunk.get("choices") or []
                                        if choices:
                                            delta = choices[0].get("delta") or {}
                                            if isinstance(delta.get("content"), str):
                                                content_parts.append(delta["content"])
                                            rc = delta.get("reasoning_content") or delta.get("reasoning") or ""
                                            if isinstance(rc, str) and rc:
                                                reasoning_parts.append(rc)
                                            fr = choices[0].get("finish_reason")
                                            if fr:
                                                finish_reason = fr
                                        if "usage" in chunk and isinstance(chunk["usage"], dict):
                                            usage_data = chunk["usage"]
                                # If we already produced result via fallback JSON inside loop, continue
                                if parsed_result is not None or text_result is not None or final_error is not None:
                                    break
                                if not had_sse and not content_parts and not usage_data:
                                    # No SSE data at all — try to read body as JSON (server returned JSON with text/event-stream header or empty)
                                    # Already handled json fallback above, but if still empty, treat as empty_response
                                    pass
                                content = "".join(content_parts)
                                last_content = content or last_content
                                reasoning = "".join(reasoning_parts)
                                # If SSE was empty (handler returned JSON), fallback already handled via content_type branch above; if we reach here with empty content, it means handler returned SSE empty
                                data = {"choices": [{"message": {"content": content, "reasoning_content": reasoning}, "finish_reason": finish_reason}], "usage": usage_data}
                                if is_google and usage_data:
                                    data["_google_raw"] = {"usageMetadata": usage_data, "candidates": [{"content": {"parts": [{"text": content}]}, "finishReason": finish_reason}]}
                                p_tok, c_tok, t_tok = _read_usage(data, prompt, content)
                                usage_totals["prompt_tokens"] += p_tok
                                usage_totals["completion_tokens"] += c_tok
                                usage_totals["total_tokens"] += t_tok
                                extracted = {"content": content, "reasoning": reasoning, "finish_reason": finish_reason, "message": {"content": content}, "well_formed": True}
                                if not extracted["well_formed"]:
                                    last_error = ("malformed_response: provider returned 200 without a usable choices[0].message")
                                    if plain_retries < 1:
                                        plain_retries += 1
                                        continue
                                    breaker.record_failure(last_error)
                                    failure_meta.update({"attempts": attempt})
                                    raise AIClientError(last_error, retryable=True)
                                if not json_mode:
                                    if content.strip() and finish_reason != "length":
                                        text_result = content
                                        break
                                    action = _json_retry_action(content, finish_reason, json_mode_active=False, plain_retries=plain_retries)
                                else:
                                    parsed = None
                                    if content.strip():
                                        parsed = _parse_model_json(content)
                                    if parsed is None and reasoning.strip():
                                        parsed = _parse_model_json(reasoning)
                                    if isinstance(parsed, dict):
                                        parsed_result = parsed
                                        break
                                    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                                        parsed_result = parsed[0]
                                        break
                                    action = _json_retry_action(content, finish_reason, json_mode_active=json_mode_active, plain_retries=plain_retries)
                                if action is not None and attempt >= attempts:
                                    action = None
                                if action == "raise_max_tokens_big" or action == "raise_max_tokens":
                                    factor, floor = (4, 4000) if action == "raise_max_tokens_big" else (2, 2400)
                                    new_max = min(max(current_max_tokens * factor, floor), escalation_stop)
                                    if new_max > current_max_tokens:
                                        what = "the thinking phase used the whole budget" if not content.strip() else "the JSON was cut off"
                                        log.warning("AI %s: %s at %d tokens (finish_reason=%s) — retrying with %d tokens",
                                                    workflow, what, current_max_tokens, finish_reason, new_max)
                                        current_max_tokens = new_max
                                        if is_google:
                                            payload["generationConfig"]["maxOutputTokens"] = new_max
                                        else:
                                            payload[max_tokens_key] = new_max
                                        continue
                                    action = None
                                elif action == "drop_response_format":
                                    log.warning("AI %s: model returned an empty answer with response_format=json_object — retrying without it", workflow)
                                    json_mode_active = False
                                    if is_google and "generationConfig" in payload:
                                        payload["generationConfig"].pop("responseMimeType", None)
                                    else:
                                        payload.pop("response_format", None)
                                    continue
                                elif action == "plain_retry":
                                    log.warning("AI %s: model returned an %s answer (finish_reason=%s) — retrying",
                                                workflow, "empty" if not content.strip() else "unparseable", finish_reason)
                                    plain_retries += 1
                                    continue
                                final_error = _invalid_json_error(content, finish_reason, current_max_tokens)
                                break
                        except _StreamFixupNeeded:
                            continue
                        except httpx.TimeoutException as exc:
                            waited = time.perf_counter() - started
                            if isinstance(exc, httpx.ConnectTimeout):
                                last_error = (f"http_error: connection to {cfg['base_url']} timed out after {connect_timeout:g}s (attempt {attempt}/{attempts}) — check the base_url in Settings → AI API")
                                inc("jobhunter_ai_requests_total", workflow=workflow, status="network_error")
                                if attempt < attempts:
                                    await _sleep_backoff(attempt)
                                    continue
                                breaker.record_failure(last_error)
                                failure_meta.update({"attempts": attempt, "timeout_seconds": effective_timeout, "connect_timeout_seconds": connect_timeout, "possibly_billed": False, "usage_unknown": False})
                                raise AIClientError(last_error, reason=REASON_UNREACHABLE, retryable=True, meta={"attempts": attempt, "possibly_billed": False, "usage_unknown": False, "retry_after_hint": retry_hint_for_reason(REASON_UNREACHABLE)}) from exc
                            last_error = (f"timeout: model '{cfg['model']}' was still generating after {effective_timeout:g}s (attempt {attempt}/{attempts}, waited {waited:.1f}s) — the endpoint was reached; {attempt} attempt(s) spent and each may still have been billed. Raise Timeout in Settings → AI API (or AI_TIMEOUT) and retry")
                            inc("jobhunter_ai_requests_total", workflow=workflow, status="timeout")
                            log.warning("AI %s: %s", workflow, last_error)
                            breaker.record_failure(last_error)
                            failure_meta.update({"attempts": attempt, "timeout_seconds": effective_timeout, "possibly_billed": True, "usage_unknown": True})
                            raise AIClientError(last_error, reason=REASON_TIMEOUT, retryable=True, meta={"attempts": attempt, "possibly_billed": True, "usage_unknown": True, "retry_after_hint": retry_hint_for_reason(REASON_TIMEOUT)}) from exc
                    # end streaming sem
                else:
                    async with _sem():
                        _capture_request()
                        if is_google:
                            response = await http_client.post(url, headers=headers, json=payload, timeout=client_timeout)
                        else:
                            response = await http_client.post(url, headers=headers, json=payload, timeout=client_timeout)
                if stream:
                    if parsed_result is not None or text_result is not None or final_error is not None:
                        break
                    continue
            except httpx.TimeoutException as exc:
                if stream:
                    # already handled inside streaming block; re-raise if not
                    raise
                waited = time.perf_counter() - started
                if isinstance(exc, httpx.ConnectTimeout):
                    last_error = (
                        f"http_error: connection to {cfg['base_url']} timed out after "
                        f"{connect_timeout:g}s (attempt {attempt}/{attempts}) — check the "
                        f"base_url in Settings → AI API"
                    )
                    inc("jobhunter_ai_requests_total", workflow=workflow, status="network_error")
                    if attempt < attempts:
                        await _sleep_backoff(attempt)
                        continue
                    breaker.record_failure(last_error)
                    failure_meta.update({"attempts": attempt,
                                         "timeout_seconds": effective_timeout,
                                         "connect_timeout_seconds": connect_timeout,
                                         "possibly_billed": False,
                                         "usage_unknown": False})
                    raise AIClientError(last_error, reason=REASON_UNREACHABLE, retryable=True,
                                        meta={"attempts": attempt, "possibly_billed": False,
                                              "usage_unknown": False,
                                              "retry_after_hint": retry_hint_for_reason(REASON_UNREACHABLE)}) from exc
                last_error = (
                    f"timeout: model '{cfg['model']}' was still generating after "
                    f"{effective_timeout:g}s (attempt {attempt}/{attempts}, waited {waited:.1f}s) — "
                    f"the endpoint was reached; {attempt} attempt(s) spent and each may still "
                    f"have been billed. Raise Timeout in Settings → AI API (or AI_TIMEOUT) and retry"
                )
                inc("jobhunter_ai_requests_total", workflow=workflow, status="timeout")
                log.warning("AI %s: %s", workflow, last_error)
                breaker.record_failure(last_error)
                failure_meta.update({"attempts": attempt,
                                     "timeout_seconds": effective_timeout,
                                     "possibly_billed": True,
                                     "usage_unknown": True})
                raise AIClientError(last_error, reason=REASON_TIMEOUT, retryable=True,
                                    meta={"attempts": attempt, "possibly_billed": True,
                                          "usage_unknown": True,
                                          "retry_after_hint": retry_hint_for_reason(REASON_TIMEOUT)}) from exc
            except httpx.HTTPError as exc:
                if isinstance(exc, _StreamFixupNeeded):
                    raise
                raw = str(exc).strip() or type(exc).__name__
                last_error = f"http_error: {raw} (attempt {attempt}/{attempts})"
                inc("jobhunter_ai_requests_total", workflow=workflow, status="network_error")
                if attempt < attempts:
                    await _sleep_backoff(attempt)
                    continue
                breaker.record_failure(last_error)
                failure_meta.update({"attempts": attempt,
                                     "timeout_seconds": effective_timeout,
                                     "possibly_billed": False,
                                     "usage_unknown": False})
                raise AIClientError(last_error, reason=REASON_UNREACHABLE, retryable=True,
                                    meta={"attempts": attempt, "possibly_billed": False,
                                          "usage_unknown": False,
                                          "retry_after_hint": retry_hint_for_reason(REASON_UNREACHABLE)}) from exc
            finally:
                observe("jobhunter_ai_latency_seconds", time.perf_counter() - started, workflow=workflow)
            if stream:
                if parsed_result is not None or text_result is not None or final_error is not None:
                    break
                continue
            if response.status_code != 200:
                body = response.text[:400]
                last_error = f"status {response.status_code}: {body}"
                lowered = body.lower()
                if response.status_code == 400 and fixups < 3:
                    fixup_applied = False
                    if json_mode_active and _json_mode_rejected(lowered):
                        log.warning("AI %s: provider rejected json_object response_format, retrying without it", workflow)
                        if is_google and "generationConfig" in payload:
                            payload["generationConfig"].pop("responseMimeType", None)
                        else:
                            payload.pop("response_format", None)
                        json_mode_active = False
                        fixup_applied = True
                    if (not fixup_applied and max_tokens_key in payload
                            and "max_completion_tokens" in lowered and "unsupported" in lowered):
                        log.warning("AI %s: provider wants max_completion_tokens, renaming the parameter", workflow)
                        payload["max_completion_tokens"] = payload.pop(max_tokens_key)
                        max_tokens_key = "max_completion_tokens"
                        fixup_applied = True
                    if (not fixup_applied and "temperature" in payload and "temperature" in lowered
                            and ("unsupported" in lowered or "not supported" in lowered)):
                        log.warning("AI %s: provider rejected temperature, retrying without it", workflow)
                        payload.pop("temperature", None)
                        if is_google and "generationConfig" in payload:
                            payload["generationConfig"].pop("temperature", None)
                        fixup_applied = True
                    if (not fixup_applied and max_tokens_key in payload and "max_tokens" in lowered
                            and any(w in lowered for w in ("too large", "too high", "at most", "maximum", "exceeds"))):
                        clamped = max(256, current_max_tokens // 2)
                        if clamped < current_max_tokens:
                            log.warning("AI %s: max_tokens=%d exceeds the model limit, retrying with %d",
                                        workflow, current_max_tokens, clamped)
                            current_max_tokens = clamped
                            if is_google:
                                payload["generationConfig"]["maxOutputTokens"] = clamped
                            else:
                                payload[max_tokens_key] = clamped
                            fixup_applied = True
                    if fixup_applied:
                        fixups += 1
                        attempt -= 1
                        continue
                retryable = response.status_code == 429 or 500 <= response.status_code < 600
                http_reason = reason_from_message(last_error, response.status_code)
                inc("jobhunter_ai_requests_total", workflow=workflow, status=str(response.status_code))
                if retryable and attempt < attempts:
                    delay = _retry_delay(response, attempt)
                    log.warning("AI %s -> %s, retrying in %.1fs", workflow, response.status_code, delay)
                    await asyncio.sleep(delay)
                    continue
                if not retryable:
                    if response.status_code in (401, 403):
                        breaker.record_failure(last_error)
                    failure_meta.update({"attempts": attempt})
                    raise AIClientError(last_error, status=response.status_code, retryable=False,
                                        meta={"attempts": attempt})
                breaker.record_failure(last_error)
                failure_meta.update({"attempts": attempt})
                raise AIClientError(last_error, status=response.status_code, retryable=True,
                                    meta={"attempts": attempt,
                                          "retry_after_hint": retry_hint_for_reason(http_reason, workflow=workflow)})
            inc("jobhunter_ai_requests_total", workflow=workflow, status="200")
            try:
                data = response.json()
            except ValueError as exc:
                last_error = f"malformed_response: provider returned a non-JSON body ({exc})"
                if plain_retries < 1:
                    plain_retries += 1
                    continue
                breaker.record_failure(last_error)
                failure_meta.update({"attempts": attempt})
                raise AIClientError(last_error, retryable=True) from exc
            extracted = _extract_content(data)
            content = extracted["content"]
            last_content = content or last_content
            finish_reason = extracted["finish_reason"]
            reasoning = extracted["reasoning"]
            p_tok, c_tok, t_tok = _read_usage(data, prompt, content)
            usage_totals["prompt_tokens"] += p_tok
            usage_totals["completion_tokens"] += c_tok
            usage_totals["total_tokens"] += t_tok
            if not extracted["well_formed"]:
                last_error = ("malformed_response: provider returned 200 without a usable "
                              "choices[0].message")
                if plain_retries < 1:
                    plain_retries += 1
                    continue
                breaker.record_failure(last_error)
                failure_meta.update({"attempts": attempt})
                raise AIClientError(last_error, retryable=True)
            if not json_mode:
                if content.strip() and finish_reason != "length":
                    text_result = content
                    break
                action = _json_retry_action(content, finish_reason,
                                            json_mode_active=False, plain_retries=plain_retries)
            else:
                parsed = None
                if content.strip():
                    parsed = _parse_model_json(content)
                if parsed is None and reasoning.strip():
                    parsed = _parse_model_json(reasoning)
                if isinstance(parsed, dict):
                    parsed_result = parsed
                    break
                if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                    parsed_result = parsed[0]
                    break
                action = _json_retry_action(content, finish_reason,
                                            json_mode_active=json_mode_active,
                                            plain_retries=plain_retries)
            if action is not None and attempt >= attempts:
                action = None
            if action == "raise_max_tokens_big" or action == "raise_max_tokens":
                factor, floor = (4, 4000) if action == "raise_max_tokens_big" else (2, 2400)
                new_max = min(max(current_max_tokens * factor, floor), escalation_stop)
                if new_max > current_max_tokens:
                    what = "the thinking phase used the whole budget" if not content.strip() else "the JSON was cut off"
                    log.warning("AI %s: %s at %d tokens (finish_reason=%s) — retrying with %d tokens",
                                workflow, what, current_max_tokens, finish_reason, new_max)
                    current_max_tokens = new_max
                    if is_google:
                        payload["generationConfig"]["maxOutputTokens"] = new_max
                    else:
                        payload[max_tokens_key] = new_max
                    continue
                action = None
            elif action == "drop_response_format":
                log.warning("AI %s: model returned an empty answer with response_format=json_object — retrying without it", workflow)
                json_mode_active = False
                if is_google and "generationConfig" in payload:
                    payload["generationConfig"].pop("responseMimeType", None)
                else:
                    payload.pop("response_format", None)
                continue
            elif action == "plain_retry":
                log.warning("AI %s: model returned an %s answer (finish_reason=%s) — retrying",
                            workflow, "empty" if not content.strip() else "unparseable", finish_reason)
                plain_retries += 1
                continue
            final_error = _invalid_json_error(content, finish_reason, current_max_tokens)
            break
        if final_error is not None:
            breaker.record_failure(str(final_error))
            failure_meta.update({"attempts": attempt})
            raise final_error
        if parsed_result is None and text_result is None:
            breaker.record_failure(last_error or "unknown error")
            failure_meta.update({"attempts": attempt})
            raise AIClientError(last_error or "unknown error", retryable=True,
                                meta={"attempts": attempt,
                                      "retry_after_hint": retry_hint_for_reason(None, workflow=workflow)})
        prompt_tokens = usage_totals["prompt_tokens"]
        completion_tokens = usage_totals["completion_tokens"]
        total_tokens = usage_totals["total_tokens"]
        latency_ms = int((time.perf_counter() - call_started) * 1000)
        breaker.record_success()
        bucket = _usage.setdefault(workflow, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0})
        bucket["calls"] += 1
        bucket["prompt_tokens"] += prompt_tokens
        bucket["completion_tokens"] += completion_tokens
        bucket["total_tokens"] += total_tokens
        set_gauge("jobhunter_ai_tokens_total", bucket["total_tokens"], workflow=workflow)
        track_ai_usage(
            db=db,
            user_id=user_id,
            workflow=workflow,
            model=cfg["model"],
            provider=str(cfg.get("provider") or ""),
            base_url=str(cfg.get("base_url") or ""),
            request=_request_envelope(url, sent_requests, temperature=temperature,
                                      json_mode=json_mode_active, stream=stream,
                                      max_tokens=current_max_tokens, attempts=attempt),
            response=_answer_excerpt(text_result, parsed_result),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            success=True,
            latency_ms=latency_ms,
            meta={"attempts": attempt, "timeout_seconds": effective_timeout,
                  "input_truncated": input_truncated, "max_tokens": current_max_tokens,
                  "output_ceiling": output_ceiling,
                  "output_unlimited": output_ceiling <= 0,
                  "escalation_ceiling": escalation_stop,
                  "input_unlimited": resolved_input <= 0, "stream": stream, "provider": cfg.get("provider")},
        )
        if not json_mode:
            return {"content": text_result or "", "raw": data,
                    "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                              "total_tokens": total_tokens}}
        if parsed_result is None:
            raise AIClientError(last_error or "unknown error", retryable=True)
        return parsed_result
    except AIClientError as exc:
        track_ai_usage(
            db=db, user_id=user_id, workflow=workflow, model=cfg["model"],
            provider=str(cfg.get("provider") or ""), base_url=str(cfg.get("base_url") or ""),
            request=_request_envelope(url, sent_requests, temperature=temperature,
                                      json_mode=json_mode_active, stream=stream,
                                      max_tokens=current_max_tokens,
                                      attempts=int(failure_meta.get("attempts") or attempt)),
            response=_answer_excerpt(last_content, None),
            **usage_totals, success=False, error=str(exc),
            latency_ms=int((time.perf_counter() - call_started) * 1000),
            # ``reason``/``http_status`` make the owner-only log filterable by
            # *why* a call failed, not just that it did.
            meta={**failure_meta, **exc.meta, "reason": exc.reason,
                  "http_status": exc.status or failure_meta.get("http_status")},
        )
        raise
    finally:
        if resolved.owned_session and resolved.db is not None:
            try:
                resolved.db.close()
            except Exception:
                pass


class _StreamFixupNeeded(Exception):
    pass

def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return min(30.0, float(retry_after))
        except ValueError:
            pass
    return min(20.0, settings.ai_backoff_base ** attempt) * (0.7 + random.random() * 0.6)

def _retry_delay_stream(body: str, response, attempt: int) -> float:
    retry_after = None
    if hasattr(response, "headers"):
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


# --------------------------------------------------------------------------- #
# Prompt secret scrubbing — defence in depth at the AI boundary
# --------------------------------------------------------------------------- #
#: The canonical shapes live in :mod:`app.core.redaction`, shared with the audit
#: boundary and the log formatters: a new secret shape is then masked on every
#: outbound channel at once rather than on two of the three.
_PROMPT_SECRET_PATTERNS = tuple(pattern for pattern, _ in SECRET_PATTERNS)


def _scrub_prompt_secrets(text: Optional[str]) -> str:
    """Mask any known secret shape in a prompt before it reaches an AI provider.

    No current workflow includes credentials in prompts, but this is the
    defence-in-depth boundary: if a caller accidentally embeds a vault
    password, API key or bearer token in a prompt, it is masked before the
    request is sent, and a metric is incremented for visibility.
    """
    if not text:
        return ""
    result = text
    for pattern in _PROMPT_SECRET_PATTERNS:
        new_result = pattern.sub("***", result)
        if new_result != result:
            inc("jobhunter_ai_prompt_secrets_scrubbed_total")
            log.warning("AI prompt contained a secret-shaped value — masked before sending")
        result = new_result
    return result


def _provider_error(response: Optional[httpx.Response]) -> str:
    """Best-effort human-readable error from a provider response (OpenAI + Google)."""
    if response is None:
        return ""
    try:
        data = response.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            # Google: {error: {message, code, status}} ; OpenAI: {error: {message}}
            msg = err.get("message") or err.get("status") or ""
            if not msg and "code" in err:
                msg = f"{err.get('code')}: {err.get('message') or ''}".strip(": ")
            return str(msg)[:300]
        if err:
            return str(err)[:300]
        # Google may return error at top level without wrapper
        if isinstance(data, dict) and "message" in data:
            return str(data.get("message"))[:300]
    except Exception:
        pass
    return (response.text or "")[:300]


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


async def _ping_with_config(cfg: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    key = (cfg.get("api_key") or "").strip()
    base = (cfg.get("base_url") or "").strip().rstrip("/")
    model = (cfg.get("model") or "").strip()
    provider = _normalize_provider(cfg.get("provider")) if cfg.get("provider") else _detect_provider_from_base_url(base)
    meta = {
        "base_url": base,
        "model": model,
        "provider": provider,
        "key_source": cfg.get("key_source"),
        "key_preview": _key_preview(key),
    }

    if cfg.get("key_error"):
        return {"online": False, "reason": "stored_key_unreadable", "latency_ms": None,
                "hint": "The saved API key could not be decrypted on this server (its ENCRYPTION_KEY changed since the key was saved). Re-enter the key in Settings → AI API to fix it.",
                **meta}
    if not key:
        return {"online": False, "reason": "no_api_key", "latency_ms": None,
                "hint": "Set API key in Settings → AI API (OpenAI compatible: base_url like https://api.openai.com/v1. For Google, use https://generativelanguage.googleapis.com)",
                **meta}

    cache_key = f"{base}|{model}|{provider}|{hashlib.sha256(key.encode()).hexdigest()[:16]}"
    now = time.monotonic()
    cached = _PING_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    is_google = provider == AI_PROVIDER_GOOGLE or _is_google_provider(cfg)
    if is_google:
        headers = {"Content-Type": "application/json", "x-goog-api-key": key}
        # Base for google probes
        ping_base = base
        if "generativelanguage.googleapis.com" not in ping_base:
            ping_base = "https://generativelanguage.googleapis.com"
        if "/v1beta" not in ping_base and "/v1" not in ping_base:
            ping_base = f"{ping_base}/v1beta"
        elif ping_base.endswith("/v1"):
            ping_base = ping_base[:-3] + "/v1beta"
        models_url = f"{ping_base}/models"
    else:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        models_url = f"{base}/models"
    started = time.perf_counter()

    # Reuse the shared pooled client (SSRF guard, connection reuse). Per-request
    # timeout override keeps the probe budget (``timeout``) intact.
    http_client = await get_http_client()

    # Stage 1 — cheap models listing (sufficient on most providers).
    try:
        response = await http_client.get(models_url, headers=headers, timeout=timeout)
    except httpx.TimeoutException as exc:
        # The endpoint may be reachable but slow (httpx stringifies timeouts
        # as "" — say so explicitly). NOT cached: slowness is transient.
        if isinstance(exc, httpx.ConnectTimeout):
            return {"online": False, "reason": "unreachable",
                    "error": f"connection to {base} timed out after {timeout}s",
                    "latency_ms": None,
                    "hint": f"Could not reach {base} — check the base URL and network connectivity.",
                    **meta}
        return {"online": False, "reason": "timeout",
                "error": f"the provider was still answering after {timeout}s",
                "latency_ms": None,
                "hint": "The provider is reachable but slow — retry, or raise the probe timeout.",
                **meta}
    except httpx.HTTPError as exc:
        # Transient network errors are NOT cached — connectivity can recover
        # at any moment, unlike a definitive auth rejection.
        raw = str(exc).strip() or type(exc).__name__
        return {"online": False, "reason": "unreachable", "error": raw, "latency_ms": None,
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
        if is_google:
            probe = await http_client.post(
                f"{ping_base}/models/{model}:generateContent",
                headers=headers,
                json={"contents": [{"role": "user", "parts": [{"text": "ping"}]}], "generationConfig": {"maxOutputTokens": 1}},
                timeout=timeout,
            )
        else:
            probe = await http_client.post(
                f"{base}/chat/completions",
                headers=headers,
                json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                timeout=timeout,
            )
    except httpx.TimeoutException:
        # The chat endpoint was reached but is slow (reasoning models "think"
        # even for a 1-token ping) — that is a wait problem, not proof the
        # key is wrong. Report what /models told us.
        result = {"online": False, "latency_ms": int((time.perf_counter() - started) * 1000),
                  "status": response.status_code, "reason": "timeout",
                  "detail": _provider_error(response) or f"chat probe still answering after {timeout}s",
                  "probe": "models", **meta}
        _PING_CACHE[cache_key] = (now + _PING_TTL_SECONDS, result)
        return result
    except httpx.HTTPError:
        # Chat endpoint unreachable: report what /models told us.
        result = {"online": False, "latency_ms": int((time.perf_counter() - started) * 1000),
                  "status": response.status_code, "reason": f"status_{response.status_code}",
                  "detail": _provider_error(response), "probe": "models", **meta}
        _PING_CACHE[cache_key] = (now + _PING_TTL_SECONDS, result)
        return result

    latency_ms = int((time.perf_counter() - started) * 1000)
    if probe.status_code == 200:
        result = {"online": True, "latency_ms": latency_ms, "status": 200, "probe": "chat_completions" if not is_google else "generateContent",
                  "note": "models endpoint unavailable/restricted — chat completions verified working",
                  **meta}
    elif probe.status_code in (401, 403):
        result = {"online": False, "reason": "invalid_api_key", "status": probe.status_code,
                  "latency_ms": latency_ms, "probe": "chat_completions" if not is_google else "generateContent",
                  "detail": _provider_error(probe) or _provider_error(response),
                  "hint": f"The provider rejected this key for {base}. Double-check the key, and that base_url + model belong to the same provider.",
                  **meta}
    else:
        result = {"online": False, "reason": f"status_{probe.status_code}", "status": probe.status_code,
                  "latency_ms": latency_ms, "probe": "chat_completions" if not is_google else "generateContent",
                  "detail": _provider_error(probe), **meta}
    _PING_CACHE[cache_key] = (now + _PING_TTL_SECONDS, result)
    return result
