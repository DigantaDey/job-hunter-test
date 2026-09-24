"""
Browser-assisted application sessions — the human-in-the-loop API.

The surface is deliberately small and honest about who does what:

* ``POST /application-sessions`` starts an isolated run for one job.
* ``POST …/observe`` folds a *structured, privacy-minimized* page observation
  into the session (field names/types/required/options, markers) and returns
  either the fields that are safe to fill or the pause the run must raise.
* ``POST …/fills`` records what the assistant typed — a fingerprint per field,
  never a value — which is what keeps a resume from typing anything twice.
* ``POST …/pause`` / ``…/resume`` are the user's own controls; resume runs
  checkpoint validation (job, URL, employer, application identity) first.
* ``GET  /application-sessions/actions`` is the actionable queue: CAPTCHA and
  MFA items are first-class rows there, each with its handoff descriptor.
* ``POST …/actions/{id}/handoff`` mints a one-time, short-lived window for the
  user to complete a login/MFA/CAPTCHA step **in the browser themselves** — and,
  when this host can show one, opens the session's own Playwright window on
  that step's URL (``window_opened`` in the descriptor says which happened), so
  the login's cookies belong to the automation's context and the continuing
  pass resumes signed in.
* ``POST …/actions/{id}/complete`` closes the item. It accepts no secret: a
  payload carrying a password/code field is refused with ``restricted_field``.

Nothing here accepts or returns a password, an MFA code or a CAPTCHA solution,
and the session payload never includes persisted browser state — only whether it
is stored, when it was saved, and when it was purged.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.core import audit
from app.core.entitlements import enforce
from app.core.logging import get_logger
from app.models.models import ApplicationSession, Job
from app.services import assisted_fill, live_browser
from app.services import browser_session as sessions
from app.services.apply_flow import prepare_form_plan

router = APIRouter(prefix="/application-sessions", tags=["application-sessions"])
log = get_logger("app.sessions.api")

#: Field names an answer may never carry: these are the values the user types in
#: the browser, and accepting one would defeat the entire handoff model.
SECRET_FIELD_PATTERN = re.compile(
    r"(pass(word|phrase|code)|passwd|\botp\b|one[-_ ]?time|mfa|2fa|totp|"
    r"verification[-_ ]?code|security[-_ ]?code|captcha|\bpin\b|\bsecret\b)",
    re.IGNORECASE,
)


class StartSessionRequest(BaseModel):
    job_id: int
    #: Refresh the detected form/plan before the session starts (default: yes).
    refresh_form: bool = True


class ObservationRequest(BaseModel):
    """A structural page observation. Values are neither required nor wanted."""

    url: str = ""
    host: str = ""
    title: str = ""
    employer: str = ""
    application_identity: str = ""
    markers: List[str] = Field(default_factory=list)
    challenge: str = ""
    submit_present: bool = False
    logged_in: Optional[bool] = None
    fields: List[Dict[str, Any]] = Field(default_factory=list)


class FillRecord(BaseModel):
    name: str
    value: Optional[str] = None
    classification: Optional[str] = None
    profile_key: Optional[str] = None


class FillsRequest(BaseModel):
    fills: List[FillRecord] = Field(default_factory=list)


class ResumeRequest(BaseModel):
    observed: Optional[ObservationRequest] = None
    #: What the user is looking at, when the user is the one confirming.
    confirm_job_id: Optional[int] = None
    confirm_url: Optional[str] = None
    confirm_employer: Optional[str] = None
    confirm_application_identity: Optional[str] = None


class AnswersRequest(BaseModel):
    answers: Dict[str, Any] = Field(default_factory=dict)


class CompleteActionRequest(BaseModel):
    note: str = ""
    answers: Dict[str, Any] = Field(default_factory=dict)


class PassRequest(BaseModel):
    """Run one assisted pass with a real browser (requires Playwright)."""

    open_browser: bool = True
    max_steps: int = Field(default=assisted_fill.MAX_PASS_STEPS, ge=1, le=12)


def _ensure_no_secrets(payload: Dict[str, Any]) -> None:
    """Refuse a payload that carries a value only the user may type in the browser."""
    offending = [key for key in payload if SECRET_FIELD_PATTERN.search(str(key))]
    if offending:
        raise HTTPException(
            422,
            {
                "code": "restricted_field",
                "message": ("Passwords, one-time codes and CAPTCHA answers are entered by you in "
                            "the browser. This endpoint never receives them."),
                "fields": offending[:10],
            },
        )


def _job_for(db, user, job_id: int) -> Job:
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == user.id).first()
    if job is None:
        raise HTTPException(404, {"code": "job_not_found", "message": "Job not found"})
    return job


def _session_or_404(db, user, session_id: int) -> ApplicationSession:
    try:
        return sessions.own_session(db, user, session_id)
    except sessions.SessionNotFound as exc:
        raise HTTPException(exc.status_code, exc.payload()) from exc


def _queue_continuing_pass(db, user, session: ApplicationSession, *, trigger: str) -> Optional[Dict[str, Any]]:
    """
    Queue the next assisted pass after the user unblocks a session.

    Best-effort by design: a deployment without browser automation, or a queue
    hiccup, must not fail the request that finished the human's step — the
    session state is already committed, and the pass can be queued again by hand.
    """
    from app.services.autofill import assisted_apply_available

    if not assisted_apply_available()["available"]:
        return None
    try:
        return assisted_fill.enqueue_pass(db, user=user, session=session, trigger=trigger)
    except Exception as exc:  # pragma: no cover - a queue failure is not a session failure
        log.warning("could not queue a continuing pass for session %s (%s)", session.id, type(exc).__name__)
        return None


def _session_error(exc: sessions.SessionError) -> HTTPException:
    return HTTPException(exc.status_code, exc.payload())


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
@router.post("")
async def start_session(body: StartSessionRequest, request: Request, user: CurrentUser, db: DbSession):
    """
    Start (or return) the assisted browser session for one job.

    Preparing first is what makes the first observation cheap: the form schema,
    the field plan and the resume choice already exist, so the session starts
    with something to compare the live page against.
    """
    job = _job_for(db, user, body.job_id)
    if body.refresh_form:
        try:
            await prepare_form_plan(db, user, job)
        except ValueError as exc:
            # A missing profile is a real blocker, not a session error.
            raise HTTPException(400, {"code": str(exc), "message": str(exc)}) from exc

    try:
        session, created = sessions.start_session(db, user=user, job=job)
    except sessions.SessionError as exc:
        raise _session_error(exc) from exc

    if created:
        audit.audit(db, "application.session_started", user=user, target=f"job:{job.id}",
                    detail={"session_id": session.id, "portal": session.portal_type,
                            "host": session.expected_host, "ttl_minutes":
                            sessions.effective_policy(db, user.id).ttl_minutes},
                    request=request)
    return {"created": created, "session": sessions.session_detail(db, session=session)}


@router.get("")
def list_sessions(user: CurrentUser, db: DbSession, include_closed: bool = Query(False)):
    """This user's sessions, newest first. Closed ones only when asked for."""
    return {
        "sessions": sessions.list_sessions(db, user, include_closed=include_closed),
        "stats": sessions.session_stats(db, user.id),
        "policy": sessions.effective_policy(db, user.id).as_dict(),
    }


