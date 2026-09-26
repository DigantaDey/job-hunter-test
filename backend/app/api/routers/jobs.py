"""Jobs, discovery, application and user-input endpoints — now with monetization."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession, search_context
from app.core import audit
from app.core.auth import require_consent
from app.core.config import settings
from app.core.entitlements import can, enforce, get_user_plan, increment_usage
from app.core.logging import get_logger
from app.models.models import FundingCompany, Job, JobEvent, PipelineJob, Profile, UserInputRequest
from app.schemas.schemas import JobDetail
from app.services import persona as persona_service
from app.services.apply_flow import mark_applied
from app.services.classifier import ai_company_size
from app.services.company_normalize import normalize_company_name
from app.services.discovery import discovery_sources_config
from app.services.events import record_job_event
from app.services.job_queue import enqueue, enqueue_or_existing, queue_stats
from app.services.user_settings import get_setting

router = APIRouter(tags=["jobs"])
log = get_logger("app.jobs")


#: Verdicts that got no answer from an engine, so re-running is meaningful and
#: the response should say how: ``rejected`` (the model answered, the accuracy
#: guardrail refused the answer) and ``pending`` (the model could not be
#: reached). A real verdict is never re-run implicitly — see v2.5's guardrail
#: rescue in ``services/scoring.py``, which now retries ``rejected`` pairs on the
#: local decision engine automatically before this state is ever persisted.
RETRYABLE_SCORE_SOURCES = ("rejected", "pending")


def _job_or_404(db: Session, user_id: int, job_id: int) -> Job:
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == user_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    return job


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
@router.get("/jobs")
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
    company: Optional[str] = None,
):
    # v2.2.5 linkage: build funded normalized set for the funding chip
    funded_norms: set[str] = set()
    for row in db.query(FundingCompany).filter(FundingCompany.user_id == user.id).all():
        n = normalize_company_name(row.name)
        if n:
            funded_norms.add(n)
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
    # Company exact normalized filter (prompt B): exact match only, suffix-stripped.
    # The normalized identity is a column (``company_name_normalized``, stored at
    # create/import time, backfilled by migration e5f6a7b8c9d0), so the match
    # happens in SQL and LIMIT/OFFSET are applied by the database — a Pro+
    # board of 20k rows is never fetched whole to filter in Python.
    if company is not None and str(company).strip():
        target = normalize_company_name(company)
        if not target:
            return []
        query = query.filter(Job.company_name_normalized == target)
    rows = query.order_by(Job.score.desc(), Job.discovered_at.desc()).offset(offset).limit(limit).all()
    out = []
    for r in rows:
        funding_norm = normalize_company_name(r.company)
        is_funded = bool(funding_norm and funding_norm in funded_norms)
        d = {
            "id": r.id, "title": r.title, "company": r.company, "location": r.location, "url": r.url,
            "source": r.source, "status": r.status, "score": r.score, "company_size": r.company_size,
            # Provenance travels with the number (v2.5): a board row whose score
            # could not be produced (``rejected`` / ``pending``) renders as
            # "unscored", not as a 0/100 verdict — the list used to show a
            # literal "0 score" for a pair the model was never allowed to
            # answer, which reads as "terrible match" instead of "no verdict".
            "score_source": r.score_source,
            "discovered_at": r.discovered_at, "posted_at": r.posted_at, "applied_at": r.applied_at,
            "error": r.error,
            # Funding chip only. The old payload also carried
            # ``has_open_positions`` set to this same funding flag — a semantic
            # lie the Jobs UI rendered as an "open roles" chip. A Job row *is*
            # an open position by definition, so the field had no honest
            # meaning here and is gone (the real signal lives on the funding
            # radar's own companies).
            "funding": is_funded,
            "is_funded": is_funded,
            "funding_match": is_funded,
        }
        out.append(d)
    return out


@router.get("/jobs/sources")
def job_sources(user: CurrentUser, db: DbSession):
    """Available/blocked job sources + the user's current selection."""
    from app.services.sources import list_sources

    config = discovery_sources_config(db, user.id)
    plan = get_user_plan(db, user.id)
    return {
        "sources": list_sources(),
        "selected": config["sources"],
        "live_enabled": config["live_enabled"],
        "board_tokens": config["board_tokens"],
        "plan": plan,
    }


