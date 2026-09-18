"""
Job source registry.

Only ToS-compliant, documented endpoints are used:

* public, keyless job APIs (Remotive, Arbeitnow, Jobicy, RemoteOK, Himalayas,
  The Muse, We Work Remotely RSS);
* public ATS board APIs (Greenhouse, Lever, Ashby, Workable, Recruitee,
  SmartRecruiters, Personio, Workday CXS) for companies the user chooses to
  follow;
* official partner APIs where the operator holds credentials (Adzuna, Jooble,
  USAJOBS).

LinkedIn / Indeed / Naukri / Instahyre are registered but reported as
``unavailable`` with the reason: they require partner/official API access or a
user-owned authenticated session. The product never scrapes them silently.

Every adapter is reached through the :class:`~app.services.sources.base.Source`
interface (identity, capabilities, fetch/pagination, rate limits, retry,
freshness, canonicalization, error classification, health). A failing source
never stops the others.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.logging import get_logger
from app.core.metrics import inc
from app.services.sources import health as source_health
from app.services.sources.adapters import ADAPTERS, SOURCE_META, SourceError
from app.services.sources.base import (  # noqa: F401  (re-export)
    ADAPTER_VERSION,
    FetchPage,
    FetchRequest,
    Posting,
    Source,
    classify_error,
    merge_postings,
)
from app.services.sources.throttle import throttle

log = get_logger("app.sources")


def list_sources() -> List[Dict[str, Any]]:
    """Metadata for every known source (used by the Settings/Jobs UI)."""
    out = []
    for source_id, meta in SOURCE_META.items():
        entry = dict(meta)
        entry["id"] = source_id
        adapter = ADAPTERS.get(source_id)
        entry["available"] = bool(adapter and adapter.available())
        if not entry["available"]:
            entry["unavailable_reason"] = adapter.unavailable_reason() if adapter else "not implemented"
        entry["health"] = source_health.snapshot(source_id)
        out.append(entry)
    return sorted(out, key=lambda s: (not s["available"], s["id"]))


def available_source_ids() -> List[str]:
    return [s["id"] for s in list_sources() if s["available"]]


def unconfigured_source_ids() -> List[str]:
    """Available adapters that need credentials the operator has not provided."""
    return [s["id"] for s in list_sources() if s["available"] and s.get("requires_key") and not s.get("configured")]


async def _fetch_with_retry(adapter: Source, *, keywords, limit, since_hours, board_tokens) -> List[Posting]:
    """Apply the adapter's retry policy around ``fetch``. Gated/auth errors never retry."""
    policy = adapter.retry_policy
    attempts = max(1, int(policy.max_attempts or 1))
    last_error: Optional[SourceError] = None
    for attempt in range(1, attempts + 1):
        try:
            await throttle.acquire_source(adapter)
            postings = await adapter.fetch(
                keywords=keywords, limit=limit, since_hours=since_hours, board_tokens=board_tokens,
            )
            return [adapter.canonicalize(p) for p in (postings or [])]
        except asyncio.CancelledError:  # pragma: no cover - cooperative cancel
            raise
        except Exception as exc:
            classified = adapter.classify_error(exc)
            last_error = classified
            retryable = classified.retryable and classified.code in policy.retry_on
            if not retryable or attempt >= attempts:
                raise classified from exc
            delay = min(8.0, float(policy.backoff_base or 1.5) ** attempt)
            log.debug("source %s attempt %s/%s failed (%s); retrying in %.1fs",
                      adapter.id, attempt, attempts, classified.code, delay)
            await asyncio.sleep(delay)
    raise last_error or SourceError("fetch failed", code="unknown")


async def fetch_from_source(
    source_id: str,
    keywords: List[str],
    *,
    limit: int = 15,
    since_hours: int = 24 * 7,
    board_tokens: Optional[List[str]] = None,
) -> List[Posting]:
    adapter = ADAPTERS.get(source_id)
    if not adapter:
        raise SourceError(f"unknown source '{source_id}'", code="not_found")
    if not adapter.available():
        raise SourceError(adapter.unavailable_reason(), code="gated" if adapter.kind == "gated" else "unavailable")
    inc("jobhunter_source_fetch_total", source=source_id)
    started = time.perf_counter()
    try:
        postings = await _fetch_with_retry(
            adapter, keywords=keywords, limit=limit, since_hours=since_hours,
            board_tokens=board_tokens or [],
        )
    except SourceError as exc:
        latency = (time.perf_counter() - started) * 1000.0
        source_health.record_failure(source_id, exc, latency_ms=round(latency, 1))
        raise
    except Exception as exc:  # pragma: no cover - defensive
        classified = classify_error(exc)
        latency = (time.perf_counter() - started) * 1000.0
        source_health.record_failure(source_id, classified, latency_ms=round(latency, 1))
        raise classified from exc
    latency = (time.perf_counter() - started) * 1000.0
    source_health.record_success(source_id, results=len(postings), latency_ms=round(latency, 1))
    inc("jobhunter_source_results_total", source=source_id, value=len(postings))
    return postings


