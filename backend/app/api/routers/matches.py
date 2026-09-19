"""
Multi-stage matching endpoints.

The board reads (``GET /api/jobs``) keep using the denormalised ``jobs.score*``
columns; these endpoints are the multi-stage system's own read — the stored,
versioned, explained verdict with its history, the user's corrections and the
calibration gate that keeps "estimated fit" from ever becoming a claimed
interview probability.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession
from app.contracts.vocabulary import MATCH_FEEDBACK_KINDS
from app.core import audit
from app.core.entitlements import can, get_user_plan
from app.models.models import Job, MatchFeedback
from app.services import matching
from app.services import persona as persona_service

router = APIRouter(tags=["matches"])

UPGRADE_HINT = "Upgrade to Pro for the AI evidence review of matches"


def _job_or_404(db: Session, user_id: int, job_id: int) -> Job:
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == user_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    return job


# --------------------------------------------------------------------------- #
# Compute / read one match
# --------------------------------------------------------------------------- #
class MatchRequest(BaseModel):
    #: Re-score even if identical inputs were already stored (a new row is
    #: inserted, the previous one superseded — never an in-place edit).
    refresh: bool = False
    #: ``None`` = use the plan gate (Pro gets the AI evidence review, free
    #: tier gets the deterministic stages). Explicit true/false override it
    #: (free users can still *ask* for AI; the plan decides).
    ai: Optional[bool] = None
    persona_id: Optional[int] = None


@router.post("/jobs/{job_id}/match")
async def compute_match(
    job_id: int,
    payload: MatchRequest,
    user: CurrentUser,
    db: DbSession,
):
    """
    Run the three stages (hard filters → deterministic score → AI evidence
    review) for one job and persist the explained verdict.

    The response carries the score *and* its full explanation: stage-1 checks
    with itemised penalties, stage-2 feature contributions, matched/missing
    required skills, and — when the model ran — the guarded evidence review.
    The number is an **estimated fit**, and the row's ``disclaimer`` says so.
    """
    job = _job_or_404(db, user.id, job_id)
    plan = get_user_plan(db, user.id)
    ai_allowed = can(db, user.id, "can_use_advanced_matching")
    use_ai = ai_allowed if payload.ai is None else (payload.ai and ai_allowed)
    plan_gated = bool(payload.ai or payload.ai is None) and not ai_allowed

    persona = None
    if payload.persona_id is not None:
        persona = persona_service.get_persona(db, user.id, payload.persona_id)
        if persona is None:
            raise HTTPException(404, "Persona not found")

    # Idempotent by construction: identical inputs (profile sha, JD sha,
    # scorer version, persona) return the stored row and charge nothing — the
    # check runs *before* the AI stage, so a repeated click is free work.
    result = await matching.compute_match(db, user.id, job, ai=use_ai, persona=persona, force=payload.refresh)
    if plan_gated:
        result["plan_gated"] = True
        result["upgrade_hint"] = UPGRADE_HINT
    result["plan"] = plan
    return result


@router.get("/jobs/{job_id}/match")
def get_match(
    job_id: int,
    user: CurrentUser,
    db: DbSession,
    history: int = Query(0, ge=0, le=50, description="1..50 to include previous verdicts"),
    persona_id: Optional[int] = None,
):
    """The current explained verdict for a job (plus its history when asked)."""
    job = _job_or_404(db, user.id, job_id)
    current = matching.get_current_match(db, user.id, job.id, persona_id)
    payload: Dict[str, Any] = {
        "job_id": job.id,
        "title": job.title,
        "company": job.company,
        "persona_id": persona_id,
        "current": current,
        # The user's latest correction/outcome on this job — a wrong
        # recommendation is visibly corrected, not silently re-ranked.
        "user_feedback": matching.latest_feedback(db, user.id, job.id),
        "actions": {
            "rescore": {"allowed": True, "route": f"/api/jobs/{job.id}/match", "method": "POST",
                        "body": {"refresh": True}},
            "feedback": {"allowed": True, "route": f"/api/jobs/{job.id}/match/feedback", "method": "POST",
                         "kinds": list(MATCH_FEEDBACK_KINDS)},
        },
        "server_time": datetime.utcnow(),
    }
    if current is not None and current.get("staleness") != "fresh":
        payload["actions"]["rescore"]["stale"] = current["staleness"]
    if history:
        payload["history"] = matching.list_match_history(db, user.id, job.id, persona_id, limit=history)
    return payload


# --------------------------------------------------------------------------- #
# Cross-job read
# --------------------------------------------------------------------------- #
@router.get("/matches")
def list_matches(
    user: CurrentUser,
    db: DbSession,
    min_score: Optional[float] = Query(None, ge=0, le=100),
    band: Optional[str] = Query(None, pattern="^(strong|good|possible|weak|unknown)$"),
    limit: int = Query(50, ge=1, le=200),
    include_filtered: bool = False,
):
    """
    The cross-job "best matches" read — current verdicts only, highest
    estimated fit first, with the user's corrections (``not_relevant``)
    visibly demoted and every entry carrying its missing *required* skills.
    """
    return matching.list_best_matches(
        db, user.id, limit=limit, min_score=min_score, band=band,
        include_filtered=include_filtered,
    )


# --------------------------------------------------------------------------- #
# Feedback — corrections and the calibration dataset
# --------------------------------------------------------------------------- #
class FeedbackRequest(BaseModel):
    kind: str = Field(pattern="^(relevant|not_relevant|applied|rejected|interview)$")
    #: Why — for ``not_relevant`` this is the correction of record
    #: ("I do not know Kafka, the match claimed I do").
    reason: Optional[str] = Field(default=None, max_length=1000)
    meta: Dict[str, Any] = Field(default_factory=dict)


@router.post("/jobs/{job_id}/match/feedback")
def post_feedback(
    job_id: int,
    payload: FeedbackRequest,
    user: CurrentUser,
    db: DbSession,
):
    """
    Record a user signal against the recommendation.

    * ``relevant`` / ``not_relevant`` correct the recommendation itself — the
      reason is stored on the row, the match read surfaces it, and the
      best-matches list demotes the job. This is the "the system got it
      wrong" path.
    * ``applied`` / ``rejected`` / ``interview`` are outcomes: they build the
      dataset a *future* calibrated interview probability could be based on.
      Until :func:`calibration_status` says there is enough, the product keeps
      saying "estimated fit" — the response repeats the gate's verdict.
    """
    job = _job_or_404(db, user.id, job_id)
    result = matching.record_feedback(db, user.id, job, payload.kind,
                                      reason=payload.reason, meta=payload.meta)
    audit.audit(
        db,
        "match.feedback",
        user=user,
        target=f"{job.company}:{job.title}",
        detail={"job_id": job.id, "kind": payload.kind,
                "has_reason": bool(payload.reason)},
    )
    return result


@router.get("/matches/calibration")
def calibration(user: CurrentUser, db: DbSession):
    """
    Whether a calibrated interview probability could be offered — and how far
    the recorded outcomes are from supporting one.

    The answer is ``calibrated: false`` for this entire release: the gate
    exists so the moment the outcome dataset grows past the minimum, the
    product can start calibrating *and say so honestly* instead of having
    claimed a probability all along.
    """
    return {
        **matching.calibration_status(db, user.id),
        "scorer_versions": _versions_with_outcomes(db, user.id),
    }


def _versions_with_outcomes(db: Session, user_id: int) -> List[Dict[str, Any]]:
    rows = (
        db.query(MatchFeedback.scorer_version, MatchFeedback.kind)
        .filter(MatchFeedback.user_id == user_id)
        .all()
    )
    per: Dict[str, Dict[str, int]] = {}
    for version, kind in rows:
        bucket = per.setdefault(version or "unknown", {"applied": 0, "rejected": 0, "interview": 0})
        if kind in bucket:
            bucket[kind] += 1
    out = []
    for version in sorted(per):
        bucket = per[version]
        out.append({
            "scorer_version": version,
            "applied": bucket["applied"],
            "rejected": bucket["rejected"],
            "interview": bucket["interview"],
            "outcomes": bucket["rejected"] + bucket["interview"],
            "sufficient": (bucket["rejected"] + bucket["interview"])
            >= matching.MIN_OUTCOMES_FOR_CALIBRATION,
        })
    return out