@router.get("/actions")
def action_queue(user: CurrentUser, db: DbSession):
    """
    The actionable queue across every session — including CAPTCHA and MFA items.

    This is the endpoint the acceptance criterion points at: a bot check or a
    second factor is not a log line, it is a row the user can act on.
    """
    return {"actions": sessions.action_queue(db, user.id),
            "stats": sessions.session_stats(db, user.id)}


@router.get("/stats")
def session_stats(user: CurrentUser, db: DbSession):
    return sessions.session_stats(db, user.id)


@router.get("/{session_id}")
def session_detail(session_id: int, user: CurrentUser, db: DbSession):
    session = _session_or_404(db, user, session_id)
    sessions.sweep_expired_sessions(db, user_id=user.id)
    db.refresh(session)
    detail = sessions.session_detail(db, session=session)
    job = db.query(Job).filter(Job.id == session.job_id).first()
    detail["plan"] = (assisted_fill.plan_for_session(job, session, db=db) if job else None)
    return detail


# --------------------------------------------------------------------------- #
# Observation, filling, pausing, resuming
# --------------------------------------------------------------------------- #
@router.post("/{session_id}/observe")
def observe(session_id: int, body: ObservationRequest, request: Request, user: CurrentUser, db: DbSession):
    """
    Fold one structural page observation into the session.

    Returns ``to_fill`` (the fields that are safe to type, already excluding
    anything a checkpoint says is done) or the ``pause`` the run must raise.
    """
    session = _session_or_404(db, user, session_id)
    job = db.query(Job).filter(Job.id == session.job_id).first()
    # Values are resolved server-side from the job's plan and the session's own
    # working set; the API never carries profile values in from the client.
    working = sessions.session_values(session)
    values = sessions.planned_values(job, answers=working) if job else working
    try:
        outcome = sessions.record_observation(db, session, body.model_dump(), user=user, values=values)
    except sessions.SessionExpired as exc:
        raise _session_error(exc) from exc
    except sessions.SessionError as exc:
        raise _session_error(exc) from exc
    if outcome.get("pause"):
        audit.audit(db, "application.session_paused", user=user, target=f"job:{session.job_id}",
                    detail={"session_id": session.id, "kind": outcome["pause"],
                            "action_id": outcome.get("action_id")}, request=request)
    return outcome


