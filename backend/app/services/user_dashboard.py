"""
The end-user dashboard read (``GET /api/me/dashboard``).

This is the *product* surface: one tenant-scoped aggregate that answers "what
is my job search doing and what should I do next" — profile completeness,
discovery status, top matches with the reasons behind them, work waiting on the
user, applications in flight, interview activity, the week's outcomes and
follow-up reminders.

Deliberately **not** on this surface: AI provider configuration, token/cost
accounting, queue internals, worker health — those live behind owner-only
endpoints (``/api/admin/*``, ``/api/ops/status``). A job seeker's success
metric is interviews and applications, not tokens.

Everything here is tenant-scoped by construction: every query filters on
``user_id == caller``.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.models import (
    ApplicationPacket,
    ApplicationTracking,
    Email,
    InterviewPrep,
    Job,
    MatchResult,
    PipelineJob,
    Profile,
    Resume,
    UserInputRequest,
)
from app.services.application_tracking import (
    STATE_LABELS,
    upcoming_follow_ups,
)

#: Match score at which a discovery is called a "strong match" in user language.
STRONG_MATCH_SCORE = 75.0

#: How long after applying with no response the dashboard suggests a follow-up.
FOLLOW_UP_SUGGESTION_DAYS = 7

_PROFILE_WEIGHTS = [
    ("name", "Your name", 10),
    ("email", "Contact email", 10),
    ("phone", "Phone number", 10),
    ("location", "Location", 10),
    ("current_title", "Current job title", 10),
    ("summary", "Professional summary", 10),
    ("skills", "Your skills", 15),
    ("experience", "Work experience", 15),
    ("education", "Education", 10),
]


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def profile_completeness(db: Session, user_id: int) -> Dict[str, Any]:
    """Percent complete + what is missing, in the user's own words."""
    profile = (
        db.query(Profile).filter(Profile.user_id == user_id)
        .order_by(Profile.created_at.desc()).first()
    )
    data: Dict[str, Any] = (profile.data if profile else {}) or {}
    missing: List[Dict[str, Any]] = []
    earned = 0
    for key, label, weight in _PROFILE_WEIGHTS:
        value = data.get(key)
        filled = bool(value) if not isinstance(value, (list, dict)) else len(value) > 0
        if filled:
            earned += weight
        else:
            missing.append({"field": key, "label": label, "weight": weight})
    has_master_resume = (
        db.query(Resume).filter(Resume.user_id == user_id, Resume.type == "master").count() > 0
    )
    if has_master_resume:
        earned += 10
    else:
        missing.append({"field": "master_resume", "label": "Master resume upload", "weight": 10})

    if not missing:
        next_step = "Your profile is complete — discovery and matching have everything they need."
    else:
        next_step = f"Add your {missing[0]['label'].lower()} to improve your matches."

    return {
        "percent": int(round(earned)),
        "missing": missing,
        "has_master_resume": has_master_resume,
        "next_step": next_step,
        "profile_id": profile.id if profile else None,
    }


def discovery_status(db: Session, user_id: int) -> Dict[str, Any]:
    """Where the user's job discovery stands, without queue internals."""
    week_ago = datetime.utcnow() - timedelta(days=7)
    last_run = (
        db.query(PipelineJob).filter(PipelineJob.user_id == user_id, PipelineJob.pipeline == "discovery")
        .order_by(PipelineJob.updated_at.desc()).first()
    )
    total = db.query(Job).filter(Job.user_id == user_id).count()
    new_matches = db.query(Job).filter(
        Job.user_id == user_id, Job.status == "discovered", Job.discovered_at >= week_ago,
    ).count()
    strong = db.query(Job).filter(
        Job.user_id == user_id, Job.score >= STRONG_MATCH_SCORE,
        Job.status.in_(("discovered", "queued", "preparing", "needs_input", "ready_to_apply")),
    ).count()
    never_discovered = total == 0 and last_run is None

    state = "idle"
    detail = "Discovery has not run yet — find your first matches to get started."
    if last_run is not None:
        run_state = (last_run.status or "").lower()
        if run_state in ("queued", "processing"):
            state = "running"
            detail = "We are searching for new matches right now."
        elif run_state == "paused":
            state = "paused"
            detail = "Discovery is paused — it will resume automatically when the assistant is back online."
        elif run_state in ("failed", "dead"):
            state = "attention"
            detail = "The last search could not finish. You can retry it from the Jobs page."
        else:
            state = "results"
            detail = "Fresh matches are waiting on your Jobs page."

    return {
        "state": state,
        "detail": detail,
        "total_discovered": total,
        "new_matches_7d": new_matches,
        "strong_matches": strong,
        "last_run_at": _iso(last_run.updated_at if last_run else None),
        "last_run_state": (last_run.status or "") if last_run else "",
        "never_discovered": never_discovered,
    }