@router.get("/jobs/{job_id}", response_model=JobDetail)
def get_job(job_id: int, user: CurrentUser, db: DbSession):
    return _job_or_404(db, user.id, job_id)


@router.get("/jobs/{job_id}/intelligence")
async def job_intelligence(job_id: int, request: Request, user: CurrentUser, db: DbSession):
    """Transparent job intelligence breakdown.\n\n    Pro: the AI rubric verdict (an AI outage is reported as ``ai_error``,\n    never papered over). Free tier: the deterministic keyword-overlap\n    breakdown, explicitly labelled (``score_source="preliminary"`` +\n    ``upgrade_hint``) — a plan difference with provenance, not a silent\n    substitute for the AI verdict.\n    """
    job = _job_or_404(db, user.id, job_id)
    profile = db.query(Profile).filter(Profile.user_id == user.id).order_by(Profile.created_at.desc()).first()
    profile_data = (profile.data if profile else {}) or {}

    force = bool(request.query_params.get("refresh")) if hasattr(request, "query_params") else False
    stored = (job.extra or {}).get("intelligence")
    if stored and not force:
        return stored

    from app.services.scoring import RUBRIC_WEIGHTS, preliminary_score_detailed, score_job

    use_advanced = can(db, user.id, "can_use_advanced_matching")
    persona = persona_service.get_persona(db, user.id, job.persona_id)
    persona_context = (
        {"name": persona.name, "target_role": persona.target_role,
         "observed_skills": list((persona_service.memory_summary(persona).get("observed_skills") or {}).keys())[:12]}
        if persona else None
    )

    if use_advanced:
        result = await score_job(profile_data, job.description or "", db=db, user_id=user.id,
                                 persona=persona_context)
        detail = result.get("detail") or {}
        detail["source"] = result["score_source"]
        if result.get("error"):
            # AI offline is reported, never papered over with a keyword estimate.
            detail["ai_error"] = result["error"]
            detail["preliminary_score"] = result.get("preliminary_score")
    else:
        detail = preliminary_score_detailed(profile_data, job.description or "")
        detail["upgrade_hint"] = "Upgrade to Pro for AI-powered advanced matching breakdown"

    # Provenance is part of the contract, not a detail: the UI must be able to
    # tell an AI verdict apart from a keyword-overlap estimate at a glance, and
    # the persisted breakdown must say the same as the response.
    # ``weights`` is what the scorer calls it; ``rubric`` is the API/UI name.
    detail["rubric"] = detail.get("weights") or RUBRIC_WEIGHTS
    detail["score"] = float(detail.get("overall") or detail.get("score") or job.score or 0)
    detail["score_source"] = str(detail.get("source") or "preliminary")

    # The retry affordance (v2.5). A verdict the guardrail refused — or one the
    # model was never reachable for — used to dead-end here: the diagnosis told
    # the user to "retry" and there was no way to do it, and (because the stored
    # payload short-circuits the read below) the dead end was permanent. The
    # route that re-runs scoring is this same read with ``?refresh=1``; it is
    # announced on the response so any client can offer it, and it says it
    # costs an analysis.
    if str(detail.get("source") or "") in RETRYABLE_SCORE_SOURCES:
        detail["actions"] = {
            "retry": {"allowed": True, "route": f"/api/jobs/{job.id}/intelligence?refresh=1",
                      "method": "GET", "costs_quota": True},
        }

    # Persist the verdict + its provenance on the job row so lists can be honest.
    job.score = detail["score"]
    job.score_reason = str(detail.get("reason") or job.score_reason or "")
    job.score_source = str(detail.get("source") or "preliminary")
    job.score_detail = detail
    try:
        extra = dict(job.extra or {})
        extra["intelligence"] = detail
        job.extra = extra
        db.commit()
    except Exception:
        db.rollback()

    if persona and detail.get("source") == "ai":
        persona_service.record_signal(db, user.id, persona.id, "job_scored",
                                      {"title": job.title, "company": job.company,
                                       "score": detail.get("overall"),
                                       "score_band": detail.get("recommendation"),
                                       "skills": detail.get("missing_weak") or []})

    # Provenance is part of the contract, not a detail: the UI must be able to
    # tell an AI verdict apart from a keyword-overlap estimate at a glance.
    detail["score"] = job.score
    detail["score_source"] = job.score_source
    return detail


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
        {"id": r.id, "stage": r.stage, "status": r.status, "message": r.message, "meta": r.meta, "created_at": r.created_at}
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
#: How far back the persona filter may scan. The filter runs in Python (see
#: :func:`discovery_last_run`), so the scan is bounded rather than open-ended —
#: and 200 completed runs is far past anything the empty-board banner needs.
LAST_RUN_SCAN_LIMIT = 200


