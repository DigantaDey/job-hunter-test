"""Application tracking — the lifecycle & outcome-feedback API.

``docs/contracts/07-application-state-machine.md`` §11, ``docs/contracts/13``.

The surface is small on purpose. One record per ``(user, job)``, an append-only
timeline, and a handful of writes that each mean one thing a job seeker actually
says:

* ``POST /application-tracking``               — start tracking a job
* ``POST …/{id}/status``                       — "it's now X" (the machine validates)
* ``POST …/{id}/interview``                    — **"I got an interview"**
* ``POST …/{id}/interview-complete``           — the interview happened
* ``POST …/{id}/response``                     — a recruiter replied
* ``POST …/{id}/rejection`` / ``…/offer`` / ``…/withdraw``
* ``POST …/{id}/note``                         — free text, no state change
* ``POST …/{id}/follow-up``                    — promise a reminder (notifications)
* ``POST …/{id}/correction``                   — manual correction, with a reason
* ``POST …/{id}/attribution``                  — correct the *source* of the application
* ``GET  …/{id}``                              — the whole document, timeline included
* ``GET  …/{id}/events?after_sequence=``       — keyset-paginated timeline
* ``GET  /application-tracking/report``        — application→interview conversion, grouped
* ``GET  /application-tracking/follow-ups``    — the reminder agenda

Every write returns the document it changed plus the events it emitted
(contracts/13 §7 rule 7), and every refusal is a typed ``code`` the SPA can show
as-is: ``invalid_state_transition`` (409) with the states that *are* allowed,
``origin_not_trusted`` (422) when an inference tries to claim an interview,
``correction_reason_required`` (422), ``tracking_not_found`` (404).

Nothing here reads email. An interview is only ever recorded by the user, by our
own submission machinery, or by a verified integration — see
:func:`app.services.application_tracking.record_email_suggestion` for the one
guarded path an inbound message may take, and the test that pins it.
"""
from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any, Dict, Iterator, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.contracts.vocabulary import (
    APPLICATION_TRACKING_STATES,
    IDEMPOTENCY_HEADER,
    INTERVIEW_FORMATS,
    INTERVIEW_SELF_ASSESSMENTS,
    RESPONSE_CHANNELS,
    SUBMISSION_CHANNELS,
    TRACKING_MANUAL_SOURCES,
    TRACKING_ORIGINS,
    TRACKING_PHASES,
)
from app.core import audit
from app.core.logging import get_logger, request_id_var
from app.models.models import ApplicationTracking, Job
from app.services import application_tracking as tracking
from app.services.application_tracking import STATE_LABELS

router = APIRouter(prefix="/application-tracking", tags=["application-tracking"])
log = get_logger("app.tracking.api")

#: How many timeline rows a write response carries back (the document itself
#: carries the newest 20; a write returns just what it emitted).
WRITE_TIMELINE_LIMIT = 20


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class OpenRequest(BaseModel):
    """Start tracking a job. Idempotent: an existing record is returned as-is."""

    job_id: int = Field(..., description="The job to track (must belong to the caller)")
    #: Where the application really came from, when it is not the job's board id.
    source: Optional[str] = Field(None, max_length=60)
    channel: Optional[str] = Field(None, description="automation | manual_user | assisted_dry_run")
    note: Optional[str] = Field(None, max_length=4000)
    #: Open *and* move in one write (``applied`` is the usual case).
    state: str = Field("not_applied")
    occurred_at: Optional[datetime] = None


class StatusRequest(BaseModel):
    """A generic status change. The state machine decides whether it is legal."""

    state: str = Field(..., description="One of APPLICATION_TRACKING_STATES")
    note: Optional[str] = Field(None, max_length=4000)
    occurred_at: Optional[datetime] = None
    follow_up_at: Optional[datetime] = None
    follow_up_note: str = Field("", max_length=400)
    #: ``user_reported`` by default: a status typed into the app is the user's claim.
    origin: str = Field("user_reported")
    channel: Optional[str] = None
    source: Optional[str] = Field(None, max_length=60)


