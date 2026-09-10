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
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc, observe, set_gauge
from app.core.rate_limiter import rate_limiter

log = get_logger("app.ai")


class AIClientError(Exception):
    """Raised when the AI layer cannot produce a result (unconfigured, timeout, HTTP error…)."""

    def __init__(self, message: str, *, status: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


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
}

_workflow_overrides: Dict[str, Dict[str, str]] = {}
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
def set_workflow_overrides(overrides: Dict[str, Dict[str, str]]) -> None:
    for workflow, cfg in (overrides or {}).items():
        if workflow not in WORKFLOWS:
            continue
        if isinstance(cfg, dict):
            cleaned = {k: (str(v) if v else "") for k, v in cfg.items() if k in ("base_url", "api_key", "model")}
            if any(cleaned.values()):
                _workflow_overrides[workflow] = cleaned
            else:
                _workflow_overrides.pop(workflow, None)


def get_workflow_overrides() -> Dict[str, Dict[str, str]]:
    return {k: dict(v) for k, v in _workflow_overrides.items()}


def resolve_config(workflow: Optional[str] = None) -> Dict[str, str]:
    override = _workflow_overrides.get(workflow or "", {}) if workflow else {}
    return {
        "base_url": (override.get("base_url") or settings.ai_base_url).rstrip("/"),
        "api_key": override.get("api_key") or settings.ai_api_key,
        "model": override.get("model") or settings.ai_model,
    }


def is_configured(workflow: Optional[str] = None) -> bool:
    return bool(resolve_config(workflow)["api_key"])


def usage_snapshot() -> Dict[str, Dict[str, int]]:
    return {k: dict(v) for k, v in _usage.items()}


def breaker_snapshot() -> Dict[str, Dict[str, Any]]:
    return {k: v.state() for k, v in _breakers.items()}


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
) -> Dict[str, Any]:
    """Send a chat completion. Raises ``AIClientError`` on failure."""
    cfg = resolve_config(workflow)
    if ai_config:
        cfg = {
            "base_url": (ai_config.get("base_url") or cfg["base_url"]).rstrip("/"),
            "api_key": ai_config.get("api_key") or cfg["api_key"],
            "model": ai_config.get("model") or cfg["model"],
        }
    if not cfg["api_key"]:
        raise AIClientError("no_api_key: AI is not configured (set AI_API_KEY or a per-workflow override)")

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
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: Dict[str, Any] = {"model": cfg["model"], "messages": messages, "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    payload["max_tokens"] = max_tokens or settings.ai_max_output_tokens

    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    url = f"{cfg['base_url']}/chat/completions"
    attempts = max(1, settings.ai_max_retries)
    last_error: Optional[str] = None

    for attempt in range(1, attempts + 1):
        # Never exceed the configured RPM budget, regardless of concurrency.
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
            raise AIClientError(last_error, retryable=True) from exc
        finally:
            observe("jobhunter_ai_latency_seconds", time.perf_counter() - started, workflow=workflow)

        if response.status_code == 200:
            breaker.record_success()
            break

        body = response.text[:300]
        last_error = f"status {response.status_code}: {body}"
        retryable = response.status_code == 429 or 500 <= response.status_code < 600
        inc("jobhunter_ai_requests_total", workflow=workflow, status=str(response.status_code))
        if retryable and attempt < attempts:
            delay = _retry_delay(response, attempt)
            log.warning("AI %s -> %s, retrying in %.1fs", workflow, response.status_code, delay)
            await asyncio.sleep(delay)
            continue
        if not retryable:
            breaker.record_failure(last_error) if response.status_code in (401, 403) else None
            raise AIClientError(last_error, status=response.status_code, retryable=False)

        breaker.record_failure(last_error)
        raise AIClientError(last_error, status=response.status_code, retryable=True)
    else:  # pragma: no cover - loop always breaks or raises
        raise AIClientError(last_error or "unknown error", retryable=True)

    try:
        data = response.json()
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        breaker.record_failure(f"malformed_response: {exc}")
        raise AIClientError(f"malformed_response: {exc}") from exc

    usage = data.get("usage") or {}
    _spend_tokens(int(usage.get("total_tokens") or 0))
    bucket = _usage.setdefault(workflow, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0})
    bucket["calls"] += 1
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if isinstance(usage.get(key), int):
            bucket[key] += usage[key]
    set_gauge("jobhunter_ai_tokens_total", bucket["total_tokens"], workflow=workflow)
    inc("jobhunter_ai_requests_total", workflow=workflow, status="200")

    if not json_mode:
        return {"content": content, "raw": data, "usage": usage}

    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AIClientError(f"invalid_json: {exc}") from exc


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


async def ping(workflow: Optional[str] = None, timeout: int = 5) -> Dict[str, Any]:
    """Health probe for the green/red status dot. Never raises."""
    cfg = resolve_config(workflow)
    if not cfg["api_key"]:
        return {"online": False, "reason": "no_api_key", "latency_ms": None}
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{cfg['base_url']}/models",
                                        headers={"Authorization": f"Bearer {cfg['api_key']}"})
    except httpx.HTTPError as exc:
        return {"online": False, "error": str(exc), "latency_ms": None, "base_url": cfg["base_url"]}
    latency_ms = int((time.perf_counter() - start) * 1000)
    if response.status_code == 200:
        return {"online": True, "latency_ms": latency_ms, "status": 200, "base_url": cfg["base_url"],
                "model": cfg["model"]}
    if response.status_code in (401, 403):
        return {"online": False, "latency_ms": latency_ms, "status": response.status_code,
                "reason": "invalid_api_key", "base_url": cfg["base_url"]}
    return {"online": False, "latency_ms": latency_ms, "status": response.status_code,
            "reason": f"status_{response.status_code}", "base_url": cfg["base_url"]}