def _payload_persona(payload: Dict[str, Any]) -> Optional[int]:
    """The track a queued run was scoped to, as an ``int`` — or ``None``.

    Enqueuers write an int (``trigger_discovery``, the auto-mode scheduler), but a
    payload assembled from a query string can carry ``"2"``, and this is the one
    place the comparison happens — exactly the normalisation a SQL JSON subscript
    cannot do portably. ``None`` (unscoped) never matches a filter, and a bool is
    not an id even though Python thinks it is an int.
    """
    value = payload.get("persona_id")
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@router.get("/jobs/discovery/last-run")
def discovery_last_run(user: CurrentUser, db: DbSession, persona_id: Optional[int] = Query(None)):
    """The latest *completed* discovery run for this user — why the board is empty.

    An empty board used to say only "no fresh jobs", which is three different
    situations with three different fixes (enable a source, retry after an
    outage, widen the freshness window). This is the read side of the report
    :func:`app.services.discovery.discover_for_user` already writes into the
    queue item's result JSON: the classification happens once, at run time, and
    is *never* re-derived here, so the queue row, the run's log line and this
    response cannot disagree.

    Only the summary blocks come back. The stored report also embeds the run's
    job rows (and grows with every run), so returning it whole would put tens of
    KB of job data on the wire for a one-line banner.

    A run is "the last run" only when it actually finished *and* left a report:
    ``pipeline == "discovery"``, ``status == "done"``, ``payload.result`` a dict.
    A dead or failed run has no report and must not produce a banner state — and
    must not shadow an older completed one, which is why the row scan skips
    report-less rows instead of stopping at the newest one.

    ``persona_id`` filters on the queue item's payload **in Python**, not with a
    SQL JSON subscript: the two supported databases do not agree about that
    operation (SQLite's ``json_extract`` and PostgreSQL's ``->>`` differ on the
    type they hand back, so the same comparison matches on one backend and
    silently matches nothing on the other, and SQLite raises ``malformed JSON``
    on a payload cell it cannot parse — one bad row would 500 the whole read).
    Filtering here keeps the comparison in one place and lets an unreadable row be
    skipped instead of poisoning the answer.
    """
    rows = (
        db.query(PipelineJob)
        .filter(
            PipelineJob.user_id == user.id,
            PipelineJob.pipeline == "discovery",
            PipelineJob.status == "done",
        )
        # ``id`` is the queue's own chronology: two rows completed in the same
        # instant still have a deterministic order, and a paused run that
        # re-executed keeps the place it was enqueued in.
        .order_by(PipelineJob.id.desc())
        .limit(LAST_RUN_SCAN_LIMIT)
        .all()
    )
    row: Optional[PipelineJob] = None
    report: Optional[Dict[str, Any]] = None
    for candidate in rows:
        payload: Dict[str, Any] = dict(candidate.payload or {})
        stored = payload.get("result")
        if not isinstance(stored, dict):
            continue
        if persona_id is not None and _payload_persona(payload) != persona_id:
            continue
        row, report = candidate, stored
        break

    if row is None or report is None:
        raise HTTPException(404, "No completed discovery run")

    # The run's own clock when the report carries one (it always does since
    # v2.2.4); the queue row's finish time for a report written before that.
    at = report.get("started_at") or (
        (row.finished_at or row.created_at).isoformat() if (row.finished_at or row.created_at) else None
    )
    response: Dict[str, Any] = {"at": at, "added": len(report.get("jobs") or [])}
    # Absent, never null: ``why_empty`` exists only on a run that added nothing,
    # and ``summary`` / ``ai_rescore`` only on reports new enough to carry them.
    for key in ("why_empty", "summary", "ai_rescore"):
        if report.get(key) is not None:
            response[key] = report[key]
    return response


