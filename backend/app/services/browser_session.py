"""
Browser-assisted application sessions — lifecycle, isolation, pauses, checkpoints.

This is the *policy and state* half of browser assistance; the browser itself
lives in :mod:`app.services.assisted_fill`. Everything here is deterministic and
testable without a browser, because the interesting questions are not "can we
click the button" but:

* **Is this session still the one we think it is?** — checkpoint validation
  re-checks the job, the URL, the employer and the application identity before a
  paused run is resumed, so a resume can never continue against a different
  posting, a different tenant, or a redirected host.
* **Did we already do this?** — the per-field checkpoint records what was
  filled (a fingerprint, never the value), and the fill planner skips it. A
  resumed form is never typed twice.
* **Has it been sent already?** — ``application_submissions`` is an at-most-once
  ledger; a retry, a replayed request or a resumed session cannot double-send.
* **Is the human being asked, visibly?** — every pause writes an
  ``application_actions`` queue item. A pause nobody can see is a stall.

What this module deliberately cannot do
---------------------------------------
No function here solves a CAPTCHA, intercepts an MFA challenge, or reads page
content. ``mfa``/``captcha`` are *handoffs* — the user does the step themselves
in the browser, and the process only ever learns "it is done" (plus a
structured, privacy-minimized observation). ``login`` is a handoff too by
default; it stops being one only when the user's own ``create_accounts``
opt-in is on *and* the caller holds a vault credential for the page — this
module then plans the fill, and the driver in :mod:`app.services.assisted_fill`
types it. Nothing here ever types anything. Session persistence stores the
browser's own cookie jar — opt-in, encrypted with the per-user key, cleared on
expiry/cancel/erasure — and never a credential.

Isolation
---------
One session belongs to exactly one ``(user, job)``. Every service entry point
takes the authenticated ``user`` and treats another user's session as *not
found*; the on-disk profile lives under a per-user directory whose resolved path
is checked against the profile root, so a value in the database can never walk
the automation out of its own sandbox.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.contracts import (
    APPLICATION_SESSION_PHASE,
    APPLICATION_SESSION_STATES,
    APPLICATION_SESSION_TERMINAL_STATES,
    USER_ACTION_KINDS,
)
from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc
from app.core.security import decrypt_secret, encrypt_secret, sha256_hex
from app.models.models import (
    ApplicationAction,
    ApplicationSession,
    ApplicationSubmission,
    Job,
    User,
)
from app.services import live_browser, net_guard
from app.services.company_normalize import normalize_company_name
from app.services.events import record_job_event
from app.services.field_classifier import (
    FieldVerdict,
    FormVerdicts,
    classify_form,
    is_captcha_marker,
)
from app.services.form_detector import VAULT_DOMAINS, hosts_in_same_ats_family
from app.services.reliability import (
    note_dedupe,
    note_user_action_outcome,
    note_user_action_pause,
)
from app.services.user_settings import get_setting, set_setting

log = get_logger("app.browser_session")

#: Per-field checkpoint statuses.
#:
#: * ``filled`` — we typed it into the page; never typed again.
#: * ``user_completed`` — the user did this step *in the browser* (typed it in a
#:   handoff window); never typed again, and never asked twice.
#: * ``answered`` — the user gave us the value in the app; it still has to be
#:   typed into the page, but it must never be *asked* again.
#: * ``declined`` — the user said "do not answer this"; left alone.
CHECKPOINT_STATUSES: Tuple[str, ...] = (
    "pending", "filled", "answered", "user_completed", "declined", "skipped", "failed",
)

#: Statuses that mean "do not type this again".
CHECKPOINT_DONE_STATUSES: Tuple[str, ...] = ("filled", "user_completed", "declined")

#: Keys allowed inside ``checkpoint["fields"][name]``. A value is *never* one of
#: them: only a fingerprint, so the checkpoint is safe to log, export and render.
FIELD_CHECKPOINT_KEYS: Tuple[str, ...] = (
    "status", "classification", "profile_key", "filled_at", "answered_at",
    "value_fingerprint", "attempts", "source",
)

#: Live (non-terminal) states.
LIVE_SESSION_STATES: Tuple[str, ...] = tuple(
    state for state in APPLICATION_SESSION_STATES if state not in APPLICATION_SESSION_TERMINAL_STATES
)

#: The state machine. A transition not listed here is refused — a session that
#: can jump from ``expired`` back to ``active`` would be an expiry that means
#: nothing.
ALLOWED_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    # A session may pause straight from ``created``: a page that asks for a
    # login (or shows a bot check) before any field exists is a legitimate first
    # observation, and refusing that edge would make it a crash instead of a pause.
    "created": ("launching", "preparing", "awaiting_user", "paused", "cancelled", "failed", "expired"),
    "launching": ("preparing", "active", "awaiting_user", "failed", "cancelled", "expired"),
    "preparing": ("active", "awaiting_user", "paused", "failed", "cancelled", "expired"),
    "active": ("awaiting_user", "paused", "resuming", "completed", "failed", "cancelled", "expired"),
    "awaiting_user": ("resuming", "paused", "completed", "failed", "cancelled", "expired"),
    "paused": ("resuming", "awaiting_user", "completed", "cancelled", "expired", "failed"),
    "resuming": ("active", "awaiting_user", "paused", "completed", "failed", "cancelled", "expired"),
    "completed": (),
    "expired": ("launching", "preparing"),  # re-authentication starts a new pass on the same row
    "failed": ("launching", "preparing"),
    "cancelled": (),
}

#: Actions that are always a browser handoff (the user does it themselves).
HANDOFF_ACTION_KINDS: Tuple[str, ...] = ("login", "mfa", "captcha")

#: Actions whose resolution happens *in the browser window* rather than by typing
#: an answer into the app. ``review_required`` joins the handoff kinds here: a
#: pass that ran out of safe steps hands the flow back to the user, who continues
#: it in the very window the assistant opened.
BROWSER_STEP_KINDS: Tuple[str, ...] = HANDOFF_ACTION_KINDS + ("review_required",)

#: The flow journal: what actually happened during a pass, in order. This is what
#: makes a run reviewable afterwards ("it clicked 'Apply for this job', filled 4
#: fields on step 2, then stopped at a sign-in wall") instead of a black box that
#: reports a status. Entries carry *structure* — hosts, field names, control
#: labels, counters — never a value, a code or page text.
FLOW_STEP_KEYS: Tuple[str, ...] = (
    "event", "at", "step", "host", "url", "title", "fields", "filled", "control",
    "reason", "kind", "fields_total", "filled_total", "pages", "mode",
)
FLOW_STEP_EVENTS: Tuple[str, ...] = (
    "page",        # a page was observed (host/title/field counts)
    "fill",        # fields were typed into the page
    "advance",     # a navigation control was clicked to move the flow on
    "pause",       # the run stopped and asked the human for something
    "blocked",     # the run could not proceed and hands the flow over
    "submit",      # the portal's own submission receipt
    "confirmed",   # the portal said the application arrived
    "resumed",     # the human unblocked the session; the next pass continues
)
#: How many journal entries the session payload renders (the row keeps more).
FLOW_PAYLOAD_LIMIT = 40

#: Screens are never captured while a human is typing a secret into them.
NEVER_SCREENSHOT_KINDS: Tuple[str, ...] = ("login", "mfa", "captcha")

#: Observation keys we keep. Everything else on an incoming observation —
#: ``html``, ``text``, ``content``, a field ``value`` — is dropped: the session
#: records *structure*, not page content.
OBSERVATION_KEYS: Tuple[str, ...] = (
    "url", "host", "title", "employer", "application_identity", "markers",
    "challenge", "fields", "submit_present", "logged_in", "confirmation",
    "step_index", "steps_total",
)

#: Confirmation tokens a page may report (never free text). ``submitted`` and
#: ``received`` are the portal telling us the application arrived — the only
#: evidence that lets a pass close a session as *done* rather than *handed over*.
OBSERVATION_CONFIRMATIONS: Tuple[str, ...] = (
    "submitted", "received", "thank_you", "under_review",
)
OBSERVATION_FIELD_KEYS: Tuple[str, ...] = (
    "name", "id", "label", "type", "required", "options", "value_present",
    "filled_by_user", "captcha", "classes", "src", "autocomplete", "aria_required",
    "placeholder", "aria_label", "role", "data_sitekey",
    # Set server-side by the live-pass AI mapper (never sent from the browser):
    "ai_mapped_key",
    # Classifier/record helpers set server-side too — keep them on the field so
    # subsequent classify_form passes see them:
    "profile_key", "signature", "legal", "sensitive", "eeo",
)

MAX_OBSERVED_FIELDS = 120
MAX_SESSION_EVENTS = 400
DEFAULT_TTL_MINUTES = 45
DEFAULT_HANDOFF_TTL_MINUTES = 20
MAX_HANDOFF_TTL_MINUTES = 120
MAX_SCREENSHOT_RETENTION_DAYS = 30


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class SessionError(RuntimeError):
    """A refused session operation. ``code`` is machine-readable, never prose."""

    def __init__(self, code: str, message: str = "", *, status_code: int = 409,
                 detail: Optional[Dict[str, Any]] = None) -> None:
        self.code = code
        self.status_code = status_code
        self.detail = detail or {}
        super().__init__(message or code)

    def payload(self) -> Dict[str, Any]:
        return {"code": self.code, "message": str(self), **self.detail}


class SessionNotFound(SessionError):
    """Another tenant's session, or a deleted one — both are ``404``."""

    def __init__(self, session_id: Any = None) -> None:
        super().__init__("not_found", f"application session {session_id} not found", status_code=404,
                         detail={"session_id": session_id})


class SessionExpired(SessionError):
    def __init__(self, session: ApplicationSession, reason: str = "ttl_elapsed") -> None:
        super().__init__("session_expired",
                         "This browser session expired — re-authentication is required",
                         status_code=409,
                         detail={"session_id": session.id, "reason": reason,
                                 "requires_reauthentication": True})


class CheckpointFailed(SessionError):
    def __init__(self, failures: Sequence[Dict[str, str]]) -> None:
        codes = [f["code"] for f in failures]
        super().__init__("checkpoint_failed",
                         "The session no longer matches this application — resume refused",
                         status_code=409,
                         detail={"failures": list(failures), "codes": codes})


class SubmissionRefused(SessionError):
    def __init__(self, reason: str, **extra: Any) -> None:
        super().__init__("application_not_submittable",
                         f"Submission refused ({reason})", status_code=409,
                         detail={"reason": reason, **extra})


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SessionPolicy:
    """Effective, per-user policy for one assisted session."""

    ttl_minutes: int = DEFAULT_TTL_MINUTES
    max_live_sessions: int = 2
    persist_state: bool = False
    handoff_ttl_minutes: int = DEFAULT_HANDOFF_TTL_MINUTES
    screenshots_enabled: bool = False
    screenshot_retention_days: int = 7
    pause_on_optional_unknown: bool = True
    allow_submit: bool = False
    dry_run: bool = True
    #: The user explicitly opted in to portal account creation: a pass may
    #: generate/reuse a vault credential and type the password into sign-up
    #: fields. Default False — a password is typed only when this says so.
    create_accounts: bool = False

    #: The user opted in to AI assistance during live passes: when a field is
    #: unrecognised the pass may consult the ``form_detect`` AI to map it to a
    #: canonical profile key or suggest a value, before pausing for the user.
    #: An AI outage is surfaced as a pause (``ai_unavailable``) rather than
    #: silently downgrading to a guess — exactly the contract Settings states.
    ai_assist: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ttl_minutes": self.ttl_minutes,
            "max_live_sessions": self.max_live_sessions,
            "persist_state": self.persist_state,
            "handoff_ttl_minutes": self.handoff_ttl_minutes,
            "screenshots_enabled": self.screenshots_enabled,
            "screenshot_retention_days": self.screenshot_retention_days,
            "pause_on_optional_unknown": self.pause_on_optional_unknown,
            "allow_submit": self.allow_submit,
            "dry_run": self.dry_run,
            "create_accounts": self.create_accounts,
            "ai_assist": self.ai_assist,
        }


