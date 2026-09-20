"""
The end-user surface (``/api/me/*``).

Everything an ordinary job seeker needs — and nothing owner-level. The dashboard
aggregate is product data (matches, applications, interviews, reminders); the
assistant read is a *sanitized* availability signal with no provider URL, API
key, model name or usage accounting on it. Those live behind owner-only
endpoints; this router never forwards them.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.services import user_dashboard

router = APIRouter(tags=["me"])


@router.get("/me/dashboard")
def my_dashboard(user: CurrentUser, db: DbSession) -> Dict[str, Any]:
    """One aggregate: what my job search is doing and what needs me next."""
    return user_dashboard.build_dashboard(db, int(user.id))


@router.get("/me/assistant")
def my_assistant(
    user: CurrentUser,
    db: DbSession,
    probe: bool = Query(default=True, description="Include a live availability probe"),
) -> Dict[str, Any]:
    """
    User-safe assistant availability.

    Answers "is my assistant working / is my work paused" with stable,
    user-oriented state codes — deliberately **without** the provider URL, API
    key material, model id, rate-limit counters or token usage that
    ``GET /api/settings/ai/status`` (owner-only) reports.
    """
    from app.services.ai_client import is_configured
    from app.services.job_queue import paused_count

    configured = is_configured(db=db, user_id=int(user.id))
    paused = int(paused_count(db, user_id=int(user.id)) or 0)
    payload: Dict[str, Any] = {
        "configured": configured,
        "state": "not_configured",
        "paused_items": paused,
        "message": (
            "Your assistant isn't connected yet — the workspace owner can connect it in the admin console."
            if not configured else
            ("Some of your work is paused and will resume automatically."
             if paused else
             "Your assistant is ready to help.")
        ),
    }
    if configured and probe:
        # Whitelist the fields, never pass the availability payload through:
        # it carries the provider URL, model id and key source — owner material.
        availability = await_aware_availability(db, int(user.id))
        payload.update({
            "state": availability.get("state"),
            "online": availability.get("online", False),
            "reason": availability.get("reason"),
            "hint": availability.get("hint"),
            "retry_after_hint": availability.get("retry_after_hint"),
        })
    return payload


def await_aware_availability(db: Any, user_id: int) -> Dict[str, Any]:
    """Bridge: FastAPI runs this sync handler in a threadpool, so block on the probe."""
    import asyncio

    from app.services.ai_client import ai_availability

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        return loop.run_until_complete(ai_availability(db=db, user_id=user_id))
    return asyncio.run(ai_availability(db=db, user_id=user_id))
