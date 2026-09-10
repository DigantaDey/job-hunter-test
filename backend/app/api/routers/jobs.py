"""Jobs, discovery, application and user-input endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession, search_context
from app.core import audit
from app.core.auth import require_consent
from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import Job, JobEvent, Profile, UserInputRequest
from app.schemas.schemas import JobDetail, JobOut
from app.services.apply_flow import mark_applied, prepare_application
from app.services.classifier import ai_company_size, heuristic_company_size
from app.services.discovery import discovery_sources_config
from app.services.events import record_job_event
from app.services.job_queue import enqueue, queue_stats
from app.services.user_settings import get_setting

router = APIRouter(tags=["jobs"])
log = get_logger("app.jobs")


def _job_or_404(db: Session, user_id: int, job_id: int) -> Job:
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == user_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    return job


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
@router.get("/jobs", response_model=List[JobOut])
def list_jobs(
    user: CurrentUser,
    db: DbSession,
    status: Optional[str] = None,
    source: Optional[str] = None,
    company_size: Optional[str] = None,
    q: Optional[str] = None,
    min_score: Optional[float] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    query = db.query(Job).filter(Job.user_id == user.id)
    if status:
        query = query.filter(Job.status == status)
    if source:
        query = query.filter(Job.source == source)
    if company_size:
        query = query.filter(Job.company_size == company_size)
    if min_score is not None:
        query = query.filter(Job.score >= min_score)
    if q:
        pattern = f"%{q}%"
        query = query.filter(Job.title.ilike(pattern) | Job.company.ilike(pattern) | Job.location.ilike(pattern))
    return query.order_by(Job.score.desc(), Job.discovered_at.desc()).offset(offset).limit(limit).all()


# NOTE: static paths must be declared *before* ``/jobs/{job_id}`` — Starlette
# matches routes in registration order, so a later ``/jobs/sources`` would be
# swallowed by the integer path parameter and fail validation.
@router.get("/jobs/sources")
def job_sources(user: CurrentUser, db: DbSession):
    """Available/blocked job sources + the user's current selection."""
    from app.services.sources import list_sources

    config = discovery_sources_config(db, user.id)
    return {"sources": list_sources(), "selected": config["sources"], "live_enabled": config["live_enabled"],
            "board_tokens": config["board_tokens"]}


@router.get("/jobs/{job_id}", response_model=JobDetail)
def get_job(job_id: int, user: CurrentUser, db: DbSession):
    return _job_or_404(db, user.id, job_id)