def _int_setting(value: Any, default: int, *, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def effective_policy(db: Session, user_id: int) -> SessionPolicy:
    """
    Resolve the policy for this user: server ceiling ∧ per-user settings.

    The server flags are a ceiling, never a suggestion: a user cannot turn
    auto-submit on for a deployment that has it off, and the screenshot
    retention a user asks for is clamped to what the deployment permits.
    """
    ttl = _int_setting(get_setting(db, user_id, "browser", "session_ttl_minutes",
                                   settings.browser_session_ttl_minutes),
                       DEFAULT_TTL_MINUTES, low=5, high=24 * 60)
    max_live = _int_setting(get_setting(db, user_id, "browser", "max_live_sessions",
                                        settings.browser_session_max_live),
                            settings.browser_session_max_live, low=1, high=10)
    handoff = _int_setting(get_setting(db, user_id, "browser", "handoff_ttl_minutes",
                                       settings.browser_handoff_ttl_minutes),
                           DEFAULT_HANDOFF_TTL_MINUTES, low=1, high=MAX_HANDOFF_TTL_MINUTES)
    persist = bool(get_setting(db, user_id, "browser", "persist_session",
                               settings.browser_session_persist))
    screenshots_requested = bool(get_setting(db, user_id, "browser", "screenshots_enabled", False))
    retention = _int_setting(get_setting(db, user_id, "browser", "screenshot_retention_days", 0),
                             0, low=0, high=MAX_SCREENSHOT_RETENTION_DAYS)
    ceiling = max(0, min(MAX_SCREENSHOT_RETENTION_DAYS, settings.browser_screenshot_max_retention_days))
    allow_submit = bool(
        settings.autofill_enabled
        and not settings.autofill_dry_run
        and settings.autofill_allow_submit
        and get_setting(db, user_id, "application", "allow_auto_submit", False)
    )
    # Account creation is doubly gated: the per-user opt-in (default OFF) and
    # the deployment ceiling. Either being off means passwords are refused
    # exactly as before — the pause, never a silent type. The read is
    # deliberately ``is True`` (fail-closed): only a literal boolean opt-in
    # counts, never a stray truthy string.
    create_accounts = bool(
        settings.browser_create_accounts_enabled
        and get_setting(db, user_id, "browser", "create_accounts", False) is True
    )
    ai_assist = bool(
        settings.browser_ai_assist_enabled
        and get_setting(db, user_id, "browser", "ai_assist", False) is True
    )
    return SessionPolicy(
        ttl_minutes=ttl,
        max_live_sessions=max_live,
        persist_state=persist,
        handoff_ttl_minutes=handoff,
        screenshots_enabled=bool(screenshots_requested and settings.browser_screenshots_enabled
                                 and ceiling > 0),
        screenshot_retention_days=min(retention, ceiling),
        pause_on_optional_unknown=bool(get_setting(db, user_id, "browser", "pause_on_optional_unknown", True)),
        allow_submit=allow_submit,
        dry_run=not allow_submit,
        create_accounts=create_accounts,
        ai_assist=ai_assist,
    )


# --------------------------------------------------------------------------- #
# Identity fingerprints (safe to store, compare and log)
# --------------------------------------------------------------------------- #
def canonical_url(url: str) -> str:
    """Scheme + host + path, query removed, fragments dropped, lower-cased host."""
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw if "//" in raw else f"//{raw}")
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower().rstrip(".")
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    path = re.sub(r"/+", "/", parts.path or "/").rstrip("/") or "/"
    return urlunsplit((scheme, f"{host}{port}", path, "", ""))


def url_fingerprint(url: str) -> str:
    return sha256_hex(canonical_url(url))


def employer_fingerprint(company: str) -> str:
    """Digest of the normalised employer name ('' when there is nothing to hash)."""
    normalised = normalize_company_name(company or "")
    return sha256_hex(normalised) if normalised else ""


def application_identity(job: Job) -> str:
    """
    The posting/application identity this session is bound to.

    The portal's own requisition id when the source gave us one, otherwise the
    canonical URL digest — either way it is *not* the URL we were told to visit,
    so a redirect to a different posting cannot satisfy it.
    """
    external = (job.external_id or "").strip()
    if external:
        return f"ext:{sha256_hex(external)[:32]}"
    return f"url:{url_fingerprint(job.url)[:32]}"