class InterviewRequest(BaseModel):
    """"I got an interview." The date is what the reminder is built from."""

    interview_at: Optional[datetime] = None
    format: str = Field("", description="phone | video | onsite | take_home | assessment | panel | other")
    round_name: str = Field("", max_length=60)
    interviewer_role: str = Field("", max_length=120)
    location: str = Field("", max_length=200)
    note: Optional[str] = Field(None, max_length=4000)
    occurred_at: Optional[datetime] = None
    follow_up_at: Optional[datetime] = None
    follow_up_note: str = Field("", max_length=400)


class InterviewCompleteRequest(BaseModel):
    self_assessment: str = Field("", description="went_well | mixed | went_poorly | unknown")
    next_step: str = Field("", max_length=120)
    note: Optional[str] = Field(None, max_length=4000)
    occurred_at: Optional[datetime] = None
    follow_up_at: Optional[datetime] = None
    follow_up_note: str = Field("", max_length=400)


class ResponseRequest(BaseModel):
    channel: str = Field("", description="email | phone | linkedin | portal | sms | in_person | other")
    response_kind: str = Field("", max_length=30)
    summary: str = Field("", max_length=2000, description="What was said — stored as a note, never parsed")
    note: Optional[str] = Field(None, max_length=4000)
    occurred_at: Optional[datetime] = None
    follow_up_at: Optional[datetime] = None
    follow_up_note: str = Field("", max_length=400)


class OutcomeRequest(BaseModel):
    """Shared body for rejection / offer / withdrawal."""

    reason: str = Field("", max_length=200)
    note: Optional[str] = Field(None, max_length=4000)
    occurred_at: Optional[datetime] = None


class NoteRequest(BaseModel):
    note: str = Field(..., min_length=1, max_length=4000)
    occurred_at: Optional[datetime] = None


class FollowUpRequest(BaseModel):
    follow_up_at: datetime = Field(..., description="When to be reminded")
    note: str = Field("", max_length=400)


class DoneRequest(BaseModel):
    """Closing a follow-up: the note is optional (the row already carries it)."""

    note: Optional[str] = Field(None, max_length=4000)


class CorrectionRequest(BaseModel):
    """A manual correction. The reason is required — it changes the report."""

    state: str = Field(..., description="The state this application is actually in")
    #: Required *in meaning*, not in shape: the service raises the typed
    #: ``correction_reason_required`` error, so a refusal reads the same whether
    #: the caller came through HTTP or through the service directly.
    reason: str = Field("", max_length=400)
    #: The timeline row being corrected (default: the last state-changing one).
    correction_of_event_id: Optional[int] = None
    note: Optional[str] = Field(None, max_length=4000)
    occurred_at: Optional[datetime] = None


class AttributionRequest(BaseModel):
    source: Optional[str] = Field(None, max_length=60)
    channel: Optional[str] = None
    reason: str = Field("", max_length=400)


class SuggestionRequest(BaseModel):
    reason: str = Field("", max_length=400)
    state: Optional[str] = None
    note: Optional[str] = Field(None, max_length=4000)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _tracked() -> Iterator[None]:
    """Translate a service refusal into the contract's error payload."""
    try:
        yield
    except tracking.TrackingError as exc:
        raise HTTPException(exc.http_status, exc.payload()) from exc


def _record_or_404(db, user, tracking_id: int):
    with _tracked():
        return tracking.get_tracking(db, int(user.id), int(tracking_id))


def _job_or_404(db, user, job_id: int) -> Job:
    job = db.query(Job).filter(Job.id == int(job_id), Job.user_id == int(user.id)).first()
    if job is None:
        raise HTTPException(404, {"code": "job_not_found", "message": "Job not found"})
    return job


def _request_id() -> str:
    return (request_id_var.get() or "")[:64]


def _idempotency_key(request: Request) -> Optional[str]:
    """The client's retry key, when it sent one (contracts/01 §Idempotency)."""
    raw = request.headers.get(IDEMPOTENCY_HEADER) or ""
    return raw.strip()[:64] or None