@router.post("/{session_id}/fills")
def record_fills(session_id: int, body: FillsRequest, user: CurrentUser, db: DbSession):
    """Record what was typed. Values are fingerprinted and discarded, never stored."""
    session = _session_or_404(db, user, session_id)
    payload = [{"name": f.name, "value": f.value, "classification": f.classification,
                "profile_key": f.profile_key} for f in body.fills]
    result = sessions.record_fills(db, session, payload)
    return {"recorded": result["recorded"], "filled_total": result["filled_total"],
            "already_completed": sessions.already_completed_fields(session)}


@router.post("/{session_id}/pause")
def pause(session_id: int, user: CurrentUser, db: DbSession, reason: str = Query("user_requested")):
    session = _session_or_404(db, user, session_id)
    try:
        detail = sessions.pause_for_user(db, user=user, session=session, reason=reason)
    except sessions.SessionExpired as exc:
        raise _session_error(exc) from exc
    except sessions.SessionError as exc:
        raise _session_error(exc) from exc
    return detail


@router.post("/{session_id}/resume")
def resume(session_id: int, body: ResumeRequest, request: Request, user: CurrentUser, db: DbSession):
    """
    Resume a paused session — after checkpoint validation, not before.

    A mismatch (URL, employer, application identity, ownership, expiry) is a
    ``409`` with the failing codes; the session stays paused with the reason
    recorded and a fresh queue item telling the user what to confirm.
    """
    session = _session_or_404(db, user, session_id)
    confirmations: Dict[str, Any] = {}
    if body.confirm_job_id is not None:
        confirmations["job_id"] = body.confirm_job_id
    if body.confirm_url:
        confirmations["url"] = body.confirm_url
    if body.confirm_employer:
        confirmations["employer"] = body.confirm_employer
    if body.confirm_application_identity:
        confirmations["application_identity"] = body.confirm_application_identity
    if body.confirm_job_id is not None and body.confirm_job_id != session.job_id:
        raise HTTPException(409, {"code": "checkpoint_failed",
                                  "failures": [{"code": "job_reassigned",
                                                "detail": "the confirmed job is not this session's job"}]})
    observed = body.observed.model_dump() if body.observed else None
    try:
        outcome = sessions.resume_session(db, user=user, session=session, observed=observed,
                                          confirmations=confirmations)
    except sessions.SessionExpired as exc:
        raise _session_error(exc) from exc
    except sessions.CheckpointFailed as exc:
        raise _session_error(exc) from exc
    except sessions.SessionError as exc:
        raise _session_error(exc) from exc
    audit.audit(db, "application.session_resumed", user=user, target=f"job:{session.job_id}",
                detail={"session_id": session.id, "resumed_count": outcome["resumed_count"],
                        "completed_fields": len(outcome["completed_fields"])},
                request=request)
    queued = _queue_continuing_pass(db, user, session, trigger="retry")
    if queued:
        outcome["continuing_pass"] = queued
    return outcome


@router.post("/{session_id}/reauthenticate")
async def reauthenticate(session_id: int, request: Request, user: CurrentUser, db: DbSession):
    """
    Restart an expired session the safe way: drop persisted state, new TTL, and a
    login handoff so the user re-proves who they are in the browser.

    Any window left from the old session closes first — its cookie jar is the
    one we just purged — and a fresh one opens on the posting for the new
    sign-in, when this host can show a window at all.
    """
    session = _session_or_404(db, user, session_id)
    try:
        outcome = sessions.reauthenticate(db, user=user, session=session)
    except sessions.SessionError as exc:
        raise _session_error(exc) from exc
    # The old window's cookie jar is the one we just purged: close it, then
    # open a fresh one on the posting for the new sign-in.
    await live_browser.close(session.id)
    job = db.query(Job).filter(Job.id == session.job_id).first()
    if job is not None:
        window = await assisted_fill.open_handoff_window(
            db, user=user, session=session,
            url=job.url or str((session.last_observation or {}).get("url") or ""))
        outcome["window"] = window
    audit.audit(db, "application.session_expired", user=user, target=f"job:{session.job_id}",
                detail={"session_id": session.id, "action": "reauthenticate"}, request=request)
    return outcome