def host_of(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    return (urlsplit(raw if "//" in raw else f"//{raw}").hostname or "").lower().rstrip(".")


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #
def isolation_key(user_id: int) -> str:
    """A per-user opaque slot id — never a shared name, never a raw user id."""
    return f"u{user_id}-{secrets.token_hex(6)}"


def profile_ref(user_id: int, key: str) -> str:
    """The on-disk profile reference: a *relative* name, never an absolute path."""
    return f"u{user_id}/{key}"


def profile_dir(session: ApplicationSession) -> str:
    """
    Absolute profile directory for a session, or raise if it escapes the root.

    The database value is a name, not a path: joining it here (and checking the
    result stays under the configured root) is what stops a tampered row from
    pointing a browser profile at ``/etc`` or another tenant's directory.
    """
    root = settings.browser_profile_path()
    ref = (session.browser_profile_ref or "").strip()
    if not ref:
        raise SessionError("isolation_missing", "session has no isolated browser profile reference",
                           status_code=500)
    target = os.path.abspath(os.path.join(root, ref))
    if target != root and not target.startswith(root + os.sep):
        raise SessionError("isolation_violation", "session profile path escapes the profile root",
                           status_code=500, detail={"session_id": session.id})
    return target


def own_session(db: Session, user: User, session_id: int) -> ApplicationSession:
    """Fetch a session that belongs to *user* — another tenant's is ``404``."""
    session = (
        db.query(ApplicationSession)
        .filter(ApplicationSession.id == session_id, ApplicationSession.user_id == user.id)
        .first()
    )
    if session is None:
        raise SessionNotFound(session_id)
    return session


def _assert_owner(session: ApplicationSession, user: User) -> None:
    if int(session.user_id) != int(user.id):
        # Cross-tenant access is reported as "not found" on purpose: an id must
        # never be usable to probe whether another user's session exists.
        raise SessionNotFound(session.id)


# --------------------------------------------------------------------------- #
# Observation sanitisation (safe structured progress, not page content)
# --------------------------------------------------------------------------- #
def sanitize_observation(observation: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Reduce a raw page observation to the structure this service is allowed to keep.

    Anything not on :data:`OBSERVATION_KEYS` is dropped — including any page
    text, HTML or field value the driver may have collected. Field values are
    reduced to booleans (``value_present`` / ``filled_by_user``), because the one
    thing a checkpoint must not contain is what the user typed.
    """
    clean: Dict[str, Any] = {}
    if not isinstance(observation, Mapping):
        return clean
    for key in OBSERVATION_KEYS:
        if key in observation and observation.get(key) is not None:
            clean[key] = observation[key]
    fields: List[Dict[str, Any]] = []
    for raw in list(clean.get("fields") or [])[:MAX_OBSERVED_FIELDS]:
        if not isinstance(raw, Mapping):
            continue
        entry: Dict[str, Any] = {}
        for key in OBSERVATION_FIELD_KEYS:
            if key in raw and raw.get(key) is not None:
                value = raw[key]
                if isinstance(value, str):
                    value = value[:200]
                elif isinstance(value, (list, tuple)):
                    value = [str(v)[:120] for v in list(value)[:24]]
                entry[key] = value
        # Never carry a value, even if a driver sent one.
        entry.pop("value", None)
        entry["value_present"] = bool(raw.get("value_present")) or raw.get("value") not in (None, "", [], {})
        entry["filled_by_user"] = bool(raw.get("filled_by_user")) and not bool(raw.get("value_present") and False)
        if entry.get("name") or entry.get("id") or entry.get("label"):
            fields.append(entry)
    if "fields" in clean:
        clean["fields"] = fields
    if "markers" in clean:
        markers = clean["markers"]
        if isinstance(markers, str):
            markers = [markers]
        clean["markers"] = [str(m).lower()[:40] for m in list(markers or [])[:20]]
    for key in ("url", "host", "title", "employer", "application_identity", "challenge"):
        if key in clean and isinstance(clean[key], str):
            clean[key] = clean[key][:300]
    if "url" in clean:
        clean["url"] = canonical_url(str(clean["url"]))
        clean.setdefault("host", host_of(str(clean["url"])))
    confirmation = str(clean.get("confirmation") or "").strip().lower()
    clean["confirmation"] = confirmation if confirmation in OBSERVATION_CONFIRMATIONS else ""
    for key in ("step_index", "steps_total"):
        if key in clean:
            try:
                clean[key] = max(0, min(99, int(clean[key])))
            except (TypeError, ValueError):
                clean.pop(key, None)
    return clean


#: Field classifications / names whose values must be stripped from action
#: payloads. A credential, SSN or card number must never survive into an
#: action row — the checkpoint already stores only fingerprints.
_SENSITIVE_ACTION_FIELD_NAMES = frozenset({
    "password", "passwd", "pwd", "pass",
    "ssn", "social_security", "social_security_number",
    "credit_card", "card_number", "cvv", "cvc", "security_code",
    "mfa_code", "otp", "totp", "verification_code", "2fa_code",
    "pin",
})

#: Field shapes a *browser-window recording* may never keep, even though the
#: page-level recorder already filtered them: the server is where the rule is
#: actually enforced, by name, by type and by the classifier's own restricted
#: verdict. Mirrors the API's ``SECRET_FIELD_PATTERN`` (the router refuses the
#: same names over HTTP; this refuses them out of the internal binding).
_RESTRICTED_RECORD_TYPES = frozenset({"password", "hidden", "file"})
_RESTRICTED_RECORD_PATTERN = re.compile(
    r"(pass(word|phrase|code)|passwd|\botp\b|one[-_ ]?time|mfa|2fa|totp|"
    r"verification[-_ ]?code|security[-_ ]?code|captcha|\bpin\b|\bsecret\b|"
    r"\bssn\b|social[-_ ]?security|credit[-_ ]?card|\bcvv\b|\bcvc\b)",
    re.IGNORECASE,
)


def sanitize_action_payload(payload: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Strip credential/PII values from an action payload before persistence.

    The checkpoint already stores only fingerprints (``value_fingerprint``),
    never the value itself. Action payloads are a secondary record of what
    happened — and this function ensures that if a raw value is included
    (a field value from a user answer, a driver observation), it is
    replaced with ``***`` when the field is classified as sensitive.
    """
    if not payload:
        return {}
    result = dict(payload)
    field_name = str(result.get("field") or result.get("name") or "").lower().replace("-", "_")
    classification = str(result.get("classification") or "").lower()
    is_sensitive = (
        field_name in _SENSITIVE_ACTION_FIELD_NAMES
        or classification in ("credential", "password", "sensitive_pii", "mfa", "captcha")
    )
    if is_sensitive and "value" in result:
        result["value"] = "***"
    return result


def detect_challenges(observation: Mapping[str, Any]) -> List[str]:
    """
    Which human-only steps the page is asking for, most urgent first.

    Detection is evidence-based (markers, control types, labels) and produces
    handoff kinds only — there is no code path that attempts a challenge.
    """
    found: List[str] = []
    markers = observation.get("markers") or []
    if isinstance(markers, str):
        markers = [markers]
    marker_text = " ".join(str(m) for m in markers).lower()
    challenge = str(observation.get("challenge") or "").lower()
    combined = f"{marker_text} {challenge}"
    if any(is_captcha_marker(token) for token in re.split(r"[\s,:;]+", combined) if token) \
            or is_captcha_marker(combined):
        found.append("captcha")
    if any(word in combined for word in ("mfa", "otp", "2fa", "one-time", "verification-code", "totp")):
        found.append("mfa")
    if "login" in combined or "signin" in combined or "sign-in" in combined:
        found.append("login")

    for field in observation.get("fields") or []:
        if not isinstance(field, Mapping):
            continue
        verdict = _verdict_for_observation_field(field)
        if verdict.pause_kind in HANDOFF_ACTION_KINDS and verdict.pause_kind not in found:
            # A password/OTP/CAPTCHA control is itself the evidence.
            found.append(verdict.pause_kind)
    order = {"captcha": 0, "mfa": 1, "login": 2}
    return sorted(dict.fromkeys(found), key=lambda kind: order.get(kind, 9))


def _verdict_for_observation_field(field: Mapping[str, Any], *, value: Any = None,
                                   checkpoint_status: str = "") -> FieldVerdict:
    from app.services.field_classifier import classify_field

    return classify_field(field, value=value, checkpoint_status=checkpoint_status)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.utcnow()


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def transition(session: ApplicationSession, state: str, *, actor: str = "system_api",
               reason: str = "") -> ApplicationSession:
    """Move a session to *state*, refusing an illegal edge."""
    if state not in APPLICATION_SESSION_STATES:
        raise SessionError("invalid_state", f"unknown session state: {state}", status_code=400)
    if state == session.state:
        return session
    allowed = ALLOWED_TRANSITIONS.get(session.state, ())
    if state not in allowed:
        raise SessionError(
            "invalid_transition",
            f"a session in '{session.state}' cannot move to '{state}'",
            status_code=409,
            detail={"from": session.state, "to": state, "allowed": list(allowed)},
        )
    previous = session.state
    session.state = state
    session.phase = APPLICATION_SESSION_PHASE.get(state, "working")
    session.actor_type = actor
    if reason:
        session.state_reason = reason[:60]
    session.last_activity_at = _now()
    if state in APPLICATION_SESSION_TERMINAL_STATES:
        session.ended_at = _now()
        # A finished session has no use for the values it could have typed: the
        # working set is dropped here so "completed" and "cancelled" cannot leave
        # a copy of the candidate's data behind.
        session.fill_values = {}
    log.debug("session %s: %s → %s (%s)", session.id, previous, state, reason or actor)
    return session


def live_sessions(db: Session, user_id: int) -> List[ApplicationSession]:
    return (
        db.query(ApplicationSession)
        .filter(ApplicationSession.user_id == user_id,
                ApplicationSession.state.notin_(APPLICATION_SESSION_TERMINAL_STATES))
        .order_by(ApplicationSession.created_at.desc())
        .all()
    )


def live_session_for_job(db: Session, user_id: int, job_id: int) -> Optional[ApplicationSession]:
    """One live session per ``(user, job)`` — a second start returns the first."""
    return (
        db.query(ApplicationSession)
        .filter(ApplicationSession.user_id == user_id, ApplicationSession.job_id == job_id,
                ApplicationSession.state.notin_(APPLICATION_SESSION_TERMINAL_STATES))
        .order_by(ApplicationSession.created_at.desc())
        .first()
    )


def start_session(
    db: Session,
    *,
    user: User,
    job: Job,
    portal_type: str = "",
    portal_domain: str = "",
    resume: bool = False,
) -> Tuple[ApplicationSession, bool]:
    """
    Create (or return) this user's session for this job.

    Returns ``(session, created)``. Refuses when the user already has the
    maximum number of live sessions: a per-user *slot* limit is what keeps one
    account from opening unbounded browser contexts.
    """
    if job.user_id != user.id:
        # The job is another tenant's: same answer as a missing job.
        raise SessionNotFound(job.id)

    sweep_expired_sessions(db, user_id=user.id)
    policy = effective_policy(db, user.id)

    existing = live_session_for_job(db, user.id, job.id)
    if existing is not None:
        return existing, False

    live = live_sessions(db, user.id)
    if len(live) >= policy.max_live_sessions:
        raise SessionError(
            "session_limit_reached",
            f"{len(live)} live browser session(s) already open — finish or cancel one first",
            status_code=429,
            detail={"live": len(live), "max": policy.max_live_sessions,
                    "session_ids": [row.id for row in live]},
        )

    extra = dict(job.extra or {})
    forms = extra.get("forms") or {}
    key = isolation_key(user.id)
    session = ApplicationSession(
        user_id=user.id,
        job_id=job.id,
        state="created",
        phase=APPLICATION_SESSION_PHASE["created"],
        state_reason="requested",
        actor_type="user",
        portal_type=portal_type or str(forms.get("portal_type") or "custom")[:30],
        portal_domain=(portal_domain or str(forms.get("vault_domain") or "") or host_of(job.url))[:200],
        isolation_key=key,
        browser_profile_ref=profile_ref(user.id, key),
        url_fingerprint=url_fingerprint(job.url),
        expected_host=host_of(job.url),
        employer_fingerprint=employer_fingerprint(job.company),
        application_identity=application_identity(job),
        checkpoint={"fields": {}, "steps": []},
        fill_values={},
        progress={"fields_total": 0, "filled": 0, "skipped": 0, "awaiting_user": 0, "paused": 0},
        last_observation={},
        expires_at=_now() + timedelta(minutes=policy.ttl_minutes),
        last_activity_at=_now(),
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    record_job_event(
        db, user_id=user.id, job_id=job.id, stage="session_started", status="info",
        message=f"Browser session #{session.id} started (isolated profile, {policy.ttl_minutes}m TTL)",
        meta={"session_id": session.id, "policy": policy.as_dict(), "host": session.expected_host},
    )
    inc("jobhunter_application_sessions_total", result="started")
    log.info("session %s started for job %s (user %s)", session.id, job.id, user.id)
    return session, True


def touch(db: Session, session: ApplicationSession, *, commit: bool = True) -> ApplicationSession:
    session.last_activity_at = _now()
    if commit:
        db.commit()
    return session


def record_observation(
    db: Session,
    session: ApplicationSession,
    observation: Mapping[str, Any],
    *,
    user: Optional[User] = None,
    values: Optional[Mapping[str, Any]] = None,
    actor: str = "system_api",
    allow_credentials: bool = False,
) -> Dict[str, Any]:
    """
    Fold one sanitized page observation into the session.

    This is where a pause is decided: challenge markers first (login/MFA/CAPTCHA),
    then field classification (legal/sensitive/ambiguous/unknown stop the run).
    Nothing is typed here — the caller asks for the fill instructions afterwards
    (:func:`fill_instructions`), which is what makes "safe autofill" reviewable.

    ``allow_credentials`` is passed only by a pass whose policy opted in *and*
    that holds a vault credential for this page: it lifts the login pause for
    password fields and lets them classify as autofill. MFA and CAPTCHA pauses
    are never lifted, and the credential value still never enters the session
    working set.
    """
    if user is not None:
        _assert_owner(session, user)
    if is_expired(session):
        expire_session(db, session, reason="observation_after_expiry")
        raise SessionExpired(session)

    clean = sanitize_observation(observation)
    session.last_observation = clean
    session.last_activity_at = _now()

    # When the flow redirects inside the same ATS family (jobs.lever.co →
    # auth.lever.co, a Workday posting → company subdomain, a job board that
    # hands off to the company's Greenhouse) extend the expected host rather
    # than pausing. This is the same-family rule the driver advance uses at
    # click-time; without it an unbroken sign-up redirect looks like an
    # off-host navigation.
    observed_host = str(clean.get("host") or "")
    if observed_host and session.expected_host \
            and not net_guard.host_matches(observed_host, session.expected_host) \
            and hosts_in_same_ats_family(observed_host, session.expected_host):
        session.expected_host = observed_host

    checkpoint = dict(session.checkpoint or {})
    checkpoint["fields"] = dict(checkpoint.get("fields") or {})
    merged_values = session_values(session, values)

    form = classify_form(clean.get("fields") or [], values=merged_values, checkpoint=checkpoint,
                         allow_credentials=allow_credentials)
    challenges = detect_challenges(clean)
    if allow_credentials:
        # The sign-in wall this pass can clear itself: it holds a vault
        # credential for exactly this page. Anything the human still owns —
        # a code, a bot check — stays a pause.
        challenges = [kind for kind in challenges if kind != "login"]

    progress = progress_snapshot(form)
    session.progress = progress
    _checkpoint_fields(checkpoint, form)
    # Keep only what a verdict cleared for autofill; a code, a CAPTCHA token or
    # a restricted identifier never reaches this set — and neither does a
    # credential, even when ``allow_credentials`` cleared it: a password lives
    # in memory for the one fill and nowhere else.
    cleared = {v.name: merged_values[v.name] for v in form.autofillable
               if v.name in merged_values and v.classification != "credential"}
    remember_values(session, cleared)

    # An observation only ever moves a session forward through legal edges.
    if challenges:
        kind = challenges[0]
        action = pause_session(
            db, session, kind=kind, reason=f"{kind}_detected",
            fields=form.blocking or form.handoffs,
            instructions=_handoff_instructions(kind, session),
            actor=actor, checkpoint=checkpoint, commit=False,
        )
        db.commit()
        return {
            "session_id": session.id,
            "status": session.state,
            "pause": kind,
            "action_id": action.id,
            "requires_browser_handoff": True,
            "to_fill": safe_fills_for_pause(form),
            "progress": progress,
        }

    if session.state in ("created", "launching"):
        transition(session, "preparing", actor=actor, reason="observation_received")
    if session.state in ("preparing", "paused", "awaiting_user", "resuming"):
        # A user- or system-paused session does not resume by observation alone:
        # the caller must go through resume_session (checkpoint validation).
        if session.state in ("paused", "awaiting_user"):
            session.checkpoint = checkpoint
            db.commit()
            return {
                "session_id": session.id,
                "status": session.state,
                "pause": session.pause_kind or None,
                "action_id": _pending_action_id(db, session),
                "requires_browser_handoff": session.pause_kind in HANDOFF_ACTION_KINDS,
                "to_fill": [],
                "progress": progress,
            }
        transition(session, "active", actor=actor, reason="observation_received")

    if form.must_pause:
        action = pause_session(
            db, session, kind=form.pause_kind, reason=form.pause_reason,
            fields=form.blocking, instructions=_answer_instructions(form),
            actor=actor, checkpoint=checkpoint, commit=False,
        )
        db.commit()
        return {
            "session_id": session.id,
            "status": session.state,
            "pause": form.pause_kind,
            "action_id": action.id,
            "requires_browser_handoff": form.pause_kind in HANDOFF_ACTION_KINDS,
            "to_fill": safe_fills_for_pause(form),
            "progress": progress,
        }

    session.checkpoint = checkpoint
    db.commit()
    record_job_event(
        db, user_id=session.user_id, job_id=session.job_id, stage="session_observed", status="info",
        message=(f"Session #{session.id}: {progress['fields_total']} field(s) observed, "
                 f"{progress['autofillable']} safe to fill"),
        meta={"session_id": session.id, "progress": progress},
    )
    return {
        "session_id": session.id,
        "status": session.state,
        "pause": None,
        "action_id": None,
        "requires_browser_handoff": False,
        "to_fill": [v.name for v in form.autofillable],
        "progress": progress,
    }


def safe_fills_for_pause(form: FormVerdicts) -> List[str]:
    """
    What may still be typed on the page that just paused the session.

    A *handoff* pause (login, MFA, CAPTCHA, expiry) is a barrier the human steps
    through — it says nothing bad about the other fields on the page, and a
    signup page can carry a password field next to the name and email. Those
    fields are filled, and then the pass stops: no navigation, no submit, no
    second pass until the human has acted. Refusing to fill them would only make
    the user retype data we already have.

    An *answer* pause (unknown, ambiguous, sensitive, legal, review) is different:
    the page's own field set is in question, so nothing on it is typed — an
    unknown field stops the automation.
    """
    if form.pause_kind in HANDOFF_ACTION_KINDS:
        return [v.name for v in form.autofillable]
    return []


def progress_snapshot(form: FormVerdicts) -> Dict[str, Any]:
    summary = form.summary()
    return {
        "fields_total": summary["fields_total"],
        "autofillable": summary["autofillable"],
        "filled": 0,
        "skipped": summary["skipped"],
        "asking": summary["asking"],
        "handoffs": summary["handoffs"],
        "blocking": summary["blocking"],
        "awaiting_user": summary["blocking"],
        "paused": 0,
        "must_pause": summary["must_pause"],
        "pause_kind": summary["pause_kind"],
        "requires_browser_handoff": summary["requires_browser_handoff"],
    }


def _checkpoint_fields(checkpoint: Dict[str, Any], form: FormVerdicts) -> None:
    """Seed/refresh the per-field checkpoint from a classification pass."""
    fields = checkpoint.setdefault("fields", {})
    for verdict in form.verdicts:
        entry = dict(fields.get(verdict.name) or {})
        entry.setdefault("status", "pending")
        entry["classification"] = verdict.classification
        entry["profile_key"] = verdict.profile_key or ""
        if verdict.action == "autofill" and entry.get("status") == "pending":
            entry["status"] = "pending"
        if verdict.action in ("skip", "never") and entry.get("status") == "pending":
            entry["status"] = "skipped"
        fields[verdict.name] = {k: v for k, v in entry.items() if k in FIELD_CHECKPOINT_KEYS}


def record_fills(
    db: Session,
    session: ApplicationSession,
    fills: Sequence[Mapping[str, Any]],
    *,
    commit: bool = True,
    journal: bool = True,
) -> Dict[str, Any]:
    """
    Record what was typed — a fingerprint per field, never a value.

    ``fills`` is ``[{"name": ..., "value": ..., "classification": ...}]``. The
    value is consumed here (to fingerprint it) and discarded; it is never
    written to the session, the events or the logs.
    """
    checkpoint = dict(session.checkpoint or {})
    fields = dict(checkpoint.get("fields") or {})
    recorded: List[str] = []
    for fill in fills:
        name = str(fill.get("name") or "")
        if not name:
            continue
        value = fill.get("value")
        entry = dict(fields.get(name) or {})
        entry.update({
            "status": "filled",
            "classification": str(fill.get("classification") or entry.get("classification") or "canonical"),
            "profile_key": str(fill.get("profile_key") or entry.get("profile_key") or ""),
            "source": str(fill.get("source") or "profile"),
            "filled_at": _iso(_now()),
            "value_fingerprint": fingerprint_value(value),
            "attempts": int(entry.get("attempts") or 0) + 1,
        })
        fields[name] = {k: v for k, v in entry.items() if k in FIELD_CHECKPOINT_KEYS}
        recorded.append(name)
    checkpoint["fields"] = fields
    if journal:
        # The pass loop journals its own (richer) fill entry — host, step, page —
        # so a caller that is mid-flow passes ``journal=False`` and the journal
        # keeps one entry per action instead of two.
        _append_step(checkpoint, {"event": "fill", "fields": recorded, "at": _iso(_now())})
    session.checkpoint = checkpoint
    progress = dict(session.progress or {})
    progress["filled"] = sum(1 for f in fields.values() if f.get("status") == "filled")
    session.progress = progress
    session.last_activity_at = _now()
    if commit:
        db.commit()
    return {"recorded": recorded, "filled_total": progress["filled"]}


def record_user_answers(
    db: Session,
    session: ApplicationSession,
    answers: Mapping[str, Any],
    *,
    commit: bool = True,
) -> Dict[str, Any]:
    """
    Record answers the user gave in-app (never a credential or a code).

    Answers are keyed by the portal's own field name. The value goes two places,
    both deliberate: the **action row** that asked for it (the shipped
    ``user_input_requests`` precedent, so the queue can show what the user said)
    and the session's **working set** (so the next pass can type it). The
    checkpoint — which is rendered, exported and logged — keeps only the status
    and a value *fingerprint*.
    """
    checkpoint = dict(session.checkpoint or {})
    fields = dict(checkpoint.get("fields") or {})
    accepted: List[str] = []
    for name, value in (answers or {}).items():
        name = str(name)
        entry = dict(fields.get(name) or {})
        entry.update({
            "status": "answered",
            "answered_at": _iso(_now()),
            "value_fingerprint": fingerprint_value(value),
            "source": "user_answer",
        })
        fields[name] = {k: v for k, v in entry.items() if k in FIELD_CHECKPOINT_KEYS}
        accepted.append(name)

    actions = (
        db.query(ApplicationAction)
        .filter(ApplicationAction.session_id == session.id)
        .order_by(ApplicationAction.id.desc())
        .all()
    )
    remaining = set(accepted)
    for action in actions:
        rows = [dict(row or {}) for row in (action.fields or [])]
        touched = False
        for row in rows:
            key = str(row.get("name") or "")
            if key in remaining and key in answers:
                row["value"] = answers[key]
                remaining.discard(key)
                touched = True
        if touched:
            action.fields = rows
    checkpoint["fields"] = fields
    checkpoint.pop("answers", None)
    session.checkpoint = checkpoint
    # An answer the user gave us for a named field is a confirmed value for this
    # session (and only this session): it may be typed on the next pass, and it
    # is never asked for twice.
    remember_values(session, {name: answers[name] for name in accepted if name in answers})
    # ...and, when it is not a credential or a restricted identifier, for
    # *future* sessions against this host too: an answer the human already
    # gave once is the definition of something worth replaying.
    for name in accepted:
        value = answers.get(name)
        if value in (None, ""):
            continue
        verdict = _verdict_for_observation_field({"name": name, "label": "", "type": "text"},
                                                 value=value)
        if str(verdict.sensitivity or "") == "restricted" or verdict.action in ("never", "handoff"):
            continue
        learn_answer(db, user_id=session.user_id, host=learning_host(session),
                     name=name, value=value, commit=False)
    session.last_activity_at = _now()
    if commit:
        db.commit()
    return {"accepted": accepted}


def fingerprint_value(value: Any) -> str:
    """A short, non-reversible digest used for change detection without a value."""
    text = "" if value is None else str(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def record_browser_input(
    db: Session,
    session: ApplicationSession,
    entry: Mapping[str, Any],
    *,
    commit: bool = True,
) -> Dict[str, Any]:
    """
    Fold one field the human typed in the **shared browser window** into the
    session — the "watch me once, reuse later" path.

    The window a pass keeps open is the same Playwright context the automation
    continues in, so what the user types there can be observed and reused
    *without* a keystroke logger: the in-page recorder reports a committed
    field (on ``change``), and this function decides what that report may
    become.

    Per accepted field, four things are recorded:

    * the portal's own identity for it — name/label/type (structure);
    * the value, **only** when it is not a credential, code or restricted
      identifier — filtered in the page, filtered again here by name/type/
      autocomplete, and finally by the classifier's own restricted verdict;
    * checkpoint status ``user_completed`` with a fingerprint, so the run
      never retypes or re-asks for it in this session;
    * the session working set **and** the user's per-host *learned answers*,
      so later pages of this flow — and future sessions against the same
      host — replay it instead of asking again.

    The journal gets one ``fill`` entry carrying the field *name* and
    ``reason="user_in_browser"`` — never the value. Refused fields are
    returned in ``restricted`` and are stored nowhere at all.
    """
    if not isinstance(entry, Mapping):
        return {"recorded": [], "restricted": []}
    name = str(entry.get("name") or "").strip()[:200]
    raw_value = entry.get("value")
    value = "" if raw_value is None else str(raw_value)
    if not name or not value.strip():
        return {"recorded": [], "restricted": []}
    field_type = str(entry.get("type") or "").lower()[:40]
    autocomplete = str(entry.get("autocomplete") or "").lower()[:60]
    label = str(entry.get("label") or "").strip()[:200]

    restricted = (
        field_type in _RESTRICTED_RECORD_TYPES
        or "password" in autocomplete
        or "one-time-code" in autocomplete
        or name.lower() in _SENSITIVE_ACTION_FIELD_NAMES
        or bool(_RESTRICTED_RECORD_PATTERN.search(name))
        or bool(_RESTRICTED_RECORD_PATTERN.search(label))
    )
    classification = "canonical"
    profile_key = str((dict(session.checkpoint or {}).get("fields") or {})
                      .get(name, {}).get("profile_key") or "")
    if not restricted:
        verdict = _verdict_for_observation_field({
            "name": name, "label": label, "type": field_type,
            "autocomplete": autocomplete, "required": False, "options": [],
        }, value=value)
        classification = str(verdict.classification or "canonical")
        profile_key = str(verdict.profile_key or profile_key or "")
        restricted = (str(verdict.sensitivity or "") == "restricted"
                      or verdict.action in ("never", "handoff"))
    if restricted:
        return {"recorded": [], "restricted": [name]}

    trimmed = value[:1000]
    checkpoint = dict(session.checkpoint or {})
    fields = dict(checkpoint.get("fields") or {})
    row = dict(fields.get(name) or {})
    row.update({
        "status": "user_completed",
        "classification": classification,
        "profile_key": profile_key,
        "filled_at": _iso(_now()),
        "value_fingerprint": fingerprint_value(trimmed),
        "source": "user_in_browser",
    })
    fields[name] = {k: v for k, v in row.items() if k in FIELD_CHECKPOINT_KEYS}
    checkpoint["fields"] = fields
    session.checkpoint = checkpoint
    remember_values(session, {name: trimmed})

    # The open action (if any) that asked for this step gets the value too —
    # the same precedent as record_user_answers, so the queue can show what
    # the human actually put in the field.
    actions = (
        db.query(ApplicationAction)
        .filter(ApplicationAction.session_id == session.id,
                ApplicationAction.status.in_(("pending", "in_progress")))
        .order_by(ApplicationAction.id.desc())
        .all()
    )
    for action in actions:
        rows = action.fields or []
        if any(isinstance(r, Mapping) and str(r.get("name") or "") == name for r in rows):
            action.fields = [
                {**dict(r), "value": trimmed}
                if isinstance(r, Mapping) and str(r.get("name") or "") == name else r
                for r in rows
            ]

    learn_answer(db, user_id=session.user_id,
                 host=learning_host(session), name=name, value=trimmed, label=label)
    record_flow_step(db, session, "fill", host=host_of(session.last_observation.get("url") or ""),
                     fields=[name], reason="user_in_browser", commit=False)
    session.last_activity_at = _now()
    if commit:
        db.commit()
    return {"recorded": [name], "restricted": []}


def _append_step(checkpoint: Dict[str, Any], step: Mapping[str, Any]) -> None:
    steps = list(checkpoint.get("steps") or [])
    steps.append(step)
    checkpoint["steps"] = steps[-MAX_SESSION_EVENTS:]


def record_flow_step(
    db: Session,
    session: ApplicationSession,
    event: str,
    *,
    commit: bool = True,
    **entry: Any,
) -> Dict[str, Any]:
    """
    Append one entry to the session's flow journal (``checkpoint["steps"]``).

    The journal is the answer to "what did the run actually do?" — it is what the
    user reads after a pass, and what makes a resume/replay auditable. Everything
    passed in is filtered through :data:`FLOW_STEP_KEYS`, so a caller cannot
    smuggle page text or a value into it, and the entry always carries ``event``
    and ``at``.
    """
    checkpoint = dict(session.checkpoint or {})
    steps = list(checkpoint.get("steps") or [])
    row: Dict[str, Any] = {"event": str(event)[:40], "at": _iso(_now())}
    for key in FLOW_STEP_KEYS:
        if key in ("event", "at") or key not in entry:
            continue
        value = entry[key]
        if isinstance(value, str):
            value = value[:200]
        elif isinstance(value, (list, tuple)):
            value = [str(item)[:80] for item in list(value)[:24]]
        elif isinstance(value, dict):
            value = {str(k)[:40]: (str(v)[:120] if isinstance(v, (str, int, float, bool)) else None)
                     for k, v in list(value.items())[:12]}
        row[key] = value
    steps.append(row)
    checkpoint["steps"] = steps[-MAX_SESSION_EVENTS:]
    session.checkpoint = checkpoint
    session.last_activity_at = _now()
    if commit:
        db.commit()
    return row


def flow_journal(session: ApplicationSession, *,
                 limit: int = FLOW_PAYLOAD_LIMIT) -> List[Dict[str, Any]]:
    """
    The most recent journal entries, newest last, for the session payload.

    A *view*: only keys the journal itself writes are returned, so an older row
    (or a tampered one) can never widen what the API exposes.
    """
    steps = list((session.checkpoint or {}).get("steps") or [])
    rows: List[Dict[str, Any]] = []
    for raw in steps[-max(1, limit):]:
        if not isinstance(raw, Mapping):
            continue
        rows.append({key: raw[key] for key in FLOW_STEP_KEYS if key in raw})
    return rows


def flow_progress(session: ApplicationSession) -> Dict[str, Any]:
    """Counters over the journal for the session card (what has been *done*)."""
    steps = [s for s in ((session.checkpoint or {}).get("steps") or []) if isinstance(s, Mapping)]
    pages = [s for s in steps if s.get("event") == "page"]
    return {
        "entries": len(steps),
        "pages": len(pages),
        "advanced": sum(1 for s in steps if s.get("event") == "advance"),
        "pauses": sum(1 for s in steps if s.get("event") == "pause"),
        "filled_fields": len({name for s in steps if s.get("event") == "fill"
                              for name in (s.get("fields") or [])}),
        "hosts": list(dict.fromkeys(str(s.get("host")) for s in steps if s.get("host")))[:5],
        "last_event": str(steps[-1].get("event")) if steps else "",
        # Whether the last pass opened a window the user could watch (and act in).
        "browser_mode": next((str(s.get("mode") or "") for s in reversed(steps) if s.get("mode")), ""),
    }


def session_values(session: ApplicationSession,
                   values: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """
    The values this session may type: the caller's, else the session's own set.

    ``ApplicationSession.fill_values`` holds only values a classifier verdict
    cleared for ``autofill`` (a profile or plan value, or an answer the user gave
    us in the app for a specific field). Codes, CAPTCHA tokens, restricted
    identifiers and unanswered legal/EEO questions have no verdict that reaches
    it — and neither does a credential: even an opted-in pass that cleared one
    for typing keeps that value in memory for the single fill (see
    :func:`record_observation`). The set is purged when the session ends — see
    :func:`purge_session_values`.
    """
    if values is not None:
        return dict(values)
    stored = session.fill_values if isinstance(session.fill_values, Mapping) else {}
    return {str(k): v for k, v in stored.items()}


def remember_values(session: ApplicationSession, values: Mapping[str, Any]) -> List[str]:
    """
    Keep the given values on the session's working set (never in the checkpoint).

    Returns the names it kept. Callers pass only values whose verdict says
    ``autofill``; this function is deliberately dumb about policy so the decision
    stays in one place (the classifier).
    """
    stored = dict(session.fill_values or {})
    kept: List[str] = []
    for name, value in (values or {}).items():
        name = str(name)
        if value in (None, ""):
            continue
        stored[name] = value
        kept.append(name)
    session.fill_values = stored
    return kept


def purge_session_values(db: Session, session: ApplicationSession, *, reason: str = "session_ended",
                         commit: bool = True) -> ApplicationSession:
    """Drop the working set of field values. Called whenever a session ends."""
    if session.fill_values:
        session.fill_values = {}
        if commit:
            db.commit()
    return session


# ---------------------------------------------------------------------------
# Learned answers — what the human taught us, replayed in future sessions
# ---------------------------------------------------------------------------
#: Where a per-host learned answer lives: a user setting, category ``browser``.
#: Only non-secret values ever reach this store (``record_browser_input`` and
#: ``record_user_answers`` both filter first), and it is ordinary user data —
#: erased with the user's settings like every other per-user row.
LEARNED_ANSWERS_CATEGORY = "browser"
LEARNED_ANSWERS_KEY = "learned_answers"
#: Bounded on purpose: a portal farm that asks a new name for the same answer
#: must not grow this forever. Oldest entries are pruned first.
LEARNED_ANSWERS_MAX = 300


def learning_host(session: ApplicationSession) -> str:
    """
    The host a learned answer is scoped to — the portal this session is bound
    to, falling back to wherever the browser actually is. Scoping by host is
    what keeps an answer typed for one employer's portal from being typed into
    an unrelated one that happens to use the same field name.
    """
    host = str(session.expected_host or "").strip().lower()
    if host:
        return host
    return str(host_of((session.last_observation or {}).get("url") or "")).lower()


def learn_answer(db: Session, *, user_id: int, host: str, name: str, value: Any,
                 label: str = "", commit: bool = True) -> bool:
    """
    Persist one non-secret answer for **future** sessions against the same
    host. The value never enters the checkpoint, the journal or any log — it
    lives only in this per-user setting, keyed by ``(host, field name)``.
    """
    if not user_id or not name or value in (None, ""):
        return False
    # SessionLocal runs with autoflush=False: without an explicit flush, a
    # second learn in the same transaction would not see the first one's row
    # and would try to INSERT it again (UNIQUE user/category/key).
    db.flush()
    raw = get_setting(db, user_id, LEARNED_ANSWERS_CATEGORY, LEARNED_ANSWERS_KEY, None)
    entries = dict(((raw or {}).get("entries") if isinstance(raw, Mapping) else None) or {})
    entries[f"{host or ''}\x1f{name}"] = {
        "n": str(name)[:200], "h": str(host or ""), "v": str(value)[:1000],
        "label": str(label or "")[:200], "at": _iso(_now()),
    }
    if len(entries) > LEARNED_ANSWERS_MAX:
        oldest = sorted(entries, key=lambda k: str((entries[k] or {}).get("at") or ""))
        for key in oldest[:len(entries) - LEARNED_ANSWERS_MAX]:
            entries.pop(key, None)
    set_setting(db, user_id, LEARNED_ANSWERS_CATEGORY, LEARNED_ANSWERS_KEY,
                {"entries": entries})
    if commit:
        db.commit()
    return True


def learned_values(db: Session, *, user_id: int, host: str) -> Dict[str, Any]:
    """
    The answers learned for this host: ``{field name: value}``. An empty host
    matches only entries learned with an empty host — a never-named portal
    does not get to borrow another portal's answers.
    """
    if not user_id:
        return {}
    raw = get_setting(db, user_id, LEARNED_ANSWERS_CATEGORY, LEARNED_ANSWERS_KEY, None)
    entries = ((raw or {}).get("entries") if isinstance(raw, Mapping) else None) or {}
    out: Dict[str, Any] = {}
    wanted = host or ""
    for item in entries.values():
        if not isinstance(item, Mapping):
            continue
        if str(item.get("h") or "") != wanted:
            continue
        name = str(item.get("n") or "")
        value = item.get("v")
        if name and value not in (None, ""):
            out[name] = value
    return out


def fill_instructions(
    session: ApplicationSession,
    *,
    observations: Optional[Sequence[Mapping[str, Any]]] = None,
    values: Optional[Mapping[str, Any]] = None,
    allow_credentials: bool = False,
) -> List[Dict[str, Any]]:
    """
    The fields this session may still type, with their values, for the driver.

    A field whose checkpoint says ``filled``/``user_completed``/``declined`` is
    not returned, which is exactly what makes a resume idempotent; a field the
    page already holds user-typed content in is not returned either. Only
    ``autofill`` verdicts can appear — a code, CAPTCHA, legal or unknown field
    has no path into this list, and a password only has one when the caller
    passes ``allow_credentials`` (opted-in account creation) *with* a
    vault-sourced value in ``values``.
    """
    checkpoint = dict(session.checkpoint or {})
    fields_checkpoint = dict(checkpoint.get("fields") or {})
    known = {str(name) for name, entry in fields_checkpoint.items()
             if isinstance(entry, Mapping) and entry.get("status") in CHECKPOINT_DONE_STATUSES}
    merged_values = session_values(session, values)

    instructions: List[Dict[str, Any]] = []
    for observation in observations or [session.last_observation or {}]:
        clean = sanitize_observation(observation)
        form = classify_form(clean.get("fields") or [], values=merged_values, checkpoint=checkpoint,
                             allow_credentials=allow_credentials)
        for verdict in form.autofillable:
            if verdict.name in known:
                continue
            value = merged_values.get(verdict.name)
            if value is None and verdict.profile_key:
                value = merged_values.get(verdict.profile_key)
            if value in (None, ""):
                continue
            instructions.append({
                "name": verdict.name,
                "label": verdict.label,
                "type": verdict.field_type,
                "classification": verdict.classification,
                "profile_key": verdict.profile_key,
                "value": value,
                "already_filled": False,
            })
    return instructions


def already_completed_fields(session: ApplicationSession) -> List[str]:
    """Fields the session must not type again — the resume/no-duplicate contract."""
    checkpoint = session.checkpoint or {}
    fields = checkpoint.get("fields") or {}
    return sorted(
        str(name) for name, entry in fields.items()
        if isinstance(entry, Mapping) and entry.get("status") in CHECKPOINT_DONE_STATUSES
    )


# --------------------------------------------------------------------------- #
# Pauses and the action queue
# --------------------------------------------------------------------------- #
def _answer_instructions(form: FormVerdicts) -> str:
    """User-facing text for an answer-type pause. Never contains a value."""
    labels = [v.label or v.name for v in form.asking[:5]]
    if not labels:
        return "This form needs your input before it can continue."
    joined = "; ".join(labels)
    return f"We stopped rather than guess. Please answer: {joined}"


def _handoff_instructions(kind: str, session: ApplicationSession) -> str:
    host = session.expected_host or "the portal"
    if kind == "captcha":
        return (f"Open {host} in your browser and complete the bot check yourself. "
                "We never solve, relay or outsource a CAPTCHA.")
    if kind == "mfa":
        return (f"Open {host} in your browser and enter the verification code yourself. "
                "The code is never sent to us and never stored.")
    if kind == "ai_unavailable":
        return (f"Open {host} in your browser and fill the highlighted fields yourself. "
                "AI-assisted field identification is temporarily unavailable; we did not "
                "guess any values rather than risk filling the wrong thing.")
    return (f"Open {host} in your browser and sign in yourself. "
            "Your password is never typed by automation and never stored by this session.")


def _action_dedupe_key(session: ApplicationSession, kind: str, reason: str,
                       fields: Sequence[FieldVerdict]) -> str:
    names = ",".join(sorted(v.name for v in fields))[:120]
    return f"s{session.id}:{kind}:{reason}:{names}"[:200]


def pause_session(
    db: Session,
    session: ApplicationSession,
    *,
    kind: str,
    reason: str,
    fields: Sequence[FieldVerdict] = (),
    instructions: str = "",
    actor: str = "system_api",
    checkpoint: Optional[Dict[str, Any]] = None,
    commit: bool = True,
) -> ApplicationAction:
    """
    Pause the session and raise (or bump) the queue item the user must act on.

    Re-pausing on the same blocker increments ``occurrences`` on the existing
    item instead of stacking duplicates — a queue the user cannot finish is a
    queue they will ignore.
    """
    if kind not in USER_ACTION_KINDS:
        raise SessionError("invalid_action_kind", f"unknown action kind: {kind}", status_code=400)

    if checkpoint is not None:
        session.checkpoint = checkpoint
    if session.state in ("active", "preparing", "launching", "created"):
        transition(session, "awaiting_user", actor=actor, reason=reason)
    elif session.state in ("paused", "resuming"):
        session.state = "awaiting_user"
        session.phase = APPLICATION_SESSION_PHASE["awaiting_user"]
        session.actor_type = actor
    session.pause_kind = kind
    session.pause_reason = reason[:80]
    session.state_reason = reason[:60]
    session.last_activity_at = _now()

    policy = effective_policy(db, session.user_id)
    dedupe = _action_dedupe_key(session, kind, reason, fields)
    action = (
        db.query(ApplicationAction)
        .filter(ApplicationAction.user_id == session.user_id, ApplicationAction.dedupe_key == dedupe)
        .first()
    )
    field_payload = [_action_field(v) for v in fields][:24]
    if action is None:
        action = ApplicationAction(
            user_id=session.user_id,
            job_id=session.job_id,
            session_id=session.id,
            kind=kind,
            status="pending",
            reason=reason[:60],
            title=_action_title(kind),
            instructions=instructions or _handoff_instructions(kind, session),
            fields=field_payload,
            dedupe_key=dedupe,
            handoff=_handoff_payload(session, kind),
            expires_at=session.expires_at,
        )
        db.add(action)
        db.flush()
    else:
        # The same blocker again: bump the existing item instead of adding a
        # second one the user has to dismiss. Counted, because a queue that
        # silently stops deduping is indistinguishable from a busy week.
        note_dedupe("action")
        action.occurrences = int(action.occurrences or 0) + 1
        action.status = "pending" if action.status != "completed" else action.status
        action.fields = field_payload or action.fields
        action.instructions = instructions or action.instructions
        action.handoff = _handoff_payload(session, kind)
        action.expires_at = action.expires_at or session.expires_at
        action.completed_at = None

    progress = dict(session.progress or {})
    progress["awaiting_user"] = len(fields) or progress.get("awaiting_user", 0)
    progress["paused"] = int(progress.get("paused") or 0) + 1
    session.progress = progress
    if commit:
        db.commit()
        db.refresh(action)

    if commit:
        record_job_event(
            db, user_id=session.user_id, job_id=session.job_id, stage="action_required",
            status="warning",
            message=f"Session #{session.id} paused for you: {_action_title(kind)}",
            meta={"session_id": session.id, "action_id": action.id, "kind": kind, "reason": reason,
                  "fields": [v.name for v in fields][:20],
                  "policy": {"ttl_minutes": policy.ttl_minutes,
                             "handoff_ttl_minutes": policy.handoff_ttl_minutes}},
        )
    inc("jobhunter_application_session_pauses_total", kind=kind)
    # The cross-workflow view: every place the product stops and hands a step to
    # the human, whatever raised it. ``kind`` is a USER_ACTION_KINDS value.
    note_user_action_pause(kind)
    return action


def _action_field(verdict: FieldVerdict) -> Dict[str, Any]:
    """The field metadata an action shows. No values, ever."""
    return {
        "name": verdict.name,
        "label": verdict.label,
        "type": verdict.field_type,
        "required": verdict.required,
        "classification": verdict.classification,
        "sensitivity": verdict.sensitivity,
        "profile_key": verdict.profile_key,
        "reason": verdict.reason,
        "question": verdict.question,
        "options": [],
    }


def _action_title(kind: str) -> str:
    return {
        "login": "Sign in to the application portal",
        "mfa": "Enter your verification code",
        "captcha": "Complete the bot check",
        "unknown_field": "A form field needs your answer",
        "ambiguous_field": "A form field is ambiguous",
        "sensitive_field": "A sensitive question needs your answer",
        "legal_question": "A legal question needs your answer",
        "session_expired": "Re-authenticate the browser session",
        "review_required": "Review the prepared application",
        "ai_unavailable": "AI field identification is unavailable",
    }.get(kind, "Your input is needed")


def _handoff_payload(session: ApplicationSession, kind: str) -> Dict[str, Any]:
    """
    The browser-handoff descriptor.

    It carries *where* and *why*, never *what*: no password, no code, no token
    value of the portal's own. ``never`` states the rules in the payload the UI
    renders, so the promise is visible where the user is being asked.
    """
    return {
        "kind": kind,
        "url": canonical_url(session.last_observation.get("url") or "") or None,
        "host": session.expected_host,
        "session_id": session.id,
        "user_completes_in_browser": True,
        "requires": {
            "login": "sign in yourself in the browser window",
            "mfa": "enter the one-time code yourself in the browser window",
            "captcha": "complete the bot check yourself in the browser window",
            "review_required": ("continue this application yourself in the browser window — "
                                "the assistant has done everything it is allowed to do"),
            "ai_unavailable": ("fill the highlighted fields yourself in the browser window — "
                               "AI field identification is temporarily unavailable"),
        }.get(kind, "answer in the app"),
        "never": [
            "we never ask you for your password",
            "we never store an MFA code",
            "we never solve or outsource a CAPTCHA",
        ],
    }


def _pending_action_id(db: Session, session: ApplicationSession) -> Optional[int]:
    row = (
        db.query(ApplicationAction)
        .filter(ApplicationAction.session_id == session.id, ApplicationAction.status == "pending")
        .order_by(ApplicationAction.id.desc())
        .first()
    )
    return row.id if row else None


def pending_actions(db: Session, user_id: int, *, session_id: Optional[int] = None,
                    kinds: Optional[Sequence[str]] = None) -> List[ApplicationAction]:
    query = db.query(ApplicationAction).filter(ApplicationAction.user_id == user_id,
                                               ApplicationAction.status == "pending")
    if session_id is not None:
        query = query.filter(ApplicationAction.session_id == session_id)
    if kinds:
        query = query.filter(ApplicationAction.kind.in_(tuple(kinds)))
    return query.order_by(ApplicationAction.created_at.desc(), ApplicationAction.id.desc()).all()


def action_queue(db: Session, user_id: int) -> List[Dict[str, Any]]:
    """
    The actionable queue: what this user must do, across every session.

    CAPTCHA and MFA items appear here as first-class rows (kind, where, why, and
    the handoff descriptor) — the acceptance criterion is that they are
    *actionable*, not merely logged.
    """
    sweep_expired_sessions(db, user_id=user_id)
    rows = pending_actions(db, user_id)
    out: List[Dict[str, Any]] = []
    for row in rows:
        session = db.query(ApplicationSession).filter(ApplicationSession.id == row.session_id).first() \
            if row.session_id else None
        job = db.query(Job).filter(Job.id == row.job_id, Job.user_id == user_id).first() if row.job_id else None
        out.append(action_public(row, session=session, job=job))
    return out


def action_public(action: ApplicationAction, *, session: Optional[ApplicationSession] = None,
                  job: Optional[Job] = None) -> Dict[str, Any]:
    """A queue item as the API returns it (no secrets; token only when minted)."""
    return {
        "id": action.id,
        "kind": action.kind,
        "status": action.status,
        "reason": action.reason,
        "title": action.title,
        "instructions": action.instructions,
        "fields": action.fields or [],
        "handoff": action.handoff or {},
        "occurrences": action.occurrences,
        "job_id": action.job_id,
        "job": {"title": job.title if job else "", "company": job.company if job else "",
                "url": job.url if job else ""},
        "session_id": action.session_id,
        "session_state": session.state if session else None,
        "requires_browser_handoff": action.kind in BROWSER_STEP_KINDS,
        "created_at": _iso(action.created_at),
        "expires_at": _iso(action.expires_at),
    }


def complete_action(
    db: Session,
    *,
    user: User,
    action: ApplicationAction,
    note: str = "",
) -> ApplicationAction:
    """
    Mark a queue item done. **No secret is accepted here** — the API rejects a
    payload carrying a password/code field outright, so a "completion" can never
    smuggle the value it claims the user typed elsewhere.
    """
    if int(action.user_id) != int(user.id):
        raise SessionNotFound(action.id)
    action.status = "completed"
    action.completed_at = _now()
    if note:
        action.instructions = f"{action.instructions}\n\n[{note[:200]}]"[:4000]
    session = db.query(ApplicationSession).filter(ApplicationSession.id == action.session_id).first() \
        if action.session_id else None
    if session is not None and session.state == "awaiting_user":
        checkpoint = dict(session.checkpoint or {})
        if action.kind in HANDOFF_ACTION_KINDS:
            # The user performed this step in the browser. Record that the field
            # is done *without* recording anything they typed: no value, no
            # fingerprint of a secret, just the fact and its classification.
            fields = dict(checkpoint.get("fields") or {})
            for field in action.fields or []:
                name = str((field or {}).get("name") or "")
                if not name:
                    continue
                entry = dict(fields.get(name) or {})
                entry.update({"status": "user_completed", "source": "user_in_browser",
                              "classification": str((field or {}).get("classification") or
                                                    entry.get("classification") or "canonical"),
                              "filled_at": _iso(_now())})
                fields[name] = {k: v for k, v in entry.items() if k in FIELD_CHECKPOINT_KEYS}
            checkpoint["fields"] = fields
        _append_step(checkpoint, {"step": "action_completed", "kind": action.kind,
                                  "action_id": action.id, "at": _iso(_now())})
        session.checkpoint = checkpoint
        session.pause_kind = ""
        session.pause_reason = ""
    db.commit()
    if session is not None:
        record_job_event(
            db, user_id=user.id, job_id=session.job_id, stage="action_completed", status="success",
            message=f"Completed: {action.title}",
            meta={"session_id": session.id, "action_id": action.id, "kind": action.kind},
        )
    inc("jobhunter_application_actions_total", kind=action.kind, result="completed")
    waited = ((action.completed_at - action.created_at).total_seconds()
              if action.completed_at and action.created_at else None)
    note_user_action_outcome(action.kind, "completed", waited_seconds=waited)
    return action


# --------------------------------------------------------------------------- #
# Browser handoff (login / MFA / CAPTCHA)
# --------------------------------------------------------------------------- #
def issue_handoff(
    db: Session,
    *,
    user: User,
    session: ApplicationSession,
    action: ApplicationAction,
) -> Dict[str, Any]:
    """
    Mint a one-time handoff window for a human-only browser step.

    The token authenticates *the assistant's* attach to this session for this
    action window — the user still types any secret directly into the browser,
    and the token itself carries no credential. Stored as a SHA-256 hash and
    single-use.
    """
    _assert_owner(session, user)
    if int(action.user_id) != int(user.id) or action.session_id != session.id:
        raise SessionNotFound(action.id)
    if action.kind not in HANDOFF_ACTION_KINDS:
        raise SessionError("handoff_not_applicable",
                           f"'{action.kind}' is answered in the app, not in the browser",
                           status_code=400)
    if is_expired(session):
        expire_session(db, session, reason="handoff_after_expiry")
        raise SessionExpired(session)
    if action.status not in ("pending", "in_progress"):
        raise SessionError("action_already_closed", "this action is already closed", status_code=409)

    policy = effective_policy(db, user.id)
    token = f"ho_{secrets.token_urlsafe(32)}"
    ttl = min(policy.handoff_ttl_minutes, MAX_HANDOFF_TTL_MINUTES)
    session.handoff_token_hash = sha256_hex(token)
    session.handoff_expires_at = _now() + timedelta(minutes=ttl)
    session.handoff_action_id = action.id
    action.status = "in_progress"
    action.acknowledged_at = action.acknowledged_at or _now()
    db.commit()

    if session.job_id:
        record_job_event(
            db, user_id=user.id, job_id=session.job_id, stage="handoff_issued", status="info",
            message=f"Browser handoff for '{action.title}' — the step is yours to complete",
            meta={"session_id": session.id, "action_id": action.id, "kind": action.kind,
                  "expires_at": _iso(session.handoff_expires_at)},
        )
    return {
        "action_id": action.id,
        "session_id": session.id,
        "kind": action.kind,
        "token": token,  # returned exactly once; only its hash is stored
        "expires_at": _iso(session.handoff_expires_at),
        "url": (action.handoff or {}).get("url") or canonical_url(session.last_observation.get("url") or ""),
        "host": session.expected_host,
        "user_completes_in_browser": True,
        "never": (action.handoff or {}).get("never", []),
    }


def handoff_token_valid(session: ApplicationSession, token: str) -> bool:
    """Single-use, time-boxed, constant-time comparison against the stored hash."""
    if not token or not session.handoff_token_hash:
        return False
    if session.handoff_expires_at and session.handoff_expires_at <= _now():
        return False
    return secrets.compare_digest(sha256_hex(token), session.handoff_token_hash)


def consume_handoff_token(db: Session, session: ApplicationSession, token: str) -> bool:
    if not handoff_token_valid(session, token):
        return False
    session.handoff_token_hash = ""
    session.handoff_expires_at = None
    session.handoff_action_id = None
    db.commit()
    return True


def is_session_recording_open(session: ApplicationSession) -> bool:
    """Whether an already-paired client tab may keep posting observations.

    After a handoff token has been consumed (the user opened their own
    browser tab and the companion bridge is recording) the tab is allowed to
    continue streaming observations and non-secret inputs for the rest of
    this session's life — until it completes, fails, expires, or is
    cancelled. A terminal or expired session refuses further reports.
    """
    if not session:
        return False
    if session.state in APPLICATION_SESSION_TERMINAL_STATES:
        return False
    if is_expired(session):
        return False
    return session.state in ("active", "awaiting_user", "paused", "resuming", "preparing", "launching")


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #
def is_expired(session: ApplicationSession, *, now: Optional[datetime] = None) -> bool:
    if session.state == "expired":
        return True
    if session.state in APPLICATION_SESSION_TERMINAL_STATES:
        return False
    moment = now or _now()
    return bool(session.expires_at and session.expires_at <= moment)


def expire_session(db: Session, session: ApplicationSession, *, reason: str = "ttl_elapsed",
                   commit: bool = True) -> ApplicationSession:
    """
    Expire a session: purge persisted state and close the open queue items.

    An expired session does not resume — ``reauthenticate`` is the only way
    forward, and it starts a fresh pass with a fresh TTL after the user proves
    they are still there.
    """
    if session.state in APPLICATION_SESSION_TERMINAL_STATES and session.state != "expired":
        return session
    purge_storage_state(db, session, reason=f"expired:{reason}", commit=False)
    purge_session_values(db, session, reason=f"expired:{reason}", commit=False)
    session.state = "expired"
    session.phase = APPLICATION_SESSION_PHASE["expired"]
    session.state_reason = reason[:60]
    session.actor_type = "system_scheduler"
    session.ended_at = session.ended_at or _now()
    session.pause_kind = ""
    session.pause_reason = ""
    open_actions = (
        db.query(ApplicationAction)
        .filter(ApplicationAction.session_id == session.id, ApplicationAction.status.in_(("pending", "in_progress")))
        .all()
    )
    for action in open_actions:
        action.status = "expired"
        action.completed_at = _now()
        waited = ((action.completed_at - action.created_at).total_seconds()
                  if action.created_at else None)
        note_user_action_outcome(action.kind, "expired", waited_seconds=waited)
    if commit:
        db.commit()
    record_job_event(
        db, user_id=session.user_id, job_id=session.job_id, stage="session_expired", status="warning",
        message=f"Browser session #{session.id} expired ({reason}) — re-authentication required",
        meta={"session_id": session.id, "reason": reason,
              "closed_actions": [a.id for a in open_actions]},
    )
    inc("jobhunter_application_sessions_total", result="expired")
    return session


def sweep_expired_sessions(db: Session, *, user_id: Optional[int] = None,
                           now: Optional[datetime] = None) -> int:
    """Expire every session whose TTL has passed. Idempotent, cheap, called on read."""
    moment = now or _now()
    query = db.query(ApplicationSession).filter(
        ApplicationSession.state.notin_(APPLICATION_SESSION_TERMINAL_STATES),
        ApplicationSession.expires_at.isnot(None),
        ApplicationSession.expires_at <= moment,
    )
    if user_id is not None:
        query = query.filter(ApplicationSession.user_id == user_id)
    expired = 0
    for session in query.all():
        expire_session(db, session, reason="ttl_elapsed", commit=False)
        expired += 1
    if expired:
        db.commit()
    return expired


def reauthenticate(
    db: Session,
    *,
    user: User,
    session: ApplicationSession,
) -> Dict[str, Any]:
    """
    Restart an expired (or failed) session safely: new TTL, no stale state.

    This is the *only* transition out of ``expired``/``failed``. The persisted
    cookie jar is dropped first — continuing with a half-dead session is how an
    automation silently acts as someone else — and the user is handed a login
    item so the human step is explicit.
    """
    _assert_owner(session, user)
    if session.state not in ("expired", "failed"):
        raise SessionError("not_reauthenticatable",
                           f"a session in '{session.state}' does not need re-authentication",
                           status_code=409)
    policy = effective_policy(db, user.id)
    purge_storage_state(db, session, reason="reauthenticate", commit=False)
    session.handoff_token_hash = ""
    session.handoff_expires_at = None
    session.expires_at = _now() + timedelta(minutes=policy.ttl_minutes)
    session.resumed_count = 0
    session.checkpoint = {"fields": {}, "steps": list((session.checkpoint or {}).get("steps") or [])}
    session.progress = {"fields_total": 0, "filled": 0, "skipped": 0, "awaiting_user": 0, "paused": 0}
    transition(session, "launching", actor="user", reason="reauthentication")
    db.commit()
    action = pause_session(
        db, session, kind="login", reason="reauthentication_required",
        instructions=_handoff_instructions("login", session), actor="user",
    )
    record_job_event(
        db, user_id=user.id, job_id=session.job_id, stage="session_reauthenticated", status="info",
        message=f"Session #{session.id} re-authenticated by the user — sign-in required again",
        meta={"session_id": session.id, "action_id": action.id},
    )
    return {"session_id": session.id, "status": session.state, "action_id": action.id,
            "expires_at": _iso(session.expires_at), "requires_login": True}


def cancel_session(db: Session, *, user: User, session: ApplicationSession,
                   reason: str = "user_cancelled") -> ApplicationSession:
    _assert_owner(session, user)
    if session.state in APPLICATION_SESSION_TERMINAL_STATES and session.state != "expired":
        return session
    purge_storage_state(db, session, reason=f"cancelled:{reason}", commit=False)
    session.state = "cancelled"
    session.phase = APPLICATION_SESSION_PHASE["cancelled"]
    session.state_reason = reason[:60]
    session.actor_type = "user"
    session.ended_at = _now()
    session.pause_kind = ""
    session.pause_reason = ""
    session.fill_values = {}
    for action in pending_actions(db, user.id, session_id=session.id):
        action.status = "cancelled"
        action.completed_at = _now()
    db.commit()
    record_job_event(db, user_id=user.id, job_id=session.job_id, stage="session_cancelled", status="info",
                     message=f"Browser session #{session.id} cancelled ({reason})",
                     meta={"session_id": session.id})
    inc("jobhunter_application_sessions_total", result="cancelled")
    return session


# --------------------------------------------------------------------------- #
# Checkpoint validation
# --------------------------------------------------------------------------- #
def checkpoint_failures(
    db: Session,
    session: ApplicationSession,
    *,
    observed: Optional[Mapping[str, Any]] = None,
    confirmations: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
) -> List[Dict[str, str]]:
    """
    Everything that must still be true before a paused session resumes.

    Employer, job, URL and application identity are all re-checked here — the
    acceptance criterion is that a resume validates *identity*, not just state.
    ``confirmations`` carries the identity the user is looking at
    (``{"job_id":…, "url":…, "employer":…, "application_identity":…}``) and is
    treated exactly like an observation: a mismatch is a refusal, never a warning.
    """
    failures: List[Dict[str, str]] = []
    moment = now or _now()

    if session.expires_at and session.expires_at <= moment:
        failures.append({"code": "session_expired",
                         "detail": f"the session TTL ended at {_iso(session.expires_at)}"})
    if session.state not in ("awaiting_user", "paused", "active", "resuming"):
        failures.append({"code": "session_not_resumable",
                         "detail": f"a session in '{session.state}' cannot be resumed"})

    job = db.query(Job).filter(Job.id == session.job_id).first()
    if job is None:
        failures.append({"code": "job_reassigned", "detail": "the job row no longer exists"})
    elif int(job.user_id) != int(session.user_id):
        failures.append({"code": "job_not_owned", "detail": "the job now belongs to another account"})

    merged: Dict[str, Any] = {}
    if observed:
        merged.update(sanitize_observation(observed))
    if confirmations:
        merged.update({k: v for k, v in confirmations.items() if v not in (None, "")})

    if job is not None:
        expected_url_sha = session.url_fingerprint
        if expected_url_sha and url_fingerprint(job.url) != expected_url_sha:
            failures.append({"code": "url_changed",
                             "detail": "the posting URL changed since this session started"})
        if application_identity(job) != session.application_identity:
            failures.append({"code": "application_identity_mismatch",
                             "detail": "the posting's own identity changed since this session started"})

    observed_url = merged.get("url") or ""
    if observed_url:
        if session.url_fingerprint and url_fingerprint(str(observed_url)) != session.url_fingerprint:
            failures.append({"code": "url_changed",
                             "detail": f"the browser is on {canonical_url(str(observed_url))}, "
                                       f"not the posting this session started on"})
        observed_host = str(merged.get("host") or host_of(str(observed_url)))
        if session.expected_host and observed_host and not net_guard.host_matches(observed_host,
                                                                                  session.expected_host):
            failures.append({"code": "portal_domain_not_allowed",
                             "detail": f"{observed_host} is not {session.expected_host}"})
        elif not _host_allowed_for(session, observed_host):
            failures.append({"code": "portal_domain_not_allowed",
                             "detail": f"{observed_host} is not an allowed application domain"})

    observed_employer = str(merged.get("employer") or "").strip()
    if observed_employer and session.employer_fingerprint:
        if employer_fingerprint(observed_employer) != session.employer_fingerprint:
            failures.append({"code": "employer_mismatch",
                             "detail": f"the page names '{observed_employer[:80]}', "
                                       f"not this application's employer"})

    observed_identity = str(merged.get("application_identity") or "").strip()
    if observed_identity and session.application_identity:
        expected = session.application_identity
        candidate = observed_identity if observed_identity.startswith(("ext:", "url:")) \
            else f"ext:{sha256_hex(observed_identity)[:32]}"
        if candidate != expected:
            failures.append({"code": "application_identity_mismatch",
                             "detail": "the application on screen is not the one this session prepared"})

    deduped: List[Dict[str, str]] = []
    seen: set[str] = set()
    for failure in failures:
        if failure["code"] in seen:
            continue
        seen.add(failure["code"])
        deduped.append(failure)
    return deduped


def _host_allowed_for(session: ApplicationSession, host: str) -> bool:
    """The mapped host must be the session's portal, a known ATS, or the job's host."""
    if not host:
        return True
    allowed = {session.expected_host, host_of(session.last_observation.get("url") or "")}
    allowed.discard("")
    for ats in VAULT_DOMAINS.values():
        allowed.add(ats.lower())
    return any(net_guard.host_matches(host, candidate) for candidate in allowed)


def resume_session(
    db: Session,
    *,
    user: User,
    session: ApplicationSession,
    observed: Optional[Mapping[str, Any]] = None,
    confirmations: Optional[Mapping[str, Any]] = None,
    values: Optional[Mapping[str, Any]] = None,
    actor: str = "user",
) -> Dict[str, Any]:
    """
    Resume a paused session **only** after the checkpoint validates.

    On success the session moves to ``active`` and the caller receives the fill
    instructions for everything not already done — the resume path never
    re-types a completed field (see :func:`fill_instructions`).
    """
    _assert_owner(session, user)
    if is_expired(session):
        expire_session(db, session, reason="resume_after_expiry")
        raise SessionExpired(session)

    failures = checkpoint_failures(db, session, observed=observed, confirmations=confirmations)
    if failures:
        session.last_checkpoint_failure = failures[0]["code"]
        # A refused resume re-pauses with the most specific reason it has: the
        # user must be told *why*, and the queue item must match the reason.
        kind = "session_expired" if any(f["code"] == "session_expired" for f in failures) else "review_required"
        action = pause_session(
            db, session, kind=kind,
            reason=failures[0]["code"],
            instructions=(f"Resume refused: {failures[0]['detail']}. "
                          "Confirm the job, URL and employer before continuing."),
            actor="system_api",
        )
        inc("jobhunter_application_session_resume_refused_total", reason=failures[0]["code"])
        raise CheckpointFailed(failures)

    session.last_checkpoint_failure = ""
    transition(session, "resuming", actor=actor, reason="checkpoint_validated")
    session.resumed_count = int(session.resumed_count or 0) + 1
    session.pause_kind = ""
    session.pause_reason = ""
    session.last_activity_at = _now()
    checkpoint = dict(session.checkpoint or {})
    _append_step(checkpoint, {"step": "resumed", "at": _iso(_now()),
                              "resumed_count": session.resumed_count})
    session.checkpoint = checkpoint
    for action in pending_actions(db, user.id, session_id=session.id):
        action.status = "completed"
        action.completed_at = _now()
    transition(session, "active", actor=actor, reason="resumed")
    db.commit()

    record_job_event(
        db, user_id=user.id, job_id=session.job_id, stage="session_resumed", status="info",
        message=(f"Session #{session.id} resumed after checkpoint validation "
                 f"({session.resumed_count} resume(s); completed fields will not be re-typed)"),
        meta={"session_id": session.id, "completed_fields": already_completed_fields(session)},
    )
    inc("jobhunter_application_sessions_total", result="resumed")
    job = db.query(Job).filter(Job.id == session.job_id).first()
    if values is None:
        answers = session_values(session)
        values = planned_values(job, answers=answers) if job is not None else answers
    return {
        "session_id": session.id,
        "status": session.state,
        "resumed_count": session.resumed_count,
        "completed_fields": already_completed_fields(session),
        "to_fill": [i["name"] for i in fill_instructions(session, values=values)],
        "progress": session.progress or {},
    }


def pause_for_user(
    db: Session,
    *,
    user: User,
    session: ApplicationSession,
    reason: str = "user_requested",
) -> Dict[str, Any]:
    """A pause the user asked for (they are stepping away, not handing off a step)."""
    _assert_owner(session, user)
    if is_expired(session):
        expire_session(db, session, reason="pause_after_expiry")
        raise SessionExpired(session)
    if session.state in APPLICATION_SESSION_TERMINAL_STATES:
        raise SessionError("session_closed", f"session is {session.state}", status_code=409)
    transition(session, "paused", actor="user", reason=reason)
    session.pause_kind = "review_required"
    session.pause_reason = reason[:80]
    db.commit()
    record_job_event(db, user_id=user.id, job_id=session.job_id, stage="session_paused", status="info",
                     message=f"Session #{session.id} paused by you ({reason})",
                     meta={"session_id": session.id})
    return session_detail(db, session=session)


# --------------------------------------------------------------------------- #
# Encrypted, optional session persistence
# --------------------------------------------------------------------------- #
def storage_state_scope(user_id: int) -> str:
    """Per-user encryption scope: another user's key can never read this state."""
    return f"user:{user_id}:session"


def _sanitize_storage_state(state: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Keep only what a Playwright ``storage_state`` may carry, and only its
    documented cookie fields.

    A cookie jar is session material by nature (that is what makes persistence
    useful); what it must never carry is an unrelated blob dropped into the same
    JSON, so the shape is pinned here.
    """
    cookies: List[Dict[str, Any]] = []
    allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    for cookie in list((state or {}).get("cookies") or [])[:400]:
        if not isinstance(cookie, Mapping):
            continue
        entry = {k: cookie[k] for k in allowed if k in cookie}
        if not entry.get("name") or "value" not in entry:
            continue
        entry["name"] = str(entry["name"])[:120]
        entry["value"] = str(entry["value"])[:4096]
        entry["domain"] = str(entry.get("domain") or "")[:200]
        cookies.append(entry)
    origins = []
    for origin in list((state or {}).get("origins") or [])[:50]:
        if not isinstance(origin, Mapping):
            continue
        item: Dict[str, Any] = {"origin": str(origin.get("origin") or "")[:300], "localStorage": []}
        for entry in list(origin.get("localStorage") or [])[:200]:
            if not isinstance(entry, Mapping):
                continue
            name = str(entry.get("name") or "")[:120]
            if not name or name.lower() in ("password", "passwd", "otp", "code", "mfa"):
                continue
            item["localStorage"].append({"name": name, "value": str(entry.get("value") or "")[:2048]})
        origins.append(item)
    return {"cookies": cookies, "origins": origins}


def persist_storage_state(
    db: Session,
    session: ApplicationSession,
    state: Mapping[str, Any],
    *,
    policy: Optional[SessionPolicy] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """
    Encrypt and store the browser's own session state — only if the user opted in.

    Refused (and reported as refused, not silently dropped) when persistence is
    off: an automation that keeps cookies for a user who asked it not to is a
    surprise the user cannot see.
    """
    policy = policy or effective_policy(db, session.user_id)
    if not policy.persist_state:
        return {"persisted": False, "reason": "persistence_disabled_by_user"}
    clean = _sanitize_storage_state(state)
    if not clean["cookies"] and not clean["origins"]:
        return {"persisted": False, "reason": "nothing_to_persist"}
    payload = json.dumps(clean, separators=(",", ":"))
    session.storage_state_enc = encrypt_secret(payload, storage_state_scope(session.user_id))
    session.storage_state_saved_at = _now()
    session.storage_state_purged_at = None
    session.storage_state_expires_at = session.expires_at
    if commit:
        db.commit()
    return {
        "persisted": True,
        "cookies": len(clean["cookies"]),
        "origins": len(clean["origins"]),
        "expires_at": _iso(session.storage_state_expires_at),
    }


def load_storage_state(
    db: Session,
    session: ApplicationSession,
    *,
    user: User,
    policy: Optional[SessionPolicy] = None,
) -> Optional[Dict[str, Any]]:
    """
    Decrypt this session's persisted state — for its own user only.

    Returns ``None`` (never an error the caller might paper over) when there is
    nothing to load, the owner does not match, persistence is off, or the state
    is stale; each case is logged with its reason and nothing else.
    """
    _assert_owner(session, user)
    policy = policy or effective_policy(db, user.id)
    if not session.storage_state_enc:
        return None
    if not policy.persist_state:
        return None
    if session.storage_state_expires_at and session.storage_state_expires_at <= _now():
        purge_storage_state(db, session, reason="stale")
        return None
    try:
        raw = decrypt_secret(session.storage_state_enc, storage_state_scope(user.id))
    except Exception as exc:
        log.warning("session %s: stored state could not be decrypted (%s)", session.id, type(exc).__name__)
        purge_storage_state(db, session, reason="undecryptable")
        return None
    try:
        return _sanitize_storage_state(json.loads(raw))
    except Exception:
        purge_storage_state(db, session, reason="unreadable")
        return None


def purge_storage_state(db: Session, session: ApplicationSession, *, reason: str = "cleared",
                        commit: bool = True) -> bool:
    """Drop persisted browser state. Idempotent; used by expiry, cancel and erasure."""
    had = bool(session.storage_state_enc)
    session.storage_state_enc = None
    session.storage_state_saved_at = None
    session.storage_state_expires_at = None
    session.handoff_token_hash = ""
    session.handoff_expires_at = None
    if had:
        session.storage_state_purged_at = _now()
        log.info("session %s: persisted browser state purged (%s)", session.id, reason)
    if commit:
        db.commit()
    return had


# --------------------------------------------------------------------------- #
# Screenshots (privacy/retention gated)
# --------------------------------------------------------------------------- #
def screenshot_decision(policy: SessionPolicy, *, kind: str) -> Dict[str, Any]:
    """
    May this step be captured?

    Three independent conditions, all of which must hold: the deployment enabled
    screenshots, the user opted in with a non-zero retention, and the screen is
    not one where a human is typing a secret (login/MFA/CAPTCHA). The reasoning
    is returned so a refusal is visible instead of mysterious.
    """
    if kind in NEVER_SCREENSHOT_KINDS:
        return {"allowed": False, "reason": "handoff_screen_never_captured", "retention_days": 0}
    if not policy.screenshots_enabled:
        return {"allowed": False, "reason": "screenshots_not_enabled", "retention_days": 0}
    if policy.screenshot_retention_days <= 0:
        return {"allowed": False, "reason": "retention_window_zero", "retention_days": 0}
    return {"allowed": True, "reason": "permitted", "retention_days": policy.screenshot_retention_days}


def record_screenshot(
    db: Session,
    session: ApplicationSession,
    *,
    kind: str,
    path: str,
    policy: Optional[SessionPolicy] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """Record screenshot *metadata* when the policy permits one. Never the pixels."""
    policy = policy or effective_policy(db, session.user_id)
    decision = screenshot_decision(policy, kind=kind)
    if not decision["allowed"]:
        return {"recorded": False, **decision}
    digest = ""
    try:
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return {"recorded": False, "reason": "screenshot_unreadable", "retention_days": 0}
    shots = list(session.screenshots or [])
    entry = {
        "action": kind,
        "path": os.path.basename(path),
        "sha256": digest,
        "captured_at": _iso(_now()),
        "retention_days": decision["retention_days"],
        "expires_at": _iso(_now() + timedelta(days=decision["retention_days"])),
    }
    shots.append(entry)
    session.screenshots = shots[-20:]
    if commit:
        db.commit()
    inc("jobhunter_application_screenshots_total", result="recorded")
    return {"recorded": True, **decision, "entry": entry}


# --------------------------------------------------------------------------- #
# Submissions: at-most-once
# --------------------------------------------------------------------------- #
def submission_idempotency_key(job: Job, session: Optional[ApplicationSession] = None) -> str:
    """Stable per ``(job, attempt)``: a retry of the same intent is the same key."""
    base = f"sub:{job.user_id}:{job.id}:{application_identity(job)}"
    if session is not None:
        base = f"{base}:s{session.id}"
    return sha256_hex(base)[:48]


def existing_submission(db: Session, user_id: int, job_id: int) -> Optional[ApplicationSubmission]:
    return (
        db.query(ApplicationSubmission)
        .filter(ApplicationSubmission.user_id == user_id, ApplicationSubmission.job_id == job_id,
                ApplicationSubmission.state.in_(("reserved", "submitted", "verified")))
        .order_by(ApplicationSubmission.id.desc())
        .first()
    )


def submission_decision(
    db: Session,
    *,
    user: User,
    job: Job,
    session: Optional[ApplicationSession] = None,
    policy: Optional[SessionPolicy] = None,
    explicit_policy: bool = False,
) -> Dict[str, Any]:
    """
    Pure-ish decision: may this job be submitted right now?

    Duplicate prevention is the first check and it is answered from the ledger
    (and the job's own ``applied`` status), not from the session: a session that
    was deleted, expired or resumed cannot make a second submission legal.

    The submission *gate* is the automation-policy engine (contracts/10 §5):
    the same explicit, source-specific decision the legacy autofill path
    consumes. When a caller hands in its own ``SessionPolicy`` with
    ``allow_submit=True`` (``explicit_policy=True``), that is the internal
    "gates are open" override — workers and tests use it to exercise the
    session-level checks below — and the engine is left to the request path,
    where it is authoritative.
    """
    from app.services import automation_policy as policy_engine

    policy = policy or effective_policy(db, user.id)
    existing = existing_submission(db, user.id, job.id)
    if existing is not None:
        return {"allowed": False, "reason": "already_submitted", "submission_id": existing.id,
                "state": existing.state, "channel": existing.channel}
    if (job.status or "") == "applied":
        return {"allowed": False, "reason": "already_submitted", "submission_id": None,
                "state": "submitted", "channel": "manual_user"}

    decision: Dict[str, Any] = {}
    if not (explicit_policy and policy.allow_submit):
        decision = policy_engine.evaluate_policy(
            db,
            user=user,
            job=job,
            workflow=policy_engine.SUBMIT_WORKFLOW,
            persona_id=job.persona_id if job else None,
            for_http=True,
        )
        if not decision["allowed"]:
            # The submission surface speaks the refusal vocabulary
            # (``SUBMISSION_REFUSALS``); the engine's finer reason travels
            # nested under ``policy`` so nothing is lost.
            decision.setdefault("server", {
                "autofill_enabled": settings.autofill_enabled,
                "autofill_dry_run": settings.autofill_dry_run,
                "autofill_allow_submit": settings.autofill_allow_submit,
            })
            decision.setdefault("user_consent",
                                bool(get_setting(db, user.id, "application", "allow_auto_submit", False)))
            return {"allowed": False, "reason": "policy_disallows_submit", "submission_id": None,
                    "policy_id": decision.get("policy_id"),
                    "policy_version": decision.get("policy_version"),
                    "policy": decision}

    if session is None:
        return {"allowed": False, "reason": "no_session", "submission_id": None}
    if is_expired(session):
        return {"allowed": False, "reason": "session_expired", "submission_id": None,
                "requires_reauthentication": True}
    open_actions = pending_actions(db, user.id, session_id=session.id)
    if open_actions:
        return {"allowed": False, "reason": "action_required", "submission_id": None,
                "action_ids": [a.id for a in open_actions]}
    failures = checkpoint_failures(db, session)
    if failures:
        return {"allowed": False, "reason": "checkpoint_invalid", "submission_id": None,
                "failures": failures}

    allow_submit = bool(
        (explicit_policy and policy.allow_submit)
        or (decision.get("mode") == "auto_submit" and policy.allow_submit)
    )
    return {
        "allowed": True,
        "reason": "allowed",
        "submission_id": None,
        "dry_run": bool(policy.dry_run or not allow_submit),
        "channel": "automation" if allow_submit else "assisted_dry_run",
        "policy_id": decision.get("policy_id") if decision else None,
        "policy_version": decision.get("policy_version") if decision else None,
        "consent_snapshot": (decision.get("inputs", {}).get("consents", {}) if decision else {}),
        "policy": decision,
    }


def reserve_submission(
    db: Session,
    *,
    user: User,
    job: Job,
    session: Optional[ApplicationSession] = None,
    channel: str = "automation",
    policy: Optional[SessionPolicy] = None,
) -> Tuple[Optional[ApplicationSubmission], Dict[str, Any]]:
    """
    Reserve the single submission slot for this ``(user, job)``.

    Returns ``(row, decision)``. ``row`` is ``None`` when the submission was
    refused — refused, not silently skipped: the caller reports ``decision`` to
    the user. A unique constraint on ``(user_id, job_id)`` for live rows is the
    database-level backstop, and an ``IntegrityError`` is mapped back to the
    same refusal rather than a 500.

    The policy decision is stored on the row (``policy_id`` / ``policy_version`` /
    ``consent_snapshot``, contracts/10 §5 rule 3), so the attempt is
    reconstructible even if the policy changes (or is deleted) afterwards.
    ``policy`` is ``None`` on the HTTP path (the engine decides); callers that
    pass a policy with ``allow_submit=True`` are the internal override.
    """
    explicit_policy = bool(policy is not None and policy.allow_submit)
    decision = submission_decision(db, user=user, job=job, session=session, policy=policy,
                                   explicit_policy=explicit_policy)
    if not decision["allowed"]:
        inc("jobhunter_application_submissions_total", result="refused",
            reason=str(decision["reason"]))
        return None, decision

    policy = policy or effective_policy(db, user.id)
    resolved_channel = str(decision.get("channel") or channel)
    row = ApplicationSubmission(
        user_id=user.id,
        job_id=job.id,
        session_id=session.id if session else None,
        state="reserved",
        channel=resolved_channel,
        idempotency_key=submission_idempotency_key(job, session),
        dry_run=bool(decision.get("dry_run", policy.dry_run)),
        reserved_at=_now(),
        policy_id=decision.get("policy_id"),
        policy_version=decision.get("policy_version"),
        consent_snapshot=dict(decision.get("consent_snapshot") or {}),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = existing_submission(db, user.id, job.id)
        decision = {"allowed": False, "reason": "already_submitted",
                    "submission_id": existing.id if existing else None,
                    "state": existing.state if existing else "submitted",
                    "channel": existing.channel if existing else "manual_user"}
        inc("jobhunter_application_submissions_total", result="refused", reason="already_submitted")
        return None, decision
    db.refresh(row)
    inc("jobhunter_application_submissions_total", result="reserved")
    decision["submission_id"] = row.id
    return row, decision


def finalize_submission(
    db: Session,
    row: ApplicationSubmission,
    *,
    state: str,
    receipt: Optional[Mapping[str, Any]] = None,
    refusal_reason: str = "",
    commit: bool = True,
) -> ApplicationSubmission:
    """
    Close a reservation. ``submitted`` means a human-visible submit actually
    happened; ``failed``/``abandoned`` release the slot for a later attempt
    (``abandoned`` keeps the row, so the history of attempts survives).
    """
    row.state = state
    if receipt:
        row.receipt = dict(receipt)
    if refusal_reason:
        row.refusal_reason = refusal_reason[:40]
    if state in ("submitted", "verified", "failed", "abandoned"):
        row.finished_at = _now()
    if state in ("submitted", "verified"):
        row.submitted_at = row.submitted_at or _now()
    if commit:
        db.commit()
        db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def session_public(session: ApplicationSession, *, actions: Optional[Sequence[ApplicationAction]] = None,
                   job: Optional[Job] = None) -> Dict[str, Any]:
    """
    The session as the API returns it.

    Deliberately absent: ``storage_state_enc``, ``handoff_token_hash``,
    ``browser_profile_ref`` and field values. What is present is the state, the
    fingerprints (as *booleans* — "the employer still matches" — never as the
    expected value), the checkpoint statuses and the safe progress counters.
    """
    checkpoint = session.checkpoint or {}
    fields = checkpoint.get("fields") or {}
    return {
        "id": session.id,
        "job_id": session.job_id,
        "job": {"title": job.title if job else "", "company": job.company if job else "",
                "url": job.url if job else ""},
        "state": session.state,
        "phase": session.phase,
        "state_reason": session.state_reason,
        "last_checkpoint_failure": session.last_checkpoint_failure,
        "portal_type": session.portal_type,
        "portal_domain": session.portal_domain,
        "expected_host": session.expected_host,
        "employer_bound": bool(session.employer_fingerprint),
        "application_identity_bound": bool(session.application_identity),
        "url_bound": bool(session.url_fingerprint),
        "pause": {"kind": session.pause_kind or "", "reason": session.pause_reason or ""},
        "progress": session.progress or {},
        "checkpoint": {
            "fields": {
                name: {"status": entry.get("status"), "classification": entry.get("classification"),
                       "profile_key": entry.get("profile_key")}
                for name, entry in fields.items() if isinstance(entry, dict)
            },
            "completed_fields": already_completed_fields(session),
            "steps": len(checkpoint.get("steps") or []),
            "answer_fields": sorted(
                name for name, entry in fields.items()
                if isinstance(entry, dict) and entry.get("status") == "answered"
            ),
        },
        # What the run actually did, in order. This is the "record everything for
        # reference" half of Assisted Apply: hosts, control labels, field names
        # and counters — never a value, a code or page text.
        "flow": {
            "journal": flow_journal(session),
            "progress": flow_progress(session),
        },
        "persistence": {
            "enabled": bool(session.user_id and session.storage_state_enc),
            "saved_at": _iso(session.storage_state_saved_at),
            "expires_at": _iso(session.storage_state_expires_at),
            "purged_at": _iso(session.storage_state_purged_at),
        },
        "handoff": {"active": bool(session.handoff_token_hash),
                    "expires_at": _iso(session.handoff_expires_at),
                    "action_id": session.handoff_action_id},
        # The session's *visible* browser window, if one is still open for the
        # human to work in. Process-local state, reported as a fact — never a
        # driver, a loop or a URL.
        "window": live_browser.status(session.id),
        "screenshots": list(session.screenshots or []),
        "resumed_count": session.resumed_count,
        "last_activity_at": _iso(session.last_activity_at),
        "expires_at": _iso(session.expires_at),
        "ended_at": _iso(session.ended_at),
        "created_at": _iso(session.created_at),
        "actions": [action_public(a, session=session, job=job) for a in (actions or [])],
    }


def session_detail(db: Session, *, session: ApplicationSession) -> Dict[str, Any]:
    job = db.query(Job).filter(Job.id == session.job_id).first()
    actions = (
        db.query(ApplicationAction)
        .filter(ApplicationAction.session_id == session.id)
        .order_by(ApplicationAction.id.desc())
        .limit(50)
        .all()
    )
    return session_public(session, actions=actions, job=job)


def list_sessions(db: Session, user: User, *, include_closed: bool = False) -> List[Dict[str, Any]]:
    sweep_expired_sessions(db, user_id=user.id)
    query = db.query(ApplicationSession).filter(ApplicationSession.user_id == user.id)
    if not include_closed:
        query = query.filter(ApplicationSession.state.notin_(APPLICATION_SESSION_TERMINAL_STATES))
    sessions = query.order_by(ApplicationSession.created_at.desc()).limit(50).all()
    job_ids = [s.job_id for s in sessions]
    jobs = {j.id: j for j in db.query(Job).filter(Job.id.in_(job_ids)).all()} if job_ids else {}
    out = []
    for session in sessions:
        actions = pending_actions(db, user.id, session_id=session.id)
        out.append(session_public(session, actions=actions, job=jobs.get(session.job_id)))
    return out


def session_stats(db: Session, user_id: int) -> Dict[str, Any]:
    """Counters for the sessions surface (no content, no identifiers)."""
    rows = (
        db.query(ApplicationSession.state, func.count(ApplicationSession.id))
        .filter(ApplicationSession.user_id == user_id)
        .group_by(ApplicationSession.state)
        .all()
    )
    by_state = {state: int(count) for state, count in rows}
    return {
        "by_state": by_state,
        "live": sum(count for state, count in by_state.items()
                    if state not in APPLICATION_SESSION_TERMINAL_STATES),
        "awaiting_user": int(by_state.get("awaiting_user", 0)),
        "pending_actions": len(pending_actions(db, user_id)),
        "by_action_kind": {
            kind: int(count)
            for kind, count in (
                db.query(ApplicationAction.kind, func.count(ApplicationAction.id))
                .filter(ApplicationAction.user_id == user_id, ApplicationAction.status == "pending")
                .group_by(ApplicationAction.kind)
                .all()
            )
        },
    }


def planned_values(job: Job, *, answers: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """
    Values this session may fill, from the job's stored plan plus user answers.

    Credential values are dropped on the way out: the plan may carry a decrypted
    vault password (it is what the legacy autofill used), and this workflow
    never types a plan's password — when the user has opted in to account
    creation, the pass sources a fresh credential from the vault for the page
    it is on, at typing time, instead of taking one from a possibly stale plan.
    Field *values* from a plan are user data, so the caller keeps them in
    memory only — they are never written to a session row.
    """
    values: Dict[str, Any] = {}
    plan = ((job.extra or {}).get("autofill_plan") or {}).get("fields") or []
    for entry in plan:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("type") == "password" or entry.get("value_source") == "vault":
            continue
        value = entry.get("value")
        if value in (None, ""):
            continue
        if entry.get("name"):
            values[str(entry["name"])] = value
        if entry.get("profile_key"):
            values.setdefault(str(entry["profile_key"]), value)
    for name, value in (answers or {}).items():
        values[str(name)] = value
    return values


__all__ = [
    "ALLOWED_TRANSITIONS",
    "FLOW_PAYLOAD_LIMIT",
    "OBSERVATION_CONFIRMATIONS",
    "CHECKPOINT_STATUSES",
    "FIELD_CHECKPOINT_KEYS",
    "BROWSER_STEP_KINDS",
    "FLOW_STEP_EVENTS",
    "FLOW_STEP_KEYS",
    "HANDOFF_ACTION_KINDS",
    "LIVE_SESSION_STATES",
    "SessionError",
    "SessionExpired",
    "SessionNotFound",
    "SessionPolicy",
    "CheckpointFailed",
    "SubmissionRefused",
    "action_public",
    "action_queue",
    "already_completed_fields",
    "application_identity",
    "cancel_session",
    "canonical_url",
    "checkpoint_failures",
    "complete_action",
    "consume_handoff_token",
    "effective_policy",
    "employer_fingerprint",
    "existing_submission",
    "expire_session",
    "fill_instructions",
    "finalize_submission",
    "fingerprint_value",
    "flow_journal",
    "flow_progress",
    "handoff_token_valid",
    "host_of",
    "is_expired",
    "is_session_recording_open",
    "issue_handoff",
    "list_sessions",
    "learn_answer",
    "learned_values",
    "learning_host",
    "live_session_for_job",
    "live_sessions",
    "load_storage_state",
    "own_session",
    "pause_for_user",
    "pause_session",
    "pending_actions",
    "persist_storage_state",
    "planned_values",
    "profile_dir",
    "progress_snapshot",
    "purge_storage_state",
    "reauthenticate",
    "record_browser_input",
    "record_fills",
    "record_flow_step",
    "record_observation",
    "record_screenshot",
    "record_user_answers",
    "reserve_submission",
    "resume_session",
    "sanitize_action_payload",
    "sanitize_observation",
    "screenshot_decision",
    "session_detail",
    "session_public",
    "session_stats",
    "start_session",
    "submission_decision",
    "submission_idempotency_key",
    "sweep_expired_sessions",
    "touch",
    "transition",
    "url_fingerprint",
]