def _merge_by_canonical(postings: List[Posting]) -> Tuple[List[Posting], int]:
    """Dedupe a batch by canonical job identity; merge collisions instead of dropping."""
    by_key: Dict[str, Posting] = {}
    by_hash: Dict[str, str] = {}
    merged = 0
    for posting in postings:
        posting.ensure_normalized()
        key = posting.canonical_id()
        existing = by_key.get(key)
        if existing is None and posting.content_hash:
            alias = by_hash.get(posting.content_hash)
            if alias:
                existing = by_key.get(alias)
        if existing is None:
            by_key[key] = posting
            if posting.content_hash:
                by_hash.setdefault(posting.content_hash, key)
            continue
        merge_postings(existing, posting)
        merged += 1
    return list(by_key.values()), merged


async def fetch_all(
    keywords: List[str],
    *,
    limit: int = 40,
    since_hours: int = 24 * 7,
    sources: Optional[List[str]] = None,
    board_tokens: Optional[List[str]] = None,
    timeout_seconds: float = 45.0,
) -> Tuple[List[Posting], Dict[str, Any]]:
    """
    Fan out across sources concurrently. Individual source failures degrade
    gracefully and are reported (never raised) so discovery always returns
    whatever succeeded.

    ``sources`` distinguishes *unset* from *empty*, and the distinction is the
    caller's meaning, not this function's to reinterpret:

    * ``None`` — the caller has no opinion, so every available adapter is
      fetched (the historical default);
    * ``[]`` — the caller deliberately selected no sources, so **nothing** is
      fetched and the report says ``requested: []``.

    The old ``sources or available_source_ids()`` collapsed the two, so a user
    who had saved an explicitly empty source list still got every available
    adapter fetched — and discovery could then never report
    ``no_sources_configured`` honestly. The report's ``requested`` list is what
    :func:`app.services.discovery._classify_empty_run` reads, so swallowing the
    distinction here would have made an empty board look like a quiet market.
    """
    selected = available_source_ids() if sources is None else sources
    requested = [s for s in selected if s in ADAPTERS]
    report: Dict[str, Any] = {
        "requested": requested, "ok": {}, "errors": {}, "skipped": {},
        "error_codes": {}, "merged": 0, "expired": 0, "total": 0,
    }
    if not requested:
        # Same shape as the fan-out below (``total`` included) so a reader — the
        # discovery report's ``summary.fetched`` — never has to special-case the
        # "nothing was attempted" run.
        return [], report

    per_source_limit = max(4, min(25, limit // max(1, len(requested)) + 5))

    async def run(source_id: str):
        try:
            postings = await asyncio.wait_for(
                fetch_from_source(source_id, keywords, limit=per_source_limit,
                                  since_hours=since_hours, board_tokens=board_tokens),
                timeout=timeout_seconds,
            )
            return source_id, postings, None, None
        except SourceError as exc:
            return source_id, [], str(exc), exc.code
        except asyncio.TimeoutError:
            return source_id, [], "timeout", "timeout"
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("source %s failed: %s", source_id, exc)
            classified = classify_error(exc)
            return source_id, [], f"{type(exc).__name__}: {exc}", classified.code

    results = await asyncio.gather(*(run(s) for s in requested), return_exceptions=True)

    collected: List[Posting] = []
    cutoff = datetime.utcnow() - timedelta(hours=max(1, since_hours))
    now = datetime.utcnow()
    for result in results:
        if isinstance(result, BaseException):  # pragma: no cover - gather-level
            log.warning("source fan-out task failed: %s", result)
            continue
        source_id, postings, error, error_code = result
        if error:
            report["errors"][source_id] = error
            if error_code:
                report["error_codes"][source_id] = error_code
            continue
        kept = 0
        for posting in postings:
            if posting.is_expired(now):
                report["expired"] += 1
                continue
            if posting.posted_at and posting.posted_at < cutoff:
                continue
            collected.append(posting)
            kept += 1
        report["ok"][source_id] = kept

    merged, merge_count = _merge_by_canonical(collected)
    report["merged"] = merge_count
    merged.sort(key=lambda p: p.posted_at or datetime.min, reverse=True)
    report["total"] = len(merged)
    return merged[:limit], report
