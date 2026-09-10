"""
Unified OpenAI-compatible AI client.

Every AI call in the system is meant to flow through this module so that:

1. Per-workflow API overrides (base_url / api_key / model) are honoured —
   configured in Settings → "Per-workflow AI", persisted in the `ai_workflows`
   settings category and mirrored into this module's in-memory registry.
2. Every call acquires a token from the global `rate_limiter` BEFORE hitting
   the wire, so the system can never exceed the configured requests-per-minute
   budget regardless of how many pipelines are running in parallel.
3. All chat completions use a single, well-formed payload with JSON-mode
   support and consistent error surfacing (`AIClientError`).

Services treat this as the *only* gateway to the AI layer and always keep a
deterministic heuristic fallback for offline / unconfigured runs.
"""
import json
import time
from typing import Any, Dict, Optional

import httpx

from app.core.config import settings
from app.core.rate_limiter import rate_limiter


class AIClientError(Exception):
    """Raised when the AI layer cannot produce a result (unconfigured, timeout, HTTP error…)."""


# Workflow → override dict {base_url, api_key, model}. Populated at startup from
# the DB and kept in sync by PUT /api/ai/config. The default (env) config is
# used for any workflow without an override.
_workflow_overrides: Dict[str, Dict[str, str]] = {}

# Canonical workflow names so overrides + rate-limiting are applied consistently.
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


def set_workflow_overrides(overrides: Dict[str, Dict[str, str]]) -> None:
    """Merge a set of workflow overrides into the in-memory registry."""
    for workflow, cfg in (overrides or {}).items():
        if isinstance(cfg, dict):
            cleaned = {k: (str(v) if v else "") for k, v in cfg.items()}
            if any(cleaned.values()):
                _workflow_overrides[workflow] = cleaned


def get_workflow_overrides() -> Dict[str, Dict[str, str]]:
    return {k: dict(v) for k, v in _workflow_overrides.items()}


def resolve_config(workflow: Optional[str] = None) -> Dict[str, str]:
    """Resolve the effective (base_url, api_key, model) for a workflow."""
    override = _workflow_overrides.get(workflow or "", {}) if workflow else {}
    return {
        "base_url": (override.get("base_url") or settings.ai_base_url).rstrip("/"),
        "api_key": override.get("api_key") or settings.ai_api_key,
        "model": override.get("model") or settings.ai_model,
    }


def is_configured(workflow: Optional[str] = None) -> bool:
    return bool(resolve_config(workflow)["api_key"])


async def chat_completion(
    workflow: str,
    prompt: str,
    *,
    temperature: float = 0.2,
    timeout: Optional[int] = None,
    json_mode: bool = True,
    max_tokens: Optional[int] = None,
    ai_config: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Send a chat completion through the OpenAI-compatible endpoint.

    - Always acquires a rate-limiter token first (never exceeds RPM).
    - Uses the workflow override unless an explicit `ai_config` is supplied.
    - Returns the raw JSON-decoded response dict.
    - Raises AIClientError on any failure (callers fall back to heuristics).
    """
    cfg = resolve_config(workflow)
    if ai_config:
        cfg = {
            "base_url": (ai_config.get("base_url") or cfg["base_url"]).rstrip("/"),
            "api_key": ai_config.get("api_key") or cfg["api_key"],
            "model": ai_config.get("model") or cfg["model"],
        }

    if not cfg["api_key"]:
        raise AIClientError("no_api_key: AI is not configured (add AI_API_KEY or a per-workflow override)")

    # Respect the configured RPM budget before touching the wire.
    await rate_limiter.wait_and_acquire(1)

    payload: Dict[str, Any] = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if max_tokens:
        payload["max_tokens"] = max_tokens

    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout or settings.ai_timeout) as client:
            resp = await client.post(f"{cfg['base_url']}/chat/completions", headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise AIClientError(f"http_error: {exc}") from exc

    if resp.status_code != 200:
        raise AIClientError(f"status {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise AIClientError(f"malformed_response: {exc}") from exc

    if json_mode:
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise AIClientError(f"invalid_json: {exc}") from exc
    return {"content": content, "raw": data}


async def chat_text(
    workflow: str,
    prompt: str,
    *,
    temperature: float = 0.4,
    timeout: Optional[int] = None,
    max_tokens: Optional[int] = None,
    ai_config: Optional[Dict[str, str]] = None,
) -> str:
    """Convenience wrapper returning the plain-text content string."""
    result = await chat_completion(
        workflow, prompt, temperature=temperature, timeout=timeout,
        json_mode=False, max_tokens=max_tokens, ai_config=ai_config,
    )
    return str(result.get("content", ""))


async def ping(
    workflow: Optional[str] = None,
    timeout: int = 5,
) -> Dict[str, Any]:
    """
    Health probe for the AI endpoint (used by the green/red online dot).
    A 200 means online; a reachable-but-unauthorised endpoint is reported as
    offline with a reason. Never raises.
    """
    cfg = resolve_config(workflow)
    if not cfg["api_key"]:
        return {"online": False, "reason": "no_api_key", "latency_ms": None}
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{cfg['base_url']}/models", headers={"Authorization": f"Bearer {cfg['api_key']}"})
        latency_ms = int((time.time() - start) * 1000)
        if resp.status_code == 200:
            return {"online": True, "latency_ms": latency_ms, "status": 200}
        return {"online": False, "latency_ms": latency_ms, "status": resp.status_code,
                "reason": f"status_{resp.status_code}"}
    except httpx.HTTPError as exc:
        return {"online": False, "error": str(exc), "latency_ms": None}