@router.get("/jobs/{job_id}/events")
def job_events(job_id: int, user: CurrentUser, db: DbSession):
    _job_or_404(db, user.id, job_id)
    rows = (
        db.query(JobEvent)
        .filter(JobEvent.job_id == job_id, JobEvent.user_id == user.id)
        .order_by(JobEvent.created_at.asc())
        .all()
    )
    return [
        {"id": r.id, "stage": r.stage, "status": r.status, "message": r.message,
         "meta": r.meta, "created_at": r.created_at}
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
class DiscoveryRequest(BaseModel):
    keywords: Optional[List[str]] = None
    freshness_hours: Optional[int] = Field(default=None, ge=1, le=24 * 90)
    limit: int = Field(default=40, ge=1, le=200)
    live_enabled: Optional[bool] = None
    sources: Optional[List[str]] = None


@router.post("/jobs/discover")
async def trigger_discovery(payload: DiscoveryRequest, request: Request, user: CurrentUser, db: DbSession):
    """
    Queue a discovery run. The worker does the fetching so the request returns
    immediately and long source fan-outs cannot time out the HTTP client.
    """
    context = await search_context(db, user.id)
    config = discovery_sources_config(db, user.id)

    settings_keywords = get_setting(db, user.id, "scraping", "keywords", settings.default_keywords)
    if isinstance(settings_keywords, list):
        settings_keywords = ", ".join(str(k) for k in settings_keywords)

    keywords = list(payload.keywords or [])
    for source in (settings_keywords, ):
        for token in str(source or "").split(","):
            token = token.strip()
            if token and token not in keywords:
                keywords.append(token)
    for token in context.get("keywords", [])[:16]:
        if token not in keywords:
            keywords.append(str(token))

    freshness = payload.freshness_hours or int(
        get_setting(db, user.id, "scraping", "freshness_hours", settings.default_freshness_hours)
    )
    live_enabled = payload.live_enabled if payload.live_enabled is not None else config["live_enabled"]

    item = enqueue(
        db,
        user_id=user.id,
        pipeline="discovery",
        payload={"keywords": keywords[:20], "freshness_hours": freshness, "limit": payload.limit,
                 "live_enabled": live_enabled, "sources": payload.sources or config["sources"],
                 "board_tokens": config["board_tokens"]},
        priority=3,
        dedupe_key=f"discovery:{','.join(keywords[:5])}:{freshness}",
    )
    return {
        "queued": item is not None,
        "pipeline_job_id": item.id if item else None,
        "duplicate": item is None,
        "keywords": keywords[:20],
        "freshness_hours": freshness,
        "sources": payload.sources or config["sources"],
        "live_enabled": live_enabled,
    }


# --------------------------------------------------------------------------- #
# Application flow
# --------------------------------------------------------------------------- #
class ApplyRequest(BaseModel):
    resume_choice: str = Field(default="auto", pattern="^(auto|master|generated|selected)$")
    resume_id: Optional[int] = None
    answers: Dict[str, Any] = Field(default_factory=dict)
    queue_only: bool = False


@router.post("/jobs/{job_id}/apply")
async def apply_job(
    job_id: int,
    payload: ApplyRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
):
    """
    Prepare (and, when automation + consent allow, execute) an application.

    The heavy work runs in the worker; this endpoint prepares synchronously so
    the UI can immediately show the field map / user-input request.
    """
    job = _job_or_404(db, user.id, job_id)
    if job.status in ("applied",):
        return {"status": "applied", "message": "Already applied", "job_id": job.id}

    profile = db.query(Profile).filter(Profile.user_id == user.id).first()
    if not profile:
        raise HTTPException(400, {"code": "profile_missing", "message": "Upload a master resume first"})

    if payload.queue_only:
        item = enqueue(db, user_id=user.id, pipeline="application", job_id=job.id,
                       payload={"resume_choice": payload.resume_choice, "resume_id": payload.resume_id,
                                "answers": payload.answers},
                       priority=1, dedupe_key=f"apply:{job.id}")
        job.status = "queued"
        db.commit()
        record_job_event(db, user_id=user.id, job_id=job.id, stage="queued", status="info",
                         message="Application queued for processing")
        return {"status": "queued", "pipeline_job_id": item.id if item else None, "duplicate": item is None}

    try:
        result = await prepare_application(db, user, job, resume_choice=payload.resume_choice,
                                           resume_id=payload.resume_id, answers=payload.answers)
    except ValueError as exc:
        raise HTTPException(
            400, {"code": str(exc), "message": "Cannot prepare this application yet"}
        ) from exc

    if result["status"] == "needs_input":
        return {
            **result,
            "status": "needs_input",
            "vault_created": result.get("credential_created", False),
            "message": "Some required fields need your input before the application can be submitted",
        }

    # Automation is opt-in three times over: platform config + user setting +
    # the automation disclosure. Reaching this branch means the click really
    # submits, so the consent is enforced here (preparing a dry run above must
    # stay possible without it) — a 403 names the disclosure to accept.
    if settings.autofill_enabled and not settings.autofill_dry_run \
            and bool(get_setting(db, user.id, "application", "allow_auto_submit", False)):
        require_consent("automation")(user)
        item = enqueue(db, user_id=user.id, pipeline="application", job_id=job.id,
                       payload={"resume_choice": payload.resume_choice, "resume_id": payload.resume_id,
                                "answers": payload.answers},
                       priority=1, dedupe_key=f"apply:{job.id}")
        result.update({"status": "queued", "pipeline_job_id": item.id if item else None})

    audit.audit(db, "job.applied", user=user, target=f"{job.company}:{job.title}",
                detail={"job_id": job.id, "status": result["status"], "resume_decision": result["resume_decision"]},
                request=request)
    return result


class InputPayload(BaseModel):
    answers: Dict[str, Any] = Field(default_factory=dict)


@router.post("/jobs/{job_id}/input")
async def submit_input(job_id: int, payload: InputPayload, user: CurrentUser, db: DbSession):
    """Answer the User Input Needed request and re-run the application."""
    job = _job_or_404(db, user.id, job_id)
    request_row = (
        db.query(UserInputRequest)
        .filter(UserInputRequest.job_id == job_id, UserInputRequest.user_id == user.id,
                UserInputRequest.status == "pending")
        .order_by(UserInputRequest.created_at.desc())
        .first()
    )
    if not request_row:
        raise HTTPException(404, "No pending input request for this job")

    for field in request_row.fields or []:
        if field["name"] in payload.answers:
            field["value"] = payload.answers[field["name"]]
    request_row.status = "completed"
    request_row.completed_at = datetime.utcnow()
    db.commit()

    # Merge the answers into the stored plan so the next prepare() reuses them.
    extra = dict(job.extra or {})
    answers = dict(extra.get("answers") or {})
    answers.update({k: v for k, v in payload.answers.items() if v not in (None, "")})
    extra["answers"] = answers
    job.extra = extra
    job.status = "queued"
    db.commit()

    record_job_event(db, user_id=user.id, job_id=job.id, stage="user_input", status="success",
                     message=f"User provided {len(payload.answers)} field(s); application re-queued")
    item = enqueue(db, user_id=user.id, pipeline="application", job_id=job.id,
                   payload={"resume_choice": "auto", "answers": answers}, priority=1,
                   dedupe_key=f"apply:{job.id}")
    audit.audit(db, "job.input_submitted", user=user, target=f"job:{job.id}",
                detail={"fields": list(payload.answers)}, request=None)
    return {"ok": True, "requeued": True, "pipeline_job_id": item.id if item else None}


@router.post("/jobs/{job_id}/mark-applied")
def confirm_applied(job_id: int, user: CurrentUser, db: DbSession, note: str = ""):
    """Confirm a manual submission (used when automation is disabled)."""
    job = _job_or_404(db, user.id, job_id)
    result = mark_applied(db, user, job, note=note)
    audit.audit(db, "job.applied", user=user, target=f"{job.company}:{job.title}",
                detail={"job_id": job.id, "confirmed_by": "user"})
    return result


@router.post("/jobs/{job_id}/retry")
def retry_application(job_id: int, user: CurrentUser, db: DbSession):
    job = _job_or_404(db, user.id, job_id)
    job.status = "queued"
    job.error = ""
    db.commit()
    item = enqueue(db, user_id=user.id, pipeline="application", job_id=job.id,
                   payload={"resume_choice": "auto"}, priority=1, dedupe_key=f"apply:{job.id}")
    record_job_event(db, user_id=user.id, job_id=job.id, stage="retry", status="info",
                     message="Application re-queued after failure")
    return {"ok": True, "pipeline_job_id": item.id if item else None, "duplicate": item is None}


@router.post("/jobs/{job_id}/skip")
def skip_job(job_id: int, user: CurrentUser, db: DbSession):
    job = _job_or_404(db, user.id, job_id)
    job.status = "skipped"
    db.commit()
    record_job_event(db, user_id=user.id, job_id=job.id, stage="skipped", status="info",
                     message="Skipped by the user")
    return {"ok": True, "status": job.status}


@router.get("/user-input-queue")
def user_input_queue(user: CurrentUser, db: DbSession):
    rows = (
        db.query(UserInputRequest)
        .filter(UserInputRequest.user_id == user.id, UserInputRequest.status == "pending")
        .order_by(UserInputRequest.created_at.desc())
        .all()
    )
    jobs = {j.id: j for j in db.query(Job).filter(Job.user_id == user.id).all()}
    return [
        {
            "id": row.id,
            "job_id": row.job_id,
            "job": {
                "title": jobs[row.job_id].title if row.job_id in jobs else "",
                "company": jobs[row.job_id].company if row.job_id in jobs else "",
                "url": jobs[row.job_id].url if row.job_id in jobs else "",
            },
            "fields": row.fields,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.post("/classify/company")
async def classify_company(user: CurrentUser, db: DbSession, company: str = Query(...), jd: str = ""):
    size, confidence = await ai_company_size(company, jd)
    if confidence is None:  # pragma: no cover - defensive
        size, confidence = heuristic_company_size({}, {"company": company, "description": jd}), 0.5
    return {"company": company, "size": size, "confidence": confidence}


@router.get("/pipelines/stats")
def pipelines(user: CurrentUser, db: DbSession):
    from app.core.rate_limiter import rate_limiter

    stats = queue_stats(db, user_id=user.id)
    stats["rate_limiter"] = rate_limiter.stats()
    return stats


@router.get("/pipelines/jobs")
def pipeline_items(user: CurrentUser, db: DbSession, pipeline: Optional[str] = None,
                   status_filter: Optional[str] = Query(None, alias="status"), limit: int = 50):
    from app.services.job_queue import recent_items

    rows = recent_items(db, user_id=user.id, pipeline=pipeline, status=status_filter, limit=min(200, limit))
    return [
        {"id": r.id, "pipeline": r.pipeline, "job_id": r.job_id, "status": r.status,
         "priority": r.priority, "attempts": r.attempts, "max_attempts": r.max_attempts,
         "error": r.error, "created_at": r.created_at, "updated_at": r.updated_at,
         "scheduled_at": r.scheduled_at, "payload": {k: v for k, v in (r.payload or {}).items() if k != "result"}}
        for r in rows
    ]
