"""Application tracking — the lightweight lifecycle & outcome-feedback layer.

``docs/contracts/07-application-state-machine.md`` §11, ``docs/contracts/09``.

What this is
------------
One row per ``(user, job)`` in :class:`~app.models.models.ApplicationTracking`
holding the **current** outcome state plus the snapshots a later report needs,
and an append-only timeline in
:class:`~app.models.models.ApplicationTrackingEvent` holding **everything that
happened**. The state is a projection of the timeline, never the other way
round: a status change appends an event, and the row is updated in the same
transaction.

What this is not
----------------
A CRM. No contacts, no email threads, no per-company pipelines, no activity
feeds. Four questions are answered — what happened, who says so, what did we
send, what is due next — and the module stops there.

The three rules that shaped the API
-----------------------------------
1. **History is never overwritten.** A correction is a *new* event with
   ``is_correction = true`` naming the row it revises. The wrong row stays,
   because "we recorded X, the user corrected it to Y" is itself the history the
   report has to be honest about (``correction_count``).
2. **Origin is part of the fact.** Every event carries ``origin``
   (:data:`~app.contracts.vocabulary.TRACKING_ORIGINS`): ``system_observed``
   (our automation submitted it / saw the receipt), ``user_reported`` (the user
   typed it), ``email_inferred`` (a parsed message — always provisional) or
   ``verified_integration``. The report can be filtered by it, and the timeline
   renders it, because a system observation and a person's memory are not the
   same claim.
3. **An interview is never inferred.** :data:`TRACKING_TRUSTED_ONLY_STATES` are
   reachable only from :data:`~app.contracts.vocabulary.TRACKING_TRUSTED_ORIGINS`.
   A recruiter email can propose ``recruiter_response`` as a *provisional
   suggestion* the user confirms or dismisses; it can never produce
   ``interview_scheduled``. :func:`record_email_suggestion` refuses, and
   ``backend/tests/test_application_tracking.py`` pins the refusal.

Duplicates
----------
Three layers, because a double click, a replayed request and a re-run sweep are
three different accidents: an explicit ``idempotency_key`` (remembered on the
record), a derived ``dedupe_key`` (unique per user — the same interview date
cannot be recorded twice), and the ``(user_id, tracking_id, sequence)`` unique
constraint that makes a racing writer retry instead of corrupting the order. A
duplicate is **not an error**: the caller gets ``{"duplicate": true}`` and the
event that already exists.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.contracts.vocabulary import (
    ACTOR_TYPES,
    APPLICATION_TRACKING_STATES,
    EMAIL_INFERRED_TRACKING_STATES,
    EVENT_TYPES,
    JOB_STATUSES,
    NOTIFICATION_SEVERITIES,
    QUEUE_TRIGGERS,
    SUBMISSION_CHANNELS,
    TRACKING_INTERVIEW_STATES,
    TRACKING_ORIGINS,
    TRACKING_PHASES,
    TRACKING_STATE_PHASE,
    TRACKING_TERMINAL_STATES,
    TRACKING_TRUSTED_ONLY_STATES,
    TRACKING_TRUSTED_ORIGINS,
    tracking_job_status_for,
)
from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc
from app.models.models import (
    ApplicationPacket,
    ApplicationTracking,
    ApplicationTrackingEvent,
    Job,
    MatchResult,
    Notification,
    Resume,
    ResumeDocument,
    User,
)
from app.services.matching import band_for
from app.services.role_family import role_family
from app.utils.timefmt import coerce_datetime, iso_utc

log = get_logger("app.tracking")

__all__ = [
    "TRACKING_TRANSITIONS",
    "TrackingError",
    "InvalidTransition",
    "UntrustedOrigin",
    "CorrectionReasonRequired",
    "TrackingNotFound",
    "UnknownValue",
    "add_note",
    "allowed_transitions",
    "append_event",
    "change_state",
    "complete_follow_up",
    "complete_interview",
    "confirm_suggestion",
    "correct_state",
    "dismiss_suggestion",
    "due_follow_ups",
    "event_to_dict",
    "for_job",
    "get_tracking",
    "list_records",
    "notify_due_follow_ups",
    "observe_submission",
    "open_tracking",
    "record_applied",
    "record_email_suggestion",
    "record_interview",
    "record_offer",
    "record_rejection",
    "record_response",
    "record_withdrawal",
    "refresh_snapshot",
    "report",
    "state_counts",
    "REPORT_GROUP_KEYS",
    "STATE_LABELS",
    "FOLLOW_UP_NOTIFICATION_KIND",
    "set_follow_up",
    "timeline",
    "to_document",
    "update_attribution",
    "upcoming_follow_ups",
]


# --------------------------------------------------------------------------- #
# The state machine
# --------------------------------------------------------------------------- #
#: Every legal transition, as ``{from: (to, …)}``. Self-loops exist where a
#: repeat is a *fact* and not a duplicate: an interview gets rescheduled
#: (``interview_scheduled → interview_scheduled``, a new date), and a second
#: round completes (``interview_completed → interview_completed``).
#:
#: The table is deliberately small. Anything not listed is a
#: :class:`InvalidTransition` — and the only way to reach a state the machine
#: forbids is :func:`correct_state`, which writes an audited correction event
#: instead of pretending the transition was legal.
TRACKING_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "not_applied": ("applied", "withdrawn"),
    "applied": (
        "recruiter_response", "interview_scheduled", "interview_completed",
        "rejected_by_employer", "withdrawn",
    ),
    # ``recruiter_response → recruiter_response``: a second reply is a new fact
    # (another email, a call back), not a duplicate of the first.
    "recruiter_response": (
        "recruiter_response", "interview_scheduled", "interview_completed", "offer_received",
        "rejected_by_employer", "withdrawn",
    ),
    "interview_scheduled": (
        "interview_scheduled", "interview_completed", "offer_received",
        "rejected_by_employer", "withdrawn",
    ),
    "interview_completed": (
        "interview_scheduled", "interview_completed", "offer_received",
        "rejected_by_employer", "withdrawn",
    ),
    # An offer can still be declined; a rejection and a withdrawal cannot be
    # "un-happened" — they are corrected, not transitioned.
    "offer_received": ("withdrawn",),
    "rejected_by_employer": (),
    "withdrawn": (),
}

#: The event type a generic status change writes, per target state — so the
#: timeline reads as "Interview reported" and not as eight identical
#: "Status changed" rows.
STATE_EVENT_TYPES: Dict[str, str] = {
    "not_applied": "application.state_changed",
    "applied": "application.state_changed",
    "recruiter_response": "application.response_received",
    "interview_scheduled": "application.interview_reported",
    "interview_completed": "application.interview_completed",
    "offer_received": "application.offer_received",
    "rejected_by_employer": "application.rejection_received",
    "withdrawn": "application.withdrawn",
}

#: Chip colour per state (``NOTIFICATION_SEVERITIES``).
STATE_SEVERITIES: Dict[str, str] = {
    "not_applied": "info",
    "applied": "success",
    "recruiter_response": "info",
    "interview_scheduled": "success",
    "interview_completed": "success",
    "offer_received": "success",
    "rejected_by_employer": "warning",
    "withdrawn": "info",
}

#: Human-readable state names for ``message`` (the SPA has its own labels; this
#: is the sentence the timeline stores, so it stays readable in an export).
STATE_LABELS: Dict[str, str] = {
    "not_applied": "Not applied",
    "applied": "Applied",
    "recruiter_response": "Recruiter responded",
    "interview_scheduled": "Interview scheduled",
    "interview_completed": "Interview completed",
    "offer_received": "Offer received",
    "rejected_by_employer": "Rejected by employer",
    "withdrawn": "Withdrawn",
}

#: ``jobs.status`` values the *queue* owns while a run is in flight. The
#: tracking projection never writes over them except to say the application went
#: out — a tracker must not erase an automation's in-progress board state.
QUEUE_OWNED_JOB_STATUSES: Tuple[str, ...] = ("queued", "preparing", "needs_input")

#: How many idempotency keys a record remembers (bounded, oldest dropped).
IDEMPOTENCY_KEY_MEMORY = 64

#: A note is the user's prose: bounded, and never a field answer.
NOTE_LIMIT = 4000
MESSAGE_LIMIT = 400


# --------------------------------------------------------------------------- #
# Errors — each one carries the contract error payload (contracts/01 §5)
# --------------------------------------------------------------------------- #
class TrackingError(Exception):
    """A refused tracking write. ``payload()`` is what the API returns."""

    http_status = 409
    code = "invalid_state_transition"

    def __init__(self, message: str, *, code: Optional[str] = None,
                 http_status: Optional[int] = None, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.extra: Dict[str, Any] = extra

    def payload(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


class TrackingNotFound(TrackingError):
    http_status = 404
    code = "tracking_not_found"


class UnknownValue(TrackingError):
    """A value outside the vocabulary — ``422``, never a silent empty list."""

    http_status = 422
    code = "validation_error"


class InvalidTransition(TrackingError):
    http_status = 409
    code = "invalid_state_transition"


class UntrustedOrigin(TrackingError):
    """An ``email_inferred`` write tried to reach an interview state."""

    http_status = 422
    code = "origin_not_trusted"


class CorrectionReasonRequired(TrackingError):
    http_status = 422
    code = "correction_reason_required"


def _require_state(state: str) -> str:
    if state not in APPLICATION_TRACKING_STATES:
        raise UnknownValue(
            f"'{state}' is not an application tracking state.",
            field="state", allowed=list(APPLICATION_TRACKING_STATES),
        )
    return state


def _require_origin(origin: str) -> str:
    if origin not in TRACKING_ORIGINS:
        raise UnknownValue(
            f"'{origin}' is not a tracking origin.", field="origin", allowed=list(TRACKING_ORIGINS),
        )
    return origin


def _require_actor(actor_type: str) -> str:
    if actor_type not in ACTOR_TYPES:
        raise UnknownValue(
            f"'{actor_type}' is not an actor type.", field="actor_type", allowed=list(ACTOR_TYPES),
        )
    return actor_type


def _require_trigger(trigger: str) -> str:
    if trigger not in QUEUE_TRIGGERS:
        raise UnknownValue(
            f"'{trigger}' is not a queue trigger.", field="trigger", allowed=list(QUEUE_TRIGGERS),
        )
    return trigger


def allowed_transitions(state: str) -> Tuple[str, ...]:
    """The states reachable from *state* without a correction."""
    return TRACKING_TRANSITIONS.get(state, ())


def validate_transition(state_from: str, state_to: str) -> None:
    """Raise :class:`InvalidTransition` unless the move is in the table."""
    _require_state(state_from)
    _require_state(state_to)
    allowed = allowed_transitions(state_from)
    if state_to not in allowed:
        reason = (
            f"'{STATE_LABELS.get(state_from, state_from)}' is a closed outcome — it can be "
            f"corrected (with a reason), not transitioned out of."
            if state_from in TRACKING_TERMINAL_STATES
            else f"Cannot move from '{STATE_LABELS.get(state_from, state_from)}' to "
                 f"'{STATE_LABELS.get(state_to, state_to)}'."
        )
        raise InvalidTransition(
            reason,
            state_from=state_from,
            state_to=state_to,
            allowed=list(allowed),
            fix="/tracking",
        )


def validate_origin_for_state(origin: str, state_to: Optional[str]) -> None:
    """Enforce rule 3: an inference may never reach an interview/outcome state."""
    if state_to is None or origin in TRACKING_TRUSTED_ORIGINS:
        return
    if state_to in TRACKING_TRUSTED_ONLY_STATES or state_to not in EMAIL_INFERRED_TRACKING_STATES:
        raise UntrustedOrigin(
            f"An '{origin}' event cannot record '{STATE_LABELS.get(state_to, state_to)}'. "
            "An interview is only ever recorded by you, by our own submission machinery, or by "
            "an integration you connected — never inferred from a message.",
            origin=origin,
            state=state_to,
            allowed_origins=list(TRACKING_TRUSTED_ORIGINS),
            suggestion_states=list(EMAIL_INFERRED_TRACKING_STATES),
        )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def get_tracking(db: Session, user_id: int, tracking_id: int) -> ApplicationTracking:
    """One record by id, tenant-scoped. A foreign id is a 404, not a 403."""
    row = (
        db.query(ApplicationTracking)
        .filter(ApplicationTracking.id == int(tracking_id), ApplicationTracking.user_id == int(user_id))
        .first()
    )
    if row is None:
        raise TrackingNotFound("No application tracking record with that id.")
    return row


def for_job(db: Session, user_id: int, job_id: int) -> Optional[ApplicationTracking]:
    """The record for a job, or ``None`` (there is at most one)."""
    return (
        db.query(ApplicationTracking)
        .filter(ApplicationTracking.job_id == int(job_id), ApplicationTracking.user_id == int(user_id))
        .first()
    )


def _job_or_none(db: Session, user_id: int, job_id: Optional[int]) -> Optional[Job]:
    if job_id is None:
        return None
    return db.query(Job).filter(Job.id == int(job_id), Job.user_id == int(user_id)).first()


def list_records(
    db: Session,
    user_id: int,
    *,
    state: Optional[str] = None,
    phase: Optional[str] = None,
    source: Optional[str] = None,
    job_id: Optional[int] = None,
    q: Optional[str] = None,
    due_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[ApplicationTracking], int]:
    """Filtered page of tracking rows plus the total (contracts/01 §4)."""
    query = db.query(ApplicationTracking).filter(ApplicationTracking.user_id == int(user_id))
    if state:
        _require_state(state)
        query = query.filter(ApplicationTracking.state == state)
    if phase:
        if phase not in TRACKING_PHASES:
            raise UnknownValue(f"'{phase}' is not a tracking phase.",
                               field="phase", allowed=list(TRACKING_PHASES))
        query = query.filter(ApplicationTracking.phase == phase)
    if source:
        query = query.filter(ApplicationTracking.source == source[:60])
    if job_id is not None:
        query = query.filter(ApplicationTracking.job_id == int(job_id))
    if due_only:
        query = query.filter(ApplicationTracking.follow_up_at.is_not(None),
                             ApplicationTracking.follow_up_completed_at.is_(None))
    if q:
        needle = f"%{q.strip()[:80]}%"
        query = query.filter(or_(
            ApplicationTracking.job_title_snapshot.ilike(needle),
            ApplicationTracking.company_snapshot.ilike(needle),
            ApplicationTracking.note.ilike(needle),
        ))
    total = int(query.count())
    rows = (
        query.order_by(ApplicationTracking.updated_at.desc(), ApplicationTracking.id.desc())
        .limit(max(1, min(int(limit), 200)))
        .offset(max(0, int(offset)))
        .all()
    )
    return list(rows), total


# --------------------------------------------------------------------------- #
# Snapshots — source attribution, match score, artefact version
# --------------------------------------------------------------------------- #
def _artifact_snapshot(db: Session, user_id: int, job: Job) -> Dict[str, Any]:
    """What was (or would be) attached, frozen as ``{kind, id, version, label, sha256}``.

    Preference order: the job's current application packet (the versioned,
    approval-gated artefact), then the resume the job was actually applied with,
    then the newest stored resume document. ``kind = none`` is a real answer —
    "we do not know what was sent" must not be rendered as a version number.
    """
    packet = (
        db.query(ApplicationPacket)
        .filter(ApplicationPacket.user_id == int(user_id), ApplicationPacket.job_id == int(job.id))
        .order_by(ApplicationPacket.is_current.desc(), ApplicationPacket.version.desc())
        .first()
    )
    if packet is not None:
        return {
            "kind": "packet",
            "id": int(packet.id),
            "version": int(packet.version or 1),
            "label": (packet.job_title or job.title or "")[:200],
            "sha256": (packet.jd_hash or "")[:64],
            "status": packet.status,
        }
    if job.applied_with_resume_id:
        resume = (
            db.query(Resume)
            .filter(Resume.id == int(job.applied_with_resume_id), Resume.user_id == int(user_id))
            .first()
        )
        if resume is not None:
            return {
                "kind": "resume",
                "id": int(resume.id),
                "version": None,
                "label": (resume.display_name or resume.filename or "")[:200],
                "sha256": (resume.jd_hash or "")[:64],
                "status": resume.status,
            }
    document = (
        db.query(ResumeDocument)
        .filter(ResumeDocument.user_id == int(user_id), ResumeDocument.archived_at.is_(None))
        .order_by(ResumeDocument.id.desc())
        .first()
    )
    if document is not None:
        return {
            "kind": "resume_document",
            "id": int(document.id),
            "version": None,
            "label": (document.display_name or document.filename or "")[:200],
            "sha256": (document.sha256 or "")[:64],
            "status": document.state,
        }
    return {"kind": "none", "id": None, "version": None, "label": "", "sha256": "", "status": ""}


def _match_snapshot(db: Session, user_id: int, job: Job) -> Dict[str, Any]:
    """The score we believed when tracking opened, with its provenance.

    A ``match_results`` row wins (it carries the band, the scorer version and the
    explanation); otherwise the denormalised ``jobs.score`` is used and labelled
    with its own ``score_source``, because "78" from a keyword pre-rank and "78"
    from a guardrail-checked model verdict are not the same claim.
    """
    match = (
        db.query(MatchResult)
        .filter(MatchResult.user_id == int(user_id), MatchResult.job_id == int(job.id),
                MatchResult.is_current.is_(True))
        .order_by(MatchResult.id.desc())
        .first()
    )
    if match is not None:
        return {
            "score": float(match.score) if match.score is not None else None,
            "band": (match.band or "")[:12],
            "score_source": (match.score_source or "")[:24],
            "scorer_version": (match.scorer_version or "")[:24],
            "match_id": int(match.id),
            "computed_at": iso_utc(match.computed_at),
        }
    score = float(job.score) if job.score is not None else None
    # No ``match_results`` row, but the board still shows a score. The band is
    # derived with the *shared* helper (contract 06 §3) rather than left empty:
    # a report grouped by band would otherwise file every pre-ranked job under
    # "unscored" while showing its score in the same row. ``source_known`` is
    # what separates "we scored this" from "we inherited a number".
    return {
        "score": score,
        "band": band_for(score, source_known=bool(job.score_source))[:12] if score is not None else "",
        "score_source": (job.score_source or "")[:24],
        "scorer_version": "",
        "match_id": None,
        "computed_at": None,
    }


def build_snapshot(db: Session, user_id: int, job: Job) -> Dict[str, Any]:
    """Everything the record freezes at open time, in one read."""
    match = _match_snapshot(db, user_id, job)
    artifact = _artifact_snapshot(db, user_id, job)
    return {
        "source": (job.source or "unknown")[:60],
        "role_family": role_family(job.title)[:80],
        "job_title_snapshot": (job.title or "")[:300],
        "company_snapshot": (job.company or "")[:200],
        "match_score": match["score"],
        "match_band": match["band"],
        "match_id": match["match_id"],
        "score_source": match["score_source"],
        "scorer_version": match["scorer_version"],
        "artifact_kind": artifact["kind"],
        "artifact_id": artifact["id"],
        "artifact_version": artifact["version"],
        "artifact_label": artifact["label"],
        "artifact_sha256": artifact["sha256"],
        "match": match,
        "artifact": artifact,
    }


def _apply_snapshot(record: ApplicationTracking, snapshot: Dict[str, Any]) -> None:
    record.source = snapshot["source"]
    record.role_family = snapshot["role_family"]
    record.job_title_snapshot = snapshot["job_title_snapshot"]
    record.company_snapshot = snapshot["company_snapshot"]
    record.match_score = snapshot["match_score"]
    record.match_band = snapshot["match_band"]
    record.match_id = snapshot["match_id"]
    record.score_source = snapshot["score_source"]
    record.scorer_version = snapshot["scorer_version"]
    record.artifact_kind = snapshot["artifact_kind"]
    record.artifact_id = snapshot["artifact_id"]
    record.artifact_version = snapshot["artifact_version"]
    record.artifact_label = snapshot["artifact_label"]
    record.artifact_sha256 = snapshot["artifact_sha256"]


def refresh_snapshot(db: Session, *, user: User, record: ApplicationTracking,
                     job: Optional[Job] = None, trigger: str = "user",
                     commit: bool = True) -> Optional[ApplicationTrackingEvent]:
    """Re-read the snapshots; write an event **only if something changed**.

    Called when the user asks ("refresh what we sent") and when tracking opens
    on a job whose packet was regenerated since. A no-op refresh writes nothing:
    a timeline full of "snapshot unchanged" rows would bury the facts.
    """
    target = job or _job_or_none(db, int(user.id), record.job_id)
    if target is None:
        return None
    snapshot = build_snapshot(db, int(user.id), target)
    changed = sorted(
        key for key in (
            "source", "role_family", "match_score", "match_band", "score_source",
            "scorer_version", "artifact_kind", "artifact_id", "artifact_version",
            "artifact_sha256",
        )
        if getattr(record, key) != snapshot[key]
    )
    if not changed:
        return None
    before = {key: getattr(record, key) for key in changed}
    _apply_snapshot(record, snapshot)
    event, _duplicate = append_event(
        db,
        record=record,
        event_type="application.snapshot_recorded",
        origin="system_observed",
        actor_type="user" if trigger == "user" else "system_api",
        actor_id=int(user.id),
        actor_label=(user.email or f"user:{user.id}")[:320],
        trigger=trigger,
        severity="info",
        message=f"Snapshot refreshed — {len(changed)} value(s) changed: {', '.join(changed)}.",
        payload={"changed": changed, "before": before,
                 "match": snapshot["match"], "artifact": snapshot["artifact"]},
        dedupe_key=None,
        commit=False,
    )
    if commit:
        db.commit()
    return event


# --------------------------------------------------------------------------- #
# Opening a record
# --------------------------------------------------------------------------- #
def open_tracking(
    db: Session,
    *,
    user: User,
    job: Job,
    origin: str = "user_reported",
    actor_type: str = "user",
    trigger: str = "user",
    source: Optional[str] = None,
    channel: Optional[str] = None,
    note: Optional[str] = None,
    state: str = "not_applied",
    occurred_at: Optional[datetime] = None,
    request_id: Optional[str] = None,
    respect_board_status: bool = True,
    commit: bool = True,
) -> Tuple[ApplicationTracking, bool]:
    """Create (or return) the tracking row for a job.

    Idempotent by the ``(user_id, job_id)`` unique constraint: a second call
    returns the existing row with ``created = False`` and writes nothing. When
    *state* is not ``not_applied`` the row is opened **and** moved in the same
    transaction, so "I applied manually" on an untracked job is one write, not
    two half-writes.

    ``respect_board_status`` decides who gets the credit for a job the board
    already shows as applied. The default reads the board's own fact and labels
    it ``system_observed`` — right when a user starts tracking a job they applied
    to last month. A caller that is *itself* recording the application right now
    (the apply flow, ``POST /jobs/{id}/mark-applied``) passes ``False``, so the
    timeline carries that caller's claim and its own event type instead of an
    "opened from the board status" row written a millisecond after the fact.
    """
    _require_origin(origin)
    _require_actor(actor_type)
    _require_trigger(trigger)
    _require_state(state)
    if channel is not None and channel not in SUBMISSION_CHANNELS:
        raise UnknownValue(f"'{channel}' is not a submission channel.",
                           field="channel", allowed=list(SUBMISSION_CHANNELS))

    existing = for_job(db, int(user.id), int(job.id))
    if existing is not None:
        if state != "not_applied" and existing.state != state:
            change_state(db, user=user, record=existing, state=state, origin=origin,
                         actor_type=actor_type, trigger=trigger, note=note,
                         occurred_at=occurred_at, request_id=request_id, commit=commit)
        elif commit:
            db.commit()
        return existing, False

    snapshot = build_snapshot(db, int(user.id), job)
    record = ApplicationTracking(
        user_id=int(user.id),
        job_id=int(job.id),
        persona_id=job.persona_id,
        state="not_applied",
        phase=TRACKING_STATE_PHASE["not_applied"],
        state_origin=origin,
        state_actor_type=actor_type,
        channel=(channel or "")[:20],
        note=(note or "")[:NOTE_LIMIT],
        applied_idempotency_keys={},
    )
    if source:
        snapshot["source"] = str(source)[:60]
    _apply_snapshot(record, snapshot)
    # A job the board already shows as applied was applied: opening the record
    # must not claim otherwise, and the timestamp we have is the job's own.
    already_applied = bool(respect_board_status) and (job.status == "applied" or job.applied_at is not None)
    if already_applied and state == "not_applied":
        state = "applied"
        # The claim came from the board, not from the person pressing the button,
        # so the record says system_observed — otherwise the row would credit the
        # user with a fact the product already had, and ``by_origin`` in the report
        # would count it twice over.
        origin = origin if origin != "user_reported" else "system_observed"
        record.state_origin = origin
    record.state = state
    record.phase = TRACKING_STATE_PHASE[state]
    if state == "applied":
        record.applied_at = coerce_datetime(occurred_at) or job.applied_at or datetime.utcnow()
    db.add(record)
    try:
        db.flush()
    except IntegrityError:  # pragma: no cover - a concurrent open won the race
        db.rollback()
        winner = for_job(db, int(user.id), int(job.id))
        if winner is None:
            raise
        return winner, False

    event, _duplicate = append_event(
        db,
        record=record,
        event_type="application.tracking_opened",
        origin=origin,
        actor_type=actor_type,
        actor_id=int(user.id),
        actor_label=(user.email or f"user:{user.id}")[:320],
        trigger=trigger,
        state_to=state,
        severity="info",
        message=(f"Tracking opened for {job.company} — {job.title} "
                 f"({STATE_LABELS.get(state, state)})."),
        note=note,
        payload={
            "job_id": int(job.id),
            "title": (job.title or "")[:300],
            "company": (job.company or "")[:200],
            "source": record.source,
            "channel": record.channel,
            "role_family": record.role_family,
            "match": snapshot["match"],
            "artifact": snapshot["artifact"],
            "job_status_at_open": job.status,
            "already_applied": already_applied,
        },
        occurred_at=occurred_at,
        request_id=request_id,
        commit=False,
    )
    _project_job_status(job, state, record.applied_at)
    if commit:
        db.commit()
        db.refresh(record)
    inc("jobhunter_application_tracking_opened_total", origin=origin)
    return record, True


# --------------------------------------------------------------------------- #
# The writer — every fact in this layer goes through here
# --------------------------------------------------------------------------- #
def _next_sequence(db: Session, record: ApplicationTracking) -> int:
    """``MAX(sequence) + 1`` inside the write (contracts/09 §4).

    The record's own counter is the fast path; the aggregate is the truth after
    anything wrote behind its back (a recovered row, a concurrent request).
    """
    highest = db.query(func.max(ApplicationTrackingEvent.sequence)).filter(
        ApplicationTrackingEvent.tracking_id == int(record.id),
        ApplicationTrackingEvent.user_id == int(record.user_id),
    ).scalar()
    return int(max(int(record.last_sequence or 0), int(highest or 0))) + 1


def _dedupe_hit(db: Session, record: ApplicationTracking, *,
                dedupe_key: Optional[str], idempotency_key: Optional[str]) -> Optional[ApplicationTrackingEvent]:
    """The event a repeated write would duplicate, if there is one."""
    if idempotency_key:
        seen = record.applied_idempotency_keys or {}
        event_id = seen.get(idempotency_key)
        if event_id:
            row = (
                db.query(ApplicationTrackingEvent)
                .filter(ApplicationTrackingEvent.user_id == int(record.user_id),
                        ApplicationTrackingEvent.event_id == str(event_id))
                .first()
            )
            if row is not None:
                return row
    if dedupe_key:
        return (
            db.query(ApplicationTrackingEvent)
            .filter(ApplicationTrackingEvent.user_id == int(record.user_id),
                    ApplicationTrackingEvent.dedupe_key == dedupe_key[:200])
            .first()
        )
    return None


def _remember_key(record: ApplicationTracking, idempotency_key: Optional[str], event_id: str) -> None:
    if not idempotency_key:
        return
    seen = dict(record.applied_idempotency_keys or {})
    seen[idempotency_key[:64]] = event_id
    if len(seen) > IDEMPOTENCY_KEY_MEMORY:
        for key in list(seen)[: len(seen) - IDEMPOTENCY_KEY_MEMORY]:
            seen.pop(key, None)
    record.applied_idempotency_keys = seen


def append_event(
    db: Session,
    *,
    record: ApplicationTracking,
    event_type: str,
    origin: str,
    actor_type: str,
    actor_label: str,
    actor_id: Optional[int] = None,
    trigger: str = "user",
    state_from: Optional[str] = None,
    state_to: Optional[str] = None,
    phase: Optional[str] = None,
    severity: str = "info",
    message: str = "",
    note: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    occurred_at: Optional[datetime] = None,
    follow_up_at: Optional[datetime] = None,
    dedupe_key: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    request_id: Optional[str] = None,
    is_correction: bool = False,
    correction_of_event_id: Optional[int] = None,
    correction_reason: str = "",
    is_provisional: bool = False,
    commit: bool = True,
) -> Tuple[Optional[ApplicationTrackingEvent], bool]:
    """Append one timeline row. Returns ``(event, duplicate)``.

    A duplicate returns the row that already exists and writes nothing — the
    caller answers ``200 {"duplicate": true}``, because "you already told us
    this" is not an error the user has to fix.
    """
    if event_type not in EVENT_TYPES:
        raise UnknownValue(f"'{event_type}' is not a known event type.",
                           field="event_type", allowed="EVENT_TYPES")
    _require_origin(origin)
    _require_actor(actor_type)
    _require_trigger(trigger)
    if severity not in NOTIFICATION_SEVERITIES:
        raise UnknownValue(f"'{severity}' is not a notification severity.",
                           field="severity", allowed=list(NOTIFICATION_SEVERITIES))
    validate_origin_for_state(origin, state_to)

    hit = _dedupe_hit(db, record, dedupe_key=dedupe_key, idempotency_key=idempotency_key)
    if hit is not None:
        if commit:
            db.commit()
        inc("jobhunter_application_tracking_events_total", event_type=event_type,
            origin=origin, result="duplicate")
        return hit, True

    moment = coerce_datetime(occurred_at) or datetime.utcnow()
    resolved_phase = phase if phase is not None else (
        TRACKING_STATE_PHASE.get(state_to) if state_to else None
    )
    event = ApplicationTrackingEvent(
        event_id=str(uuid.uuid4()),
        user_id=int(record.user_id),
        tracking_id=int(record.id),
        job_id=record.job_id,
        sequence=_next_sequence(db, record),
        event_type=event_type,
        state_from=state_from,
        state_to=state_to,
        phase=resolved_phase,
        origin=origin,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=(actor_label or "")[:320],
        trigger=trigger,
        is_correction=bool(is_correction),
        correction_of_event_id=correction_of_event_id,
        correction_reason=(correction_reason or "")[:MESSAGE_LIMIT],
        is_provisional=bool(is_provisional),
        severity=severity,
        message=(message or "")[:MESSAGE_LIMIT],
        note=(note or "")[:NOTE_LIMIT] or None,
        payload=dict(payload or {}),
        evidence=list(evidence or []),
        follow_up_at=coerce_datetime(follow_up_at),
        dedupe_key=str(dedupe_key)[:200] if dedupe_key else None,
        idempotency_key=str(idempotency_key)[:64] if idempotency_key else None,
        request_id=str(request_id)[:64] if request_id else None,
        occurred_at=moment,
        recorded_at=datetime.utcnow(),
    )

    db.add(event)
    try:
        db.flush()
    except IntegrityError:
        # The unique constraints are the backstop for a racing writer: whichever
        # row lost reports a duplicate rather than corrupting the sequence.
        db.rollback()
        hit = _dedupe_hit(db, record, dedupe_key=dedupe_key, idempotency_key=idempotency_key)
        if hit is not None:
            return hit, True
        log.warning("tracking event write conflicted for record %s (%s)", record.id, event_type)
        raise

    _remember_key(record, idempotency_key, event.event_id)
    record.last_sequence = int(event.sequence)
    record.event_count = int(record.event_count or 0) + 1
    if is_correction:
        record.correction_count = int(record.correction_count or 0) + 1
    if commit:
        db.commit()
        db.refresh(event)
    inc("jobhunter_application_tracking_events_total", event_type=event_type,
        origin=origin, result="written")
    return event, False


# --------------------------------------------------------------------------- #
# State changes
# --------------------------------------------------------------------------- #
def _project_job_status(job: Optional[Job], state: str,
                        applied_at: Optional[datetime] = None) -> bool:
    """Keep the legacy board column in step (``jobs.status`` is a projection).

    Never a downgrade over a status the queue owns: an automation mid-run shows
    ``preparing``/``needs_input``, and a tracker reset to ``not_applied`` must
    not erase that. Everything else — including "the user says it went out" over
    a stuck ``needs_input`` — is written.

    The date travels with the column. A board row that says ``applied`` with no
    ``applied_at`` is dropped by every "applied this week" filter and sorted to
    the bottom of the list, so when the tracking record knows when it happened
    and the job does not, the job learns it.
    """
    if job is None:
        return False
    projected = tracking_job_status_for(state)
    changed = False
    if projected in JOB_STATUSES and job.status != projected:
        if not (job.status in QUEUE_OWNED_JOB_STATUSES and projected == "discovered"):
            job.status = projected
            changed = True
    if projected == "applied" and job.applied_at is None:
        moment = coerce_datetime(applied_at)
        if moment is not None:
            job.applied_at = moment
            changed = True
    return changed


def _apply_state_side_effects(record: ApplicationTracking, job: Optional[Job], state: str,
                              moment: datetime,
                              interview_at: Optional[datetime] = None) -> None:
    """Move the projection: state, phase, origin and the business timestamps.

    Timestamps are *first occurrence* columns — they are set once and never
    rewritten by a later transition, so "how long from applied to first
    interview?" stays a true measurement even after a correction.
    """
    record.state = state
    record.phase = TRACKING_STATE_PHASE.get(state, "pre_application")
    if state == "applied" and record.applied_at is None:
        record.applied_at = moment
        if job is not None and job.applied_at is None:
            job.applied_at = moment
    if state == "recruiter_response" and record.first_response_at is None:
        record.first_response_at = moment
    if state == "interview_scheduled":
        if record.interview_scheduled_at is None:
            record.interview_scheduled_at = moment
        if interview_at is not None:
            # The calendar slot the user typed is a fact about the interview, so
            # a reschedule *does* move it (it is the next one, not the first).
            record.interview_at = interview_at
    if state == "interview_completed":
        record.interview_completed_at = moment
        if interview_at is not None and record.interview_at is None:
            record.interview_at = interview_at
    if state in ("offer_received", "rejected_by_employer") and record.outcome_at is None:
        record.outcome_at = moment
    # ``closed_at`` follows the phase, so a correction out of a closed state
    # visibly re-opens the record instead of leaving a stale closing time.
    if TRACKING_STATE_PHASE.get(state) == "closed":
        record.closed_at = record.closed_at or moment
    else:
        record.closed_at = None
    record.is_provisional = False
    record.provisional_evidence = {}


def change_state(
    db: Session,
    *,
    user: User,
    record: ApplicationTracking,
    state: str,
    origin: str = "user_reported",
    actor_type: str = "user",
    trigger: str = "user",
    event_type: Optional[str] = None,
    message: Optional[str] = None,
    note: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    occurred_at: Optional[datetime] = None,
    interview_at: Optional[datetime] = None,
    follow_up_at: Optional[datetime] = None,
    follow_up_note: str = "",
    dedupe_key: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    request_id: Optional[str] = None,
    is_correction: bool = False,
    correction_of_event_id: Optional[int] = None,
    correction_reason: str = "",
    severity: Optional[str] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """Validate one transition, append its event, move the projection.

    This is the single write path for a state change: the transition table, the
    origin rule, the dedupe layers and the ``jobs.status`` projection all live
    here, so no caller can record an outcome without them.
    """
    state = _require_state(state)
    _require_origin(origin)
    state_from = record.state
    moment = coerce_datetime(occurred_at) or datetime.utcnow()

    resolved_type = event_type or (
        "application.state_corrected" if is_correction else STATE_EVENT_TYPES.get(state, "application.state_changed")
    )
    body: Dict[str, Any] = {
        "state_from": state_from,
        "state_to": state,
        "origin": origin,
        "note_present": bool(note),
        **(payload or {}),
    }
    if is_correction:
        body["reason"] = (correction_reason or "")[:MESSAGE_LIMIT]
        body["correction_of_event_id"] = correction_of_event_id
    text = message or (
        f"Corrected to {STATE_LABELS.get(state, state)} (was {STATE_LABELS.get(state_from, state_from)})."
        if is_correction else
        f"Status changed from {STATE_LABELS.get(state_from, state_from)} to {STATE_LABELS.get(state, state)}."
    )
    # The key names the *fact*, not the edge: ``state_from`` would make a repeat
    # of the same write look new (the first click already moved the record), so
    # "Rejected" twice in a row would be judged as an illegal move out of a
    # closed state instead of as the replay it is.
    key = dedupe_key if dedupe_key is not None else (
        None if is_correction else
        f"trk:{record.id}:{resolved_type}:{state}:{moment.strftime('%Y%m%d%H%M')}"
    )

    # A replay is answered *before* the transition table, and origin trust
    # before both:
    #
    # * clicking "Rejected" twice must say "already recorded", not 409 — the
    #   second click would otherwise be judged against a state the first click
    #   just made terminal, and a retry after a network blip would look like a
    #   protocol violation to the person who caused nothing;
    # * an untrusted origin may never write, legal move or not — the rule is
    #   about who is allowed to make the claim, not about where it would land.
    if not is_correction:
        already = _dedupe_hit(db, record, dedupe_key=key, idempotency_key=idempotency_key)
        if already is not None:
            if commit:
                db.commit()
            return {"record": record, "event": already, "duplicate": True, "state_changed": False}
    validate_origin_for_state(origin, state)
    if not is_correction:
        validate_transition(state_from, state)

    event, duplicate = append_event(
        db,
        record=record,
        event_type=resolved_type,
        origin=origin,
        actor_type=actor_type,
        actor_id=int(user.id) if actor_type == "user" else None,
        actor_label=(user.email or f"user:{user.id}")[:320] if actor_type == "user" else f"{actor_type}",
        trigger=trigger,
        state_from=state_from,
        state_to=state,
        severity=severity or STATE_SEVERITIES.get(state, "info"),
        message=text,
        note=note,
        payload=body,
        evidence=evidence,
        occurred_at=moment,
        follow_up_at=follow_up_at,
        dedupe_key=key,
        idempotency_key=idempotency_key,
        request_id=request_id,
        is_correction=is_correction,
        correction_of_event_id=correction_of_event_id,
        correction_reason=correction_reason,
        commit=False,
    )
    if duplicate and event is not None and event.state_to == state and not is_correction:
        # The same fact, already recorded: the projection is already right, so
        # nothing moves and the caller is told it was a replay.
        if commit:
            db.commit()
        return {"record": record, "event": event, "duplicate": True, "state_changed": False}

    job = _job_or_none(db, int(user.id), record.job_id)
    _apply_state_side_effects(record, job, state, moment, interview_at=interview_at)
    if follow_up_at is not None:
        record.follow_up_at = coerce_datetime(follow_up_at)
        record.follow_up_note = (follow_up_note or "")[:MESSAGE_LIMIT]
        record.follow_up_notified_at = None
        record.follow_up_completed_at = None
    if note:
        record.note = (note or "")[:NOTE_LIMIT]
    record.state_origin = origin
    record.state_actor_type = actor_type
    projected = _project_job_status(job, state, record.applied_at)
    if commit:
        db.commit()
        db.refresh(record)
    inc("jobhunter_application_tracking_state_changes_total",
        state=state, origin=origin, correction=str(bool(is_correction)).lower())
    return {"record": record, "event": event, "duplicate": False,
            "state_changed": True, "job_status_projected": projected}


# --------------------------------------------------------------------------- #
# The domain writes the product exposes
# --------------------------------------------------------------------------- #
def _actor(user: User, actor_type: str) -> Tuple[str, Optional[int], str]:
    if actor_type == "user":
        return actor_type, int(user.id), (user.email or f"user:{user.id}")[:320]
    return actor_type, None, actor_type


def record_applied(db: Session, *, user: User, job: Job, origin: str = "user_reported",
                   actor_type: str = "user", trigger: str = "user", channel: Optional[str] = None,
                   source: Optional[str] = None, note: Optional[str] = None,
                   occurred_at: Optional[datetime] = None, follow_up_at: Optional[datetime] = None,
                   follow_up_note: str = "", idempotency_key: Optional[str] = None,
                   request_id: Optional[str] = None, commit: bool = True) -> Dict[str, Any]:
    """"I applied" — the write every other outcome is measured against."""
    record, _created = open_tracking(db, user=user, job=job, origin=origin, actor_type=actor_type,
                                     trigger=trigger, channel=channel, source=source,
                                     occurred_at=occurred_at, request_id=request_id, commit=False)
    if channel:
        record.channel = str(channel)[:20]
    moment = coerce_datetime(occurred_at) or job.applied_at or datetime.utcnow()
    if record.state == "applied" and record.applied_at is not None:
        # Already applied: a repeat is a duplicate, not a second application.
        if commit:
            db.commit()
        return {"record": record, "event": None, "duplicate": True, "state_changed": False}
    label = STATE_LABELS["applied"]
    result = change_state(
        db, user=user, record=record, state="applied", origin=origin, actor_type=actor_type,
        trigger=trigger, message=f"Application recorded as sent ({record.source}).",
        note=note, occurred_at=moment, follow_up_at=follow_up_at, follow_up_note=follow_up_note,
        payload={"channel": record.channel or None, "label": label},
        evidence=[{"kind": "user_statement", "locator": "user", "captured_at": iso_utc(moment)}]
        if origin == "user_reported" else None,
        idempotency_key=idempotency_key, request_id=request_id, commit=commit,
    )
    return result


def record_interview(db: Session, *, user: User, record: ApplicationTracking,
                     interview_at: Optional[datetime] = None, format: str = "",
                     round_name: str = "", interviewer_role: str = "", location: str = "",
                     note: Optional[str] = None, origin: str = "user_reported",
                     actor_type: str = "user", trigger: str = "user",
                     occurred_at: Optional[datetime] = None,
                     follow_up_at: Optional[datetime] = None, follow_up_note: str = "",
                     idempotency_key: Optional[str] = None, request_id: Optional[str] = None,
                     commit: bool = True) -> Dict[str, Any]:
    """"I got an interview."

    The one write the whole layer exists for. It is always the user's (or a
    verified integration's) assertion — never an inference — and it carries the
    date, the format and an optional follow-up reminder, because an interview
    nobody is reminded about is the expensive kind of forgotten.
    """
    validate_origin_for_state(origin, "interview_scheduled")
    slot = coerce_datetime(interview_at)
    moment = coerce_datetime(occurred_at) or datetime.utcnow()
    parts = [p for p in (
        f"on {slot.strftime('%Y-%m-%d %H:%M UTC')}" if slot else "",
        f"({format.replace('_', ' ')})" if format else "",
        round_name.strip()[:60] if round_name else "",
    ) if p]
    message = "Interview scheduled " + " ".join(parts) if parts else "Interview scheduled."
    payload = {
        "interview_at": iso_utc(slot),
        "format": (format or "")[:20],
        "round": (round_name or "")[:60],
        "interviewer_role": (interviewer_role or "")[:120],
        "location": (location or "")[:200],
        "follow_up_at": iso_utc(coerce_datetime(follow_up_at)),
    }
    result = change_state(
        db, user=user, record=record, state="interview_scheduled", origin=origin,
        actor_type=actor_type, trigger=trigger, event_type="application.interview_reported",
        message=message, note=note, payload=payload, occurred_at=moment, interview_at=slot,
        follow_up_at=follow_up_at, follow_up_note=follow_up_note,
        evidence=[{"kind": "user_statement", "locator": "user", "captured_at": iso_utc(moment)}],
        dedupe_key=(f"trk:{record.id}:interview:{slot.strftime('%Y%m%d%H%M')}" if slot
                    else f"trk:{record.id}:interview:{moment.strftime('%Y%m%d%H%M')}"),
        idempotency_key=idempotency_key, request_id=request_id, commit=commit,
    )
    return result


def complete_interview(db: Session, *, user: User, record: ApplicationTracking,
                       self_assessment: str = "", next_step: str = "",
                       note: Optional[str] = None, origin: str = "user_reported",
                       actor_type: str = "user", trigger: str = "user",
                       occurred_at: Optional[datetime] = None,
                       follow_up_at: Optional[datetime] = None, follow_up_note: str = "",
                       idempotency_key: Optional[str] = None, request_id: Optional[str] = None,
                       commit: bool = True) -> Dict[str, Any]:
    """The interview happened — with the user's own read on how it went."""
    validate_origin_for_state(origin, "interview_completed")
    moment = coerce_datetime(occurred_at) or datetime.utcnow()
    payload = {
        "self_assessment": (self_assessment or "")[:20],
        "next_step": (next_step or "")[:120],
        "interview_at": iso_utc(record.interview_at),
        "follow_up_at": iso_utc(coerce_datetime(follow_up_at)),
    }
    text = "Interview completed" + (
        f" — {self_assessment.replace('_', ' ')}" if self_assessment else ""
    ) + "."
    return change_state(
        db, user=user, record=record, state="interview_completed", origin=origin,
        actor_type=actor_type, trigger=trigger, event_type="application.interview_completed",
        message=text, note=note, payload=payload, occurred_at=moment,
        interview_at=record.interview_at,
        follow_up_at=follow_up_at, follow_up_note=follow_up_note,
        dedupe_key=f"trk:{record.id}:interview_completed:{moment.strftime('%Y%m%d%H%M')}",
        idempotency_key=idempotency_key, request_id=request_id, commit=commit,
    )