def _match_why(row: MatchResult) -> List[str]:
    """Human reasons behind a match — the honest subset, highest signal first."""
    reasons: List[str] = []
    rubric = row.rubric or {}
    if row.reason:
        reasons.append(str(row.reason).strip())
    recommendation = rubric.get("recommendation")
    if recommendation and recommendation not in reasons:
        reasons.append(str(recommendation))
    for contribution in (rubric.get("contributions") or [])[:3]:
        text = contribution.get("reason") or contribution.get("detail") or ""
        if text:
            label = contribution.get("feature", "").replace("_", " ")
            reasons.append(f"Strong {label}: {text}" if label else str(text))
    matched = [s.get("name") for s in (row.matched_skills or []) if s.get("name")]
    if matched:
        reasons.append("Your profile matches: " + ", ".join(str(s) for s in matched[:6]))
    review = row.ai_review or {}
    for strength in (review.get("strengths") or [])[:2]:
        if isinstance(strength, str) and strength.strip():
            reasons.append(strength.strip())
        elif isinstance(strength, dict) and strength.get("text"):
            reasons.append(str(strength["text"]).strip())
    seen: set[str] = set()
    unique: List[str] = []
    for reason in reasons:
        key = reason.lower()
        if reason and key not in seen:
            seen.add(key)
            unique.append(reason)
    return unique[:4]


