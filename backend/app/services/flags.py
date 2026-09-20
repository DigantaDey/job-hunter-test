"""
Global feature flags — the owner console's switches, consulted by real code.

A flag here is a *product* switch, not an environment variable: the owner turns
it on or off from ``/admin`` while the app is running, and the change is
audited. Every flag declares its default, its user-facing label and a one-line
description, so the admin UI renders the registry instead of hard-coding rows.

Consumers (the reason this module exists rather than a bare table):

* ``discovery.live_sources``      — AND-ed into the per-user "live sources"
  setting in :mod:`app.services.discovery`; turning it off stops live job-board
  fetching for the whole workspace.
* ``assistant.interview_prep``    — checked by ``POST /api/interview/generate``;
  off means new prep sessions are refused with a friendly message.
* ``assistant.weekly_report``     — checked by ``POST /api/notifications/generate-summary``.

Reading is cheap (one indexed query, tiny table). There is deliberately no
cache: a flag flip must be visible on the next request, and the table is read
at most a handful of times per request path.
"""
from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.core import audit
from app.models.models import GlobalSetting


class Flag:
    """One registered flag: key, default, label, description."""

    def __init__(self, key: str, default: bool, label: str, description: str) -> None:
        self.key = key
        self.default = default
        self.label = label
        self.description = description


#: The registry. Adding a flag is one line here plus one consumer check.
REGISTRY: List[Flag] = [
    Flag(
        "discovery.live_sources", True,
        "Live job-board discovery",
        "Let discovery fetch fresh postings from live job boards (user source settings still apply).",
    ),
    Flag(
        "assistant.interview_prep", True,
        "Interview practice sessions",
        "Users can generate AI interview practice sessions for their applications.",
    ),
    Flag(
        "assistant.weekly_report", True,
        "Weekly outcome report",
        "Users can generate their weekly outcome summary (applications, responses, interviews).",
    ),
]

_BY_KEY = {flag.key: flag for flag in REGISTRY}


def is_registered(key: str) -> bool:
    return key in _BY_KEY


def default_value(key: str) -> bool:
    flag = _BY_KEY.get(key)
    return bool(flag.default) if flag else False


def is_enabled(db: Session, key: str) -> bool:
    """The effective value: the stored override when present, else the default."""
    flag = _BY_KEY.get(key)
    if flag is None:
        return False
    row = db.query(GlobalSetting).filter(GlobalSetting.key == key).first()
    if row is None or not isinstance(row.value, bool):
        return flag.default
    return row.value


def all_flags(db: Session) -> List[Dict[str, Any]]:
    """The registry with each flag's effective value — what the console renders."""
    rows = {row.key: row.value for row in db.query(GlobalSetting).all()}
    return [
        {
            "key": flag.key,
            "label": flag.label,
            "description": flag.description,
            "default": flag.default,
            "enabled": rows[flag.key] if isinstance(rows.get(flag.key), bool) else flag.default,
            "overridden": isinstance(rows.get(flag.key), bool),
        }
        for flag in REGISTRY
    ]


def set_flag(db: Session, key: str, value: bool, *, actor=None, request=None) -> Dict[str, Any]:
    """Persist one flag and audit the change. Unknown keys are refused (400 upstream)."""
    flag = _BY_KEY[key]
    row = db.query(GlobalSetting).filter(GlobalSetting.key == key).first()
    if row is None:
        row = GlobalSetting(key=key)
        db.add(row)
    previous = row.value if isinstance(row.value, bool) else flag.default
    row.value = bool(value)
    row.updated_by = getattr(actor, "id", None)
    db.commit()
    if actor is not None:
        audit.audit(db, "admin.flag_updated", user=actor, target=key,
                    detail={"from": previous, "to": bool(value)}, request=request)
    return {"key": key, "enabled": bool(value), "previous": previous}
