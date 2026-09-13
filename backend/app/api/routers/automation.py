"""Auto-mode (scheduled work) observability.

``GET /api/automation`` is the one place the UI reads to answer "is auto mode
running for me, how often, when does each workflow run next, what happened
last, and how much budget is left". It is assembled entirely from the
scheduler's own state (:mod:`app.services.auto_scheduler`) and the
``scheduled_runs`` history — no second calculation, no optimistic guessing: a
skip is reported as a skip, with the reason the scheduler recorded.

Authz is the house pattern: the payload is built from the authenticated user's
own row (``CurrentUser``), so there is no id to forge and nothing to leak.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.services.auto_scheduler import overview

router = APIRouter(prefix="/automation", tags=["automation"])


@router.get("")
def automation(user: CurrentUser, db: DbSession):
    """This user's auto-mode state: switch, tier gate, cadence, clocks, history."""
    return overview(db, int(user.id))
