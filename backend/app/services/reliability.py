"""
Reliability contract for the background workflows.

Every unit of background work in this product — onboarding extraction, a
discovery run, a match, an artifact, a browser pass, a submission — ends in one
of four places, and *which* one is a decision this module makes once instead of
every handler guessing::

    transient outage   -> ``paused``    (re-queued by the watchdog, no attempt spent)
    retryable failure  -> ``queued``    (backoff, ``attempts`` grows)
    permanent failure  -> ``dead``      (visible to the user, manual retry only)
    needs the user     -> ``needs_input`` (parked, **never** retried, and expires)

Why a single classifier
-----------------------
Before this, the worker had one rule — "is it an AI error?" — and everything
else fell through to ``fail(retryable=True)``. That is the wrong default in both
directions: a ``RuntimeError("job_missing")`` retried three times is noise that
hides a real bug, and a genuine connection reset dead-lettered on the first try
is a lost run. It also meant the *reason* was only ever free text in
``item.error``, so nothing could answer "what fraction of failures are
permanent?" — the first question an operator asks during an incident.

Cardinality
-----------
:func:`classify_failure` returns a ``code`` from :data:`FAILURE_CODES` and
nothing else: an exception message is caller-controlled text (a provider can put
anything in one) and a label per distinct message is an unbounded series. Every
metric written by this module passes its labels through
:func:`bounded_label`, which consults :data:`METRIC_CATALOG` and collapses
anything unexpected to ``other``. See ``docs/OBSERVABILITY.md`` for the full
catalog and the queries these metrics are meant to answer.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc, observe
from app.core.redaction import redact_text
from app.models.models import (
    ApplicationAction,
    Job,
    PipelineJob,
    UserInputRequest,
)

log = get_logger("app.reliability")

# --------------------------------------------------------------------------- #
# The four outcomes
# --------------------------------------------------------------------------- #
#: A transient outage: the work is fine, the dependency is not. Costs no attempt.
KIND_TRANSIENT = "transient"
#: Retrying this cannot help (a bug, a deleted row, a rejected credential).
KIND_PERMANENT = "permanent"
#: Only a human can unblock it. Never retried, always expires.
KIND_USER_ACTION = "user_action"
#: Not recognised. Treated as retryable because the budget is bounded.
KIND_UNKNOWN = "unknown"

RETRY_KINDS: Tuple[str, ...] = (KIND_TRANSIENT, KIND_PERMANENT, KIND_USER_ACTION, KIND_UNKNOWN)

#: What the queue does with the decision.
OUTCOME_PAUSE = "pause"
OUTCOME_RETRY = "retry"
OUTCOME_DEAD = "dead"
OUTCOME_INPUT = "needs_input"

#: The bounded ``code`` label. Anything not in this tuple is reported as
#: ``other`` — see :func:`bounded_label`.
FAILURE_CODES: Tuple[str, ...] = (
    "ai_transient",       # provider timeout / 429 / 5xx / breaker open / budget
    "ai_blocked",         # missing or invalid key, quota exhausted, refused
    "timeout",            # our own deadline (asyncio.wait_for, httpx timeout)
    "network",            # transport error, DNS, connection reset
    "connection",         # refused / dropped mid-request
    "rate_limited",       # upstream 429 we did not absorb
    "upstream",           # 5xx / bad gateway from a source or provider
    "database",           # OperationalError, disconnect, pool exhaustion
    "auth",               # 401/403 from a source: credentials rejected
    "parse",              # malformed upstream payload
    "validation",         # our own schema/guardrail rejected the data
    "not_found",          # the row this item references is gone
    "quota",              # a plan or usage limit, not an outage
    "policy",             # automation policy / consent refused the action
    "duplicate",          # lost an idempotency race
    "cancelled",          # the user stopped it
    "bug",                # TypeError/AttributeError/KeyError: our fault
    "unsupported",        # a gated source, a missing optional dependency
    "user_action",        # parked for the human (CAPTCHA, MFA, a missing answer)
    "unknown",            # unrecognised exception type
    "other",              # a code we did not expect — watch this label
)

#: ``SourceError.code`` → classification. Adapters already classify their own
#: failures; this maps that vocabulary onto the queue's.
_SOURCE_CODE_KINDS: Dict[str, Tuple[str, str]] = {
    "rate_limited": (KIND_TRANSIENT, "rate_limited"),
    "upstream": (KIND_TRANSIENT, "upstream"),
    "network": (KIND_TRANSIENT, "network"),
    "timeout": (KIND_TRANSIENT, "timeout"),
    "server_error": (KIND_TRANSIENT, "upstream"),
    "temporarily_unavailable": (KIND_TRANSIENT, "upstream"),
    "auth": (KIND_PERMANENT, "auth"),
    "gated": (KIND_PERMANENT, "unsupported"),
    "unavailable": (KIND_PERMANENT, "unsupported"),
    "not_found": (KIND_PERMANENT, "not_found"),
    "parse": (KIND_PERMANENT, "parse"),
    "schema": (KIND_PERMANENT, "parse"),
    "blocked": (KIND_PERMANENT, "policy"),
    "robots": (KIND_PERMANENT, "policy"),
    "quota": (KIND_PERMANENT, "quota"),
    "budget": (KIND_PERMANENT, "quota"),
    "unknown": (KIND_UNKNOWN, "unknown"),
}

#: Handler-raised messages that mean "this work can never succeed". Handlers use
#: short sentinels rather than exception classes (``raise
#: RuntimeError("job_missing")``), so the sentinels are the classification input.
_PERMANENT_MESSAGE_TOKENS: Tuple[str, ...] = (
    "user_missing", "job_missing", "email_missing", "resume_missing",
    "session_missing", "packet_missing", "document_missing", "no handler",
    "unknown pipeline", "not_found", "deleted",
)

#: Message fragments that mean "wait for the human".
_USER_ACTION_TOKENS: Tuple[str, ...] = (
    "needs_input", "needs input", "user_action", "captcha", "mfa", "otp required",
    "requires_login", "missing_fields", "approval_required", "awaiting_user",
)


# --------------------------------------------------------------------------- #
# Exceptions a handler may raise to state its own intent
# --------------------------------------------------------------------------- #
class JobError(Exception):
    """Base for background-work failures that carry a machine-readable code."""

    #: One of :data:`FAILURE_CODES`.
    code: str = "unknown"
    #: One of :data:`RETRY_KINDS`.
    kind: str = KIND_UNKNOWN

    def __init__(self, message: str = "", *, code: str = "", kind: str = "") -> None:
        super().__init__(message or code or self.code)
        if code:
            self.code = code
        if kind:
            self.kind = kind


class PermanentJobError(JobError):
    """Retrying cannot help: dead-letter at once and tell the user why."""

    code = "validation"
    kind = KIND_PERMANENT


class UserActionRequired(JobError):
    """
    The work is parked until a human acts.

    The queue row becomes ``needs_input`` and is **never** retried: an
    aggressive retry loop on a CAPTCHA is both useless and, against a portal
    that rate-limits bot behaviour, actively harmful. ``action_kind`` is a
    ``USER_ACTION_KINDS`` value so the metric label stays bounded.
    """

    code = "user_action"
    kind = KIND_USER_ACTION

    def __init__(self, message: str = "", *, code: str = "user_action", kind: str = "",
                 action_kind: str = "review_required", result: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, code=code, kind=kind or KIND_USER_ACTION)
        self.action_kind = action_kind
        self.result = result or {}


@dataclass(frozen=True)
class FailureDecision:
    """What the queue should do with one exception, and how it is reported."""

    outcome: str                 # OUTCOME_*
    kind: str                    # KIND_*
    code: str                    # FAILURE_CODES
    message: str                 # redacted, one line, safe to store and show
    action_kind: str = ""        # USER_ACTION_KINDS value when outcome is needs_input
    result: Optional[Dict[str, Any]] = None

    @property
    def retryable(self) -> bool:
        return self.outcome in (OUTCOME_PAUSE, OUTCOME_RETRY)


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def _bounded_code(code: str) -> str:
    return code if code in FAILURE_CODES else "other"


def _safe_message(exc: BaseException, *, limit: int = 300) -> str:
    """``Type: message``, control-chars scrubbed, secrets and PII masked."""
    raw = f"{type(exc).__name__}: {exc}".strip()
    text = " ".join(str(raw or "unknown error").split())
    return str(redact_text(text))[:limit]


def classify_failure(exc: BaseException, *, pipeline: str = "") -> FailureDecision:
    """
    Decide how the queue should treat *exc*.

    Order matters: an exception that *states* its intent (our own
    :class:`JobError`, an AI outage the gateway already classified) wins over a
    structural guess from its type, and an unrecognised type is retryable
    because the failure budget bounds the damage while a wrong ``permanent``
    would silently drop the user's run.
    """
    # 1. The handler said so explicitly.
    if isinstance(exc, UserActionRequired):
        return FailureDecision(OUTCOME_INPUT, KIND_USER_ACTION, _bounded_code(exc.code),
                               _safe_message(exc), action_kind=exc.action_kind, result=exc.result)
    if isinstance(exc, PermanentJobError):
        return FailureDecision(OUTCOME_DEAD, KIND_PERMANENT, _bounded_code(exc.code),
                               _safe_message(exc))
    if isinstance(exc, JobError):
        kind = exc.kind if exc.kind in RETRY_KINDS else KIND_UNKNOWN
        return FailureDecision(_outcome_for(kind), kind, _bounded_code(exc.code), _safe_message(exc))

    # 2. The AI gateway already knows the difference between an outage and a
    #    blocked account — and only the first one is worth waiting out.
    from app.services.ai_client import is_ai_error, is_transient_ai_error

    if is_ai_error(exc):
        if is_transient_ai_error(exc):
            return FailureDecision(OUTCOME_PAUSE, KIND_TRANSIENT, "ai_transient", _safe_message(exc))
        return FailureDecision(OUTCOME_DEAD, KIND_PERMANENT, "ai_blocked", _safe_message(exc))

    # 3. A source adapter's own classification (``SourceError.code``).
    code = getattr(exc, "code", "")
    if code:
        mapped = _SOURCE_CODE_KINDS.get(str(code).lower())
        if mapped:
            kind, failure_code = mapped
            return FailureDecision(_outcome_for(kind), kind, failure_code, _safe_message(exc))

    # 4. Structural classification by exception type.
    if isinstance(exc, asyncio.TimeoutError):
        return FailureDecision(OUTCOME_RETRY, KIND_TRANSIENT, "timeout", _safe_message(exc))
    if isinstance(exc, (TimeoutError,)):
        return FailureDecision(OUTCOME_RETRY, KIND_TRANSIENT, "timeout", _safe_message(exc))
    if isinstance(exc, (ConnectionError,)):
        return FailureDecision(OUTCOME_RETRY, KIND_TRANSIENT, "connection", _safe_message(exc))
    try:  # httpx is a hard dependency, but never fail classification over an import
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return FailureDecision(OUTCOME_RETRY, KIND_TRANSIENT, "timeout", _safe_message(exc))
        if isinstance(exc, httpx.HTTPStatusError):
            status = int(getattr(exc.response, "status_code", 0) or 0)
            if status == 429:
                return FailureDecision(OUTCOME_RETRY, KIND_TRANSIENT, "rate_limited", _safe_message(exc))
            if status in (401, 403):
                return FailureDecision(OUTCOME_DEAD, KIND_PERMANENT, "auth", _safe_message(exc))
            if 500 <= status < 600:
                return FailureDecision(OUTCOME_RETRY, KIND_TRANSIENT, "upstream", _safe_message(exc))
            return FailureDecision(OUTCOME_DEAD, KIND_PERMANENT, "validation", _safe_message(exc))
        if isinstance(exc, httpx.TransportError):
            return FailureDecision(OUTCOME_RETRY, KIND_TRANSIENT, "network", _safe_message(exc))
    except ImportError:  # pragma: no cover - httpx is pinned in requirements.txt
        pass
    if isinstance(exc, (DisconnectionError, InterfaceError, OperationalError)):
        return FailureDecision(OUTCOME_RETRY, KIND_TRANSIENT, "database", _safe_message(exc))
    if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
        return FailureDecision(OUTCOME_RETRY, KIND_TRANSIENT, "database", _safe_message(exc))
    if isinstance(exc, SQLAlchemyError):
        # A constraint violation is a duplicate we lost the race for or a bug —
        # neither is fixed by running the same statement again.
        return FailureDecision(OUTCOME_DEAD, KIND_PERMANENT, "duplicate"
                               if "IntegrityError" in type(exc).__name__ else "database",
                               _safe_message(exc))

    # 5. Sentinels in the message (handlers raise RuntimeError("job_missing")).
    lowered = f"{type(exc).__name__}: {exc}".lower()
    if any(token in lowered for token in _PERMANENT_MESSAGE_TOKENS):
        return FailureDecision(OUTCOME_DEAD, KIND_PERMANENT, "not_found", _safe_message(exc))
    if any(token in lowered for token in _USER_ACTION_TOKENS):
        return FailureDecision(OUTCOME_INPUT, KIND_USER_ACTION, "user_action", _safe_message(exc),
                               action_kind="review_required")
    if isinstance(exc, ValueError):
        # A ValueError from a handler is either a user-fixable input problem or
        # a programming error. Both are permanent: re-running the identical
        # payload produces the identical rejection.
        return FailureDecision(OUTCOME_DEAD, KIND_PERMANENT, "validation", _safe_message(exc))
    if isinstance(exc, (TypeError, AttributeError, KeyError, IndexError, NameError)):
        return FailureDecision(OUTCOME_DEAD, KIND_PERMANENT, "bug", _safe_message(exc))
    if isinstance(exc, (NotImplementedError, ImportError, ModuleNotFoundError)):
        return FailureDecision(OUTCOME_DEAD, KIND_PERMANENT, "unsupported", _safe_message(exc))
    if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
        # Never swallow these into a retry: a shutdown is not a failure, and a
        # cancelled task must stay cancelled so the worker can stop.
        return FailureDecision(OUTCOME_DEAD, KIND_PERMANENT, "cancelled", _safe_message(exc))

    # 6. Unknown: retryable, bounded by ``max_attempts``.
    return FailureDecision(OUTCOME_RETRY, KIND_UNKNOWN, "unknown", _safe_message(exc))


def _outcome_for(kind: str) -> str:
    return {
        KIND_TRANSIENT: OUTCOME_RETRY,
        KIND_PERMANENT: OUTCOME_DEAD,
        KIND_USER_ACTION: OUTCOME_INPUT,
    }.get(kind, OUTCOME_RETRY)


# --------------------------------------------------------------------------- #
# Metric catalog — the cardinality contract, in one place
# --------------------------------------------------------------------------- #
#: Every metric this change adds or extends, with the *only* label values it may
#: carry. ``docs/OBSERVABILITY.md`` is generated from this table by hand and
#: ``tests/test_background_reliability.py`` asserts the code agrees with it, so
#: a new label value has to be added here deliberately.
#:
#: No metric in this project carries a user id, a job id, a URL, a host or an
#: exception message: a series per user is a series per signup, and the registry
#: keeps series for the life of the process.
METRIC_CATALOG: Dict[str, Dict[str, Any]] = {
    "jobhunter_job_failures_total": {
        "type": "counter",
        "help": "Background-job failures by pipeline, retry class and bounded reason.",
        "labels": {
            "pipeline": ("discovery", "application", "email", "funding", "ai", "extraction",
                         "browser_session"),
            "kind": RETRY_KINDS,
            "code": FAILURE_CODES,
        },
    },
    "jobhunter_onboarding_total": {
        "type": "counter",
        "help": "Onboarding steps started, completed and failed.",
        "labels": {
            "stage": ("session", "upload", "extraction", "profile_review"),
            "outcome": ("started", "completed", "failed", "blocked", "skipped"),
        },
    },
    "jobhunter_resume_extraction_seconds": {
        "type": "histogram",
        "help": "Resume extraction wall time, including the model call.",
        "labels": {"outcome": ("ok", "blocked", "failed", "duplicate")},
    },
    "jobhunter_discovery_run_seconds": {
        "type": "histogram",
        "help": "Discovery run wall time, end to end.",
        "labels": {"result": ("ok", "empty", "partial", "failed")},
    },
    "jobhunter_discovery_jobs_found_total": {
        "type": "counter",
        "help": "Jobs persisted per discovery run, by board and disposition.",
        "labels": {
            "source": "source_ids",
            "disposition": ("new", "updated", "expired", "duplicate"),
        },
    },
    "jobhunter_source_fetch_outcomes_total": {
        "type": "counter",
        "help": "Job-board fetch outcomes — the numerator of source failure rate.",
        "labels": {"source": "source_ids", "outcome": ("ok", "empty", "error")},
    },
    "jobhunter_search_provider_requests_total": {
        "type": "counter",
        "help": "Paid search-provider calls by provider and outcome.",
        "labels": {
            "provider": ("brave", "serper", "serpapi", "other"),
            "outcome": ("success", "error", "cached", "coalesced", "budget_exceeded", "skipped"),
        },
    },
    "jobhunter_search_provider_duration_seconds": {
        "type": "histogram",
        "help": "Search-provider latency, excluding cache hits.",
        "labels": {"provider": ("brave", "serper", "serpapi", "other")},
    },
    "jobhunter_search_provider_cost_microusd_total": {
        "type": "counter",
        "help": "Estimated search spend in micro-USD, by provider.",
        "labels": {"provider": ("brave", "serper", "serpapi", "other")},
    },
    "jobhunter_match_generation_seconds": {
        "type": "histogram",
        "help": "Match generation wall time by scoring path.",
        "labels": {"scoring_path": ("deterministic", "hybrid", "ai", "cached",
                                    "insufficient_data")},
    },
    "jobhunter_high_fit_matches_total": {
        "type": "counter",
        "help": "Matches generated per fit band, by score provenance.",
        "labels": {
            "band": ("strong", "good", "possible", "weak", "unknown"),
            "source": ("ai", "preliminary", "rejected", "insufficient_data", "other"),
        },
    },
    "jobhunter_artifact_generation_total": {
        "type": "counter",
        "help": "Generated artifacts (resumes, packets, cover letters, reports).",
        "labels": {
            "artifact": ("resume_docx", "resume_pdf", "packet", "cover_letter", "report",
                         "interview_prep"),
            "outcome": ("ok", "rejected", "failed", "cached"),
        },
    },
    "jobhunter_artifact_generation_seconds": {
        "type": "histogram",
        "help": "Artifact generation wall time.",
        "labels": {"artifact": ("resume_docx", "resume_pdf", "packet", "cover_letter", "report",
                                "interview_prep")},
    },
    "jobhunter_application_prep_total": {
        "type": "counter",
        "help": "Application preparation outcomes.",
        "labels": {"outcome": ("ready", "needs_input", "failed", "duplicate", "blocked")},
    },
    "jobhunter_browser_session_passes_seconds": {
        "type": "histogram",
        "help": "Assisted browser pass wall time.",
        "labels": {"outcome": ("completed", "awaiting_user", "failed", "noop")},
    },
    "jobhunter_user_action_pauses_total": {
        "type": "counter",
        "help": "Times a run stopped and handed a step to the human.",
        "labels": {"kind": "user_action_kinds"},
    },
    "jobhunter_user_action_outcomes_total": {
        "type": "counter",
        "help": "How user-action-required items ended.",
        "labels": {"kind": "user_action_kinds",
                   "outcome": ("completed", "expired", "cancelled", "acknowledged")},
    },
    "jobhunter_user_action_wait_seconds": {
        "type": "histogram",
        "help": "How long a user-action-required item waited before it ended.",
        "labels": {"kind": "user_action_kinds",
                   "outcome": ("completed", "expired", "cancelled")},
    },
    "jobhunter_auto_submit_total": {
        "type": "counter",
        "help": "Automatic submission decisions and results.",
        "labels": {
            "outcome": ("success", "failure", "dry_run", "refused", "duplicate"),
            "channel": ("automation", "assisted", "assisted_dry_run", "manual", "other"),
        },
    },
    "jobhunter_autofill_failures_total": {
        "type": "counter",
        "help": "Autofill runs that did not complete, by bounded reason.",
        "labels": {"reason": ("blocked", "navigation", "no_form", "login_required", "captcha",
                              "timeout", "driver_error", "unavailable", "error", "other")},
    },
    "jobhunter_dedupe_hits_total": {
        "type": "counter",
        "help": "Duplicate work prevented, by the boundary that caught it.",
        "labels": {"scope": ("queue", "submission", "action", "search_cache", "notification",
                             "upload", "extraction", "event")},
    },
    "jobhunter_interview_events_total": {
        "type": "counter",
        "help": "Interview lifecycle events.",
        "labels": {
            "event": ("scheduled", "rescheduled", "completed", "cancelled", "prep_generated",
                      "feedback_generated"),
            "outcome": ("ok", "failed", "skipped"),
        },
    },
    "jobhunter_report_generation_seconds": {
        "type": "histogram",
        "help": "Outcome-report build time.",
        "labels": {"kind": ("weekly", "window", "export_csv", "summary")},
    },
    "jobhunter_reports_total": {
        "type": "counter",
        "help": "Outcome reports generated.",
        "labels": {"kind": ("weekly", "window", "export_csv", "summary"),
                   "outcome": ("ok", "empty", "failed")},
    },
    "jobhunter_notifications_total": {
        "type": "counter",
        "help": "In-app notifications written, by kind and outcome.",
        "labels": {"kind": "notification_kinds", "outcome": ("created", "duplicate", "failed")},
    },
    "jobhunter_user_action_expiry_total": {
        "type": "counter",
        "help": "User-action-required states closed by the expiry sweep.",
        "labels": {"scope": ("queue", "input_request", "action", "session")},
    },
}

#: Label vocabularies drawn from the contracts module so a metric cannot drift
#: from the state machine it describes.
def _dynamic_vocabularies() -> Dict[str, Tuple[str, ...]]:
    from app.contracts.vocabulary import NOTIFICATION_KINDS, USER_ACTION_KINDS
    from app.services.sources.adapters import ADAPTERS

    return {
        "source_ids": tuple(sorted(set(ADAPTERS))) + ("search", "demo", "import", "manual", "other"),
        "user_action_kinds": tuple(USER_ACTION_KINDS) + ("other",),
        "notification_kinds": tuple(NOTIFICATION_KINDS) + ("other",),
    }


_VOCAB_CACHE: Optional[Dict[str, Tuple[str, ...]]] = None


def allowed_values(metric: str, label: str) -> Tuple[str, ...]:
    """The permitted values for one label of one metric (see :data:`METRIC_CATALOG`)."""
    global _VOCAB_CACHE
    spec = (METRIC_CATALOG.get(metric) or {}).get("labels") or {}
    allowed = spec.get(label)
    if allowed is None:
        return ()
    if isinstance(allowed, str):
        if _VOCAB_CACHE is None:
            _VOCAB_CACHE = _dynamic_vocabularies()
        return _VOCAB_CACHE.get(allowed, ("other",))
    return tuple(allowed)


def bounded_label(metric: str, label: str, value: Any, *, default: str = "other") -> str:
    """
    Coerce *value* into the metric's declared label vocabulary.

    This is the runtime half of the cardinality rule: a caller that passes a
    user id, a raw source name or an exception message gets ``other`` and shows
    up in the catalog as an unmapped value, instead of minting a permanent
    series. ``default`` is returned for an undeclared metric/label pair so a
    typo is visible rather than silently unbounded.
    """
    allowed = allowed_values(metric, label)
    if not allowed:
        return default
    text = str(value if value is not None else "").strip().lower()
    return text if text in set(allowed) else default


def bounded_labels(metric: str, labels: Dict[str, Any]) -> Dict[str, str]:
    """Apply :func:`bounded_label` to every label of one metric."""
    return {name: bounded_label(metric, name, value) for name, value in (labels or {}).items()}


def count(metric: str, value: float = 1.0, **labels: Any) -> None:
    """Increment a catalogued counter with its labels coerced to the vocabulary."""
    inc(metric, value, **bounded_labels(metric, labels))


def duration(metric: str, seconds: float, **labels: Any) -> None:
    """Observe a duration on a catalogued histogram, labels coerced as above."""
    observe(metric, max(0.0, float(seconds)), bounded_labels(metric, labels))


class Span:
    """
    ``with Span("jobhunter_match_generation_seconds", path="hybrid"): ...``

    Records on both the success and the exception path: a duration that is only
    recorded when the work succeeds reports the latency of the easy cases.
    """

    def __init__(self, metric: str, **labels: Any) -> None:
        self.metric = metric
        self.labels = labels
        self._started = 0.0
        self.elapsed = 0.0

    def __enter__(self) -> "Span":
        self._started = time.perf_counter()
        return self

    def __exit__(self, *_exc: object) -> None:
        # ``None``, not ``False``: returning a bool would tell the type checker
        # this context manager may swallow exceptions. It never does.
        self.elapsed = time.perf_counter() - self._started
        duration(self.metric, self.elapsed, **self.labels)


# --------------------------------------------------------------------------- #
# Applying a decision to the queue
# --------------------------------------------------------------------------- #
def record_failure(pipeline: str, decision: FailureDecision) -> None:
    """The one failure metric, labelled by pipeline, retry class and reason."""
    count("jobhunter_job_failures_total",
          pipeline=pipeline, kind=decision.kind, code=decision.code)


def apply_failure(db: Session, item: PipelineJob, exc: BaseException) -> str:
    """
    Turn one handler exception into the right queue transition.

    Returns the resulting status (``paused`` / ``queued`` / ``dead`` /
    ``needs_input``). The classification is stored next to the error text so a
    page — and an operator reading ``GET /api/ops/queue/dead`` — can tell a
    retryable hiccup from a permanent stop without re-reading the traceback.
    """
    from app.services.job_queue import fail, needs_input, pause

    decision = classify_failure(exc, pipeline=item.pipeline)
    record_failure(item.pipeline, decision)

    payload = dict(item.payload or {})
    payload["failure"] = {
        "kind": decision.kind,
        "code": decision.code,
        "outcome": decision.outcome,
        "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    item.payload = payload

    if decision.outcome == OUTCOME_PAUSE:
        outcome = pause(db, item, decision.message)
    elif decision.outcome == OUTCOME_INPUT:
        needs_input(db, item, reason=decision.message,
                    result={**(decision.result or {}), "status": "needs_input",
                            "action_kind": decision.action_kind, "code": decision.code})
        outcome = "needs_input"
    else:
        outcome = fail(db, item, decision.message, retryable=decision.outcome == OUTCOME_RETRY)

    log.warning("item %s (%s) -> %s [%s/%s]", item.id, item.pipeline, outcome,
                decision.kind, decision.code)
    return outcome


# --------------------------------------------------------------------------- #
# Duplicate execution
# --------------------------------------------------------------------------- #
def note_dedupe(scope: str, *, count_value: float = 1.0) -> None:
    """
    Count one duplicate that a boundary refused to execute twice.

    Safe re-execution is only *verifiable* if the refusals are counted: a dedupe
    key that silently stops working looks exactly like a quiet week until the
    second application goes out.
    """
    count("jobhunter_dedupe_hits_total", value=count_value, scope=scope)


# --------------------------------------------------------------------------- #
# User-action-required states: park, and expire
# --------------------------------------------------------------------------- #
def user_action_ttl_seconds() -> int:
    return max(60, int(settings.user_action_ttl_minutes) * 60)


def user_action_deadline(item: PipelineJob, *, now: Optional[datetime] = None) -> datetime:
    """
    When a ``needs_input`` row expires.

    The deadline is written into the row when it is parked
    (:func:`app.services.job_queue.needs_input`), so a restart, a redeploy or a
    second worker reads the same answer. A row parked before this field existed
    falls back to ``updated_at`` plus the current TTL — which is *longer* than
    the original wait, never shorter, so upgrading cannot expire a run the user
    was still answering.
    """
    moment = now or datetime.utcnow()
    raw = ((item.payload or {}).get("user_action") or {}).get("expires_at")
    parsed = _parse_iso(raw)
    return parsed or ((item.updated_at or item.created_at or moment) + timedelta(seconds=user_action_ttl_seconds()))


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _notify_expiry(db: Session, user_id: int, kind: str, title: str, body: str, link: str) -> None:
    """One in-app notification per expired row — the user must hear about it.

    A run that quietly waits forever is indistinguishable from a product that
    stopped working; the notification is the difference. ``create_notification``
    is best-effort by design and never raises into the sweep.
    """
    try:
        from app.api.routers.notifications import create_notification

        # ``create_notification`` counts the delivery metric itself — counting
        # here too would double every expiry notification.
        create_notification(db, int(user_id), kind, title[:200], body=body[:2000],
                            link=link, meta={"expired_by": "user_action_sweep"})
    except Exception as exc:  # pragma: no cover - notification must not break the sweep
        log.warning("expiry notification failed for user %s: %s", user_id, exc)


def expire_user_actions(db: Session, *, now: Optional[datetime] = None,
                        limit: Optional[int] = None) -> Dict[str, int]:
    """
    Close every user-action-required state whose window has passed.

    Called by the worker's maintenance pass (:class:`app.worker.Worker`) and
    idempotent by construction: each scope only selects rows still open, and
    every transition is terminal, so a second pass — or two workers running it
    at once — finds nothing to do. Expiry never *retries*: a stale CAPTCHA or a
    fortnight-old question is not a thing to run again, and the queue row is
    finished with a reason the user can see instead.

    Returns per-scope counts for the caller's log line and for tests.
    """
    from app.services.job_queue import close_expired

    moment = now or datetime.utcnow()
    cap = int(limit or settings.user_action_sweep_limit)
    expired: Dict[str, int] = {"queue": 0, "input_request": 0, "action": 0, "session": 0}

    # 1. Queue rows parked for the user.
    parked = (
        db.query(PipelineJob)
        .filter(PipelineJob.status == "needs_input")
        .order_by(PipelineJob.updated_at.asc())
        .limit(cap)
        .all()
    )
    for item in parked:
        if user_action_deadline(item, now=moment) > moment:
            continue
        payload = dict(item.payload or {})
        payload["failure"] = {"kind": KIND_USER_ACTION, "code": "user_action",
                              "outcome": "expired",
                              "at": moment.isoformat(timespec="seconds") + "Z"}
        item.payload = payload
        # ``close_expired``, not ``fail``: expiry is a decision, not a handler
        # failure, so it spends no failure budget and is never retried.
        close_expired(db, item,
                      "user_action_expired: the waiting period ended before the answer arrived")
        expired["queue"] += 1
        count("jobhunter_user_action_expiry_total", scope="queue")
        count("jobhunter_user_action_outcomes_total",
              kind=str((payload.get("user_action") or {}).get("kind") or "review_required"),
              outcome="expired")
        _notify_expiry(
            db, item.user_id, "automation_failed",
            f"A step waiting on you has expired ({item.pipeline})",
            "The run was closed because the question was not answered in time. "
            "Nothing was submitted — start it again whenever you are ready.",
            "/queues",
        )

    # 2. The question rows those runs point at.
    requests = (
        db.query(UserInputRequest)
        .filter(UserInputRequest.status == "pending")
        .order_by(UserInputRequest.created_at.asc())
        .limit(cap)
        .all()
    )
    ttl = timedelta(seconds=user_action_ttl_seconds())
    for request in requests:
        started = request.created_at or moment
        if started + ttl > moment:
            continue
        request.status = "expired"
        request.completed_at = moment
        expired["input_request"] += 1
        count("jobhunter_user_action_expiry_total", scope="input_request")
        count("jobhunter_user_action_outcomes_total", kind="review_required", outcome="expired")
        duration("jobhunter_user_action_wait_seconds",
                 (moment - started).total_seconds(), kind="review_required", outcome="expired")
    if expired["input_request"]:
        db.commit()

    # 3. Browser hand-off items (login / MFA / CAPTCHA / a field to answer).
    actions = (
        db.query(ApplicationAction)
        .filter(ApplicationAction.status.in_(("pending", "in_progress")),
                ApplicationAction.expires_at.isnot(None),
                ApplicationAction.expires_at <= moment)
        .order_by(ApplicationAction.created_at.asc())
        .limit(cap)
        .all()
    )
    for action in actions:
        action.status = "expired"
        action.completed_at = moment
        started = action.created_at or moment
        expired["action"] += 1
        count("jobhunter_user_action_expiry_total", scope="action")
        count("jobhunter_user_action_outcomes_total", kind=action.kind, outcome="expired")
        duration("jobhunter_user_action_wait_seconds", (moment - started).total_seconds(),
                 kind=action.kind, outcome="expired")
    if expired["action"]:
        db.commit()

    # 4. Session TTLs. Owned by the browser-session module (it also purges the
    #    persisted cookie jar), so the sweep delegates rather than duplicating.
    try:
        from app.services.browser_session import sweep_expired_sessions

        closed = sweep_expired_sessions(db, now=moment)
        if closed:
            expired["session"] = int(closed)
            count("jobhunter_user_action_expiry_total", value=float(closed), scope="session")
    except Exception as exc:  # pragma: no cover - optional browser dependency
        log.warning("session expiry sweep failed: %s: %s", type(exc).__name__, exc)

    total = sum(expired.values())
    if total:
        log.info("user-action expiry sweep closed %s state(s): %s", total,
                 {key: value for key, value in expired.items() if value})
    return expired


#: Autofill/submit result → the bounded ``reason`` label. The keys are matched
#: as substrings of the result's own ``code``/``reason``/``status`` text, in
#: this order, because the autofill layer reports free text (a navigation
#: refusal names the host) and a label may not carry it.
_AUTOFILL_REASON_TOKENS: Tuple[Tuple[str, str], ...] = (
    ("captcha", "captcha"),
    ("login_domain_mismatch", "navigation"),
    ("domain_mismatch", "navigation"),
    ("navigation", "navigation"),
    ("login", "login_required"),
    ("credential", "login_required"),
    ("ssrf", "blocked"),
    ("private", "blocked"),
    ("blocked", "blocked"),
    ("timeout", "timeout"),
    ("playwright", "driver_error"),
    ("browser", "driver_error"),
    ("driver", "driver_error"),
    ("no_form", "no_form"),
    ("form", "no_form"),
    ("unavailable", "unavailable"),
    ("error", "error"),
)


def autofill_failure_reason(result: Dict[str, Any]) -> str:
    """
    Count one autofill run that did not complete, under a bounded reason.

    Returns the reason it counted, so a caller can put the same word in a log
    line or a job event without deriving it twice.
    """
    payload = result if isinstance(result, dict) else {}
    haystack = " ".join(
        str(payload.get(key) or "") for key in ("code", "reason", "status", "error")
    ).lower()
    reason = "other"
    for token, mapped in _AUTOFILL_REASON_TOKENS:
        if token in haystack:
            reason = mapped
            break
    count("jobhunter_autofill_failures_total", reason=reason)
    return reason


def note_user_action_pause(kind: str) -> None:
    """A run stopped and handed a step to the human (CAPTCHA, MFA, a question)."""
    count("jobhunter_user_action_pauses_total", kind=kind)


def note_user_action_outcome(kind: str, outcome: str, *, waited_seconds: Optional[float] = None) -> None:
    """How a user-action-required item ended, and how long the user took."""
    count("jobhunter_user_action_outcomes_total", kind=kind, outcome=outcome)
    if waited_seconds is not None:
        duration("jobhunter_user_action_wait_seconds", waited_seconds, kind=kind, outcome=outcome)


def metric_names() -> Iterable[str]:
    """Every metric this module owns, for the docs and the drift test."""
    return sorted(METRIC_CATALOG)


def catalog_rows() -> List[Dict[str, Any]]:
    """The catalog flattened for ``docs/OBSERVABILITY.md``'s table."""
    rows: List[Dict[str, Any]] = []
    for name in metric_names():
        spec = METRIC_CATALOG[name]
        labels = {
            label: allowed_values(name, label)
            for label in (spec.get("labels") or {})
        }
        rows.append({"name": name, "type": spec["type"], "help": spec["help"], "labels": labels})
    return rows


def _register() -> None:
    """Publish the catalog to the metrics registry (HELP text + histogram types)."""
    try:
        from app.core.metrics import register_catalog

        register_catalog(METRIC_CATALOG)
    except Exception as exc:  # pragma: no cover - observability must never break work
        log.debug("metric catalog registration skipped: %s", exc)


_register()


def note_job_status(db: Session, job_id: Optional[int], user_id: int, status: str) -> None:
    """
    Best-effort job-status touch used by the expiry sweep's callers.

    Kept here (rather than in the sweep) so the sweep stays a pure queue
    operation; a job whose run expired keeps its status and simply shows no
    open question any more.
    """
    if not job_id:
        return
    row = db.query(Job).filter(Job.id == int(job_id), Job.user_id == int(user_id)).first()
    if row is not None and str(row.status) == "needs_input":
        row.status = "discovered"
        db.commit()
        log.info("job %s released from needs_input after its run expired", row.id)
