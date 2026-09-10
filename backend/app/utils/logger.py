"""Database-backed log helper (surfaced in the Logs UI) + structured logging."""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.core.logging import get_logger, request_id_var
from app.models.models import ErrorLog

log = get_logger("app.activity")

VALID_LEVELS = ("debug", "info", "warning", "error", "critical")


def log_event(
    db: Session,
    pipeline: str,
    message: str,
    level: str = "info",
    job_id: Optional[int] = None,
    meta: Optional[Dict[str, Any]] = None,
    user_id: Optional[int] = None,
) -> ErrorLog:
    level = level if level in VALID_LEVELS else "info"
    entry = ErrorLog(
        user_id=user_id,
        pipeline=pipeline,
        level=level,
        message=(message or "")[:4000],
        job_id=job_id,
        request_id=request_id_var.get(),
        meta=meta or {},
    )
    db.add(entry)
    db.commit()
    getattr(log, {"warning": "warning"}.get(level, level))(f"[{pipeline}] {message}")
    return entry


def log_error(
    db: Session,
    pipeline: str,
    message: str,
    level: str = "error",
    job_id: Optional[int] = None,
    meta: Optional[Dict[str, Any]] = None,
    user_id: Optional[int] = None,
) -> ErrorLog:
    return log_event(db, pipeline, message, level=level or "error", job_id=job_id, meta=meta, user_id=user_id)


def log_info(
    db: Session,
    pipeline: str,
    message: str,
    job_id: Optional[int] = None,
    meta: Optional[Dict[str, Any]] = None,
    user_id: Optional[int] = None,
) -> ErrorLog:
    return log_event(db, pipeline, message, level="info", job_id=job_id, meta=meta, user_id=user_id)


def log_warning(
    db: Session,
    pipeline: str,
    message: str,
    job_id: Optional[int] = None,
    meta: Optional[Dict[str, Any]] = None,
    user_id: Optional[int] = None,
) -> ErrorLog:
    return log_event(db, pipeline, message, level="warning", job_id=job_id, meta=meta, user_id=user_id)