@router.post("/{session_id}/cancel")
async def cancel(session_id: int, request: Request, user: CurrentUser, db: DbSession,
                 reason: str = Query("user_cancelled")):
    session = _session_or_404(db, user, session_id)
    sessions.cancel_session(db, user=user, session=session, reason=reason)
    # A cancelled session has no window: close it before the response says
    # "cancelled", not whenever the idle sweep gets around to it.
    await live_browser.close(session.id)
    audit.audit(db, "application.session_cancelled", user=user, target=f"job:{session.job_id}",
                detail={"session_id": session.id, "reason": reason}, request=request)
    return sessions.session_detail(db, session=session)


# --------------------------------------------------------------------------- #
# Actions (login / MFA / CAPTCHA / field questions)
# --------------------------------------------------------------------------- #
def _action_or_404(db, user, session: ApplicationSession, action_id: int):
    from app.models.models import ApplicationAction

    action = (
        db.query(ApplicationAction)
        .filter(ApplicationAction.id == action_id, ApplicationAction.user_id == user.id,
                ApplicationAction.session_id == session.id)
        .first()
    )
    if action is None:
        raise HTTPException(404, {"code": "not_found", "message": "Action not found",
                                  "action_id": action_id})
    return action


@router.post("/{session_id}/actions/{action_id}/handoff")
async def issue_handoff(session_id: int, action_id: int, request: Request, user: CurrentUser, db: DbSession):
    """
    Mint a one-time handoff window for a human-only browser step — and, when
    this host can show one, actually **open the session's browser window** on
    the step's URL so the human works in the very context the next pass
    continues in.

    The response carries the token exactly once (only its hash is stored) and
    states, in the payload the UI renders, that the user — not the automation —
    performs the step. ``window_opened`` is the honest answer to "did a window
    appear?"; when it is false the descriptor's link into the user's own
    browser remains, with the reason why no window could be shown.
    """
    session = _session_or_404(db, user, session_id)
    action = _action_or_404(db, user, session, action_id)
    try:
        descriptor = sessions.issue_handoff(db, user=user, session=session, action=action)
    except sessions.SessionExpired as exc:
        raise _session_error(exc) from exc
    except sessions.SessionError as exc:
        raise _session_error(exc) from exc
    window = await assisted_fill.open_handoff_window(
        db, user=user, session=session, url=str(descriptor.get("url") or ""))
    descriptor.update(window)
    audit.audit(db, "application.handoff_issued", user=user, target=f"job:{session.job_id}",
                detail={"session_id": session.id, "action_id": action.id, "kind": action.kind,
                        "window_opened": bool(window.get("window_opened"))},
                request=request)
    return descriptor


@router.post("/{session_id}/actions/{action_id}/complete")
async def complete_action(session_id: int, action_id: int, body: CompleteActionRequest, request: Request,
                          user: CurrentUser, db: DbSession):
    """
    Close an action. Secrets are refused here, by name — see ``restricted_field``.

    Before the continuing pass is queued, the live window's cookie jar is
    exported (when persistence is enabled): that is how a sign-in the human
    just performed in the window reaches the automation, so the next pass
    resumes logged in instead of meeting the login wall again.
    """
    session = _session_or_404(db, user, session_id)
    action = _action_or_404(db, user, session, action_id)
    _ensure_no_secrets(body.answers)
    if body.answers:
        sessions.record_user_answers(db, session, body.answers)
    sessions.complete_action(db, user=user, action=action, note=body.note)
    # Any values the user typed in the browser stay in the browser: only the
    # fact that the step is done is recorded — except the cookie jar, which
    # the next pass legitimately needs to still be signed in.
    if session.state == "awaiting_user":
        sessions.transition(session, "paused", actor="user", reason="action_completed")
        db.commit()
    driver = live_browser.adopt(session.id)
    if driver is not None and hasattr(driver, "export_storage_state"):
        policy = sessions.effective_policy(db, user.id)
        if policy.persist_state:
            try:
                state = await driver.export_storage_state()
                if state:
                    sessions.persist_storage_state(db, session, state, policy=policy)
            except Exception as exc:  # pragma: no cover - best effort by design
                log.warning("could not persist the window's state (%s)", type(exc).__name__)
    audit.audit(db, "application.action_completed", user=user, target=f"job:{session.job_id}",
                detail={"session_id": session.id, "action_id": action.id, "kind": action.kind},
                request=request)
    detail = sessions.session_detail(db, session=session)
    # The human finished their step; continue the run from the checkpoint
    # (never from the top — nothing already typed is typed again).
    queued = _queue_continuing_pass(db, user, session, trigger="user")
    if queued:
        detail["continuing_pass"] = queued
    return detail