def top_matches(db: Session, user_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    """The current best matches for this user, each with its "why" and gaps."""
    rows = (
        db.query(MatchResult, Job)
        .join(Job, Job.id == MatchResult.job_id)
        .filter(
            MatchResult.user_id == user_id,
            MatchResult.is_current.is_(True),
            Job.status.notin_(("skipped", "failed")),
        )
        .order_by(MatchResult.score.desc(), MatchResult.id.desc())
        .limit(max(1, min(limit, 10)) * 2)
        .all()
    )
    out: List[Dict[str, Any]] = []
    for match, job in rows:
        if match.flags and match.flags.get("hard_filtered"):
            continue
        out.append({
            "job_id": job.id,
            "title": job.title,
            "company": job.company,
            "location": job.location or "",
            "score": round(float(match.score or 0.0)),
            "band": match.band,
            "status": job.status,
            "score_source": match.score_source,
            "why": _match_why(match),
            "missing_skills": [
                s.get("name") for s in (match.missing_skills or []) if s.get("required")
            ][:4],
            "discovered_at": _iso(job.discovered_at),
            "route": f"/jobs?job={job.id}",
        })
        if len(out) >= limit:
            break
    return out


def jobs_requiring_review(db: Session, user_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    """Postings parked because the user's answer (or approval) is the missing piece."""
    rows = (
        db.query(Job).filter(Job.user_id == user_id, Job.status == "needs_input")
        .order_by(Job.updated_at.desc() if hasattr(Job, "updated_at") else Job.id.desc())
        .limit(limit).all()
    )
    items: List[Dict[str, Any]] = []
    for job in rows:
        pending = (
            db.query(UserInputRequest)
            .filter(UserInputRequest.user_id == user_id,
                    UserInputRequest.job_id == job.id,
                    UserInputRequest.status == "pending")
            .count()
        )
        reason = f"{pending} question(s) waiting for your answers" if pending else "Needs your review before we continue"
        items.append({
            "job_id": job.id,
            "title": job.title,
            "company": job.company,
            "reason": reason,
            "pending_questions": pending,
            "route": f"/queues?job={job.id}",
        })
    return items


_APPLICATION_ACTIVE_STATES = (
    "queued", "preparing", "blocked_input", "blocked_resume_approval", "blocked_consent",
    "blocked_credential", "blocked_policy", "blocked_quota", "prepared", "awaiting_approval",
    "submitting",
)

_JOB_ACTIVE_STATES = ("queued", "preparing", "ready_to_apply")

_STAGE_LABELS = {
    "queued": "Queued for preparation",
    "preparing": "Preparing your application",
    "ready_to_apply": "Ready to submit",
    "prepared": "Application packet ready",
    "awaiting_approval": "Waiting for your approval",
    "submitting": "Being submitted",
    "blocked_input": "Waiting for your answers",
    "blocked_resume_approval": "Waiting for resume approval",
    "blocked_consent": "Waiting for your consent",
    "blocked_credential": "Waiting for site credentials",
    "blocked_policy": "Waiting on your automation rules",
    "blocked_quota": "Waiting for your plan quota to reset",
}


def applications_in_progress(db: Session, user_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    """Applications moving through the pipeline right now, with a human stage."""
    items: List[Dict[str, Any]] = []
    seen_jobs: set[int] = set()

    rows = (
        db.query(ApplicationTracking, Job)
        .join(Job, Job.id == ApplicationTracking.job_id)
        .filter(
            ApplicationTracking.user_id == user_id,
            ApplicationTracking.state.in_(_APPLICATION_ACTIVE_STATES),
        )
        .order_by(ApplicationTracking.updated_at.desc())
        .limit(limit * 2)
        .all()
    )
    for tracking, job in rows:
        seen_jobs.add(job.id)
        items.append({
            "job_id": job.id,
            "title": tracking.job_title_snapshot or job.title,
            "company": tracking.company_snapshot or job.company,
            "stage": tracking.state,
            "stage_label": _STAGE_LABELS.get(tracking.state, STATE_LABELS.get(tracking.state, tracking.state)),
            "updated_at": _iso(tracking.updated_at),
            "route": f"/tracking?tracking_id={tracking.id}",
        })
        if len(items) >= limit:
            return items

    if len(items) < limit:
        jobs = (
            db.query(Job).filter(Job.user_id == user_id, Job.status.in_(_JOB_ACTIVE_STATES))
            .order_by(Job.id.desc()).limit(limit * 2).all()
        )
        for job in jobs:
            if job.id in seen_jobs:
                continue
            items.append({
                "job_id": job.id,
                "title": job.title,
                "company": job.company,
                "stage": job.status,
                "stage_label": _STAGE_LABELS.get(job.status, job.status),
                "updated_at": _iso(job.applied_at),
                "route": f"/jobs?job={job.id}",
            })
            if len(items) >= limit:
                break
    return items


def action_queue(db: Session, user_id: int, limit: int = 8) -> List[Dict[str, Any]]:
    """
    Everything waiting on *the user*, in one prioritised list — the actionable
    replacement for infrastructure status.
    """
    items: List[Dict[str, Any]] = []

    for request in (
        db.query(UserInputRequest, Job)
        .outerjoin(Job, Job.id == UserInputRequest.job_id)
        .filter(UserInputRequest.user_id == user_id, UserInputRequest.status == "pending")
        .order_by(UserInputRequest.created_at.asc())
        .limit(limit).all()
    ):
        req, job = request
        fields = req.fields or []
        required = sum(1 for f in fields if isinstance(f, dict) and f.get("required"))
        items.append({
            "kind": "questions",
            "id": req.id,
            "title": f"Answer {len(fields)} question(s) for {job.company or 'an application'}" if job
                     else f"Answer {len(fields)} question(s)",
            "subtitle": job.title if job else "",
            "detail": f"{required} required" if required else "All fields optional",
            "created_at": _iso(req.created_at),
            "route": "/queues",
        })

    for packet in (
        db.query(ApplicationPacket)
        .filter(ApplicationPacket.user_id == user_id,
                ApplicationPacket.status == "pending_approval",
                ApplicationPacket.is_current.is_(True))
        .order_by(ApplicationPacket.created_at.asc()).limit(limit).all()
    ):
        items.append({
            "kind": "packet_approval",
            "id": packet.id,
            "title": f"Review the application for {packet.company or packet.job_title or 'a role'}",
            "subtitle": packet.job_title or "",
            "detail": "Approve the packet to continue",
            "created_at": _iso(packet.created_at),
            "route": f"/packets?packet={packet.id}",
        })

    for resume in (
        db.query(Resume)
        .filter(Resume.user_id == user_id, Resume.status == "pending")
        .order_by(Resume.created_at.asc()).limit(limit).all()
    ):
        items.append({
            "kind": "resume_approval",
            "id": resume.id,
            "title": f"Review the tailored resume: {resume.display_name or resume.filename}",
            "subtitle": resume.type or "",
            "detail": "Approve it before it is used",
            "created_at": _iso(resume.created_at),
            "route": "/resumes",
        })

    for email_row in (
        db.query(Email)
        .filter(Email.user_id == user_id, Email.status == "pending_approval")
        .order_by(Email.id.asc()).limit(limit).all()
    ):
        items.append({
            "kind": "email_approval",
            "id": email_row.id,
            "title": f"Review the draft email to {email_row.to_name or email_row.to_email}",
            "subtitle": email_row.subject or "",
            "detail": "Nothing is sent until you approve",
            "created_at": None,
            "route": "/emails",
        })

    priority = {"questions": 0, "packet_approval": 1, "resume_approval": 2, "email_approval": 3}
    items.sort(key=lambda i: (priority.get(i["kind"], 9),))
    return items[:limit]


def interview_activity(db: Session, user_id: int, limit: int = 3) -> Dict[str, Any]:
    """Recorded interviews (the user's timeline is the source) + practice sessions."""
    upcoming = (
        db.query(ApplicationTracking, Job)
        .join(Job, Job.id == ApplicationTracking.job_id)
        .filter(
            ApplicationTracking.user_id == user_id,
            ApplicationTracking.interview_at.is_not(None),
            ApplicationTracking.interview_completed_at.is_(None),
        )
        .order_by(ApplicationTracking.interview_at.asc())
        .limit(limit).all()
    )
    completed = (
        db.query(ApplicationTracking, Job)
        .join(Job, Job.id == ApplicationTracking.job_id)
        .filter(
            ApplicationTracking.user_id == user_id,
            ApplicationTracking.interview_completed_at.is_not(None),
        )
        .order_by(ApplicationTracking.interview_completed_at.desc())
        .limit(limit).all()
    )
    preps = (
        db.query(InterviewPrep).filter(InterviewPrep.user_id == user_id)
        .order_by(InterviewPrep.updated_at.desc()).limit(limit).all()
    )
    prep_completed = (
        db.query(InterviewPrep)
        .filter(InterviewPrep.user_id == user_id, InterviewPrep.status == "completed").count()
    )
    return {
        "upcoming": [
            {
                "job_id": job.id,
                "title": tracking.job_title_snapshot or job.title,
                "company": tracking.company_snapshot or job.company,
                "when": _iso(tracking.interview_at),
                "state": tracking.state,
                "route": f"/tracking?tracking_id={tracking.id}",
            }
            for tracking, job in upcoming
        ],
        "recent": [
            {
                "job_id": job.id,
                "title": tracking.job_title_snapshot or job.title,
                "company": tracking.company_snapshot or job.company,
                "completed_at": _iso(tracking.interview_completed_at),
                "route": f"/tracking?tracking_id={tracking.id}",
            }
            for tracking, job in completed
        ],
        "practice_sessions": preps and [
            {
                "id": prep.id,
                "title": prep.job_title or "Practice session",
                "company": prep.company or "",
                "status": prep.status,
                "updated_at": _iso(prep.updated_at),
                "route": f"/interview?session={prep.id}",
            }
            for prep in preps
        ] or [],
        "practice_completed": prep_completed,
        "upcoming_count": len(upcoming),
    }


def weekly_outcome_report(db: Session, user_id: int) -> Dict[str, Any]:
    """
    The week in *outcomes* — applications, responses, interviews — never token
    accounting. The headline number is interviews; applications and responses
    give the funnel that produced them.
    """
    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)
    prev_week_ago = week_ago - timedelta(days=7)

    def _count(model, *filters):  # tiny local helper — SQL count with filters
        return db.query(model).filter(model.user_id == user_id, *filters).count()

    applications = _count(Job, Job.applied_at.is_not(None), Job.applied_at >= week_ago)
    prev_applications = _count(Job, Job.applied_at.is_not(None), Job.applied_at >= prev_week_ago,
                               Job.applied_at < week_ago)
    new_matches = _count(Job, Job.status == "discovered", Job.discovered_at >= week_ago)
    strong_matches = db.query(Job).filter(
        Job.user_id == user_id, Job.score >= STRONG_MATCH_SCORE,
        Job.status.notin_(("skipped", "failed")),
    ).count()
    responses = _count(ApplicationTracking,
                       ApplicationTracking.first_response_at.is_not(None),
                       ApplicationTracking.first_response_at >= week_ago)
    interviews = _count(ApplicationTracking,
                        ApplicationTracking.interview_scheduled_at.is_not(None),
                        ApplicationTracking.interview_scheduled_at >= week_ago)
    rejections = _count(ApplicationTracking,
                        ApplicationTracking.state == "rejected_by_employer",
                        ApplicationTracking.outcome_at >= week_ago)
    emails_sent = _count(Email, Email.status == "sent", Email.created_at >= week_ago)
    replies = db.query(Email).filter(
        Email.user_id == user_id, Email.opens > 0, Email.created_at >= week_ago
    ).count()

    top = top_matches(db, user_id, limit=1)
    top_line = ""
    if applications:
        top_line = f"You submitted {applications} application{'s' if applications != 1 else ''} this week."
        if responses:
            top_line += f" {responses} responded."
        if interviews:
            top_line += f" {interviews} interview{'s' if interviews != 1 else ''} — nice work."
    elif new_matches:
        top_line = f"{new_matches} new match{'es' if new_matches != 1 else ''} this week — pick one and prepare an application."
    else:
        top_line = "No new outcomes this week yet — run discovery and review your top matches."

    return {
        "week_started_at": _iso(week_ago),
        "generated_at": _iso(now),
        "applications_submitted": applications,
        "applications_prev_week": prev_applications,
        "responses": responses,
        "interviews": interviews,
        "rejections": rejections,
        "new_matches": new_matches,
        "strong_matches_open": strong_matches,
        "outreach_sent": emails_sent,
        "outreach_replies": replies,
        "headline": top_line,
        "top_match": top[0] if top else None,
    }


def follow_up_reminders(db: Session, user_id: int, limit: int = 5) -> Dict[str, Any]:
    """The user's promised follow-ups — plus gentle suggestions they did not promise."""
    promised = upcoming_follow_ups(db, user_id, days=14, limit=limit, include_overdue=True)

    suggested: List[Dict[str, Any]] = []
    if len(promised) < limit:
        cutoff = datetime.utcnow() - timedelta(days=FOLLOW_UP_SUGGESTION_DAYS)
        rows = (
            db.query(ApplicationTracking, Job)
            .join(Job, Job.id == ApplicationTracking.job_id)
            .filter(
                ApplicationTracking.user_id == user_id,
                ApplicationTracking.applied_at.is_not(None),
                ApplicationTracking.applied_at <= cutoff,
                ApplicationTracking.follow_up_at.is_(None),
                ApplicationTracking.first_response_at.is_(None),
                ApplicationTracking.state.notin_(("rejected_by_employer", "offer_received", "withdrawn")),
            )
            .order_by(ApplicationTracking.applied_at.asc())
            .limit(limit - len(promised)).all()
        )
        for tracking, job in rows:
            days = (datetime.utcnow() - tracking.applied_at).days
            suggested.append({
                "tracking_id": tracking.id,
                "job_id": job.id,
                "title": tracking.job_title_snapshot or job.title,
                "company": tracking.company_snapshot or job.company,
                "state": tracking.state,
                "state_label": STATE_LABELS.get(tracking.state, tracking.state),
                "due_at": None,
                "note": "",
                "overdue": False,
                "notified_at": None,
                "suggested": True,
                "route": f"/tracking?tracking_id={tracking.id}",
                "suggestion": f"Applied {days} day{'s' if days != 1 else ''} ago with no response yet — a short follow-up can help.",
            })
    return {"items": promised + suggested, "overdue_count": sum(1 for i in promised if i.get("overdue"))}


def assistant_state(db: Session, user_id: int) -> Dict[str, Any]:
    """Whether AI-assisted work is available, in user language — no provider detail."""
    from app.services.ai_client import is_configured
    from app.services.job_queue import paused_count

    configured = is_configured(db=db, user_id=user_id)
    paused = paused_count(db, user_id=user_id)
    if not configured:
        return {"configured": False, "paused_items": int(paused or 0),
                "message": "Your assistant is not set up yet — ask the workspace owner to connect it."}
    return {
        "configured": True,
        "paused_items": int(paused or 0),
        "message": ("Some work is paused and will resume automatically."
                    if (paused or 0) > 0 else
                    "Your assistant is ready to help."),
    }


def build_dashboard(db: Session, user_id: int) -> Dict[str, Any]:
    """The single aggregate the user dashboard renders."""
    from sqlalchemy import func

    from app.services.job_queue import queue_stats

    stats = queue_stats(db, user_id=user_id)
    application = stats.get("application", {})
    status_rows = (
        db.query(Job.status, func.count(Job.id))
        .filter(Job.user_id == user_id)
        .group_by(Job.status)
        .all()
    )
    status_counts: Dict[str, int] = {str(status): int(n) for status, n in status_rows}

    return {
        "server_time": _iso(datetime.utcnow()),
        "profile": profile_completeness(db, user_id),
        "discovery": discovery_status(db, user_id),
        "top_matches": top_matches(db, user_id),
        "needs_review": jobs_requiring_review(db, user_id),
        "applications_in_progress": applications_in_progress(db, user_id),
        "action_queue": action_queue(db, user_id),
        "interviews": interview_activity(db, user_id),
        "weekly_report": weekly_outcome_report(db, user_id),
        "follow_ups": follow_up_reminders(db, user_id),
        "assistant": assistant_state(db, user_id),
        "counts": {
            "jobs_total": sum(status_counts.values()),
            "needs_input": status_counts.get("needs_input", 0),
            "applied": status_counts.get("applied", 0),
            "applications_running": application.get("queued", 0) + application.get("processing", 0),
        },
    }
