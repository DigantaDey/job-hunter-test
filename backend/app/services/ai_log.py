"""Owner-only AI observability: the exact requests sent, and how Laya is doing.

Two questions an operator asks about a deployment whose answers look wrong,
slow or expensive, and neither is answerable from the aggregate ledger alone:

* **"What exactly did we send?"** — :func:`record_call` stores the outbound
  request bodies per attempt (plus the parameters, the endpoint, the outcome
  and a bounded excerpt of the answer) as
  :class:`~app.models.models.AICallRecord`. The ledger already counts tokens and
  cost per workflow; this is the *content*, which is what diagnoses a bad
  verdict, a guardrail rejection or a truncation after the fact.
* **"How is the local engine doing?"** — :func:`record_laya_decision` stores one
  row per forward pass as :class:`~app.models.models.LayaDecision`: task,
  status (``ok`` / ``low_confidence`` / ``timeout`` / ``error`` / ``parked``),
  checkpoint, question count, calibrated confidence against the configured
  floor, and latency. The counters were already there; this is the history.

Four rules are deliberate:

* **Owner-only, and nothing else reads it.** The only readers are the
  ``/api/admin/*`` routes (router-level ``require_owner``); there is no
  user-facing endpoint and no export.
* **Bounded twice.** Retention (``AI_LOG_RETENTION_DAYS``) deletes old rows, and
  one pruning pass per :data:`_PRUNE_INTERVAL_SECONDS` at most keeps the cost off
  the call path; prompt text is clipped to ``AI_LOG_PROMPT_CHARS`` and answers
  to ``AI_LOG_RESPONSE_CHARS``, so a 100k-character resume cannot turn the log
  into the largest table in the database.
* **Never a secret, never a header.** Only request *bodies* are stored, and they
  are pushed through the AI gateway's own secret scrubber on the way in; auth
  headers are not captured at all.
* **Never in the way.** Every write is wrapped: an observability table that can
  fail an AI call is worse than no observability. Logging is off with
  ``AI_LOG_ENABLED=false``.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import AICallRecord, LayaDecision

log = get_logger("app.ai_log")

#: The log is written on every AI call, so pruning is rate-limited by wall clock
#: rather than run on each insert: one sweep every this many seconds (per
#: process) is enough to keep the tables bounded without adding a DELETE to the
#: hot path of every verdict.
_PRUNE_INTERVAL_SECONDS = 300.0
#: ``None`` — not ``0.0`` — is the "never swept" sentinel. ``time.monotonic()``
#: counts from boot, so a numeric zero means "300 s ago" only on a machine that
#: has been up longer than the interval: on a freshly booted container (CI's
#: runners, a new pod) ``now - 0.0`` is *smaller* than the interval and the first
#: sweep would be skipped until uptime passed it. Retention must not depend on
#: how long the host has been up.
_last_prune_at: Optional[float] = None

#: Status vocabulary for a stored call.
STATUS_OK = "ok"
STATUS_ERROR = "error"

#: Laya statuses (a superset — see the module docstring).
LAYA_OK = "ok"
LAYA_LOW_CONFIDENCE = "low_confidence"
LAYA_TIMEOUT = "timeout"
LAYA_ERROR = "error"
LAYA_PARKED = "parked"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    """Whether calls/decisions are being recorded at all."""
    return bool(getattr(settings, "ai_log_enabled", True))


def retention_days() -> int:
    try:
        return max(1, int(getattr(settings, "ai_log_retention_days", 7) or 7))
    except (TypeError, ValueError):
        return 7


def _prompt_chars() -> int:
    try:
        return max(1000, int(getattr(settings, "ai_log_prompt_chars", 20000) or 20000))
    except (TypeError, ValueError):
        return 20000


def _response_chars() -> int:
    try:
        return max(200, int(getattr(settings, "ai_log_response_chars", 2000) or 2000))
    except (TypeError, ValueError):
        return 2000


def _prune_batch() -> int:
    try:
        return max(50, int(getattr(settings, "ai_log_prune_batch", 500) or 500))
    except (TypeError, ValueError):
        return 500


def _scrub(text: str) -> str:
    """Run the gateway's own secret scrubber over text bound for the log.

    Imported lazily: ``ai_client`` imports this module, and the scrubber lives
    there because that is where the pattern set is maintained.
    """
    if not text:
        return text
    try:
        from app.services.ai_client import _scrub_prompt_secrets  # noqa: PLC0415

        return _scrub_prompt_secrets(text)
    except Exception:  # pragma: no cover - scrubber unavailable must not block logging
        return text


def _scrub_tree(value: Any) -> Any:
    """Apply the secret scrubber to every string inside a JSON structure.

    Scrubbing the serialised form would also work, but it would hand back a
    *string* where the caller stored (and readers expect) a dict; walking the
    structure keeps the request body's shape while still guaranteeing no
    secret-shaped value survives.
    """
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, Mapping):
        return {str(key): _scrub_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_tree(item) for item in value]
    return value


def _clip(value: Any, limit: int) -> Any:
    """Keep a JSON structure if it fits, else a clipped serialised preview.

    Truncation is reported in the payload itself (``_truncated`` /
    ``_original_chars``) so a reader is never misled into thinking a clipped
    prompt was the whole prompt.
    """
    scrubbed = _scrub_tree(value)
    try:
        serialised = json.dumps(scrubbed, default=str, ensure_ascii=False)
    except Exception:  # pragma: no cover - unserialisable payload
        serialised = _scrub(str(value))
    if len(serialised) <= limit:
        if isinstance(scrubbed, (dict, list)):
            return scrubbed
        try:
            return json.loads(serialised)
        except Exception:  # pragma: no cover
            return {"_text": serialised}
    return {"_truncated": True, "_original_chars": len(serialised), "preview": serialised[:limit]}


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
def record_call(
    db: Optional[Session],
    *,
    user_id: Optional[int],
    workflow: str,
    status: str,
    model: str = "",
    provider: str = "",
    base_url: str = "",
    reason: str = "",
    http_status: Optional[int] = None,
    attempts: int = 1,
    latency_ms: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    estimated_cost_usd: float = 0.0,
    request: Optional[Dict[str, Any]] = None,
    response: str = "",
    error: str = "",
) -> None:
    """Store one provider call. Never raises, never blocks the call it describes."""
    if not enabled() or db is None or user_id is None:
        return
    try:
        payload: Dict[str, Any] = {}
        if request:
            payload = {
                "url": str(request.get("url") or "")[:300],
                # The parameters as they were for the *last* attempt; the
                # per-attempt bodies below show what an escalation changed.
                # Scrubbed like the bodies: parameters are routing data today,
                # but nothing reaches this table unexamined.
                "params": _scrub_tree(dict(request.get("params") or {})),
                "attempts": [
                    {"attempt": int(item.get("attempt") or index + 1),
                     "body": _clip(item.get("body"), _prompt_chars())}
                    for index, item in enumerate(request.get("attempts") or [])
                ],
            }
        row = AICallRecord(
            user_id=int(user_id),
            workflow=str(workflow or "")[:40],
            provider=str(provider or "")[:32],
            model=str(model or "")[:120],
            base_url=str(base_url or "")[:300],
            status=STATUS_OK if status == STATUS_OK else STATUS_ERROR,
            reason=str(reason or "")[:60],
            http_status=http_status,
            attempts=max(1, int(attempts or 1)),
            latency_ms=max(0, int(latency_ms or 0)),
            prompt_tokens=max(0, int(prompt_tokens or 0)),
            completion_tokens=max(0, int(completion_tokens or 0)),
            total_tokens=max(0, int(total_tokens or 0)),
            estimated_cost_usd=float(estimated_cost_usd or 0.0),
            request=payload,
            response=_scrub(str(response or "")[:_response_chars()]),
            error=_scrub(str(error or "")[:2000]),
            created_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - observability never fails the call
        try:
            db.rollback()
        except Exception:  # pragma: no cover
            pass
        log.debug("ai_log: could not store the call record: %s", exc)
        return
    maybe_prune(db)


def record_laya_decision(
    db: Optional[Session],
    *,
    user_id: Optional[int],
    task: str,
    status: str,
    model: str = "",
    questions: int = 0,
    confidence: Optional[float] = None,
    floor: Optional[float] = None,
    strict: bool = False,
    latency_ms: int = 0,
    answers: Optional[Dict[str, Any]] = None,
    error: str = "",
) -> None:
    """Store one local-engine forward pass. Never raises."""
    if not enabled() or db is None or user_id is None:
        return
    try:
        row = LayaDecision(
            user_id=int(user_id),
            task=str(task or "")[:32],
            status=str(status or LAYA_OK)[:24],
            model=str(model or "")[:48],
            questions=max(0, int(questions or 0)),
            confidence=None if confidence is None else max(0.0, min(1.0, float(confidence))),
            floor=None if floor is None else max(0.0, min(1.0, float(floor))),
            strict=bool(strict),
            latency_ms=max(0, int(latency_ms or 0)),
            answers=_clip(dict(answers or {}), _prompt_chars()),
            error=_scrub(str(error or "")[:2000]),
            created_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # pragma: no cover
            pass
        log.debug("ai_log: could not store the laya decision: %s", exc)
        return
    maybe_prune(db)


def maybe_prune(db: Session) -> int:
    """Rate-limited retention sweep; returns rows removed (0 when skipped)."""
    global _last_prune_at
    now = time.monotonic()
    if _last_prune_at is not None and now - _last_prune_at < _PRUNE_INTERVAL_SECONDS:
        return 0
    _last_prune_at = now
    try:
        return int(prune(db).get("total") or 0)
    except Exception as exc:  # noqa: BLE001 - retention is best-effort here
        log.debug("ai_log: prune skipped: %s", exc)
        return 0


def reset_prune_clock() -> None:
    """Test/CLI helper — the next write prunes immediately."""
    global _last_prune_at
    _last_prune_at = None


def prune(db: Session, *, now: Optional[datetime] = None, batch: Optional[int] = None,
          commit: bool = True) -> Dict[str, Any]:
    """Delete rows past ``AI_LOG_RETENTION_DAYS`` (bounded per call, batched)."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=retention_days())
    limit = int(batch or _prune_batch())
    removed: Dict[str, int] = {}
    for name, model in (("calls", AICallRecord), ("laya", LayaDecision)):
        ids = [int(row[0]) for row in
               db.query(model.id).filter(model.created_at < cutoff).limit(limit).all()]
        if ids:
            db.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)
        removed[name] = len(ids)
    if commit:
        db.commit()
    removed["total"] = removed["calls"] + removed["laya"]
    return removed