class DiscoveryRequest(BaseModel):
    keywords: Optional[List[str]] = None
    freshness_hours: Optional[int] = Field(default=None, ge=1, le=24 * 90)
    limit: int = Field(default=40, ge=1, le=200)
    live_enabled: Optional[bool] = None
    #: ``None`` = use the caller's saved configuration; ``[]`` = fetch nothing.
    #: The distinction is deliberate and survives to the queued payload.
    sources: Optional[List[str]] = None
    persona_id: Optional[int] = None


@router.post("/jobs/discover")
async def trigger_discovery(payload: DiscoveryRequest, request: Request, user: CurrentUser, db: DbSession):
    """Queue a discovery run — monetized via entitlements."""
    enforce(db, user.id, "jobs_discovered_per_month")
    enforce(db, user.id, "jobs_discovered_per_day")
    # Storage cap, real: a board at its jobs_max cannot be topped up. The
    # worker clamps the same way on persist, so concurrent runs can never
    # overshoot even past this check.
    enforce(db, user.id, "jobs_max")

    # Daily counter incremented at trigger time (to enforce rate); monthly by actual discovered count in discovery.py
    increment_usage(db, user.id, "jobs_discovered_per_day", 1)

    persona = persona_service.get_persona(db, user.id, payload.persona_id) or \
        persona_service.ensure_default_persona(db, user.id)
    context = await search_context(db, user.id)
    context = persona_service.context_for_persona(context, persona)
    config = discovery_sources_config(db, user.id)

    # Only what the user explicitly typed/edited — the resume-derived keywords
    # arrive via `context` below, so nothing is prefilled with generic terms.
    settings_keywords = get_setting(db, user.id, "scraping", "keywords", settings.default_keywords)
    if isinstance(settings_keywords, list):
        settings_keywords = ", ".join(str(k) for k in settings_keywords)

    keywords = list(payload.keywords or [])
    for source in (settings_keywords,):
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
    # ``sources`` keeps the unset-vs-empty distinction all the way down (v2.2.4):
    # ``None`` → the user's configuration, ``[]`` → fetch nothing. The old
    # ``payload.sources or config["sources"]`` turned a deliberate empty list into
    # every configured source, and the response then claimed the run would fetch
    # sources the caller had just asked it not to.
    sources = list(payload.sources) if payload.sources is not None else list(config["sources"])

    item, duplicate = enqueue_or_existing(
        db,
        user_id=user.id,
        pipeline="discovery",
        payload={
            "keywords": keywords[:20],
            "freshness_hours": freshness,
            "limit": payload.limit,
            "live_enabled": live_enabled,
            "sources": sources,
            "board_tokens": config["board_tokens"],
            "persona_id": persona.id if persona else None,
        },
        priority=3,
        dedupe_key=f"discovery:{persona.id if persona else 0}:{','.join(keywords[:5])}:{freshness}",
    )
    # ``pipeline_job_id`` is set on the duplicate path too (v2.2.8): the live
    # layer re-attaches to the run that is already in flight instead of
    # showing nothing because the click was a no-op.
    return {
        "queued": not duplicate,
        "pipeline_job_id": item.id if item else None,
        "duplicate": duplicate,
        "keywords": keywords[:20],
        "freshness_hours": freshness,
        "sources": sources,
        "live_enabled": live_enabled,
    }