def record_response(db: Session, *, user: User, record: ApplicationTracking,
                    channel: str = "", response_kind: str = "", summary: str = "",
                    note: Optional[str] = None, origin: str = "user_reported",
                    actor_type: str = "user", trigger: str = "user",
                    occurred_at: Optional[datetime] = None,
                    follow_up_at: Optional[datetime] = None, follow_up_note: str = "",
                    idempotency_key: Optional[str] = None, request_id: Optional[str] = None,
                    commit: bool = True) -> Dict[str, Any]:
    """A recruiter replied — recorded as a *response*, never as an interview."""
    moment = coerce_datetime(occurred_at) or datetime.utcnow()
    payload = {
        "channel": (channel or "")[:20],
        "response_kind": (response_kind or "")[:30],
        "summary_present": bool(summary),
        "summary_length": len(summary or ""),
    }
    text = "Recruiter response recorded" + (f" via {channel.replace('_', ' ')}" if channel else "") + "."
    return change_state(
        db, user=user, record=record, state="recruiter_response", origin=origin,
        actor_type=actor_type, trigger=trigger, event_type="application.response_received",
        message=text, note=note or (summary[:NOTE_LIMIT] if summary else None), payload=payload,
        occurred_at=moment, follow_up_at=follow_up_at, follow_up_note=follow_up_note,
        dedupe_key=f"trk:{record.id}:response:{moment.strftime('%Y%m%d%H%M')}:{(channel or 'any')[:20]}",
        idempotency_key=idempotency_key, request_id=request_id, commit=commit,
    )


