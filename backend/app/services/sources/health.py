"""In-process source health.

Keyed by adapter id (a closed set), so a flapping host in a job feed cannot
grow this map. Snapshots are what ``list_sources()`` and the discovery report
surface — never a guess, never "probably fine".
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional

from app.services.sources.base import SourceError, SourceHealth

_HEALTH: Dict[str, SourceHealth] = {}


def _row(source_id: str) -> SourceHealth:
    row = _HEALTH.get(source_id)
    if row is None:
        row = SourceHealth(source_id=source_id)
        _HEALTH[source_id] = row
    return row


def record_success(source_id: str, *, results: int = 0, latency_ms: Optional[float] = None) -> SourceHealth:
    row = _row(source_id)
    row.last_success_at = datetime.utcnow()
    row.consecutive_failures = 0
    row.last_error_code = None
    row.last_error = None
    row.results_last_fetch = int(results)
    row.fetches += 1
    row.latency_ms = latency_ms
    row.status = "healthy"
    return row


def record_failure(source_id: str, error: SourceError, *, latency_ms: Optional[float] = None) -> SourceHealth:
    row = _row(source_id)
    row.last_failure_at = datetime.utcnow()
    row.consecutive_failures += 1
    row.last_error_code = error.code
    row.last_error = str(error)[:300]
    row.fetches += 1
    row.latency_ms = latency_ms
    if error.code == "gated":
        row.status = "gated"
    elif error.code in {"unavailable", "auth"}:
        row.status = "unconfigured" if error.code == "unavailable" else "down"
    elif row.consecutive_failures >= 3:
        row.status = "down"
    else:
        row.status = "degraded"
    return row


def snapshot(source_id: str) -> Dict:
    return _row(source_id).to_dict()


def all_snapshots() -> Dict[str, Dict]:
    return {source_id: row.to_dict() for source_id, row in _HEALTH.items()}


def reset() -> None:
    _HEALTH.clear()


__all__ = ["record_success", "record_failure", "snapshot", "all_snapshots", "reset"]
