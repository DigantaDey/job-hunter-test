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
   ceiling: the gateway clamps every outgoing ``max_tokens`` to the resolved
   ceiling and the automatic escalation on truncation can never exceed it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
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


def resolve_config(workflow: Optional[str] = None) -> Dict[str, Any]:
    """Env-level config (no user context). Includes the legacy system bucket."""
    override = _workflow_overrides.get((0, workflow or ""), {}) if workflow else {}
    api_key = (override.get("api_key") or settings.ai_api_key or "").strip()
    return {
        "base_url": (override.get("base_url") or settings.ai_base_url).strip().rstrip("/"),
        "api_key": api_key,
        "model": (override.get("model") or settings.ai_model).strip(),
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
    try:
        from app.services.user_settings import get_user_ai_config
        user_cfg = get_user_ai_config(db, user_id)
        base_url = user_cfg.get("base_url") or settings.ai_base_url
        model = user_cfg.get("model") or settings.ai_model
        api_key = user_cfg.get("api_key") or settings.ai_api_key
        key_source = user_cfg.get("key_source") or ("env" if api_key else None)
        key_error = user_cfg.get("api_key_error")
        user_timeout = user_cfg.get("timeout", settings.ai_timeout)
        user_max_retries = user_cfg.get("max_retries", settings.ai_max_retries)
        user_max_input_tokens = user_cfg.get("max_input_tokens", settings.ai_max_input_tokens)
        user_max_output_tokens = user_cfg.get("max_output_tokens", settings.ai_max_output_tokens)
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
        "timeout": user_timeout,
        "max_retries": user_max_retries,
        "max_input_tokens": user_max_input_tokens,
        "max_output_tokens": user_max_output_tokens,
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
            if resolved.owned_session:
                resolved.db.close()
    return bool(resolve_config(workflow)["api_key"])


def usage_snapshot() -> Dict[str, Dict[str, int]]:
    return {k: dict(v) for k, v in _usage.items()}


def breaker_snapshot() -> Dict[str, Dict[str, Any]]:
    return {k: v.state() for k, v in _breakers.items()}


def input_budget_chars(db=None, user_id: Optional[int] = None,
                       ai_config: Optional[Dict[str, Any]] = None) -> int:
    """Character budget for ONE dynamic prompt part, from the user's input budget.

    Services cap every document they splice into a prompt (JD, profile JSON,
    resume text…) with this instead of hardcoded slices; the gateway then caps
    the *total* prompt the same way, so the user's ``ai.max_input_tokens`` is
    the single source of truth end to end.
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
    try:
        return max(1000, int(raw) * CHARS_PER_TOKEN)
    except (TypeError, ValueError):
        return max(1000, settings.ai_max_input_tokens * CHARS_PER_TOKEN)


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


#: Invisible characters some providers wrap answers in (BOM, zero-width joiners).
#: They break ``json.loads`` while being undetectable in logs.
_INVISIBLES_TABLE = str.maketrans("", "", "\ufeff\u200b\u200c\u200d\u2060")
#: How many opening-bracket positions to probe when salvaging the model's last
#: JSON draft out of a reasoning trace.
_MAX_JSON_SALVAGE_TRIES = 32
#: Hard upper bound for automatic max_tokens escalation (reasoning models can
#: need thousands of tokens of thinking before the JSON is even started). The
#: user's configured output ceiling is always clamped to this value as well.
_MAX_OUTPUT_TOKENS_CEILING = 16000
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
    ever raising.

    The wild is messy: ``content`` arrives as ``None``, as a list of parts
    (``[{"type": "text", "text": ...}]``), or empty with the real answer in
    ``reasoning_content`` (reasoning models whose thinking phase consumed the
    whole ``max_tokens`` budget). Legacy completions proxies put the text in
    ``choices[0].text``. All of those are normalised here.
    """
    empty: Dict[str, Any] = {"content": "", "reasoning": "", "finish_reason": None,
                             "message": {}, "well_formed": False}
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
    """Extract token usage, handling the many provider field variants."""
    raw_usage = data.get("usage") or data.get("usage_metadata") or {}
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

    prompt_tokens = _int_field("prompt_tokens", "input_tokens", "promptTokens", "inputTokens", "prompt_tokens_count")
    completion_tokens = _int_field("completion_tokens", "output_tokens", "completionTokens", "outputTokens", "candidatesTokenCount")
    total_tokens = _int_field("total_tokens", "totalTokens")
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
                "Raise AI_MAX_OUTPUT_TOKENS or use a model that does not burn the budget on reasoning",
                reason=REASON_TRUNCATED, retryable=True)
        return AIClientError(
            f"empty_response: the model returned an empty answer (finish_reason={finish_reason or 'unknown'}) "
            "— free-tier and reasoning models do this intermittently; retry or switch model",
            reason=REASON_EMPTY_RESPONSE, retryable=True)
    if finish_reason == "length":
        return AIClientError(
            f"truncated_response: the JSON was cut off at max_tokens={current_max_tokens} "
            "(finish_reason=length) — raise AI_MAX_OUTPUT_TOKENS or use a model with a larger output limit",
            reason=REASON_TRUNCATED, retryable=True)
    return AIClientError(
        f"invalid_json: model did not return valid JSON (preview: {content[:300]!r})",
        reason=REASON_INVALID_JSON, retryable=True)

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
    timeout: Optional[float] = None,
    json_mode: bool = True,
    max_tokens: Optional[int] = None,
    ai_config: Optional[Dict[str, str]] = None,
    system: Optional[str] = None,
    db=None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Send a chat completion. Raises ``AIClientError`` on failure.

    A 200 response is not trusted blindly: reasoning models can return an
    empty ``content`` (the whole ``max_tokens`` budget spent on thinking), the
    JSON can be truncated mid-way (``finish_reason=length``), and some
    providers accept ``response_format=json_object`` but then emit nothing.
    All of those are retried *automatically with a targeted fix* — more output
    tokens, or without ``response_format`` — before the caller ever sees an
    ``invalid_json`` / ``empty_response`` / ``truncated_response`` error.

    Wait budget (per wire attempt): explicit ``timeout`` → the caller's
    per-user ``ai.timeout`` setting → ``AI_TIMEOUT`` (default 300s, generous
    enough for heavy reasoning models). The TCP+TLS connect phase gets its own
    short deadline (``AI_CONNECT_TIMEOUT``) so a genuinely unreachable
    endpoint still fails fast. A read/write wait that expires is reported
    honestly as ``timeout`` — never ``unreachable`` — and is *not* retried
    automatically: the provider accepted the request and may still be
    generating (and billing), so another attempt would spend — and bill —
    a second full generation.
    """
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
                "timeout": cfg.get("timeout"),
                "max_retries": cfg.get("max_retries"),
                "max_input_tokens": ai_config.get("max_input_tokens") or cfg.get("max_input_tokens") or settings.ai_max_input_tokens,
                "max_output_tokens": ai_config.get("max_output_tokens") or cfg.get("max_output_tokens") or settings.ai_max_output_tokens,
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
                # Plan limit reached: a BLOCKED outcome (limit_exceeded) — the
                # call is refused honestly, never substituted with an estimate.
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

        # ------------------------------------------------------------------ #
        # User token budgets — the single source of truth (Settings → AI API)
        # ------------------------------------------------------------------ #
        try:
            output_ceiling = min(max(64, int(cfg.get("max_output_tokens") or settings.ai_max_output_tokens)),
                                 _MAX_OUTPUT_TOKENS_CEILING)
        except (TypeError, ValueError):
            output_ceiling = _MAX_OUTPUT_TOKENS_CEILING
        try:
            input_budget_chars = max(1000, int(cfg.get("max_input_tokens") or settings.ai_max_input_tokens) * CHARS_PER_TOKEN)
        except (TypeError, ValueError):
            input_budget_chars = max(1000, settings.ai_max_input_tokens * CHARS_PER_TOKEN)
        input_truncated = False

        messages: List[Dict[str, str]] = []
        if system:
            # Sanitize system prompt — treat external data as untrusted, then
            # cap it at the user's input budget (no hardcoded slices).
            safe_system, _system_truncated = fit_prompt_part(system.replace("```", ""), input_budget_chars,
                                                             label=f"{workflow}.system")
            input_truncated = input_truncated or _system_truncated
            messages.append({"role": "system", "content": safe_system})
        # Sanitize user prompt: remove potential injection, cap at the budget.
        safe_prompt, _prompt_truncated = fit_prompt_part(
            prompt.replace("SYSTEM:", "").replace("Ignore previous", ""), input_budget_chars,
            label=f"{workflow}.prompt")
        input_truncated = input_truncated or _prompt_truncated
        messages.append({"role": "user", "content": safe_prompt})

        payload: Dict[str, Any] = {"model": cfg["model"], "messages": messages, "temperature": temperature}
        json_mode_active = json_mode
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        # Output budget: the task's starting budget (per call) clamped to the
        # user's configured ceiling. The ceiling is the single source of truth
        # — the automatic escalation below can never exceed it either.
        current_max_tokens = min(max(1, int(max_tokens or _DEFAULT_OUTPUT_START)), output_ceiling)
        if max_tokens and int(max_tokens) > output_ceiling:
            log.warning("AI %s: max_tokens=%d clamped to the user's output ceiling %d",
                        workflow, int(max_tokens), output_ceiling)
        max_tokens_key = "max_tokens"
        payload[max_tokens_key] = current_max_tokens

        headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
        url = f"{cfg['base_url']}/chat/completions"
        # Wait budget: explicit per-call override → per-user setting → env default.
        try:
            cfg_timeout = float(cfg.get("timeout") or settings.ai_timeout)
        except (TypeError, ValueError):
            cfg_timeout = settings.ai_timeout
        effective_timeout = float(timeout) if timeout is not None else cfg_timeout
        if not effective_timeout or effective_timeout <= 0:
            effective_timeout = settings.ai_timeout
        try:
            attempts = max(1, int(cfg.get("max_retries") or settings.ai_max_retries))
        except (TypeError, ValueError):
            attempts = max(1, settings.ai_max_retries)
        # Short connect deadline inside the long generation wait: DNS/TLS/TCP
        # failures surface in seconds while a healthy generation gets minutes.
        connect_timeout = min(settings.ai_connect_timeout, effective_timeout)
        client_timeout = httpx.Timeout(effective_timeout, connect=connect_timeout)
        last_error: Optional[str] = None

        # State accumulated across the wire attempts of this logical call.
        usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        parsed_result: Optional[Dict[str, Any]] = None   # JSON mode success
        text_result: Optional[str] = None                # text mode success
        final_error: Optional[AIClientError] = None      # 200-but-unusable answer
        data: Dict[str, Any] = {}
        plain_retries = 0     # same-payload retries allowed for empty/flaky answers
        fixups = 0            # provider-parameter fix-ups (don't consume attempts)
        call_started = time.perf_counter()

        def _ledger_failure(error: str, *, meta: Optional[Dict[str, Any]] = None) -> None:
            """Record the final failure with the tokens actually spent.

            For waits that expired the provider may still have generated (and
            billed) tokens the app never saw — the row keeps 0 tokens but
            carries the attempts spent and an explicit billing caveat instead
            of silently claiming nothing was spent.
            """
            if db and user_id:
                track_ai_usage(db=db, user_id=user_id, workflow=workflow, model=cfg["model"],
                               prompt_tokens=usage_totals["prompt_tokens"],
                               completion_tokens=usage_totals["completion_tokens"],
                               total_tokens=usage_totals["total_tokens"], success=False,
                               error=error[:2000],
                               latency_ms=int((time.perf_counter() - call_started) * 1000),
                               meta=meta or {})

        attempt = 0
        while attempt < attempts:
            attempt += 1
            await rate_limiter.wait_and_acquire(1)
            started = time.perf_counter()
            try:
                async with _sem():
                    async with httpx.AsyncClient(timeout=client_timeout) as client:
                        response = await client.post(url, headers=headers, json=payload)
            except httpx.TimeoutException as exc:
                # httpx stringifies timeouts as "" — the message is built
                # explicitly so the detail is never blank. The phase matters:
                # a *connect* timeout means the endpoint was never reached
                # (honest `unreachable`, safe to retry); a read/write/pool
                # timeout means the request was accepted and the model may
                # still be generating — honest `timeout`, must NOT be retried
                # (each retry would spend and bill another full generation).
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
                    _ledger_failure(last_error, meta={"attempts": attempt,
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
                _ledger_failure(last_error, meta={"attempts": attempt,
                                                   "timeout_seconds": effective_timeout,
                                                   "possibly_billed": True,
                                                   "usage_unknown": True})
                raise AIClientError(last_error, reason=REASON_TIMEOUT, retryable=True,
                                    meta={"attempts": attempt, "possibly_billed": True,
                                          "usage_unknown": True,
                                          "retry_after_hint": retry_hint_for_reason(REASON_TIMEOUT)}) from exc
            except httpx.HTTPError as exc:
                raw = str(exc).strip() or type(exc).__name__
                last_error = f"http_error: {raw} (attempt {attempt}/{attempts})"
                inc("jobhunter_ai_requests_total", workflow=workflow, status="network_error")
                if attempt < attempts:
                    await _sleep_backoff(attempt)
                    continue
                breaker.record_failure(last_error)
                _ledger_failure(last_error, meta={"attempts": attempt,
                                                   "timeout_seconds": effective_timeout,
                                                   "possibly_billed": False,
                                                   "usage_unknown": False})
                raise AIClientError(last_error, reason=REASON_UNREACHABLE, retryable=True,
                                    meta={"attempts": attempt, "possibly_billed": False,
                                          "usage_unknown": False,
                                          "retry_after_hint": retry_hint_for_reason(REASON_UNREACHABLE)}) from exc
            finally:
                observe("jobhunter_ai_latency_seconds", time.perf_counter() - started, workflow=workflow)

            if response.status_code != 200:
                body = response.text[:400]
                last_error = f"status {response.status_code}: {body}"
                lowered = body.lower()

                # -------------------------------------------------------------- #
                # Provider-parameter fix-ups (a 400 that says exactly which
                # parameter is unsupported). They don't consume a retry.
                # -------------------------------------------------------------- #
                if response.status_code == 400 and fixups < 3:
                    fixup_applied = False
                    # Some providers (e.g. z-ai via aggregators) don't support
                    # json_object response_format and 400 mentioning it.
                    if json_mode_active and "response_format" in lowered:
                        log.warning("AI %s: provider rejected json_object response_format, retrying without it", workflow)
                        payload.pop("response_format", None)
                        json_mode_active = False
                        fixup_applied = True
                    # Newer OpenAI models want max_completion_tokens instead of max_tokens.
                    if (not fixup_applied and max_tokens_key in payload
                            and "max_completion_tokens" in lowered and "unsupported" in lowered):
                        log.warning("AI %s: provider wants max_completion_tokens, renaming the parameter", workflow)
                        payload["max_completion_tokens"] = payload.pop(max_tokens_key)
                        max_tokens_key = "max_completion_tokens"
                        fixup_applied = True
                    # Reasoning models also reject temperature != 1.
                    if (not fixup_applied and "temperature" in payload and "temperature" in lowered
                            and ("unsupported" in lowered or "not supported" in lowered)):
                        log.warning("AI %s: provider rejected temperature, retrying without it", workflow)
                        payload.pop("temperature", None)
                        fixup_applied = True
                    # We escalated max_tokens past what the model supports.
                    if (not fixup_applied and max_tokens_key in payload and "max_tokens" in lowered
                            and any(w in lowered for w in ("too large", "too high", "at most", "maximum", "exceeds"))):
                        clamped = max(256, current_max_tokens // 2)
                        if clamped < current_max_tokens:
                            log.warning("AI %s: max_tokens=%d exceeds the model limit, retrying with %d",
                                        workflow, current_max_tokens, clamped)
                            current_max_tokens = clamped
                            payload[max_tokens_key] = clamped
                            fixup_applied = True
                    if fixup_applied:
                        fixups += 1
                        attempt -= 1  # a parameter fix-up is not a retry
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
                    _ledger_failure(last_error, meta={"attempts": attempt})
                    raise AIClientError(last_error, status=response.status_code, retryable=False,
                                        meta={"attempts": attempt})

                breaker.record_failure(last_error)
                _ledger_failure(last_error, meta={"attempts": attempt})
                raise AIClientError(last_error, status=response.status_code, retryable=True,
                                    meta={"attempts": attempt,
                                          "retry_after_hint": retry_hint_for_reason(http_reason, workflow=workflow)})

            # ---------------------------------------------------------------- #
            # 200 — validate the answer instead of trusting it.
            # ---------------------------------------------------------------- #
            breaker.record_success()
            inc("jobhunter_ai_requests_total", workflow=workflow, status="200")
            try:
                data = response.json()
            except ValueError as exc:
                last_error = f"malformed_response: provider returned a non-JSON body ({exc})"
                if plain_retries < 1:
                    plain_retries += 1
                    continue
                breaker.record_failure(last_error)
                _ledger_failure(last_error, meta={"attempts": attempt})
                raise AIClientError(last_error, retryable=True) from exc

            extracted = _extract_content(data)
            content = extracted["content"]
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
                _ledger_failure(last_error, meta={"attempts": attempt})
                raise AIClientError(last_error, retryable=True)

            if not json_mode:
                if content.strip() and finish_reason != "length":
                    text_result = content
                    break
                # Empty or truncated text — same recovery decisions as JSON mode.
                action = _json_retry_action(content, finish_reason,
                                            json_mode_active=False, plain_retries=plain_retries)
            else:
                parsed: Any = None
                if content.strip():
                    parsed = _parse_model_json(content)
                if parsed is None and reasoning.strip():
                    # Reasoning model: the answer (or its final draft) lives in
                    # the reasoning channel while `content` came back empty.
                    parsed = _parse_model_json(reasoning)
                if isinstance(parsed, dict):
                    parsed_result = parsed
                    break
                if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                    # Some models return a list with one object
                    parsed_result = parsed[0]
                    break
                action = _json_retry_action(content, finish_reason,
                                            json_mode_active=json_mode_active,
                                            plain_retries=plain_retries)

            # -------------------------------------------------------------- #
            # Targeted recovery for an empty/invalid answer.
            # -------------------------------------------------------------- #
            if action is not None and attempt >= attempts:
                action = None  # no attempts left to retry with — report precisely

            if action == "raise_max_tokens_big" or action == "raise_max_tokens":
                factor, floor = (4, 4000) if action == "raise_max_tokens_big" else (2, 2400)
                # PR #29 escalation is preserved — but the user's configured
                # output ceiling is the hard stop: it can never be exceeded.
                new_max = min(max(current_max_tokens * factor, floor), output_ceiling)
                if new_max > current_max_tokens:
                    what = "the thinking phase used the whole budget" if not content.strip() \
                        else "the JSON was cut off"
                    log.warning("AI %s: %s at %d tokens (finish_reason=%s) — retrying with %d tokens",
                                workflow, what, current_max_tokens, finish_reason, new_max)
                    current_max_tokens = new_max
                    payload[max_tokens_key] = new_max
                    continue
                action = None  # already at the user's ceiling — retrying cannot help
            elif action == "drop_response_format":
                log.warning("AI %s: model returned an empty answer with response_format=json_object "
                            "— retrying without it", workflow)
                payload.pop("response_format", None)
                json_mode_active = False
                continue
            elif action == "plain_retry":
                log.warning("AI %s: model returned an %s answer (finish_reason=%s) — retrying",
                            workflow, "empty" if not content.strip() else "unparseable",
                            finish_reason)
                plain_retries += 1
                continue

            final_error = _invalid_json_error(content, finish_reason, current_max_tokens)
            break

        if final_error is not None:
            _ledger_failure(str(final_error), meta={"attempts": attempt})
            raise final_error
        if parsed_result is None and text_result is None:
            breaker.record_failure(last_error or "unknown error")
            _ledger_failure(last_error or "unknown error", meta={"attempts": attempt})
            raise AIClientError(last_error or "unknown error", retryable=True,
                                meta={"attempts": attempt,
                                      "retry_after_hint": retry_hint_for_reason(None, workflow=workflow)})

        prompt_tokens = usage_totals["prompt_tokens"]
        completion_tokens = usage_totals["completion_tokens"]
        total_tokens = usage_totals["total_tokens"]
        latency_ms = int((time.perf_counter() - call_started) * 1000)

        _spend_tokens(total_tokens)
        bucket = _usage.setdefault(workflow, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0})
        bucket["calls"] += 1
        bucket["prompt_tokens"] += prompt_tokens
        bucket["completion_tokens"] += completion_tokens
        bucket["total_tokens"] += total_tokens
        set_gauge("jobhunter_ai_tokens_total", bucket["total_tokens"], workflow=workflow)

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
                meta={"attempts": attempt, "timeout_seconds": effective_timeout,
                      "input_truncated": input_truncated, "max_tokens": current_max_tokens,
                      "output_ceiling": output_ceiling},
            )

        if not json_mode:
            return {"content": text_result or "", "raw": data,
                    "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                              "total_tokens": total_tokens}}
        if parsed_result is None:  # pragma: no cover - defensive: failures raise above
            raise AIClientError(last_error or "unknown error", retryable=True)
        return parsed_result
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


async def _ping_with_config(cfg: Dict[str, Any], timeout: int) -> Dict[str, Any]:
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
        async with httpx.AsyncClient(timeout=timeout) as client:
            probe = await client.post(
                f"{base}/chat/completions",
                headers=headers,
                json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
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