def record_rejection(db: Session, *, user: User, record: ApplicationTracking,
                     reason: str = "", note: Optional[str] = None, origin: str = "user_reported",
                     actor_type: str = "user", trigger: str = "user",
                     occurred_at: Optional[datetime] = None,
                     idempotency_key: Optional[str] = None, request_id: Optional[str] = None,
                     commit: bool = True) -> Dict[str, Any]:
    """The employer said no. A closed outcome — it is corrected, never undone."""
    validate_origin_for_state(origin, "rejected_by_employer")
    moment = coerce_datetime(occurred_at) or datetime.utcnow()
    return change_state(
        db, user=user, record=record, state="rejected_by_employer", origin=origin,
        actor_type=actor_type, trigger=trigger, event_type="application.rejection_received",
        message="Rejection recorded" + (f" — {reason[:120]}" if reason else "") + ".",
        note=note, severity="warning",
        payload={"reason": (reason or "")[:200], "days_since_applied": _days_between(record.applied_at, moment)},
        occurred_at=moment, dedupe_key=f"trk:{record.id}:rejection:{moment.strftime('%Y%m%d')}",
        idempotency_key=idempotency_key, request_id=request_id, commit=commit,
    )


def record_offer(db: Session, *, user: User, record: ApplicationTracking, note: Optional[str] = None,
                 origin: str = "user_reported", actor_type: str = "user", trigger: str = "user",
                 occurred_at: Optional[datetime] = None, idempotency_key: Optional[str] = None,
                 request_id: Optional[str] = None, commit: bool = True) -> Dict[str, Any]:
    """An offer arrived."""
    validate_origin_for_state(origin, "offer_received")
    moment = coerce_datetime(occurred_at) or datetime.utcnow()
    return change_state(
        db, user=user, record=record, state="offer_received", origin=origin, actor_type=actor_type,
        trigger=trigger, event_type="application.offer_received", severity="success",
        message="Offer received.", note=note,
        payload={"days_since_applied": _days_between(record.applied_at, moment)},
        occurred_at=moment, dedupe_key=f"trk:{record.id}:offer:{moment.strftime('%Y%m%d')}",
        idempotency_key=idempotency_key, request_id=request_id, commit=commit,
    )