# --------------------------------------------------------------------------- #
# Application flow — Prepare → Review → Confirm → Execute
# --------------------------------------------------------------------------- #
class ApplyRequest(BaseModel):
    resume_choice: str = Field(default="auto", pattern="^(auto|master|generated|selected)$")
    resume_id: Optional[int] = None
    answers: Dict[str, Any] = Field(default_factory=dict)
    #: Kept for API compatibility — since v2.2.8 every apply is queued.
    queue_only: bool = False


@router.post("/jobs/{job_id}/apply")
async def apply_job(
    job_id: int,
    payload: ApplyRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
):
    """Queue an application run — with entitlement checks.

    v2.2.8: this endpoint *always* answers ``{queued, pipeline_job_id}`` now.
    It used to run Prepare inline (resume choice, form detection, autofill
    plan, vault credential) and hand the outcome back in the response, which
    is exactly the "Status: preparing • resume: master" line that vanished the
    moment the user navigated or pressed F5 — the state lived in one React
    component and nowhere the UI could read it back. The same work now runs
    as a durable ``application`` item; the row (``GET /api/queues/ai``,
    ``GET /api/pipelines/jobs/{id}``) carries ``result`` with the prepared
    plan (``resume_decision``, ``credential_created``, ``missing_fields`` …) or
    the execution outcome, so every page can render Queued → Preparing →
    Applying → Applied / Needs input from it, on any route, after any refresh.

    ``mode`` decides how far the worker goes: ``prepare`` (default) stops after
    the plan — that is all a deployment without browser automation can do;
    ``execute`` additionally runs the autofill, and is chosen only when the
    user has enabled auto-submit *and* given the automation disclosure — the
    same gate the inline path enforced.
    """
    enforce(db, user.id, "can_run_automation")
    enforce(db, user.id, "applications_per_month")
    enforce(db, user.id, "automation_runs_per_month")

    job = _job_or_404(db, user.id, job_id)
    if job.status in ("applied",):
        return {"status": "applied", "queued": False, "pipeline_job_id": None,
                "message": "Already applied", "job_id": job.id}

    profile = db.query(Profile).filter(Profile.user_id == user.id).first()
    if not profile:
        raise HTTPException(400, {"code": "profile_missing", "message": "Upload a master resume first"})

    mode = "prepare"
    if (settings.autofill_enabled and not settings.autofill_dry_run
            and bool(get_setting(db, user.id, "application", "allow_auto_submit", False))):
        require_consent("automation")(user)
        enforce(db, user.id, "can_use_autofill")
        mode = "execute"

    dedupe_key = f"apply:{job.id}"
    item, duplicate = enqueue_or_existing(
        db,
        user_id=user.id,
        pipeline="application",
        job_id=job.id,
        payload={"resume_choice": payload.resume_choice, "resume_id": payload.resume_id,
                 "answers": payload.answers, "mode": mode},
        priority=1,
        dedupe_key=dedupe_key,
    )
    if not duplicate:
        increment_usage(db, user.id, "automation_runs_per_month", 1)
        job.status = "queued"
        job.error = ""
        db.commit()
        record_job_event(db, user_id=user.id, job_id=job.id, stage="queued", status="info",
                         message=f"Application queued for processing ({mode})",
                         meta={"pipeline_job_id": item.id if item else None, "resume_choice": payload.resume_choice})
        audit.audit(
            db,
            "job.apply_queued",
            user=user,
            target=f"{job.company}:{job.title}",
            detail={"job_id": job.id, "mode": mode, "resume_choice": payload.resume_choice,
                    "pipeline_job_id": item.id if item else None},
            request=request,
        )
    return {
        "status": "queued",
        "queued": not duplicate,
        "duplicate": duplicate,
        "pipeline_job_id": item.id if item else None,
        "queue_status": item.status if item else None,
        "job_id": job.id,
        "mode": mode,
        "message": ("An application run for this job is already in flight — showing its live status."
                    if duplicate else "Application queued — the status below updates live and survives a refresh."),
    }