# --------------------------------------------------------------------------- #
# Running the browser
# --------------------------------------------------------------------------- #
@router.post("/{session_id}/pass")
async def run_pass(session_id: int, body: PassRequest, request: Request, user: CurrentUser, db: DbSession):
    """
    Run one assisted pass in a real browser.

    Refused with ``assisted_apply_unavailable`` when the interactive browser
    runtime is not installed or has been disabled — never simulated. This is
    intentionally independent from the unattended Auto-apply feature flag.
    The pass stops at the first human-required step and the queue item says
    which one.
    """
    session = _session_or_404(db, user, session_id)
    enforce(db, user.id, "can_use_autofill")
    # A pass fills fields; it never submits unless the policy chain allows it —
    # ``run_pass`` only reaches the submit control through a reservation.
    from app.services.autofill import assisted_apply_available

    availability = assisted_apply_available()
    if not availability["available"]:
        raise HTTPException(503, {"code": "assisted_apply_unavailable", "message": availability["reason"]})

    job = db.query(Job).filter(Job.id == session.job_id).first()
    if job is None:
        raise HTTPException(404, {"code": "job_not_found", "message": "Job not found"})

    policy = sessions.effective_policy(db, user.id)
    # A window the session left open for the human is the driver to use: it is
    # the context they may have just signed in to. Otherwise a fresh one.
    driver = live_browser.adopt(session.id)
    if driver is None:
        storage_state = sessions.load_storage_state(db, session, user=user, policy=policy)
        driver = assisted_fill.PlaywrightDriver(session, target_url=job.url, storage_state=storage_state,
                                                allow_submit=policy.allow_submit)
    try:
        result = await assisted_fill.run_pass(db, user=user, session=session, driver=driver,
                                              policy=policy, open_driver=body.open_browser,
                                              max_steps=body.max_steps)
    except sessions.SessionExpired as exc:
        raise _session_error(exc) from exc
    except assisted_fill.DriverError as exc:
        raise HTTPException(502, {"code": "portal_unreachable", "message": str(exc)}) from exc
    audit.audit(db, "application.session_started", user=user, target=f"job:{job.id}",
                detail={"session_id": session.id, "pass": True, "status": result.get("status")},
                request=request)
    detail = sessions.session_detail(db, session=session)
    detail["pass"] = result
    return detail


@router.get("/{session_id}/screenshots")
def screenshots(session_id: int, user: CurrentUser, db: DbSession):
    """Screenshot *metadata* only, and only what the privacy policy permitted."""
    session = _session_or_404(db, user, session_id)
    policy = sessions.effective_policy(db, user.id)
    return {
        "entries": list(session.screenshots or []),
        "policy": sessions.screenshot_decision(policy, kind="field_fill"),
        "never_captured": list(sessions.NEVER_SCREENSHOT_KINDS),
    }


@router.post("/{session_id}/submit")
def submit(session_id: int, request: Request, user: CurrentUser, db: DbSession):
    """
    Reserve the single submission slot for this job.

    Refusals are honest and typed: ``already_submitted`` (the ledger or the job
    itself), ``policy_disallows_submit`` (the deployment/user gate), ``session_expired``,
    ``action_required`` (something is still waiting on the user) or
    ``checkpoint_invalid``. In this task auto-submit is off by default, so the
    normal answer is a refusal that says so.
    """
    session = _session_or_404(db, user, session_id)
    job = db.query(Job).filter(Job.id == session.job_id).first()
    if job is None:
        raise HTTPException(404, {"code": "job_not_found", "message": "Job not found"})
    row, decision = sessions.reserve_submission(db, user=user, job=job, session=session,
                                                channel="automation")
    if row is None:
        code = "application_already_submitted" if decision["reason"] == "already_submitted" \
            else "application_not_submittable"
        audit.audit(db, "application.submission_refused", user=user, target=f"job:{job.id}",
                    detail={"session_id": session.id, "reason": decision["reason"],
                            "policy_id": decision.get("policy_id"),
                            "policy_version": decision.get("policy_version")},
                    request=request)
        raise HTTPException(409, {"code": code, "message": decision["reason"], **decision})
    audit.audit(db, "application.submission_reserved", user=user, target=f"job:{job.id}",
                detail={"session_id": session.id, "submission_id": row.id,
                        "idempotency_key": row.idempotency_key, "dry_run": row.dry_run,
                        "policy_id": row.policy_id, "policy_version": row.policy_version},
                request=request)
    return {"reserved": True, "submission_id": row.id, "state": row.state, "dry_run": row.dry_run,
            "idempotency_key": row.idempotency_key, "decision": decision}