def record_withdrawal(db: Session, *, user: User, record: ApplicationTracking, reason: str = "",
                      note: Optional[str] = None, origin: str = "user_reported",
                      actor_type: str = "user", trigger: str = "user",
                      occurred_at: Optional[datetime] = None, idempotency_key: Optional[str] = None,
                      request_id: Optional[str] = None, commit: bool = True) -> Dict[str, Any]:
    """The candidate pulled out (including declining an offer)."""
    moment = coerce_datetime(occurred_at) or datetime.utcnow()
    return change_state(
        db, user=user, record=record, state="withdrawn", origin=origin, actor_type=actor_type,
        trigger=trigger, event_type="application.withdrawn", message="Withdrawn by you"
        + (f" — {reason[:120]}" if reason else "") + ".",
        note=note, payload={"reason": (reason or "")[:200], "by": "user"},
        occurred_at=moment, dedupe_key=f"trk:{record.id}:withdrawn:{moment.strftime('%Y%m%d')}",
        idempotency_key=idempotency_key, request_id=request_id, commit=commit,
    )


def add_note(db: Session, *, user: User, record: ApplicationTracking, note: str,
             occurred_at: Optional[datetime] = None, actor_type: str = "user",
             trigger: str = "user", request_id: Optional[str] = None,
             commit: bool = True) -> Dict[str, Any]:
    """A free-text note. No state change, no dedupe: two identical notes are two notes."""
    text = (note or "").strip()
    if not text:
        raise UnknownValue("A note needs text.", field="note")
    moment = coerce_datetime(occurred_at) or datetime.utcnow()
    actor, actor_id, label = _actor(user, actor_type)
    event, duplicate = append_event(
        db, record=record, event_type="application.note_added", origin="user_reported",
        actor_type=actor, actor_id=actor_id, actor_label=label, trigger=trigger,
        severity="info", message=f"Note added ({len(text)} characters).", note=text,
        payload={"note_present": True, "note_length": len(text)}, occurred_at=moment,
        request_id=request_id, commit=False,
    )
    record.note = text[:NOTE_LIMIT]
    if commit:
        db.commit()
    return {"record": record, "event": event, "duplicate": duplicate, "state_changed": False}


