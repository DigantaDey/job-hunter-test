"""
Assisted filling: the browser side of a session, and the pass loop around it.

Everything *policy* lives in :mod:`app.services.browser_session`; this module
knows how to drive a browser and nothing more. Two drivers:

``RecordingDriver``
    Deterministic, no network, no browser. Takes a script of observations and
    records what it was asked to type; it is what the test suite and a
    dry-run rehearsal use.
``PlaywrightDriver``
    The real thing, imported lazily. It is only constructed when Playwright is
    installed, ``AUTOFILL_ENABLED`` is true and the outbound URL policy passes —
    the same gates the shipped autofill honours, plus the session's own
    checkpoint.

What a driver may never do, at any layer
----------------------------------------
* Type into a field the classifier did not mark ``autofill``. The fill loop
  re-checks every instruction against the classification and refuses the rest.
* Overwrite a field the page already shows as user-filled.
* Solve, click, relay or "assist" a CAPTCHA; enter an MFA code; type a password.
* Read page text beyond the structural observation (labels/types/options), or
  keep a field value anywhere but in the fill call itself.
* Submit unless the *session* policy allows it. In this task auto-submit is off
  unless the deployment and the user both enabled it, and the at-most-once
  ledger still has to grant the reservation.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc
from app.models.models import ApplicationSession, Job, User
from app.services import browser_session as sessions
from app.services import net_guard
from app.services.autofill import autofill_available
from app.services.field_classifier import classify_form
from app.services.form_detector import VAULT_DOMAINS

log = get_logger("app.assisted")

#: How many observe → fill → observe rounds a single pass may run before it
#: stops and asks a human. A form that keeps producing work is not a reason to
#: loop forever inside someone's session.
MAX_PASS_STEPS = 6

#: Observation script: structure only. Values are read as *presence*, never
#: returned — a driver that shipped values back would put page content (and
#: whatever the user typed) into the session row.
OBSERVE_SCRIPT = """
() => {
  const out = {url: location.href, host: location.host, title: document.title, fields: []};
  const nodes = document.querySelectorAll('input, select, textarea');
  for (const el of nodes) {
    const type = (el.getAttribute('type') || el.tagName.toLowerCase()).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) continue;
    const label = el.labels && el.labels.length ? el.labels[0].innerText
      : (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || '');
    let options = [];
    if (el.tagName.toLowerCase() === 'select') {
      options = Array.from(el.options || []).slice(0, 24).map(o => (o.text || '').trim());
    }
    const value = el.value;
    out.fields.push({
      name: el.name || el.id || '',
      label: (label || '').trim().slice(0, 200),
      type: type,
      required: el.required || el.getAttribute('aria-required') === 'true',
      options: options,
      autocomplete: el.getAttribute('autocomplete') || '',
      value_present: !!(value && String(value).trim().length),
      filled_by_user: false,
      classes: Array.from(el.classList).slice(0, 8),
    });
    if (out.fields.length >= 120) break;
  }
  const markers = [];
  if (document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], .g-recaptcha, [data-sitekey]')) markers.push('captcha');
  if (document.querySelector('input[autocomplete="one-time-code"], input[name*="otp" i], input[name*="mfa" i]')) markers.push('mfa');
  for (const el of document.querySelectorAll('input[type=password]')) { markers.push('login'); break; }
  out.markers = markers;
  out.submit_present = !!document.querySelector('button[type=submit], input[type=submit]');
  return out;
}
""".strip()


class DriverError(RuntimeError):
    """A browser step failed. Surfaced as a failed session, never as a fake success."""


class BrowserDriver(Protocol):
    """The browser surface a session pass needs. Implements no policy itself."""

    async def open(self, session: ApplicationSession) -> None: ...

    async def observe(self) -> Dict[str, Any]: ...

    async def fill(self, instructions: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]: ...

    async def submit(self) -> Dict[str, Any]: ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# Recording driver (tests, dry-run rehearsal)
# --------------------------------------------------------------------------- #
@dataclass
class RecordingDriver:
    """
    A driver that replays a script and records the fills it was asked to make.

    ``observations`` is a list of page observations (the same shape
    ``PlaywrightDriver.observe`` returns); each ``observe()`` pops the next one.
    ``existing_values`` marks fields the page already holds content in, so the
    no-overwrite rule is exercised without a browser.
    """

    observations: List[Dict[str, Any]] = dataclass_field(default_factory=list)
    existing_values: Dict[str, Any] = dataclass_field(default_factory=dict)
    fills: List[Dict[str, Any]] = dataclass_field(default_factory=list)
    opened: bool = False
    closed: bool = False
    submitted: bool = False
    fail_on_fill: bool = False

    async def open(self, session: ApplicationSession) -> None:
        self.opened = True

    async def observe(self) -> Dict[str, Any]:
        if self.observations:
            return dict(self.observations.pop(0))
        return {"url": "", "fields": [], "markers": []}

    async def fill(self, instructions: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        if self.fail_on_fill:
            raise DriverError("recorded fill failure")
        recorded: List[Dict[str, Any]] = []
        for instruction in instructions:
            name = str(instruction.get("name") or "")
            entry = dict(instruction)
            self.fills.append(entry)
            recorded.append({"name": name, "status": "filled", "value_fingerprint":
                             sessions.fingerprint_value(instruction.get("value"))})
        return recorded

    async def submit(self) -> Dict[str, Any]:
        self.submitted = True
        return {"submitted": True, "receipt": {"kind": "recording_driver"}}

    async def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# Playwright driver (real; optional dependency)
# --------------------------------------------------------------------------- #
class PlaywrightDriver:
    """
    A real browser, isolated per session.

    * Its own context (never a shared one) and, when persistence is enabled, the
      user's encrypted ``storage_state``.
    * Navigation goes through the outbound URL policy and the session's expected
      host; a redirect to another host stops the run before anything is typed.
    * ``observe()`` runs the structural script above — no page text is returned.
    * ``fill()`` refuses any instruction that is not ``autofill``-classified, and
      refuses to overwrite a field whose value is already present.
    """

    def __init__(self, session: ApplicationSession, *, target_url: str = "",
                 storage_state: Optional[Mapping[str, Any]] = None,
                 allow_submit: bool = False) -> None:
        self.session = session
        #: Where to navigate. Comes from the job the session is bound to, so the
        #: session's own binding (not a caller) decides the destination.
        self.target_url = target_url
        self.storage_state = dict(storage_state) if storage_state else None
        self.allow_submit = allow_submit
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    async def open(self, session: ApplicationSession) -> None:
        availability = autofill_available()
        if not availability["available"]:
            raise DriverError(f"browser automation unavailable: {availability['reason']}")
        target = self.target_url or str((session.last_observation or {}).get("url") or "")
        verdict = await net_guard.preflight_navigation(target)
        if not verdict.allowed:
            raise DriverError(f"navigation refused by the outbound policy: {verdict.reason}")
        if session.expected_host and not net_guard.host_matches(verdict.host, session.expected_host) \
                and not any(net_guard.host_matches(verdict.host, ats) for ats in VAULT_DOMAINS.values()):
            raise DriverError(f"{verdict.host} is not this application's host ({session.expected_host})")

        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except Exception as exc:  # pragma: no cover - optional dependency
            raise DriverError(f"playwright import failed: {exc}") from exc

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=settings.autofill_headless)
        self._context = await self._browser.new_context(
            user_agent=settings.http_user_agent,
            storage_state=self.storage_state or None,
        )
        self._page = await self._context.new_page()
        self._page.set_default_timeout(settings.autofill_timeout_ms)
        await self._page.goto(target, wait_until="domcontentloaded", timeout=settings.autofill_timeout_ms)

    async def observe(self) -> Dict[str, Any]:
        if self._page is None:
            raise DriverError("browser not open")
        raw = await self._page.evaluate(OBSERVE_SCRIPT)
        return sessions.sanitize_observation(raw)

    async def fill(self, instructions: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        if self._page is None:
            raise DriverError("browser not open")
        recorded: List[Dict[str, Any]] = []
        current = await self.observe()
        form = classify_form(current.get("fields") or [])
        allowed = {v.name: v for v in form.autofillable}
        for instruction in instructions:
            name = str(instruction.get("name") or "")
            verdict = allowed.get(name)
            if verdict is None:
                # Defence in depth: the plan is not trusted to decide safety.
                log.warning("assisted fill refused '%s': not classified as autofillable", name)
                continue
            selector = f'[name="{name}"]'
            locator = self._page.locator(selector).first
            if await locator.count() == 0:
                recorded.append({"name": name, "status": "no_selector"})
                continue
            try:
                await locator.fill(str(instruction.get("value") or ""), timeout=5000)
            except Exception as exc:
                recorded.append({"name": name, "status": f"error:{type(exc).__name__}"})
                continue
            # Field name only — a value can be personal data, and for a password
            # field it would be the credential. The driver never logs values.
            log.debug("assisted fill: '%s' filled via %s", name, selector)
            recorded.append({"name": name, "status": "filled"})
        return recorded

    async def submit(self) -> Dict[str, Any]:
        if not self.allow_submit:
            return {"submitted": False, "reason": "policy_disallows_submit"}
        if self._page is None:
            raise DriverError("browser not open")
        for selector in ("button[type=submit]", "input[type=submit]"):
            try:
                await self._page.click(selector, timeout=5000)
                await self._page.wait_for_load_state("networkidle", timeout=20000)
                return {"submitted": True, "receipt": {"kind": "portal_ack", "selector": selector}}
            except Exception:
                continue
        return {"submitted": False, "reason": "no_submit_control"}

    async def export_storage_state(self) -> Optional[Dict[str, Any]]:
        """The context's own cookie jar, for the (opt-in) encrypted persistence."""
        if self._context is None:
            return None
        try:
            return await self._context.storage_state()
        except Exception:
            return None

    async def close(self) -> None:
        for closer in (getattr(self._context, "close", None), getattr(self._browser, "close", None)):
            try:
                if closer is not None:
                    await closer()
            except Exception:  # pragma: no cover - teardown is best effort
                pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:  # pragma: no cover
            pass
        self._page = self._context = self._browser = self._playwright = None