def _requeue_application(db: Session, user_id: int, job: Job, answers: Dict[str, Any]) -> Optional[PipelineJob]:
    """Re-run a job's application with the user's answers.

    The re-queued item keeps the *original* mode: an ``execute`` run that
    paused for a missing field must come back as ``execute`` (and be allowed
    to submit once the consent chain is green), never silently demoted to a
    prepare-only run.
    """
    dedupe_key = f"apply:{job.id}"
    existing = (
        db.query(PipelineJob)
        .filter(PipelineJob.user_id == user_id, PipelineJob.dedupe_key == dedupe_key, PipelineJob.status.in_(("queued", "processing", "needs_input")))
        .order_by(PipelineJob.id.asc())
        .first()
    )
    if existing is None:
        # No live row (finished or dead-lettered): recover the original intent
        # from the job's most recent application item so an execute run never
        # re-queues as prepare. No history at all → prepare (the safe default;
        # submission always comes from an explicit execute intent).
        last = (
            db.query(PipelineJob)
            .filter(PipelineJob.user_id == user_id, PipelineJob.job_id == job.id, PipelineJob.pipeline == "application")
            .order_by(PipelineJob.id.desc())
            .first()
        )
        last_mode = str(dict(last.payload or {}).get("mode") or "execute") if last is not None else ""
        mode = "execute" if last_mode == "execute" else "prepare"
        return enqueue(db, user_id=user_id, pipeline="application", job_id=job.id, payload={"resume_choice": "auto", "answers": answers, "mode": mode}, priority=1, dedupe_key=dedupe_key)
    # Absent mode means execute (the handler's default) — write it explicitly
    # so the mode cannot drift between runs.
    mode = str(dict(existing.payload or {}).get("mode") or "execute")
    if existing.status == "needs_input":
        existing.payload = {**(existing.payload or {}), "resume_choice": "auto", "answers": answers, "mode": mode}
        existing.status = "queued"
        existing.attempts = 0
        existing.scheduled_at = datetime.utcnow()
        existing.error = ""
        existing.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(existing)
        return existing
    if existing.status == "queued":
        existing.payload = {**(existing.payload or {}), "resume_choice": "auto", "answers": answers, "mode": mode}
        db.commit()
        db.refresh(existing)
    return existing


class InputPayload(BaseModel):
    answers: Dict[str, Any] = Field(default_factory=dict)


@router.post("/jobs/{job_id}/input")
async def submit_input(job_id: int, payload: InputPayload, user: CurrentUser, db: DbSession):
    """Answer the User Input Needed request and re-run the application."""
    job = _job_or_404(db, user.id, job_id)
    request_row = (
        db.query(UserInputRequest)
        .filter(UserInputRequest.job_id == job_id, UserInputRequest.user_id == user.id, UserInputRequest.status == "pending")
        .order_by(UserInputRequest.created_at.desc())
        .first()
    )
    if not request_row:
        raise HTTPException(404, "No pending input request for this job")

    updated_fields = []
    for field in request_row.fields or []:
        field = dict(field)
        if field.get("name") in payload.answers:
            field["value"] = payload.answers[field["name"]]
        updated_fields.append(field)
    request_row.fields = updated_fields
    request_row.status = "completed"
    request_row.completed_at = datetime.utcnow()
    db.commit()

    extra = dict(job.extra or {})
    answers = dict(extra.get("answers") or {})
    answers.update({k: v for k, v in payload.answers.items() if v not in (None, "")})
    extra["answers"] = answers
    job.extra = extra
    job.status = "queued"
    db.commit()

    record_job_event(db, user_id=user.id, job_id=job.id, stage="user_input", status="success", message=f"User provided {len(payload.answers)} field(s); application re-queued")
    item = _requeue_application(db, user.id, job, answers)
    audit.audit(db, "job.input_submitted", user=user, target=f"job:{job.id}", detail={"fields": list(payload.answers)}, request=None)
    return {"ok": True, "requeued": item is not None and item.status in ("queued", "processing"), "pipeline_job_id": item.id if item else None}