def set_follow_up(db: Session, *, user: User, record: ApplicationTracking, follow_up_at: datetime,
                  note: str = "", actor_type: str = "user", trigger: str = "user",
                  request_id: Optional[str] = None, commit: bool = True) -> Dict[str, Any]:
    """Promise a reminder. The notification is written by the due sweep."""
    due = coerce_datetime(follow_up_at)
    if due is None:
        raise UnknownValue("A follow-up needs a date.", field="follow_up_at")
    replaces = iso_utc(record.follow_up_at) if record.follow_up_at else None
    record.follow_up_at = due
    record.follow_up_note = (note or "")[:MESSAGE_LIMIT]
    record.follow_up_notified_at = None
    record.follow_up_completed_at = None
    actor, actor_id, label = _actor(user, actor_type)
    event, duplicate = append_event(
        db, record=record, event_type="application.follow_up_scheduled", origin="user_reported",
        actor_type=actor, actor_id=actor_id, actor_label=label, trigger=trigger, severity="info",
        message=f"Follow-up set for {due.strftime('%Y-%m-%d %H:%M UTC')}.",
        note=note or None, payload={"follow_up_at": iso_utc(due), "replaces": replaces},
        follow_up_at=due, dedupe_key=f"trk:{record.id}:follow_up:{due.strftime('%Y%m%d%H%M')}",
        request_id=request_id, commit=False,
    )
    if commit:
        db.commit()
    return {"record": record, "event": event, "duplicate": duplicate, "state_changed": False}


def complete_follow_up(db: Session, *, user: User, record: ApplicationTracking,
                       note: Optional[str] = None, actor_type: str = "user", trigger: str = "user",
                       request_id: Optional[str] = None, commit: bool = True) -> Dict[str, Any]:
    """Mark the promised follow-up as done (stops the reminder, keeps the row)."""
    due = record.follow_up_at
    record.follow_up_completed_at = datetime.utcnow()
    actor, actor_id, label = _actor(user, actor_type)
    event, duplicate = append_event(
        db, record=record, event_type="application.follow_up_completed", origin="user_reported",
        actor_type=actor, actor_id=actor_id, actor_label=label, trigger=trigger, severity="success",
        message="Follow-up marked done.", note=note,
        payload={"follow_up_at": iso_utc(due), "completed_at": iso_utc(record.follow_up_completed_at)},
        dedupe_key=(f"trk:{record.id}:follow_up_done:{due.strftime('%Y%m%d%H%M')}" if due else None),
        request_id=request_id, commit=False,
    )
    if commit:
        db.commit()
    return {"record": record, "event": event, "duplicate": duplicate, "state_changed": False}


def update_attribution(db: Session, *, user: User, record: ApplicationTracking,
                       source: Optional[str] = None, channel: Optional[str] = None,
                       reason: str = "", actor_type: str = "user", trigger: str = "user",
                       request_id: Optional[str] = None, commit: bool = True) -> Dict[str, Any]:
    """Correct *where this application came from* — attribution, not outcome.

    Source attribution is what makes "which board actually produces interviews?"
    answerable, and the job's own ``source`` is frequently wrong for a role the
    user found through a referral. The previous value is kept in the event, so a
    report grouped by source can say how much of its input was user-corrected.
    """
    if channel is not None and channel and channel not in SUBMISSION_CHANNELS:
        raise UnknownValue(f"'{channel}' is not a submission channel.",
                           field="channel", allowed=list(SUBMISSION_CHANNELS))
    before = {"source": record.source, "channel": record.channel}
    changed: List[str] = []
    if source and source.strip() and source.strip()[:60] != record.source:
        record.source = source.strip()[:60]
        changed.append("source")
    if channel is not None and channel[:20] != (record.channel or ""):
        record.channel = channel[:20]
        changed.append("channel")
    if not changed:
        if commit:
            db.commit()
        return {"record": record, "event": None, "duplicate": True, "state_changed": False}
    actor, actor_id, label = _actor(user, actor_type)
    event, duplicate = append_event(
        db, record=record, event_type="application.attribution_updated", origin="user_reported",
        actor_type=actor, actor_id=actor_id, actor_label=label, trigger=trigger, severity="info",
        message=f"Attribution corrected: {', '.join(changed)}.",
        payload={"before": before, "after": {"source": record.source, "channel": record.channel},
                 "reason": (reason or "")[:MESSAGE_LIMIT]},
        is_correction=bool(reason), correction_reason=reason,
        dedupe_key=None, request_id=request_id, commit=False,
    )
    if commit:
        db.commit()
    return {"record": record, "event": event, "duplicate": duplicate, "state_changed": False}