def build_instruction_values(job: Job, session: ApplicationSession, *,
                             answers: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Values the session may fill: the plan's non-credential values + user answers."""
    return sessions.planned_values(job, answers=answers or {})


# --------------------------------------------------------------------------- #
# The pass loop
# --------------------------------------------------------------------------- #
#: The queue pipeline an assisted pass runs under. A *pause* is a normal,
#: successful outcome for this pipeline: the work stops, the human acts, and a
#: later pass (a new queue item) continues from the checkpoint.
BROWSER_SESSION_PIPELINE = "browser_session"


def build_driver(
    db: Session,
    session: ApplicationSession,
    job: Job,
    *,
    user: User,
    policy: Optional[sessions.SessionPolicy] = None,
) -> Optional[BrowserDriver]:
    """
    The real driver for this session, or ``None`` when automation is unavailable.

    Constructed here — never by a request handler — so the destination, the
    stored state and the submit flag all come from the session's own binding.
    """
    availability = autofill_available()
    if not availability["available"]:
        return None
    policy = policy or sessions.effective_policy(db, user.id)
    storage_state = sessions.load_storage_state(db, session, user=user, policy=policy)
    return PlaywrightDriver(
        session,
        target_url=job.url or "",
        storage_state=storage_state,
        allow_submit=policy.allow_submit,
    )


def enqueue_pass(
    db: Session,
    *,
    user: User,
    session: ApplicationSession,
    trigger: str = "user",
    delay_seconds: int = 0,
) -> Dict[str, Any]:
    """
    Queue one assisted pass for this session.

    Deduped per ``(session, resumed_count)``: a second request while a pass is
    in flight is a no-op, but a resume — which increments ``resumed_count`` —
    legitimately queues a *new* pass that continues from the checkpoint.
    """
    from app.services.job_queue import enqueue_or_existing

    item, duplicate = enqueue_or_existing(
        db,
        user_id=session.user_id,
        pipeline=BROWSER_SESSION_PIPELINE,
        job_id=session.job_id,
        payload={"session_id": session.id, "trigger": trigger},
        dedupe_key=f"browsersession:{session.id}:{session.resumed_count}",
        priority=6,
        delay_seconds=delay_seconds,
    )
    inc("jobhunter_browser_session_passes_enqueued_total", duplicate=str(duplicate).lower())
    return {"queued": item is not None, "duplicate": duplicate,
            "item_id": item.id if item else None, "session_id": session.id}


def report_queue_progress(db: Session, item: Any, step: str, **extra: Any) -> None:
    """
    Publish a ``QUEUE_PROGRESS_STEPS["browser_session"]`` step onto the item.

    The UI renders a stepper from ``payload.progress.step``; the step names are
    the contract, so a handler never invents one. Nothing but a step name, a
    timestamp and counters ever lands here — never a page's content.
    """
    payload = dict(item.payload or {})
    payload["progress"] = {"step": step, "at": datetime.utcnow().isoformat(), **extra}
    item.payload = payload
    db.commit()


async def run_queued_pass(
    db: Session,
    item: Any,
    *,
    driver: Optional[BrowserDriver] = None,
) -> Dict[str, Any]:
    """
    Pipeline handler body for :data:`BROWSER_SESSION_PIPELINE`.

    Refuses to open a browser while the session is terminal, expired or still
    waiting on the user: the pass is a no-op that reports *why*, never a retry
    that could type over a page the human is working in.
    """
    user = db.query(User).filter(User.id == item.user_id).first()
    session_id = int((item.payload or {}).get("session_id") or 0)
    session = (db.query(ApplicationSession)
               .filter(ApplicationSession.id == session_id,
                       ApplicationSession.user_id == item.user_id)
               .first())
    if user is None or session is None:
        return {"status": "not_found", "session_id": session_id}

    if session.state in sessions.APPLICATION_SESSION_TERMINAL_STATES:
        return {"status": session.state, "session_id": session.id, "noop": "terminal"}
    if sessions.is_expired(session):
        sessions.expire_session(db, session, reason="pass_after_expiry")
        return {"status": session.state, "session_id": session.id,
                "requires_reauthentication": True, "noop": "expired"}
    open_actions = sessions.pending_actions(db, session.user_id, session_id=session.id)
    if open_actions:
        return {
            "status": session.state,
            "session_id": session.id,
            "noop": "awaiting_user",
            "pause": session.pause_kind or (open_actions[0].kind if open_actions else ""),
            "action_ids": [action.id for action in open_actions],
        }

    job = db.query(Job).filter(Job.id == session.job_id, Job.user_id == user.id).first()
    if job is None:
        return {"status": "not_found", "session_id": session.id, "noop": "job_missing"}

    policy = sessions.effective_policy(db, user.id)
    active_driver = driver or build_driver(db, session, job, user=user, policy=policy)
    if active_driver is None:
        report_queue_progress(db, item, "done", detail="autofill_unavailable")
        return {"status": "unavailable", "session_id": session.id,
                "noop": "autofill_unavailable",
                "reason": autofill_available().get("reason") or "autofill_disabled"}
    report_queue_progress(db, item, "opening", session_state=session.state)
    try:
        result = await run_pass(db, user=user, session=session, driver=active_driver, policy=policy)
    except DriverError as exc:
        report_queue_progress(db, item, "done", detail=f"driver_error:{type(exc).__name__}")
        raise
    step = "awaiting_user" if result.get("pause") or session.state in (
        "awaiting_user", "paused") else "done"
    report_queue_progress(db, item, step, pause=result.get("pause"),
                          filled=len(result.get("filled") or []))
    return result


async def run_pass(
    db: Session,
    *,
    user: User,
    session: ApplicationSession,
    driver: BrowserDriver,
    policy: Optional[sessions.SessionPolicy] = None,
    open_driver: bool = True,
    max_steps: int = MAX_PASS_STEPS,
) -> Dict[str, Any]:
    """
    One assisted pass: open → observe → (fill → observe)* → stop at a human step.

    The pass *always* stops at the first human-required step: a pause is the
    product, not a failure. Re-running it after the user acts resumes from the
    checkpoint, so nothing already typed is typed again.
    """
    policy = policy or sessions.effective_policy(db, user.id)
    if sessions.is_expired(session):
        sessions.expire_session(db, session, reason="pass_after_expiry")
        raise sessions.SessionExpired(session)

    job = db.query(Job).filter(Job.id == session.job_id).first()
    if job is None:
        raise sessions.SessionError("job_missing", "the job this session belongs to no longer exists",
                                    status_code=404)

    values = build_instruction_values(job, session, answers=sessions.session_values(session))
    result: Dict[str, Any] = {"session_id": session.id, "steps": [], "filled": [], "pause": None,
                              "status": session.state}
    try:
        if open_driver:
            await driver.open(session)
            if session.state in ("created", "launching"):
                sessions.transition(session, "preparing", actor="system_worker", reason="browser_opened")
                db.commit()
        for step in range(max_steps):
            observation = await driver.observe()
            outcome = sessions.record_observation(db, session, observation, user=user, values=values,
                                                 actor="system_worker")
            result["steps"].append({"step": step, "status": outcome["status"],
                                    "progress": outcome.get("progress")})
            if outcome.get("to_fill") is None or not outcome.get("to_fill"):
                if outcome.get("pause"):
                    result["pause"] = outcome["pause"]
                result["status"] = session.state
                break
            instructions = sessions.fill_instructions(session, observations=[observation], values=values)
            if not instructions:
                result["status"] = session.state
                break
            recorded = await driver.fill(instructions)
            filled = [row for row in recorded if row.get("status") == "filled"]
            sessions.record_fills(db, session, [
                {"name": row["name"],
                 "value": next((i.get("value") for i in instructions if i["name"] == row["name"]), ""),
                 "classification": next((i.get("classification") for i in instructions
                                         if i["name"] == row["name"]), "canonical"),
                 "profile_key": next((i.get("profile_key") for i in instructions
                                      if i["name"] == row["name"]), None)}
                for row in filled
            ])
            result["filled"].extend(row["name"] for row in filled)
            values = build_instruction_values(job, session, answers=sessions.session_values(session))
            if outcome.get("pause"):
                # The page asked for a human. Fill what was safe on it, stop
                # here: no further observation, no navigation, no submit.
                result["pause"] = outcome["pause"]
                result["status"] = session.state
                break
            if policy.persist_state and isinstance(driver, PlaywrightDriver):
                state = await driver.export_storage_state()
                if state:
                    sessions.persist_storage_state(db, session, state, policy=policy)
    finally:
        if open_driver:
            await driver.close()

    result["status"] = session.state
    result["completed_fields"] = sessions.already_completed_fields(session)
    result["progress"] = session.progress or {}
    if session.state == "active" and not result["pause"]:
        # Everything fillable is filled; the session finishes *without* submitting
        # unless the policy and the ledger both say yes.
        sessions.transition(session, "completed", actor="system_worker", reason="fields_filled")
        db.commit()
        result["status"] = session.state
    inc("jobhunter_application_session_passes_total", result=result["status"])
    return result


def run_pass_sync(db: Session, *, user: User, session: ApplicationSession, driver: BrowserDriver,
                  **kwargs: Any) -> Dict[str, Any]:
    """Synchronous entry point for queue handlers and scripts."""
    return asyncio.run(run_pass(db, user=user, session=session, driver=driver, **kwargs))


def plan_for_session(job: Job, session: ApplicationSession, *,
                     answers: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """
    A reviewable description of what this session would do — no browser needed.

    Used by the API's dry-run surface and by tests: it answers "which fields
    would be typed, which pause, and why" without opening anything. Values come
    from the session's own confirmed working set unless the caller supplies
    them.
    """
    values = build_instruction_values(
        job, session,
        answers=sessions.session_values(session) if answers is None else answers)
    observation = session.last_observation or {}
    form = classify_form(observation.get("fields") or [], values=values,
                         checkpoint=session.checkpoint or {})
    return {
        "session_id": session.id,
        "state": session.state,
        "pause_kind": form.pause_kind,
        "must_pause": form.must_pause,
        "autofillable": [v.name for v in form.autofillable],
        "asking": [v.as_dict() for v in form.asking],
        "handoffs": [v.as_dict() for v in form.handoffs],
        "skipped": [v.name for v in form.skipping],
        "already_completed": sessions.already_completed_fields(session),
        "progress": form.summary(),
    }


__all__ = [
    "BROWSER_SESSION_PIPELINE",
    "BrowserDriver",
    "DriverError",
    "MAX_PASS_STEPS",
    "OBSERVE_SCRIPT",
    "PlaywrightDriver",
    "RecordingDriver",
    "build_driver",
    "build_instruction_values",
    "enqueue_pass",
    "plan_for_session",
    "report_queue_progress",
    "run_queued_pass",
    "run_pass",
    "run_pass_sync",
]
