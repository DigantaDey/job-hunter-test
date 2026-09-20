"""
Notifications system — in-app alerts for job matches, automation failures, etc.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession
from app.core.entitlements import enforce
from app.models.models import Job, Notification
from app.services.reliability import count

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
def list_notifications(user: CurrentUser, db: DbSession, unread_only: bool = False, limit: int = Query(50, le=200)):
    q = db.query(Notification).filter(Notification.user_id == user.id)
    if unread_only:
        q = q.filter(Notification.read.is_(False))
    rows = q.order_by(Notification.created_at.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "kind": r.kind,
            "title": r.title,
            "body": r.body,
            "link": r.link,
            "read": r.read,
            "meta": r.meta,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@router.post("/{notification_id}/read")
def mark_read(notification_id: int, user: CurrentUser, db: DbSession):
    row = db.query(Notification).filter(Notification.id == notification_id, Notification.user_id == user.id).first()
    if not row:
        return {"ok": False}
    row.read = True
    db.commit()
    return {"ok": True}


@router.post("/read-all")
def mark_all_read(user: CurrentUser, db: DbSession):
    db.query(Notification).filter(Notification.user_id == user.id, Notification.read.is_(False)).update({"read": True})
    db.commit()
    return {"ok": True}


@router.delete("/{notification_id}")
def delete_notification(notification_id: int, user: CurrentUser, db: DbSession):
    row = db.query(Notification).filter(Notification.id == notification_id, Notification.user_id == user.id).first()
    if row:
        db.delete(row)
        db.commit()
    return {"ok": True}


@router.get("/preferences")
def get_preferences(user: CurrentUser, db: DbSession):
    from app.services.user_settings import get_setting
    return {
        "email_notifications": get_setting(db, user.id, "notifications", "email_enabled", True),
        "high_match_alerts": get_setting(db, user.id, "notifications", "high_match", True),
        "automation_failures": get_setting(db, user.id, "notifications", "automation_failures", True),
        "weekly_summary": get_setting(db, user.id, "notifications", "weekly_summary", True),
        "new_replies": get_setting(db, user.id, "notifications", "new_replies", True),
    }


@router.put("/preferences")
def update_preferences(payload: dict, user: CurrentUser, db: DbSession):
    from app.services.user_settings import set_setting
    for key in ("email_enabled", "high_match", "automation_failures", "weekly_summary", "new_replies"):
        if key in payload:
            set_setting(db, user.id, "notifications", key, bool(payload[key]))
    db.commit()
    return {"ok": True}


def create_notification(db: Session, user_id: int, kind: str, title: str, body: str = "", link: str = "", meta: Optional[Dict[str, Any]] = None):
    """Helper to create notification from anywhere.

    Every in-app notification is written through here, so this is where the
    delivery metric lives: a notification that silently fails to persist is the
    difference between "we told the user their run expired" and a user who
    thinks the product stopped working. ``kind`` is bounded by
    ``NOTIFICATION_KINDS`` — an unmapped kind is counted as ``other``.
    """
    try:
        n = Notification(
            user_id=user_id,
            kind=kind,
            title=title[:200],
            body=body[:2000],
            link=link[:500],
            meta=meta or {},
            created_at=datetime.utcnow(),
            read=False,
        )
        db.add(n)
        db.commit()
        count("jobhunter_notifications_total", kind=kind, outcome="created")
        return n
    except Exception:
        # Best effort
        count("jobhunter_notifications_total", kind=kind, outcome="failed")
        try:
            db.rollback()
        except Exception:
            pass
        return None


@router.post("/generate-summary")
def generate_weekly_summary(user: CurrentUser, db: DbSession):
    """Generate the weekly outcome report — the user-facing weekly summary."""
    from app.services.flags import is_enabled

    if not is_enabled(db, "assistant.weekly_report"):
        raise HTTPException(
            403,
            {"code": "feature_disabled",
             "message": "The weekly outcome report is turned off for this workspace. The owner can enable it in the admin console."},
        )
    try:
        enforce(db, user.id, "advanced_alerts")
    except Exception:
        # Free users still get basic summary
        pass

    # Calculate last 7 days
    week_ago = datetime.utcnow() - timedelta(days=7)
    jobs = db.query(Job).filter(Job.user_id == user.id, Job.discovered_at >= week_ago).all()
    high_match = [j for j in jobs if j.score >= 75]
    applied = db.query(Job).filter(Job.user_id == user.id, Job.applied_at.is_not(None), Job.applied_at >= week_ago).count() if hasattr(Job, 'applied_at') else 0

    title = f"Weekly Summary: {len(jobs)} new jobs, {len(high_match)} high matches"
    body = f"You discovered {len(jobs)} jobs this week. {len(high_match)} are high priority (score >=75). Applied to {applied} jobs."
    create_notification(db, user.id, "weekly_summary", title, body, link="/", meta={"jobs": len(jobs), "high_match": len(high_match)})
    return {"ok": True, "title": title}