def correct_state(db: Session, *, user: User, record: ApplicationTracking, state: str,
                  reason: str, correction_of_event_id: Optional[int] = None,
                  occurred_at: Optional[datetime] = None, note: Optional[str] = None,
                  actor_type: str = "user", trigger: str = "user",
                  request_id: Optional[str] = None, commit: bool = True) -> Dict[str, Any]:
    """Manual correction: put the record in the state it should have been in.

    The transition table does **not** apply — that is the point of a correction —
    but three things are non-negotiable: a reason is required, the corrected row
    is named (``correction_of_event_id``, defaulting to the last state-changing
    event), and the old row stays. Nothing is edited; the timeline grows.
    """
    state = _require_state(state)
    text = (reason or "").strip()
    if len(text) < 3:
        # Checked here rather than in the request model so the refusal arrives as
        # the typed contract code (``correction_reason_required``) instead of a
        # generic field-validation list, and so a caller that uses the service
        # directly cannot skip the rule.
        raise CorrectionReasonRequired(
            "A correction needs a reason (at least three words' worth) — it rewrites "
            "what the report says happened.",
            field="reason",
        )
    target_id = correction_of_event_id
    corrected: Optional[ApplicationTrackingEvent] = None
    if target_id is not None:
        corrected = (
            db.query(ApplicationTrackingEvent)
            .filter(ApplicationTrackingEvent.id == int(target_id),
                    ApplicationTrackingEvent.user_id == int(user.id),
                    ApplicationTrackingEvent.tracking_id == int(record.id))
            .first()
        )
        if corrected is None:
            raise TrackingNotFound("No event with that id on this application's timeline.")
        target_id = int(corrected.id)
    else:
        corrected = (
            db.query(ApplicationTrackingEvent)
            .filter(ApplicationTrackingEvent.tracking_id == int(record.id),
                    ApplicationTrackingEvent.user_id == int(user.id),
                    ApplicationTrackingEvent.state_to.is_not(None))
            .order_by(ApplicationTrackingEvent.sequence.desc())
            .first()
        )
        target_id = int(corrected.id) if corrected is not None else None
    moment = coerce_datetime(occurred_at) or (corrected.occurred_at if corrected else None) or datetime.utcnow()
    payload = {
        "corrected_from": record.state,
        "corrected_to": state,
        "reason": text[:MESSAGE_LIMIT],
        "correction_of_event_id": target_id,
        "correction_of_sequence": int(corrected.sequence) if corrected is not None else None,
        "correction_of_type": corrected.event_type if corrected is not None else None,
        "corrected_origin": corrected.origin if corrected is not None else None,
    }
    return change_state(
        db, user=user, record=record, state=state, origin="user_reported", actor_type=actor_type,
        trigger=trigger, event_type="application.state_corrected", is_correction=True,
        correction_of_event_id=target_id, correction_reason=text, note=note, payload=payload,
        occurred_at=moment, severity="warning",
        message=f"Corrected to {STATE_LABELS.get(state, state)}: {text[:200]}",
        request_id=request_id, commit=commit,
    )


# --------------------------------------------------------------------------- #
# Email inference — the guarded path (rule 3)
# --------------------------------------------------------------------------- #
def record_email_suggestion(db: Session, *, user: User, record: ApplicationTracking,
                            suggested_state: str = "recruiter_response",
                            email_id: Optional[int] = None, locator: str = "",
                            confidence: Optional[float] = None, summary: str = "",
                            actor_type: str = "system_worker", trigger: str = "system",
                            commit: bool = True) -> Dict[str, Any]:
    """Store a *provisional suggestion* from a parsed inbound message.

    This never moves the record into an interview state: the only state an
    ``email_inferred`` event may propose is in
    :data:`~app.contracts.vocabulary.EMAIL_INFERRED_TRACKING_STATES`, and it is
    stored ``is_provisional = true`` until :func:`confirm_suggestion` or
    :func:`dismiss_suggestion` runs. A caller that asks for
    ``interview_scheduled`` gets :class:`UntrustedOrigin` — the refusal is the
    feature (``backend/tests/test_application_tracking.py``).
    """
    _require_state(suggested_state)
    validate_origin_for_state("email_inferred", suggested_state)
    evidence = [{"kind": "email_message", "locator": (locator or f"email:{email_id}")[:200],
                 "captured_at": iso_utc(datetime.utcnow())}]
    provenance = {
        "source": "email_inferred",
        "confidence": float(confidence) if confidence is not None else None,
        "evidence": evidence,
        "summary_present": bool(summary),
    }
    moment = datetime.utcnow()
    actor_label = actor_type if actor_type != "user" else (user.email or f"user:{user.id}")[:320]
    event, duplicate = append_event(
        db, record=record, event_type="application.suggestion_recorded", origin="email_inferred",
        actor_type=actor_type, actor_id=None, actor_label=actor_label[:320], trigger=trigger,
        severity="info", is_provisional=True,
        message=(f"We think {record.company_snapshot or 'the employer'} replied — "
                 "confirm or dismiss (nothing is recorded as an interview from an email)."),
        payload={"suggested_state": suggested_state, "confidence": provenance["confidence"],
                 "email_id": email_id, "summary_length": len(summary or ""),
                 "is_provisional": True},
        evidence=evidence, occurred_at=moment,
        dedupe_key=f"trk:{record.id}:suggestion:{suggested_state}:{(locator or email_id or '')!s:.60}",
        commit=False,
    )
    if not duplicate:
        record.is_provisional = True
        record.provisional_evidence = provenance
        record.state_origin = "email_inferred" if record.state == suggested_state else record.state_origin
        _notify_suggestion(db, record, suggested_state, event)
    if commit:
        db.commit()
    return {"record": record, "event": event, "duplicate": duplicate, "state_changed": False,
            "is_provisional": True}


def confirm_suggestion(db: Session, *, user: User, record: ApplicationTracking,
                       state: Optional[str] = None, note: Optional[str] = None,
                       request_id: Optional[str] = None, commit: bool = True) -> Dict[str, Any]:
    """The user confirms a provisional suggestion — now it is a *user-reported* fact."""
    target = state or (record.provisional_evidence or {}).get("suggested_state") or "recruiter_response"
    _require_state(target)
    result = change_state(
        db, user=user, record=record, state=target, origin="user_reported", actor_type="user",
        trigger="user", note=note, request_id=request_id, commit=False,
        message=f"Confirmed: {STATE_LABELS.get(target, target)} (was a suggestion from an email).",
        payload={"confirmed_suggestion": True, "previous_origin": "email_inferred"},
    )
    record.is_provisional = False
    record.provisional_evidence = {}
    if commit:
        db.commit()
    return result


def dismiss_suggestion(db: Session, *, user: User, record: ApplicationTracking, reason: str = "",
                       request_id: Optional[str] = None, commit: bool = True) -> Dict[str, Any]:
    """The user says the inference was wrong. The state does not move."""
    suggested = (record.provisional_evidence or {}).get("suggested_state")
    record.is_provisional = False
    record.provisional_evidence = {}
    if record.state_origin == "email_inferred":
        record.state_origin = "user_reported"
    event, duplicate = append_event(
        db, record=record, event_type="application.state_corrected", origin="user_reported",
        actor_type="user", actor_id=int(user.id), actor_label=(user.email or f"user:{user.id}")[:320],
        trigger="user", severity="info", is_correction=True, correction_reason=reason,
        message="Dismissed the suggested status — nothing changed.",
        payload={"dismissed_suggestion": True, "suggested_state": suggested,
                 "reason": (reason or "")[:MESSAGE_LIMIT]},
        request_id=request_id, commit=False,
    )
    if commit:
        db.commit()
    return {"record": record, "event": event, "duplicate": duplicate, "state_changed": False}


# --------------------------------------------------------------------------- #
# System-observed writes (the automation side of the origin split)
# --------------------------------------------------------------------------- #
def observe_submission(db: Session, *, user: User, job: Job, channel: str = "automation",
                       actor_type: str = "system_worker", trigger: str = "auto",
                       origin: str = "system_observed",
                       occurred_at: Optional[datetime] = None,
                       receipt: Optional[Dict[str, Any]] = None,
                       submission_id: Optional[int] = None,
                       commit: bool = True) -> Optional[Dict[str, Any]]:
    """Record that an application went out through this product.

    ``origin`` defaults to ``system_observed`` (automation ran it and saw the
    receipt). The manual path passes ``user_reported`` with ``actor_type="user"``:
    the endpoint wrote the row, but the *claim* is the user's, and origin is
    about the claim.

    Called from the apply flow and the assisted-session submit path. It is
    best-effort by design: a tracking write must never fail a submission that
    already went out, so every error is logged and swallowed (the queue's own
    ``job_events`` row remains the record of the run).
    """
    _require_origin(origin)
    try:
        moment = coerce_datetime(occurred_at) or job.applied_at or datetime.utcnow()
        record, created = open_tracking(
            db, user=user, job=job, origin=origin, actor_type=actor_type,
            trigger=trigger, channel=channel, occurred_at=moment, commit=False,
            # This call *is* the record of the submission: the timeline should
            # say "submitted" with this origin, not "opened from the board status"
            # because the board was set one statement earlier in the same request.
            respect_board_status=False,
        )
        if record.state != "not_applied":
            if commit:
                db.commit()
            return None
        payload = {
            "channel": channel,
            "submission_id": submission_id,
            "receipt_present": bool(receipt),
            "external_receipt_id": str((receipt or {}).get("external_id") or "")[:200] or None,
        }
        evidence = [{"kind": "portal_response", "locator": str((receipt or {}).get("locator") or "")[:200],
                     "captured_at": iso_utc(moment)}] if receipt else None
        message = (
            f"Application submitted by JobHunter ({channel.replace('_', ' ')})."
            if origin == "system_observed"
            else "Application recorded as sent by you."
        )
        result = change_state(
            db, user=user, record=record, state="applied", origin=origin,
            actor_type=actor_type, trigger=trigger, event_type="application.submitted",
            message=message,
            payload=payload, evidence=evidence, occurred_at=moment, request_id=None, commit=commit,
        )
        result["created"] = created
        return result
    except Exception as exc:  # noqa: BLE001 - a tracking write must never fail a submission
        try:
            db.rollback()
        except Exception:  # pragma: no cover - defensive
            pass
        log.warning("could not record system-observed submission for job %s: %s: %s",
                    getattr(job, "id", None), type(exc).__name__, exc)
        return None