# --------------------------------------------------------------------------- #
# Readers (owner console)
# --------------------------------------------------------------------------- #
def _call_view(row: AICallRecord, *, include_request: bool = False) -> Dict[str, Any]:
    request = row.request if isinstance(row.request, dict) else {}
    attempts = request.get("attempts") or []
    preview = ""
    if attempts:
        body = (attempts[-1] or {}).get("body")
        if isinstance(body, dict) and not body.get("_truncated"):
            messages = body.get("messages") or body.get("contents") or []
            if isinstance(messages, Sequence):
                for message in reversed(list(messages)):
                    if isinstance(message, dict):
                        text = message.get("content")
                        if isinstance(text, str) and text.strip():
                            preview = text.strip()[:400]
                            break
        elif isinstance(body, dict):
            preview = str(body.get("preview") or "")[:400]
    view: Dict[str, Any] = {
        "id": int(row.id),
        "user_id": int(row.user_id),
        "workflow": row.workflow,
        "provider": row.provider or "",
        "model": row.model or "",
        "base_url": row.base_url or "",
        "status": row.status,
        "reason": row.reason or "",
        "http_status": row.http_status,
        "attempts": int(row.attempts or 0),
        "latency_ms": int(row.latency_ms or 0),
        "prompt_tokens": int(row.prompt_tokens or 0),
        "completion_tokens": int(row.completion_tokens or 0),
        "total_tokens": int(row.total_tokens or 0),
        "estimated_cost_usd": round(float(row.estimated_cost_usd or 0.0), 6),
        "error": row.error or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "request_url": str(request.get("url") or ""),
        "request_params": dict(request.get("params") or {}),
        "attempt_count_sent": len(attempts),
        "preview": preview,
    }
    if include_request:
        view["request"] = request
        view["response"] = row.response or ""
    return view


