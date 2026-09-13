"""
API router aggregation.

The v1.2 monolithic router has been split by domain; this module keeps the
historical import path (`app.api.routes.router`) working and composes the final
``/api`` router.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import search_context  # noqa: F401  (re-exported for callers/tests)
from app.api.routers import (
    account,
    analytics,
    auth,
    automation,
    billing,
    emails,
    funding,
    interview,
    jobs,
    notifications,
    ops,
    personas,
    resumes,
    settings_api,
    tracking,
    vault,
)

router = APIRouter()
router.include_router(auth.router)
router.include_router(account.router)
router.include_router(resumes.router)
router.include_router(jobs.router)
router.include_router(vault.router)
router.include_router(emails.router)
router.include_router(funding.router)
router.include_router(settings_api.router)
router.include_router(ops.router)
router.include_router(tracking.router)
router.include_router(billing.router)
router.include_router(analytics.router)
router.include_router(notifications.router)
router.include_router(interview.router)
router.include_router(personas.router)
router.include_router(automation.router)

__all__ = ["router", "search_context"]