@router.post("/jobs/{job_id}/mark-applied")
def confirm_applied(job_id: int, user: CurrentUser, db: DbSession, note: str = ""):
    """Confirm a manual submission (used when automation is disabled)."""
    job = _job_or_404(db, user.id, job_id)
    result = mark_applied(db, user, job, note=note)
    # Track manual application as usage too
    try:
        increment_usage(db, user.id, "applications_per_month", 1)
    except Exception:
        pass
    audit.audit(db, "job.applied", user=user, target=f"{job.company}:{job.title}", detail={"job_id": job.id, "confirmed_by": "user"})
    return result


@router.post("/jobs/{job_id}/retry")
def retry_application(job_id: int, user: CurrentUser, db: DbSession):
    enforce(db, user.id, "automation_runs_per_month")
    job = _job_or_404(db, user.id, job_id)
    job.status = "queued"
    job.error = ""
    db.commit()
    increment_usage(db, user.id, "automation_runs_per_month", 1)
    item = enqueue(db, user_id=user.id, pipeline="application", job_id=job.id, payload={"resume_choice": "auto"}, priority=1, dedupe_key=f"apply:{job.id}")
    record_job_event(db, user_id=user.id, job_id=job.id, stage="retry", status="info", message="Application re-queued after failure")
    return {"ok": True, "pipeline_job_id": item.id if item else None, "duplicate": item is None}


@router.post("/jobs/{job_id}/skip")
def skip_job(job_id: int, user: CurrentUser, db: DbSession):
    job = _job_or_404(db, user.id, job_id)
    job.status = "skipped"
    db.commit()
    record_job_event(db, user_id=user.id, job_id=job.id, stage="skipped", status="info", message="Skipped by the user")
    return {"ok": True, "status": job.status}


@router.get("/user-input-queue")
def user_input_queue(user: CurrentUser, db: DbSession):
    # One join, not "fetch every job the user has and index it in Python":
    # the queue carries a handful of pending rows at most, but the old shape
    # loaded the user's *entire* board (20k rows on Pro+) into a dict on every
    # poll just to look up three fields per pending row. The LEFT JOIN keeps
    # pending rows whose job row was deleted (empty title/company/url, as
    # before) without ever touching the rest of the board.
    rows = (
        db.query(UserInputRequest, Job)
        .outerjoin(Job, and_(Job.id == UserInputRequest.job_id, Job.user_id == user.id))
        .filter(UserInputRequest.user_id == user.id, UserInputRequest.status == "pending")
        .order_by(UserInputRequest.created_at.desc())
        .all()
    )
    return [
        {
            "id": req.id,
            "job_id": req.job_id,
            "job": {"title": job.title if job else "", "company": job.company if job else "",
                    "url": job.url if job else ""},
            "fields": req.fields,
            "created_at": req.created_at,
        }
        for req, job in rows
    ]


@router.post("/classify/company")
async def classify_company(user: CurrentUser, db: DbSession, company: str = Query(...), jd: str = ""):
    # AI is a hard dependency: an outage surfaces as the typed pausable 503
    # (handled centrally) — never a guessed size.
    size, confidence = await ai_company_size(company, jd, db=db, user_id=user.id)
    return {"company": company, "size": size, "confidence": confidence}


@router.get("/pipelines/stats")
def pipelines(user: CurrentUser, db: DbSession):
    from app.core.rate_limiter import rate_limiter
    from app.services.job_queue import ai_queue_view

    # The "AI queue" is the durable queue (v2.1.1): the ai_queue block is
    # built from real pipeline_jobs rows only — never synthesized.
    stats: Dict[str, Any] = queue_stats(db, user_id=user.id)
    stats["ai_queue"] = ai_queue_view(db, user_id=user.id)
    stats["rate_limiter"] = rate_limiter.stats()
    return stats