def recent_calls(db: Session, *, limit: int = 50, workflow: Optional[str] = None,
                 status: Optional[str] = None, user_id: Optional[int] = None,
                 before_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Newest-first page of call records (metadata only — no request bodies)."""
    limit = max(1, min(int(limit or 50), 200))
    query = db.query(AICallRecord)
    if workflow:
        query = query.filter(AICallRecord.workflow == str(workflow)[:40])
    if status:
        query = query.filter(AICallRecord.status == str(status)[:16])
    if user_id is not None:
        query = query.filter(AICallRecord.user_id == int(user_id))
    if before_id is not None:
        query = query.filter(AICallRecord.id < int(before_id))
    rows = query.order_by(AICallRecord.id.desc()).limit(limit).all()
    return [_call_view(row) for row in rows]


def get_call(db: Session, record_id: int) -> Optional[Dict[str, Any]]:
    """One record **with** the exact request bodies and the response excerpt."""
    row = db.query(AICallRecord).filter(AICallRecord.id == int(record_id)).first()
    return _call_view(row, include_request=True) if row is not None else None


def call_stats(db: Session, *, window_hours: int = 24) -> Dict[str, Any]:
    """Aggregate view of the log: volume, failures, tokens, cost, latency."""
    since = datetime.utcnow() - timedelta(hours=max(1, int(window_hours)))
    rows = (
        db.query(
            AICallRecord.status,
            func.count(AICallRecord.id),
            func.coalesce(func.sum(AICallRecord.total_tokens), 0),
            func.coalesce(func.sum(AICallRecord.estimated_cost_usd), 0.0),
            func.coalesce(func.avg(AICallRecord.latency_ms), 0.0),
            func.coalesce(func.max(AICallRecord.latency_ms), 0),
        )
        .filter(AICallRecord.created_at >= since)
        .group_by(AICallRecord.status)
        .all()
    )
    totals = {"calls": 0, "errors": 0, "tokens": 0, "cost_usd": 0.0,
              "avg_latency_ms": 0.0, "max_latency_ms": 0}
    for status, count, tokens, cost, avg_latency, max_latency in rows:
        totals["calls"] += int(count)
        totals["tokens"] += int(tokens)
        totals["cost_usd"] += float(cost)
        totals["max_latency_ms"] = max(totals["max_latency_ms"], int(max_latency or 0))
        if status != STATUS_OK:
            totals["errors"] += int(count)
        totals["avg_latency_ms"] += float(avg_latency or 0.0) * int(count)
    if totals["calls"]:
        totals["avg_latency_ms"] = round(totals["avg_latency_ms"] / totals["calls"], 1)
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    by_workflow = (
        db.query(
            AICallRecord.workflow,
            func.count(AICallRecord.id),
            func.coalesce(func.sum(AICallRecord.total_tokens), 0),
            func.coalesce(func.sum(AICallRecord.estimated_cost_usd), 0.0),
        )
        .filter(AICallRecord.created_at >= since)
        .group_by(AICallRecord.workflow)
        .order_by(func.count(AICallRecord.id).desc())
        .limit(25)
        .all()
    )
    by_reason = (
        db.query(AICallRecord.reason, func.count(AICallRecord.id))
        .filter(AICallRecord.created_at >= since, AICallRecord.status != STATUS_OK)
        .group_by(AICallRecord.reason)
        .order_by(func.count(AICallRecord.id).desc())
        .limit(25)
        .all()
    )
    return {
        "window_hours": int(window_hours),
        "enabled": enabled(),
        "retention_days": retention_days(),
        "totals": totals,
        "by_workflow": [{"workflow": wf or "-", "calls": int(n), "tokens": int(t),
                         "cost_usd": round(float(c), 6)} for wf, n, t, c in by_workflow],
        "failures_by_reason": [{"reason": reason or "-", "calls": int(n)}
                               for reason, n in by_reason],
        "stored": int(db.query(func.count(AICallRecord.id)).scalar() or 0),
    }


def _decision_view(row: LayaDecision) -> Dict[str, Any]:
    return {
        "id": int(row.id),
        "user_id": int(row.user_id),
        "task": row.task,
        "status": row.status,
        "model": row.model or "",
        "questions": int(row.questions or 0),
        "confidence": None if row.confidence is None else round(float(row.confidence), 3),
        "floor": None if row.floor is None else round(float(row.floor), 3),
        "strict": bool(row.strict),
        "latency_ms": int(row.latency_ms or 0),
        "answers": row.answers if isinstance(row.answers, dict) else {},
        "error": row.error or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def recent_laya_decisions(db: Session, *, limit: int = 50, task: Optional[str] = None,
                          status: Optional[str] = None, user_id: Optional[int] = None,
                          before_id: Optional[int] = None) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 200))
    query = db.query(LayaDecision)
    if task:
        query = query.filter(LayaDecision.task == str(task)[:32])
    if status:
        query = query.filter(LayaDecision.status == str(status)[:24])
    if user_id is not None:
        query = query.filter(LayaDecision.user_id == int(user_id))
    if before_id is not None:
        query = query.filter(LayaDecision.id < int(before_id))
    rows = query.order_by(LayaDecision.id.desc()).limit(limit).all()
    return [_decision_view(row) for row in rows]


def laya_stats(db: Session, *, window_hours: int = 24) -> Dict[str, Any]:
    """The "how is the engine doing" rollup: per-task status mix + latency."""
    since = datetime.utcnow() - timedelta(hours=max(1, int(window_hours)))
    rows = (
        db.query(
            LayaDecision.task,
            LayaDecision.status,
            func.count(LayaDecision.id),
            func.coalesce(func.avg(LayaDecision.latency_ms), 0.0),
            func.coalesce(func.avg(LayaDecision.confidence), 0.0),
        )
        .filter(LayaDecision.created_at >= since)
        .group_by(LayaDecision.task, LayaDecision.status)
        .all()
    )
    by_task: Dict[str, Dict[str, Any]] = {}
    totals = {"decisions": 0, "ok": 0, "low_confidence": 0, "timeout": 0, "error": 0,
              "parked": 0, "avg_latency_ms": 0.0, "avg_confidence": 0.0}
    latency_weight = 0.0
    confidence_sum = 0.0
    confidence_count = 0
    for task, status, count, avg_latency, avg_confidence in rows:
        task_key = task or "-"
        bucket = by_task.setdefault(task_key, {"task": task_key, "total": 0, "statuses": {},
                                              "avg_latency_ms": 0.0})
        count = int(count)
        bucket["total"] += count
        bucket["statuses"][status] = bucket["statuses"].get(status, 0) + count
        bucket["avg_latency_ms"] += float(avg_latency or 0.0) * count
        totals["decisions"] += count
        if status in totals:
            totals[status] += count
        latency_weight += float(avg_latency or 0.0) * count
        if status == LAYA_OK and avg_confidence:
            confidence_sum += float(avg_confidence) * count
            confidence_count += count
    for bucket in by_task.values():
        if bucket["total"]:
            bucket["avg_latency_ms"] = round(bucket["avg_latency_ms"] / bucket["total"], 1)
    if totals["decisions"]:
        totals["avg_latency_ms"] = round(latency_weight / totals["decisions"], 1)
    if confidence_count:
        totals["avg_confidence"] = round(confidence_sum / confidence_count, 3)
    return {
        "window_hours": int(window_hours),
        "enabled": enabled(),
        "retention_days": retention_days(),
        "totals": totals,
        "by_task": sorted(by_task.values(), key=lambda item: item["total"], reverse=True),
        "stored": int(db.query(func.count(LayaDecision.id)).scalar() or 0),
    }