def _respond(db, user, result: Dict[str, Any], *, record: Optional[ApplicationTracking] = None,
             audit_action: Optional[str] = None, request: Optional[Request] = None,
             audit_detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The one write-response shape: the document, plus what this call emitted."""
    row: Optional[ApplicationTracking] = record if record is not None else result.get("record")
    if row is None:  # pragma: no cover - every service write returns the record
        raise HTTPException(500, {"code": "tracking_record_missing",
                                  "message": "The write finished without a tracking record."})
    event = result.get("event")
    document = tracking.to_document(db, user, row, timeline_limit=WRITE_TIMELINE_LIMIT)
    if audit_action:
        audit.audit(db, audit_action, user=user, target=f"tracking:{row.id}",
                    detail={"tracking_id": int(row.id), "job_id": int(row.job_id),
                            "state": row.state, "duplicate": bool(result.get("duplicate")),
                            **(audit_detail or {})},
                    request=request, commit=False)
        db.commit()
    return {
        "ok": True,
        "duplicate": bool(result.get("duplicate")),
        "state_changed": bool(result.get("state_changed")),
        "event": tracking.event_to_dict(event) if event is not None else None,
        "events": [tracking.event_to_dict(event)] if event is not None else [],
        "document": document,
        **{key: value for key, value in document.items() if key in ("state", "phase", "server_time")},
    }


# --------------------------------------------------------------------------- #
# Reads — static paths first, so "/report" is never parsed as an id
# --------------------------------------------------------------------------- #
@router.get("")
def list_tracking(
    user: CurrentUser,
    db: DbSession,
    state: Optional[str] = Query(None, description="Filter by APPLICATION_TRACKING_STATES"),
    phase: Optional[str] = Query(None, description="Filter by TRACKING_PHASES"),
    source: Optional[str] = Query(None),
    job_id: Optional[int] = Query(None),
    q: Optional[str] = Query(None, description="Title / company / note substring"),
    due_only: bool = Query(False, description="Only records with an open follow-up"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """The tracking board, as a paginated envelope (contracts/01 §4)."""
    with _tracked():
        rows, total = tracking.list_records(
            db, int(user.id), state=state, phase=phase, source=source, job_id=job_id,
            q=q, due_only=due_only, limit=page_size, offset=(page - 1) * page_size,
        )
    items = [tracking.to_document(db, user, row, timeline_limit=0) for row in rows]
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": bool(page * page_size < total),
        "counts": tracking.state_counts(db, int(user.id)),
        "states": list(APPLICATION_TRACKING_STATES),
        "state_labels": dict(STATE_LABELS),
        "server_time": tracking.iso_utc(datetime.utcnow()),
    }


@router.get("/meta")
def tracking_meta(user: CurrentUser, db: DbSession):
    """The vocabulary, the transition table and the report's group keys.

    Server-owned so two clients cannot word the same fact differently
    (contracts/13 §7 rule 3) — and so the SPA can render a state this build has
    never seen instead of a blank chip.
    """
    return {
        "states": list(APPLICATION_TRACKING_STATES),
        "state_labels": dict(STATE_LABELS),
        "phases": list(TRACKING_PHASES),
        "origins": list(TRACKING_ORIGINS),
        "transitions": {state: list(tracking.allowed_transitions(state))
                        for state in APPLICATION_TRACKING_STATES},
        "terminal_states": list(tracking.TRACKING_TERMINAL_STATES),
        "interview_states": list(tracking.TRACKING_INTERVIEW_STATES),
        "manual_sources": list(TRACKING_MANUAL_SOURCES),
        "channels": list(SUBMISSION_CHANNELS),
        "interview_formats": list(INTERVIEW_FORMATS),
        "self_assessments": list(INTERVIEW_SELF_ASSESSMENTS),
        "response_channels": list(RESPONSE_CHANNELS),
        "report_group_keys": list(tracking.REPORT_GROUP_KEYS),
        "counts": tracking.state_counts(db, int(user.id)),
        "disclaimer": tracking.REPORT_DISCLAIMER,
    }


@router.get("/report")
def tracking_report(
    user: CurrentUser,
    db: DbSession,
    group_by: str = Query("source", description="source | role | match_band | score_bucket | artifact_version | artifact_kind | channel | state"),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    origin: Optional[str] = Query(None, description="Restrict to one TRACKING_ORIGINS value"),
    state: Optional[str] = Query(None),
    include_unapplied: bool = Query(False, description="Count records that never applied"),
):
    """Application→interview conversion, grouped by source / score / role / artefact.

    The denominator is records that actually applied; the numerator is records
    that reached an interview state — which only ever happens because the user
    (or a verified integration) said so. ``by_origin`` is returned at every
    level, so a conversion built out of memory is distinguishable from one our
    own machinery observed.
    """
    with _tracked():
        return tracking.report(db, int(user.id), group_by=group_by, since=since, until=until,
                               origin=origin, state=state, include_unapplied=include_unapplied)


@router.get("/follow-ups")
def follow_ups(
    user: CurrentUser,
    db: DbSession,
    days: int = Query(14, ge=1, le=90),
    limit: int = Query(50, ge=1, le=200),
    include_overdue: bool = Query(True),
):
    """The reminder agenda — and the sweep that makes a due reminder exist.

    The worker's maintenance pass notifies regardless of whether the app is
    open; this read also runs the (bounded, deduped, tenant-scoped) sweep for the
    requesting user, so a reminder is there by the time they look.
    """
    swept = tracking.notify_due_follow_ups(db, user_id=int(user.id))
    return {
        "items": tracking.upcoming_follow_ups(db, int(user.id), days=days, limit=limit,
                                              include_overdue=include_overdue),
        "sweep": swept,
        "notification_kind": tracking.FOLLOW_UP_NOTIFICATION_KIND,
        "server_time": tracking.iso_utc(datetime.utcnow()),
    }


@router.get("/job/{job_id}")
def tracking_for_job(job_id: int, user: CurrentUser, db: DbSession):
    """The tracking document for a job — or ``exists: false`` and what may be done.

    The Jobs drawer reads this. A ``GET`` never creates a row: "not tracked yet"
    is a real answer, with the action that would start it.
    """
    job = _job_or_404(db, user, job_id)
    record = tracking.for_job(db, int(user.id), int(job.id))
    if record is None:
        return {
            "exists": False,
            "job": {"id": int(job.id), "title": job.title, "company": job.company,
                    "url": job.url or "", "status": job.status, "source": job.source or ""},
            "actions": [{"key": "open", "label": "Track this application", "method": "POST",
                         "route": "/api/application-tracking", "primary": True,
                         "disabled": False, "disabled_reason": None}],
            "snapshot": tracking.build_snapshot(db, int(user.id), job),
            "server_time": tracking.iso_utc(datetime.utcnow()),
        }
    document = tracking.to_document(db, user, record, job=job)
    document["exists"] = True
    return document


@router.get("/{tracking_id}")
def tracking_detail(tracking_id: int, user: CurrentUser, db: DbSession,
                    timeline_limit: int = Query(20, ge=0, le=200)):
    """The application detail document: state, snapshots, timeline, actions."""
    record = _record_or_404(db, user, tracking_id)
    document = tracking.to_document(db, user, record, timeline_limit=timeline_limit)
    document["exists"] = True
    return document


@router.get("/{tracking_id}/events")
def tracking_events(
    tracking_id: int,
    user: CurrentUser,
    db: DbSession,
    after_sequence: int = Query(0, ge=0, description="Keyset cursor — never OFFSET"),
    limit: int = Query(50, ge=1, le=200),
    order: str = Query("asc", pattern="^(asc|desc)$"),
):
    """The append-only timeline (contracts/09)."""
    record = _record_or_404(db, user, tracking_id)
    events = tracking.timeline(db, int(user.id), record, after_sequence=after_sequence,
                               limit=limit, order=order)
    return {
        "tracking_id": int(record.id),
        "items": events,
        "last_sequence": int(record.last_sequence or 0),
        "has_more": bool(events) and order == "asc" and int(events[-1]["sequence"]) < int(record.last_sequence or 0),
        "count": int(record.event_count or 0),
        "corrections": int(record.correction_count or 0),
        "server_time": tracking.iso_utc(datetime.utcnow()),
    }


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
@router.post("")
def open_tracking(body: OpenRequest, request: Request, user: CurrentUser, db: DbSession):
    """Start tracking a job (or move an untracked job straight to a state)."""
    job = _job_or_404(db, user, body.job_id)
    with _tracked():
        record, created = tracking.open_tracking(
            db, user=user, job=job, source=body.source, channel=body.channel, note=body.note,
            state=body.state, occurred_at=body.occurred_at, request_id=_request_id(),
        )
    if created:
        audit.audit(db, "application.tracking_opened", user=user, target=f"job:{job.id}",
                    detail={"tracking_id": int(record.id), "state": record.state,
                            "source": record.source}, request=request)
    return _respond(db, user, {"record": record, "event": None, "duplicate": not created,
                               "state_changed": created}, record=record)


@router.post("/{tracking_id}/status")
def update_status(tracking_id: int, body: StatusRequest, request: Request, user: CurrentUser, db: DbSession):
    """Update an application's status — validated against the transition table."""
    record = _record_or_404(db, user, tracking_id)
    with _tracked():
        if body.source or body.channel:
            tracking.update_attribution(db, user=user, record=record, source=body.source,
                                        channel=body.channel, reason="status update", commit=False)
        result = tracking.change_state(
            db, user=user, record=record, state=body.state, origin=body.origin,
            actor_type="user", trigger="user", note=body.note, occurred_at=body.occurred_at,
            follow_up_at=body.follow_up_at, follow_up_note=body.follow_up_note,
            idempotency_key=_idempotency_key(request), request_id=_request_id(),
        )
    return _respond(db, user, result, request=request, audit_action="application.status_updated",
                    audit_detail={"state_to": body.state, "origin": body.origin})


@router.post("/{tracking_id}/interview")
def add_interview(tracking_id: int, body: InterviewRequest, request: Request, user: CurrentUser, db: DbSession):
    """"I got an interview."

    The write the acceptance criterion names. It moves the record to
    ``interview_scheduled``, records the slot/format/round, optionally promises a
    follow-up reminder, and stores ``origin = user_reported`` — an interview is
    never inferred from anything.
    """
    record = _record_or_404(db, user, tracking_id)
    if body.format and body.format not in INTERVIEW_FORMATS:
        raise HTTPException(422, {"code": "validation_error",
                                  "message": f"'{body.format}' is not an interview format.",
                                  "allowed": list(INTERVIEW_FORMATS)})
    with _tracked():
        result = tracking.record_interview(
            db, user=user, record=record, interview_at=body.interview_at, format=body.format,
            round_name=body.round_name, interviewer_role=body.interviewer_role,
            location=body.location, note=body.note, occurred_at=body.occurred_at,
            follow_up_at=body.follow_up_at, follow_up_note=body.follow_up_note,
            idempotency_key=_idempotency_key(request), request_id=_request_id(),
        )
    return _respond(db, user, result, request=request, audit_action="application.interview_recorded",
                    audit_detail={"interview_at": tracking.iso_utc(body.interview_at),
                                  "format": body.format})


@router.post("/{tracking_id}/interview-complete")
def interview_completed(tracking_id: int, body: InterviewCompleteRequest, request: Request,
                        user: CurrentUser, db: DbSession):
    """Record that the interview happened, with the user's own read on it."""
    record = _record_or_404(db, user, tracking_id)
    if body.self_assessment and body.self_assessment not in INTERVIEW_SELF_ASSESSMENTS:
        raise HTTPException(422, {"code": "validation_error",
                                  "message": f"'{body.self_assessment}' is not a self-assessment.",
                                  "allowed": list(INTERVIEW_SELF_ASSESSMENTS)})
    with _tracked():
        result = tracking.complete_interview(
            db, user=user, record=record, self_assessment=body.self_assessment,
            next_step=body.next_step, note=body.note, occurred_at=body.occurred_at,
            follow_up_at=body.follow_up_at, follow_up_note=body.follow_up_note,
            idempotency_key=_idempotency_key(request), request_id=_request_id(),
        )
    return _respond(db, user, result, request=request, audit_action="application.status_updated",
                    audit_detail={"state_to": "interview_completed"})


@router.post("/{tracking_id}/response")
def recruiter_response(tracking_id: int, body: ResponseRequest, request: Request,
                       user: CurrentUser, db: DbSession):
    """A recruiter replied. Recorded as a response — never as an interview."""
    record = _record_or_404(db, user, tracking_id)
    if body.channel and body.channel not in RESPONSE_CHANNELS:
        raise HTTPException(422, {"code": "validation_error",
                                  "message": f"'{body.channel}' is not a response channel.",
                                  "allowed": list(RESPONSE_CHANNELS)})
    with _tracked():
        result = tracking.record_response(
            db, user=user, record=record, channel=body.channel, response_kind=body.response_kind,
            summary=body.summary, note=body.note, occurred_at=body.occurred_at,
            follow_up_at=body.follow_up_at, follow_up_note=body.follow_up_note,
            idempotency_key=_idempotency_key(request), request_id=_request_id(),
        )
    return _respond(db, user, result, request=request, audit_action="application.status_updated",
                    audit_detail={"state_to": "recruiter_response", "channel": body.channel})


@router.post("/{tracking_id}/rejection")
def rejection(tracking_id: int, body: OutcomeRequest, request: Request, user: CurrentUser, db: DbSession):
    """The employer said no. A closed outcome: corrected, never undone."""
    record = _record_or_404(db, user, tracking_id)
    with _tracked():
        result = tracking.record_rejection(
            db, user=user, record=record, reason=body.reason, note=body.note,
            occurred_at=body.occurred_at, idempotency_key=_idempotency_key(request),
            request_id=_request_id(),
        )
    return _respond(db, user, result, request=request, audit_action="application.status_updated",
                    audit_detail={"state_to": "rejected_by_employer"})


@router.post("/{tracking_id}/offer")
def offer(tracking_id: int, body: OutcomeRequest, request: Request, user: CurrentUser, db: DbSession):
    """An offer arrived."""
    record = _record_or_404(db, user, tracking_id)
    with _tracked():
        result = tracking.record_offer(
            db, user=user, record=record, note=body.note, occurred_at=body.occurred_at,
            idempotency_key=_idempotency_key(request), request_id=_request_id(),
        )
    return _respond(db, user, result, request=request, audit_action="application.status_updated",
                    audit_detail={"state_to": "offer_received"})


@router.post("/{tracking_id}/withdraw")
def withdraw(tracking_id: int, body: OutcomeRequest, request: Request, user: CurrentUser, db: DbSession):
    """You pulled out (including declining an offer)."""
    record = _record_or_404(db, user, tracking_id)
    with _tracked():
        result = tracking.record_withdrawal(
            db, user=user, record=record, reason=body.reason, note=body.note,
            occurred_at=body.occurred_at, idempotency_key=_idempotency_key(request),
            request_id=_request_id(),
        )
    return _respond(db, user, result, request=request, audit_action="application.status_updated",
                    audit_detail={"state_to": "withdrawn"})


@router.post("/{tracking_id}/note")
def add_note(tracking_id: int, body: NoteRequest, request: Request, user: CurrentUser, db: DbSession):
    """A note. No state change, no dedupe: two identical notes are two notes."""
    record = _record_or_404(db, user, tracking_id)
    with _tracked():
        result = tracking.add_note(db, user=user, record=record, note=body.note,
                                   occurred_at=body.occurred_at, request_id=_request_id())
    return _respond(db, user, result, record=record)


@router.post("/{tracking_id}/follow-up")
def set_follow_up(tracking_id: int, body: FollowUpRequest, request: Request, user: CurrentUser, db: DbSession):
    """Promise a reminder. The notification is written when it comes due."""
    record = _record_or_404(db, user, tracking_id)
    with _tracked():
        result = tracking.set_follow_up(db, user=user, record=record, follow_up_at=body.follow_up_at,
                                        note=body.note, request_id=_request_id())
    return _respond(db, user, result, record=record)


@router.post("/{tracking_id}/follow-up/complete")
def follow_up_done(tracking_id: int, body: DoneRequest, request: Request, user: CurrentUser, db: DbSession):
    """Mark the promised follow-up done (stops the reminder, keeps the row)."""
    record = _record_or_404(db, user, tracking_id)
    with _tracked():
        result = tracking.complete_follow_up(db, user=user, record=record, note=body.note,
                                             request_id=_request_id())
    return _respond(db, user, result, record=record)


@router.delete("/{tracking_id}/follow-up")
def cancel_follow_up(tracking_id: int, user: CurrentUser, db: DbSession):
    """Cancel a reminder. The event that set it stays in the timeline."""
    record = _record_or_404(db, user, tracking_id)
    record.follow_up_at = None
    record.follow_up_note = ""
    record.follow_up_notified_at = None
    record.follow_up_completed_at = None
    db.commit()
    return {"ok": True, "document": tracking.to_document(db, user, record,
                                                         timeline_limit=WRITE_TIMELINE_LIMIT)}


@router.post("/{tracking_id}/correction")
def correct(tracking_id: int, body: CorrectionRequest, request: Request, user: CurrentUser, db: DbSession):
    """Manual correction: the transition table does not apply, a reason does.

    The previous event stays in the timeline and the new row names it, so the
    report can show how much of its input was revised (``counts.corrections``).
    """
    record = _record_or_404(db, user, tracking_id)
    with _tracked():
        result = tracking.correct_state(
            db, user=user, record=record, state=body.state, reason=body.reason,
            correction_of_event_id=body.correction_of_event_id, note=body.note,
            occurred_at=body.occurred_at, request_id=_request_id(),
        )
    return _respond(db, user, result, request=request, audit_action="application.state_corrected",
                    audit_detail={"state_to": body.state, "reason": body.reason[:200],
                                  "correction_of_event_id": body.correction_of_event_id})


@router.post("/{tracking_id}/attribution")
def attribution(tracking_id: int, body: AttributionRequest, request: Request, user: CurrentUser, db: DbSession):
    """Correct where the application came from (the report groups by it)."""
    record = _record_or_404(db, user, tracking_id)
    with _tracked():
        result = tracking.update_attribution(db, user=user, record=record, source=body.source,
                                             channel=body.channel, reason=body.reason,
                                             request_id=_request_id())
    return _respond(db, user, result, record=record, request=request,
                    audit_action="application.status_updated",
                    audit_detail={"attribution": True, "source": body.source})


@router.post("/{tracking_id}/snapshot")
def refresh_snapshot(tracking_id: int, request: Request, user: CurrentUser, db: DbSession):
    """Re-read the match-score / artefact snapshots; an event only if they moved."""
    record = _record_or_404(db, user, tracking_id)
    with _tracked():
        event = tracking.refresh_snapshot(db, user=user, record=record)
    return _respond(db, user, {"record": record, "event": event, "duplicate": event is None,
                               "state_changed": False}, record=record)


@router.post("/{tracking_id}/suggestion/confirm")
def confirm_suggestion(tracking_id: int, body: SuggestionRequest, request: Request,
                       user: CurrentUser, db: DbSession):
    """Confirm a provisional (email-inferred) suggestion — it becomes your claim."""
    record = _record_or_404(db, user, tracking_id)
    with _tracked():
        result = tracking.confirm_suggestion(db, user=user, record=record, state=body.state,
                                             note=body.note, request_id=_request_id())
    return _respond(db, user, result, request=request, audit_action="application.status_updated",
                    audit_detail={"confirmed_suggestion": True})


@router.post("/{tracking_id}/suggestion/dismiss")
def dismiss_suggestion(tracking_id: int, body: SuggestionRequest, request: Request,
                       user: CurrentUser, db: DbSession):
    """Dismiss it. Nothing moves; the suggestion row stays, marked dismissed."""
    record = _record_or_404(db, user, tracking_id)
    with _tracked():
        result = tracking.dismiss_suggestion(db, user=user, record=record, reason=body.reason,
                                             request_id=_request_id())
    return _respond(db, user, result, record=record)


__all__ = ["router"]