@router.get("/pipelines/jobs")
def pipeline_items(user: CurrentUser, db: DbSession, pipeline: Optional[str] = None, status_filter: Optional[str] = Query(None, alias="status"), limit: int = 50):
    """Recent queue rows for this tenant. ``status`` may be a comma list
    (``queued,processing,paused``) so the live layer needs a single read."""
    from app.services.job_queue import recent_items, serialize_item

    rows = recent_items(db, user_id=user.id, pipeline=pipeline, status=status_filter, limit=min(200, limit))
    return [serialize_item(r, include_result=False) for r in rows]


@router.get("/queues/ai")
def ai_queue_live(user: CurrentUser, db: DbSession, limit: int = Query(100, ge=1, le=200)):
    """The live layer's single poll (v2.2.8).

    Every AI trigger in the product — Discover jobs, Auto-apply, Generate
    resume, Draft email, Funding re-scan — answers ``{queued, pipeline_job_id}``
    and the work runs in the durable queue. This is where the UI reads the
    state of that work back, on every route and after a hard refresh: in-flight
    rows, the outcomes of the last half hour (with each handler's ``result``),
    the header-chip counts and whatever a worker is executing right now.
    Tenant-scoped: the caller only ever sees their own rows.
    """
    from app.services.job_queue import live_view

    return live_view(db, user_id=int(user.id), limit=limit)


@router.get("/pipelines/jobs/{item_id}")
def pipeline_item(item_id: int, user: CurrentUser, db: DbSession):
    """One queue row — how a page re-attaches to the id it stored before an F5."""
    from app.services.job_queue import serialize_item

    row = db.query(PipelineJob).filter(PipelineJob.id == item_id, PipelineJob.user_id == user.id).first()
    if not row:
        raise HTTPException(404, "Queue item not found")
    return serialize_item(row)


@router.get("/company/{company_name}/intel")
async def company_intelligence(company_name: str, user: CurrentUser, db: DbSession, refresh: bool = False):
    """Company intelligence — deeper research.

    Billing follows the work: the ``company_intel_per_month`` meter (and the
    AI ledger the model call writes) is spent **only when the model actually
    builds or refreshes the intel** — cache miss, stale cache, or explicit
    refresh. A fresh cache read is free work: it answers from the stored row
    and leaves the counter untouched. The old code did the exact inverse
    (charged the free cache hit, gave the paid AI run away), so the quota
    protected nothing that costs money.
    """
    # Capability gate only — NOT ``enforce()``: ``enforce`` would also check
    # the capability's mapped monthly quota, and a user at their limit must
    # still be able to read research they already paid for (a fresh cache row
    # is free work). The quota is checked below, immediately before the AI
    # call it protects.
    if not can(db, user.id, "can_use_company_intel"):
        from app.core.entitlements import UPGRADE_HINTS

        raise HTTPException(
            status_code=402,
            detail={
                "code": "upgrade_required",
                "capability": "can_use_company_intel",
                "message": UPGRADE_HINTS.get("company_intel_per_month", "Company intel is not available on your plan"),
            },
        )
    from app.services.company_intel import fetch_company_intel, get_cached_intel

    # Free path: a fresh (<30d) cached row answers the read with no AI at all.
    # ``refresh`` deliberately bypasses it — the user asked for new research.
    cached = None if refresh else get_cached_intel(db, user.id, company_name)
    if cached is not None:
        return cached

    # AI work is about to happen — this is what the monthly quota protects.
    enforce(db, user.id, "company_intel_per_month")
    data = await fetch_company_intel(company_name, db=db, user_id=user.id, force_refresh=refresh)
    # ``cached`` re-checked against the outcome, not the intent: a concurrent
    # request may have filled the cache between the check above and this call,
    # in which case no AI ran here and nothing is charged.
    if not data.get("cached"):
        try:
            increment_usage(db, user.id, "company_intel_per_month", 1)
        except Exception:
            pass
    return data
