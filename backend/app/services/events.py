"""Job/email event timeline helpers (append-only, tenant-scoped)."""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.metrics import inc
from app.models.models import EmailEvent, JobEvent

log = get_logger("app.events")


def record_job_event(
    db: Session,
    *,
    user_id: Optional[int],
    job_id: int,
    stage: str,
    status: str = "info",
    message: str = "",
    meta: Optional[Dict[str, Any]] = None,
    commit: bool = True,
) -> JobEvent:
    event = JobEvent(
        user_id=user_id,
        job_id=job_id,
        stage=stage,
        status=status,
        message=message[:2000],
        meta=meta or {},
    )
    db.add(event)
    if commit:
        db.commit()
    inc("jobhunter_job_events_total", stage=stage, status=status)
    return event


def record_email_event(
    db: Session,
    *,
    user_id: Optional[int],
    email_id: int,
    kind: str,
    detail: str = "",
    meta: Optional[Dict[str, Any]] = None,
    commit: bool = True,
) -> EmailEvent:
    event = EmailEvent(user_id=user_id, email_id=email_id, kind=kind, detail=detail[:2000], meta=meta or {})
    db.add(event)
    if commit:
        db.commit()
    inc("jobhunter_email_events_total", kind=kind)
    return event
