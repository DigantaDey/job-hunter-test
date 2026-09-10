"""
Dependency-free Prometheus-style metrics.

A tiny registry keeps the deployment surface small (no extra wheel to audit) and
the exposition format is exactly the text format Prometheus/OpenTelemetry
collectors already understand.

Two calling styles are supported, both in use in the codebase::

    inc("jobhunter_ai_requests_total", status="ok", workflow="scoring")
    metrics.counter("jobhunter_http_requests_total", {"method": "GET", "status": "200"})

``inc(name, value=N, ...)`` counts by ``N`` (default 1) — the ``value`` keyword is
reserved for the increment so callers can do ``inc("rows_total", value=len(rows))``.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_BUCKETS: Sequence[float] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0,
)

_LOCK = threading.Lock()
_COUNTERS: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = defaultdict(float)
_GAUGES: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = {}
_HISTOGRAMS: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], Dict[str, object]] = {}
_HELP: Dict[str, str] = {
    "jobhunter_info": "Build and environment information.",
    "jobhunter_http_requests_total": "HTTP requests handled, by method, path template and status.",
    "jobhunter_http_request_duration_seconds": "HTTP request latency in seconds.",
    "jobhunter_http_requests_in_progress": "HTTP requests currently being served.",
    "jobhunter_http_errors_total": "Unhandled/validation failures.",
    "jobhunter_rate_limited_total": "Requests rejected by the rate limiter.",
    "jobhunter_jobs_discovered_total": "Jobs persisted from discovery runs.",
    "jobhunter_source_fetch_total": "Job-board adapter fetches that returned a result.",
    "jobhunter_source_results_total": "Postings returned per source.",
    "jobhunter_ai_requests_total": "AI provider calls, by workflow and outcome.",
    "jobhunter_ai_latency_seconds": "AI provider latency in seconds.",
    "jobhunter_ai_tokens_total": "AI tokens consumed, by workflow.",
    "jobhunter_email_send_total": "Outbound email attempts, by outcome.",
    "jobhunter_email_events_total": "Tracked email engagement events.",
    "jobhunter_pipeline_jobs_total": "Pipeline queue transitions, by pipeline and status.",
    "jobhunter_applications_total": "Application pipeline outcomes.",
    "jobhunter_vault_credentials": "Credentials held in the encrypted vault.",
    "jobhunter_db_up": "1 when the database answers a health query.",
}


# --------------------------------------------------------------------------- #
# Core API
# --------------------------------------------------------------------------- #
def _key(name: str, labels: Optional[Dict[str, str]]) -> Tuple[str, Tuple[Tuple[str, str], ...]]:
    return name, tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))


def _render_labels(labels: Optional[Dict[str, str]]) -> str:
    if not labels:
        return ""
    inner = ",".join(
        f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
        for k, v in sorted(labels.items())
    )
    return "{" + inner + "}"


def _split(kw: Dict[str, object]) -> Tuple[Dict[str, str], Optional[float]]:
    """Separate the reserved ``value`` increment from label kwargs."""
    labels = {k: str(v) for k, v in kw.items() if k != "value"}
    amount = kw.get("value")
    return labels, (float(amount) if isinstance(amount, (int, float)) else None)


def counter(name: str, labels: Optional[Dict[str, str]] = None, value: float = 1.0, **kw: object) -> None:
    merged = dict(labels or {})
    extra, amount = _split(kw)
    merged.update(extra)
    with _LOCK:
        _COUNTERS[_key(name, merged)] += float(amount if amount is not None else value)


def inc(name: str, value: float = 1.0, **labels: object) -> None:
    """Increment a counter. ``value`` is the amount, everything else is a label."""
    counter(name, None, value, **labels)


def gauge(name: str, value: float, labels: Optional[Dict[str, str]] = None, **kw: object) -> None:
    merged = dict(labels or {})
    merged.update(_split(kw)[0])
    with _LOCK:
        _GAUGES[_key(name, merged)] = float(value)


def set_gauge(name: str, value: float, **labels: object) -> None:
    gauge(name, value, None, **labels)


def observe(name: str, value: float, labels: Optional[Dict[str, str]] = None,
            buckets: Sequence[float] = DEFAULT_BUCKETS, **kw: object) -> None:
    merged = dict(labels or {})
    merged.update(_split(kw)[0])
    with _LOCK:
        entry = _HISTOGRAMS.setdefault(
            _key(name, merged),
            {"count": 0, "sum": 0.0, "buckets": dict.fromkeys(buckets, 0), "bounds": tuple(buckets)},
        )
        entry["count"] = int(entry["count"]) + 1  # type: ignore[arg-type]
        entry["sum"] = float(entry["sum"]) + float(value)  # type: ignore[arg-type]
        bucket_counts = entry["buckets"]  # type: ignore[assignment]
        for bound in entry["bounds"]:  # type: ignore[union-attr]
            if value <= bound:
                bucket_counts[bound] = bucket_counts.get(bound, 0) + 1


class Timer:
    """``with Timer("jobhunter_ai_latency_seconds", {"workflow": w}): ...``"""

    def __init__(self, name: str, labels: Optional[Dict[str, str]] = None,
                 buckets: Sequence[float] = DEFAULT_BUCKETS) -> None:
        self.name = name
        self.labels = labels
        self.buckets = buckets

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> bool:
        observe(self.name, time.perf_counter() - self._start, self.labels, self.buckets)
        return False


# --------------------------------------------------------------------------- #
# Exposition
# --------------------------------------------------------------------------- #
def render_prometheus() -> str:
    lines: List[str] = []
    with _LOCK:
        emitted: Dict[str, List[str]] = defaultdict(list)
        for (name, labels), value in sorted(_COUNTERS.items()):
            emitted[name].append(f"{name}{_render_labels(dict(labels))} {_format(value)}")
        for (name, labels), value in sorted(_GAUGES.items()):
            emitted[name].append(f"{name}{_render_labels(dict(labels))} {_format(value)}")
        for (name, labels), entry in sorted(_HISTOGRAMS.items()):
            base = dict(labels)
            cumulative = 0.0
            for bound in entry["bounds"]:  # type: ignore[union-attr]
                cumulative += entry["buckets"].get(bound, 0)  # type: ignore[union-attr]
                emitted[name].append(f"{name}_bucket{_render_labels({**base, 'le': str(bound)})} {_format(cumulative)}")
            emitted[name].append(
                f"{name}_bucket{_render_labels({**base, 'le': '+Inf'})} {_format(entry['count'])}")
            emitted[name].append(f"{name}_sum{_render_labels(base)} {_format(entry['sum'])}")
            emitted[name].append(f"{name}_count{_render_labels(base)} {_format(entry['count'])}")

    for name in sorted(emitted):
        if name in _HELP:
            lines.append(f"# HELP {name} {_HELP[name]}")
            metric_type = "histogram" if name in _HISTOGRAM_NAMES else ("gauge" if name.endswith(("_in_progress", "_up", "_info")) else "counter")
            lines.append(f"# TYPE {name} {metric_type}")
        lines.extend(emitted[name])
    lines.append("")
    return "\n".join(lines)


_HISTOGRAM_NAMES = {
    "jobhunter_http_request_duration_seconds",
    "jobhunter_ai_latency_seconds",
    "jobhunter_http_duration_seconds",
}


def snapshot() -> Dict[str, float]:
    """Flat counter/gauge view used by the ops endpoint and tests."""
    with _LOCK:
        out = {f"{name}{_render_labels(dict(labels))}": value for (name, labels), value in _COUNTERS.items()}
        out.update({f"{name}{_render_labels(dict(labels))}": value for (name, labels), value in _GAUGES.items()})
        for (name, labels), entry in _HISTOGRAMS.items():
            out[f"{name}_count{_render_labels(dict(labels))}"] = int(entry["count"])  # type: ignore[arg-type]
            out[f"{name}_sum{_render_labels(dict(labels))}"] = round(float(entry["sum"]), 4)  # type: ignore[arg-type]
    return out


def reset() -> None:
    """Test helper — clears the registry."""
    with _LOCK:
        _COUNTERS.clear()
        _GAUGES.clear()
        _HISTOGRAMS.clear()


def _format(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def label_names(name: str) -> Iterable[str]:  # pragma: no cover - introspection helper
    with _LOCK:
        for (metric, labels) in _COUNTERS:
            if metric == name:
                return [key for key, _ in labels]
    return []
