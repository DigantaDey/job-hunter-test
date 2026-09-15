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

Cardinality
-----------
A label combination is a *series*, and in an in-process registry a series is
permanent once created. Label values therefore have to come from a bounded set —
route templates rather than raw paths, outcome codes rather than exception text,
registrable domains rather than arbitrary hosts — because the alternative is a
caller-controlled key growing this module until the process is OOM-killed (a
``Host`` header, or the host of every URL pulled out of a job feed, is exactly
such a key).

Every metric is additionally capped at ``METRICS_MAX_SERIES_PER_METRIC`` series:
past the cap the least recently updated series is evicted, so a mistake costs
observability (and shows up as ``evictions`` on ``GET /api/ops/status``) rather
than memory. The cap is a backstop, not a licence to label on request data.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.core.config import settings

DEFAULT_BUCKETS: Sequence[float] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0,
)

SeriesKey = Tuple[Tuple[str, str], ...]

_LOCK = threading.Lock()
_COUNTERS: Dict[Tuple[str, SeriesKey], float] = defaultdict(float)
_GAUGES: Dict[Tuple[str, SeriesKey], float] = {}
_HISTOGRAMS: Dict[Tuple[str, SeriesKey], Dict[str, object]] = {}
#: metric name -> its series in least-recently-updated order (the eviction queue).
_RECENCY: Dict[str, "OrderedDict[SeriesKey, None]"] = {}
#: metric name -> series dropped because the cap was reached.
_EVICTED: Dict[str, int] = defaultdict(int)
_HELP: Dict[str, str] = {
    "jobhunter_info": "Build and environment information.",
    "jobhunter_http_requests_total": "HTTP requests handled, by method, path template and status.",
    "jobhunter_http_request_duration_seconds": "HTTP request latency in seconds.",
    "jobhunter_http_requests_in_progress": "HTTP requests currently being served.",
    "jobhunter_http_errors_total": "Unhandled/validation failures.",
    "jobhunter_http_payload_too_large_total": "Requests rejected by the body-size cap, by how it was caught.",
    "jobhunter_rate_limited_total": "Requests rejected by the rate limiter.",
    "jobhunter_edge_forwarded_for_total": "X-Forwarded-For headers seen, by whether the peer was trusted.",
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
def _key(name: str, labels: Optional[Dict[str, str]]) -> Tuple[str, SeriesKey]:
    return name, tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))


def _series_cap() -> int:
    return max(16, int(settings.metrics_max_series_per_metric or 512))


def _touch(name: str, labels: SeriesKey, store: Dict[Any, Any]) -> None:
    """
    Mark a series as just-updated and enforce the per-metric series cap.

    Called under :data:`_LOCK` by every writer. Eviction is LRU by *update*
    time: the series that has not been touched for the longest is the one nobody
    is looking at, which is also the shape a cardinality mistake produces
    (thousands of one-shot label values, each touched once).
    """
    recent = _RECENCY.get(name)
    if recent is None:
        recent = _RECENCY[name] = OrderedDict()
    recent[labels] = None
    recent.move_to_end(labels)
    cap = _series_cap()
    while len(recent) > cap:
        stale, _unused = recent.popitem(last=False)
        if store.pop((name, stale), None) is not None:
            _EVICTED[name] += 1


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
        key, series = _key(name, merged)
        _COUNTERS[(key, series)] += float(amount if amount is not None else value)
        _touch(key, series, _COUNTERS)


def inc(name: str, value: float = 1.0, **labels: object) -> None:
    """Increment a counter. ``value`` is the amount, everything else is a label."""
    counter(name, None, value, **labels)


def gauge(name: str, value: float, labels: Optional[Dict[str, str]] = None, **kw: object) -> None:
    merged = dict(labels or {})
    merged.update(_split(kw)[0])
    with _LOCK:
        key, series = _key(name, merged)
        _GAUGES[(key, series)] = float(value)
        _touch(key, series, _GAUGES)


def set_gauge(name: str, value: float, **labels: object) -> None:
    gauge(name, value, None, **labels)


def observe(name: str, value: float, labels: Optional[Dict[str, str]] = None,
            buckets: Sequence[float] = DEFAULT_BUCKETS, **kw: object) -> None:
    merged = dict(labels or {})
    merged.update(_split(kw)[0])
    with _LOCK:
        key, series = _key(name, merged)
        entry = _HISTOGRAMS.setdefault(
            (key, series),
            {"count": 0, "sum": 0.0, "buckets": dict.fromkeys(buckets, 0), "bounds": tuple(buckets)},
        )
        _touch(key, series, _HISTOGRAMS)
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
def escape_label_value(value: str) -> str:
    """Prometheus exposition escaping for a label value (see the text format spec)."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def metrics_text(*, version: str = "", environment: str = "") -> str:
    """
    The full exposition payload: every series plus the build-info gauge.

    Shared by the public route and the internal metrics listener so both emit
    byte-identical output.
    """
    payload = render_prometheus()
    labels = f'version="{escape_label_value(version)}",environment="{escape_label_value(environment)}"'
    payload += "# HELP jobhunter_info Build and environment information.\n"
    payload += "# TYPE jobhunter_info gauge\n"
    payload += f"jobhunter_info{{{labels}}} 1\n"
    return payload


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
        _RECENCY.clear()
        _EVICTED.clear()


def series_count(name: Optional[str] = None) -> int:
    """Live series across the registry, or for one metric name."""
    with _LOCK:
        if name is None:
            return sum(len(series) for series in _RECENCY.values())
        return len(_RECENCY.get(name) or ())


def stats() -> Dict[str, Any]:
    """
    Registry shape for ops: series per metric, and what the cap has evicted.

    ``series`` growing toward ``max_series_per_metric`` with a rising ``evicted``
    means some caller is labelling on unbounded data (a host header, a raw path) —
    visible before it becomes an incident.
    """
    cap = _series_cap()
    with _LOCK:
        per_metric = {
            metric: {"series": len(series), "evicted": int(_EVICTED.get(metric, 0))}
            for metric, series in sorted(_RECENCY.items())
        }
        total = sum(entry["series"] for entry in per_metric.values())
        evicted = sum(entry["evicted"] for entry in per_metric.values())
    return {
        "series": total,
        "evicted": evicted,
        "max_series_per_metric": cap,
        "metrics": len(per_metric),
        "by_metric": per_metric,
    }


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
