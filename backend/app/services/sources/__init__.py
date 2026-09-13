"""
Job source registry.

Only ToS-compliant, documented endpoints are used:

* public, keyless job APIs (Remotive, Arbeitnow, Jobicy, RemoteOK, Himalayas,
  The Muse, We Work Remotely RSS);
* public ATS board APIs (Greenhouse, Lever, Ashby, Workable, SmartRecruiters,
  Workday CXS) for companies the user chooses to follow;
* official partner APIs where the operator holds credentials (Adzuna, Jooble,
  USAJOBS).

LinkedIn / Indeed / Naukri / Instahyre are registered but reported as
``unavailable`` with the reason: they require partner/official API access or a
user-owned authenticated session. The product never scrapes them silently.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.logging import get_logger
from app.core.metrics import inc
from app.services.sources.adapters import ADAPTERS, SOURCE_META, SourceError
from app.services.sources.base import Posting  # noqa: F401  (re-export)

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
        out.append(entry)
    return sorted(out, key=lambda s: (not s["available"], s["id"]))


def available_source_ids() -> List[str]:
    return [s["id"] for s in list_sources() if s["available"]]


def unconfigured_source_ids() -> List[str]:
    """Available adapters that need credentials the operator has not provided."""
    return [s["id"] for s in list_sources() if s["available"] and s.get("requires_key") and not s.get("configured")]


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
        raise SourceError(f"unknown source '{source_id}'")
    if not adapter.available():
        raise SourceError(adapter.unavailable_reason())
    inc("jobhunter_source_fetch_total", source=source_id)
    postings = await adapter.fetch(keywords=keywords, limit=limit, since_hours=since_hours, board_tokens=board_tokens or [])
    inc("jobhunter_source_results_total", source=source_id, value=len(postings))
    return postings


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
    report: Dict[str, Any] = {"requested": requested, "ok": {}, "errors": {}, "skipped": {}, "total": 0}
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
            return source_id, postings, None
        except SourceError as exc:
            return source_id, [], str(exc)
        except asyncio.TimeoutError:
            return source_id, [], "timeout"
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("source %s failed: %s", source_id, exc)
            return source_id, [], f"{type(exc).__name__}: {exc}"

    results = await asyncio.gather(*(run(s) for s in requested))

    merged: List[Posting] = []
    seen: set[str] = set()
    cutoff = datetime.utcnow() - timedelta(hours=max(1, since_hours))
    for source_id, postings, error in results:
        if error:
            report["errors"][source_id] = error
            continue
        kept = 0
        for posting in postings:
            if posting.posted_at and posting.posted_at < cutoff:
                continue
            key = posting.dedupe_key()
            if key in seen:
                continue
            seen.add(key)
            merged.append(posting)
            kept += 1
        report["ok"][source_id] = kept

    merged.sort(key=lambda p: p.posted_at or datetime.min, reverse=True)
    report["total"] = len(merged)
    return merged[:limit], report