# --------------------------------------------------------------------------- #
# Serialisation — the self-contained document (contracts/13 §7)
# --------------------------------------------------------------------------- #
def _days_between(start: Optional[datetime], end: Optional[datetime]) -> Optional[int]:
    if not start or not end:
        return None
    return int((end - start).total_seconds() // 86400)


def event_to_dict(event: ApplicationTrackingEvent) -> Dict[str, Any]:
    """One timeline row on the wire: labels included, raw values always present."""
    late = (event.recorded_at and event.occurred_at
            and event.recorded_at - event.occurred_at > timedelta(minutes=30))
    return {
        "id": int(event.id),
        "event_id": event.event_id,
        "sequence": int(event.sequence),
        "event_type": event.event_type,
        "state_from": event.state_from,
        "state_from_label": STATE_LABELS.get(event.state_from or "", None),
        "state_to": event.state_to,
        "state_to_label": STATE_LABELS.get(event.state_to or "", None),
        "phase": event.phase,
        "origin": event.origin,
        "actor_type": event.actor_type,
        "actor_label": event.actor_label,
        "trigger": event.trigger,
        "severity": event.severity,
        "message": event.message,
        "note": event.note or "",
        "payload": event.payload or {},
        "evidence": event.evidence or [],
        "is_correction": bool(event.is_correction),
        "correction_of_event_id": event.correction_of_event_id,
        "correction_reason": event.correction_reason or "",
        "is_provisional": bool(event.is_provisional),
        "follow_up_at": iso_utc(event.follow_up_at),
        "occurred_at": iso_utc(event.occurred_at),
        "recorded_at": iso_utc(event.recorded_at),
        # A fact reported long after it happened is labelled as such: the gap is
        # information (contracts/09 §4), not a rendering artefact.
        "late_reported": bool(late),
    }


def timeline(db: Session, user_id: int, record: ApplicationTracking, *,
             after_sequence: int = 0, limit: int = 50,
             order: str = "asc") -> List[Dict[str, Any]]:
    """Keyset-paginated timeline (``after_sequence``, never ``OFFSET``)."""
    query = (
        db.query(ApplicationTrackingEvent)
        .filter(ApplicationTrackingEvent.tracking_id == int(record.id),
                ApplicationTrackingEvent.user_id == int(user_id))
    )
    if after_sequence and int(after_sequence) > 0:
        query = query.filter(ApplicationTrackingEvent.sequence > int(after_sequence))
    direction = ApplicationTrackingEvent.sequence.desc() if order == "desc" else ApplicationTrackingEvent.sequence.asc()
    rows = query.order_by(direction).limit(max(1, min(int(limit), 200))).all()
    return [event_to_dict(row) for row in rows]


def _actions(record: ApplicationTracking, job: Optional[Job]) -> List[Dict[str, Any]]:
    """Server-computed buttons: the client renders, it does not reason."""
    base = f"/api/application-tracking/{record.id}"
    allowed = allowed_transitions(record.state)
    actions: List[Dict[str, Any]] = [
        {"key": "interview", "label": "I got an interview", "method": "POST",
         "route": f"{base}/interview", "primary": True,
         "disabled": "interview_scheduled" not in allowed and record.state != "interview_scheduled",
         "disabled_reason": None if "interview_scheduled" in allowed or record.state == "interview_scheduled"
         else f"Not reachable from '{STATE_LABELS.get(record.state, record.state)}' — use Correct."},
        {"key": "interview_completed", "label": "Interview completed", "method": "POST",
         "route": f"{base}/interview-complete", "primary": False,
         "disabled": "interview_completed" not in allowed,
         "disabled_reason": None if "interview_completed" in allowed else "Not reachable from this state."},
        {"key": "response", "label": "Recruiter responded", "method": "POST",
         "route": f"{base}/response", "primary": False,
         "disabled": "recruiter_response" not in allowed,
         "disabled_reason": None if "recruiter_response" in allowed else "Not reachable from this state."},
        {"key": "offer", "label": "Offer received", "method": "POST",
         "route": f"{base}/offer", "primary": False,
         "disabled": "offer_received" not in allowed,
         "disabled_reason": None if "offer_received" in allowed else "Not reachable from this state."},
        {"key": "rejected", "label": "Rejected", "method": "POST",
         "route": f"{base}/rejection", "primary": False,
         "disabled": "rejected_by_employer" not in allowed,
         "disabled_reason": None if "rejected_by_employer" in allowed else "Not reachable from this state."},
        {"key": "withdraw", "label": "Withdraw", "method": "POST",
         "route": f"{base}/withdraw", "primary": False,
         "disabled": "withdrawn" not in allowed,
         "disabled_reason": None if "withdrawn" in allowed else "Already closed."},
        {"key": "note", "label": "Add note", "method": "POST", "route": f"{base}/note",
         "primary": False, "disabled": False, "disabled_reason": None},
        {"key": "follow_up", "label": "Set follow-up", "method": "POST", "route": f"{base}/follow-up",
         "primary": False, "disabled": False, "disabled_reason": None},
        {"key": "correct", "label": "Correct status", "method": "POST", "route": f"{base}/correction",
         "primary": False, "disabled": False, "disabled_reason": None},
        {"key": "attribution", "label": "Correct source", "method": "POST",
         "route": f"{base}/attribution", "primary": False, "disabled": False, "disabled_reason": None},
    ]
    if record.state == "not_applied":
        actions.insert(0, {"key": "applied", "label": "I applied", "method": "POST",
                           "route": f"{base}/status", "primary": True, "disabled": False,
                           "disabled_reason": None})
    if record.follow_up_at and not record.follow_up_completed_at:
        actions.append({"key": "follow_up_done", "label": "Follow-up done", "method": "POST",
                        "route": f"{base}/follow-up/complete", "primary": False,
                        "disabled": False, "disabled_reason": None})
    if record.is_provisional:
        actions.append({"key": "confirm_suggestion", "label": "Confirm suggestion", "method": "POST",
                        "route": f"{base}/suggestion/confirm", "primary": True,
                        "disabled": False, "disabled_reason": None})
        actions.append({"key": "dismiss_suggestion", "label": "Dismiss suggestion", "method": "POST",
                        "route": f"{base}/suggestion/dismiss", "primary": False,
                        "disabled": False, "disabled_reason": None})
    if job is not None and record.state == "not_applied":
        actions.append({"key": "open_job", "label": "Open job", "method": "GET",
                        "route": f"/api/jobs/{job.id}", "primary": False,
                        "disabled": False, "disabled_reason": None})
    return actions


def to_document(db: Session, user: User, record: ApplicationTracking, *,
                job: Optional[Job] = None, timeline_limit: int = 20) -> Dict[str, Any]:
    """The whole truth about one application, in one read.

    Labels next to raw values, the transitions the state machine allows, the
    buttons the client may render, the snapshots the report will group by, and
    the newest ``timeline_limit`` events oldest-first — so no screen needs a
    second call to know what to show (contracts/13 §7 rule 1).
    """
    target = job if job is not None else _job_or_none(db, int(user.id), record.job_id)
    rows: List[Dict[str, Any]] = []
    if int(timeline_limit) > 0:
        rows = timeline(db, int(user.id), record, limit=timeline_limit, order="desc")
        rows.reverse()  # oldest first, as the contract asks
    now = datetime.utcnow()
    follow_up_due = record.follow_up_at
    return {
        "tracking_id": int(record.id),
        "job_id": int(record.job_id),
        "job": {
            "id": int(target.id) if target else int(record.job_id),
            "title": (target.title if target else record.job_title_snapshot) or "",
            "company": (target.company if target else record.company_snapshot) or "",
            "url": (target.url if target else "") or "",
            "status": (target.status if target else None),
            "source": (target.source if target else record.source) or "",
        },
        "state": record.state,
        "state_label": STATE_LABELS.get(record.state, record.state),
        "phase": record.phase,
        "phase_label": record.phase.replace("_", " ").title(),
        "job_status": tracking_job_status_for(record.state),
        "state_origin": record.state_origin,
        "is_provisional": bool(record.is_provisional),
        "provisional_evidence": record.provisional_evidence or {},
        "attribution": {
            "source": record.source,
            "channel": record.channel or None,
            "role_family": record.role_family or None,
        },
        "snapshots": {
            "match": {
                "score": record.match_score,
                "band": record.match_band or None,
                "score_source": record.score_source or None,
                "scorer_version": record.scorer_version or None,
                "match_id": record.match_id,
            },
            "artifact": {
                "kind": record.artifact_kind,
                "id": record.artifact_id,
                "version": record.artifact_version,
                "label": record.artifact_label or None,
                "sha256": record.artifact_sha256 or None,
            },
        },
        "timestamps": {
            "applied_at": iso_utc(record.applied_at),
            "first_response_at": iso_utc(record.first_response_at),
            "interview_scheduled_at": iso_utc(record.interview_scheduled_at),
            "interview_at": iso_utc(record.interview_at),
            "interview_completed_at": iso_utc(record.interview_completed_at),
            "outcome_at": iso_utc(record.outcome_at),
            "closed_at": iso_utc(record.closed_at),
            "created_at": iso_utc(record.created_at),
            "updated_at": iso_utc(record.updated_at),
        },
        "durations_days": {
            "applied_to_first_response": _days_between(record.applied_at, record.first_response_at),
            "applied_to_interview": _days_between(record.applied_at, record.interview_scheduled_at),
            "applied_to_outcome": _days_between(record.applied_at, record.outcome_at),
        },
        "follow_up": {
            "due_at": iso_utc(follow_up_due),
            "note": record.follow_up_note or "",
            "notified_at": iso_utc(record.follow_up_notified_at),
            "completed_at": iso_utc(record.follow_up_completed_at),
            "overdue": bool(follow_up_due and not record.follow_up_completed_at and follow_up_due <= now),
            "pending": bool(follow_up_due and not record.follow_up_completed_at),
        },
        "counts": {
            "events": int(record.event_count or 0),
            "corrections": int(record.correction_count or 0),
            "last_sequence": int(record.last_sequence or 0),
        },
        "note": record.note or "",
        "transitions": {
            "allowed": list(allowed_transitions(record.state)),
            "allowed_labels": [STATE_LABELS.get(s, s) for s in allowed_transitions(record.state)],
            "terminal": record.state in TRACKING_TERMINAL_STATES,
        },
        "timeline": rows,
        "actions": _actions(record, target),
        "server_time": iso_utc(now),
    }


# --------------------------------------------------------------------------- #
# Follow-up notifications
# --------------------------------------------------------------------------- #
FOLLOW_UP_NOTIFICATION_KIND = "application_follow_up"


def due_follow_ups(db: Session, *, now: Optional[datetime] = None, limit: int = 200,
                   user_id: Optional[int] = None) -> List[ApplicationTracking]:
    """Records whose promised reminder is due and has not been notified yet."""
    moment = now or datetime.utcnow()
    horizon = moment + timedelta(hours=max(0, int(settings.tracking_follow_up_lookahead_hours)))
    query = db.query(ApplicationTracking).filter(
        ApplicationTracking.follow_up_at.is_not(None),
        ApplicationTracking.follow_up_notified_at.is_(None),
        ApplicationTracking.follow_up_completed_at.is_(None),
        ApplicationTracking.follow_up_at <= horizon,
    )
    if user_id is not None:
        query = query.filter(ApplicationTracking.user_id == int(user_id))
    return list(query.order_by(ApplicationTracking.follow_up_at.asc())
                .limit(max(1, min(int(limit), 1000))).all())


def upcoming_follow_ups(db: Session, user_id: int, *, days: int = 14,
                        limit: int = 50, include_overdue: bool = True) -> List[Dict[str, Any]]:
    """The user's follow-up agenda (the UI renders this next to the timeline)."""
    moment = datetime.utcnow()
    horizon = moment + timedelta(days=max(1, min(int(days), 90)))
    query = db.query(ApplicationTracking).filter(
        ApplicationTracking.user_id == int(user_id),
        ApplicationTracking.follow_up_at.is_not(None),
        ApplicationTracking.follow_up_completed_at.is_(None),
        ApplicationTracking.follow_up_at <= horizon,
    )
    if not include_overdue:
        query = query.filter(ApplicationTracking.follow_up_at >= moment)
    rows = query.order_by(ApplicationTracking.follow_up_at.asc()).limit(max(1, min(int(limit), 200))).all()
    return [
        {
            "tracking_id": int(row.id),
            "job_id": int(row.job_id),
            "title": row.job_title_snapshot or "",
            "company": row.company_snapshot or "",
            "state": row.state,
            "state_label": STATE_LABELS.get(row.state, row.state),
            "due_at": iso_utc(row.follow_up_at),
            "note": row.follow_up_note or "",
            "overdue": bool(row.follow_up_at and row.follow_up_at <= moment),
            "notified_at": iso_utc(row.follow_up_notified_at),
            "route": f"/tracking?tracking_id={int(row.id)}",
        }
        for row in rows
    ]


def _notify(db: Session, user_id: int, kind: str, title: str, body: str, link: str,
            meta: Dict[str, Any]) -> bool:
    """One notification per ``(tracking_id, kind, due_at)`` — a sweep never re-notifies."""
    from app.api.routers.notifications import create_notification

    rows = (
        db.query(Notification)
        .filter(Notification.user_id == int(user_id), Notification.kind == kind)
        .order_by(Notification.id.desc())
        .limit(50)
        .all()
    )
    for row in rows:
        row_meta = row.meta if isinstance(row.meta, dict) else {}
        if (row_meta.get("tracking_id") == meta.get("tracking_id")
                and row_meta.get("due_at") == meta.get("due_at")
                and row_meta.get("reason") == meta.get("reason")):
            return False
    created = create_notification(db, int(user_id), kind, title[:200], body=body[:2000],
                                  link=link[:500], meta=meta)
    return bool(created)


def _notify_suggestion(db: Session, record: ApplicationTracking, suggested_state: str,
                       event: Optional[ApplicationTrackingEvent]) -> bool:
    """Tell the user we *think* something happened, and that it is unconfirmed."""
    return _notify(
        db, int(record.user_id), "application_outcome",
        title=f"Did {record.company_snapshot or 'an employer'} reply?",
        body=(f"We read an inbound message that looks like a response for "
              f"{record.company_snapshot} — {record.job_title_snapshot}. Nothing was recorded as an "
              f"interview: confirm or dismiss it on the tracking page."),
        link=f"/tracking?tracking_id={int(record.id)}",
        meta={"tracking_id": int(record.id), "job_id": int(record.job_id),
              "suggested_state": suggested_state, "is_provisional": True,
              "reason": "email_suggestion",
              "due_at": None,
              "event_id": event.event_id if event else None},
    )


def notify_due_follow_ups(db: Session, *, now: Optional[datetime] = None, limit: int = 200,
                          user_id: Optional[int] = None, commit: bool = True) -> Dict[str, int]:
    """Write the reminders that are due. Idempotent, tenant-safe, bounded.

    Called by the worker's maintenance sweep (so a reminder arrives even if the
    user never opens the app) and by the follow-ups read for the requesting user
    (so it is there by the time they look). ``follow_up_notified_at`` is the
    dedupe marker: a restart, a second worker or a repeated sweep notifies once.
    """
    moment = coerce_datetime(now) or datetime.utcnow()
    counts = {"due": 0, "notified": 0, "skipped": 0}
    rows = due_follow_ups(db, now=moment, limit=limit, user_id=user_id)
    counts["due"] = len(rows)
    for record in rows:
        due = record.follow_up_at
        overdue_days = _days_between(due, moment) or 0
        when = "today" if not overdue_days else f"{overdue_days} day(s) ago"
        body = (
            f"{record.company_snapshot or 'Employer'} — {record.job_title_snapshot or 'role'}: "
            f"you asked to follow up {when}. Last recorded status: "
            f"{STATE_LABELS.get(record.state, record.state)}"
            f" ({record.state_origin.replace('_', ' ')})."
        )
        if record.follow_up_note:
            body += f" Note to self: {record.follow_up_note[:200]}"
        created = _notify(
            db, int(record.user_id), FOLLOW_UP_NOTIFICATION_KIND,
            title=f"Follow-up due: {record.company_snapshot or record.job_title_snapshot or 'application'}",
            body=body,
            link=f"/tracking?tracking_id={int(record.id)}",
            meta={"tracking_id": int(record.id), "job_id": int(record.job_id),
                  "state": record.state, "due_at": iso_utc(due), "reason": "follow_up_due",
                  "overdue_days": overdue_days},
        )
        # Marked whether or not the notification was new, so a suppressed
        # duplicate cannot make the sweep reconsider this row forever.
        record.follow_up_notified_at = moment
        if created:
            counts["notified"] += 1
            inc("jobhunter_application_follow_up_notifications_total", state=record.state)
        else:
            counts["skipped"] += 1
    if commit and rows:
        db.commit()
    return counts


# --------------------------------------------------------------------------- #
# Reporting — application → interview conversion, grouped
# --------------------------------------------------------------------------- #
#: What the report may be grouped by. Anything else is a ``422``, never a silent
#: empty list (contracts/01 §4).
REPORT_GROUP_KEYS: Tuple[str, ...] = (
    "source", "role", "match_band", "score_bucket", "artifact_version",
    "artifact_kind", "channel", "state",
)

#: Score buckets for "group by match score" — a band is the scorer's word, a
#: bucket is the report's, and both are offered because the user asked for
#: "match score", not for our band names.
SCORE_BUCKETS: Tuple[Tuple[str, float, float], ...] = (
    ("90-100", 90.0, 1000.0),
    ("80-89", 80.0, 90.0),
    ("70-79", 70.0, 80.0),
    ("60-69", 60.0, 70.0),
    ("0-59", 0.0, 60.0),
)

REPORT_DISCLAIMER = (
    "Counts come from the tracking timeline: an interview is only ever counted when you "
    "recorded one (or a verified integration did). Nothing here is inferred from email, and "
    "a match score is an estimated fit, not an interview probability."
)


def _score_bucket(score: Optional[float]) -> str:
    if score is None:
        return "unscored"
    for label, low, high in SCORE_BUCKETS:
        if low <= float(score) < high:
            return label
    return "unscored"


def _artifact_label(record: ApplicationTracking) -> str:
    if record.artifact_kind == "none" or not record.artifact_kind:
        return "none"
    version = record.artifact_version
    return f"{record.artifact_kind}:v{version}" if version is not None else f"{record.artifact_kind}:unversioned"


def _group_value(record: ApplicationTracking, group_by: str) -> str:
    if group_by == "source":
        return record.source or "unknown"
    if group_by == "role":
        return record.role_family or role_family(record.job_title_snapshot) or "Other"
    if group_by == "match_band":
        return record.match_band or "unscored"
    if group_by == "score_bucket":
        return _score_bucket(record.match_score)
    if group_by == "artifact_version":
        return _artifact_label(record)
    if group_by == "artifact_kind":
        return record.artifact_kind or "none"
    if group_by == "channel":
        return record.channel or "unknown"
    if group_by == "state":
        return record.state
    raise UnknownValue(f"Cannot group by '{group_by}'.", field="group_by", allowed=list(REPORT_GROUP_KEYS))


def _tally(rows: Iterable[ApplicationTracking]) -> Dict[str, Any]:
    """The conversion numbers for a set of records."""
    tracked = applied = interviews = offers = rejections = withdrawn = responses = 0
    provisional = corrections = 0
    scores: List[float] = []
    by_origin: Dict[str, int] = {}
    days_to_interview: List[int] = []
    for row in rows:
        tracked += 1
        by_origin[row.state_origin] = by_origin.get(row.state_origin, 0) + 1
        corrections += int(row.correction_count or 0)
        provisional += 1 if row.is_provisional else 0
        was_applied = row.applied_at is not None
        applied += 1 if was_applied else 0
        if row.state in TRACKING_INTERVIEW_STATES:
            interviews += 1
            gap = _days_between(row.applied_at, row.interview_scheduled_at)
            if gap is not None:
                days_to_interview.append(gap)
        if row.state == "offer_received":
            offers += 1
        if row.state == "rejected_by_employer":
            rejections += 1
        if row.state == "withdrawn":
            withdrawn += 1
        if row.first_response_at is not None:
            responses += 1
        if row.match_score is not None:
            scores.append(float(row.match_score))
    return {
        "tracked": tracked,
        "applied": applied,
        "responses": responses,
        "interviews": interviews,
        "offers": offers,
        "rejections": rejections,
        "withdrawn": withdrawn,
        "provisional": provisional,
        "corrections": corrections,
        # The acceptance criterion: applications that produced an interview.
        "application_to_interview_rate": _rate(interviews, applied),
        "applied_to_response_rate": _rate(responses, applied),
        "interview_to_offer_rate": _rate(offers, interviews),
        "avg_match_score": round(sum(scores) / len(scores), 1) if scores else None,
        "avg_days_to_interview": (round(sum(days_to_interview) / len(days_to_interview), 1)
                                  if days_to_interview else None),
        "by_origin": by_origin,
    }


def _rate(numerator: int, denominator: int) -> Optional[float]:
    """A percentage, or ``None`` when the denominator is 0.

    ``None`` and not ``0.0``: "no applications yet" and "applications, no
    interviews" are different facts and a dashboard that renders both as 0% is
    lying about one of them (contracts/01 §2 rule 2).
    """
    if not denominator:
        return None
    return round(100.0 * float(numerator) / float(denominator), 1)


def report(db: Session, user_id: int, *, group_by: str = "source",
           since: Optional[datetime] = None, until: Optional[datetime] = None,
           origin: Optional[str] = None, state: Optional[str] = None,
           include_unapplied: bool = False) -> Dict[str, Any]:
    """Conversion grouped by source, role, match score or artefact version.

    The denominator is **records that actually applied** (``applied_at`` set), so
    a job merely saved to the board cannot dilute the rate. ``by_origin`` is
    returned at every level: a conversion built entirely out of the user's memory
    is a different claim from one our own submission machinery observed, and the
    report says which it is.
    """
    if group_by not in REPORT_GROUP_KEYS:
        raise UnknownValue(f"Cannot group by '{group_by}'.",
                           field="group_by", allowed=list(REPORT_GROUP_KEYS))
    if origin is not None and origin not in TRACKING_ORIGINS:
        raise UnknownValue(f"'{origin}' is not a tracking origin.",
                           field="origin", allowed=list(TRACKING_ORIGINS))
    if state is not None:
        _require_state(state)

    start = coerce_datetime(since)
    end = coerce_datetime(until)
    query = db.query(ApplicationTracking).filter(ApplicationTracking.user_id == int(user_id))
    # The window filters on the *application* date when there is one, and on the
    # record's creation otherwise, so "last month" means applications from last
    # month rather than rows inserted then.
    window_field = func.coalesce(ApplicationTracking.applied_at, ApplicationTracking.created_at)
    if start is not None:
        query = query.filter(window_field >= start)
    if end is not None:
        query = query.filter(window_field <= end)
    if origin:
        query = query.filter(ApplicationTracking.state_origin == origin)
    if state:
        query = query.filter(ApplicationTracking.state == state)
    rows = list(query.order_by(ApplicationTracking.id.asc()).all())
    if not include_unapplied:
        rows = [row for row in rows if row.applied_at is not None]

    grouped: Dict[str, List[ApplicationTracking]] = {}
    for row in rows:
        grouped.setdefault(_group_value(row, group_by), []).append(row)
    groups = []
    for key in sorted(grouped, key=lambda item: (-len(grouped[item]), item)):
        bucket = grouped[key]
        entry = {"key": key, "label": key, "records": len(bucket)}
        entry.update(_tally(bucket))
        groups.append(entry)

    totals = _tally(rows)
    return {
        "group_by": group_by,
        "window": {"since": iso_utc(start), "until": iso_utc(end)},
        "filters": {"origin": origin, "state": state, "include_unapplied": bool(include_unapplied)},
        "totals": totals,
        "groups": groups,
        "group_count": len(groups),
        "disclaimer": REPORT_DISCLAIMER,
        "interview_definition": list(TRACKING_INTERVIEW_STATES),
        "server_time": iso_utc(datetime.utcnow()),
    }


def state_counts(db: Session, user_id: int) -> Dict[str, int]:
    """``{state: n}`` for the board header — one grouped query, not a Python loop."""
    rows = (
        db.query(ApplicationTracking.state, func.count(ApplicationTracking.id))
        .filter(ApplicationTracking.user_id == int(user_id))
        .group_by(ApplicationTracking.state)
        .all()
    )
    counts = dict.fromkeys(APPLICATION_TRACKING_STATES, 0)
    for state, count in rows:
        counts[state] = int(count)
    return counts
