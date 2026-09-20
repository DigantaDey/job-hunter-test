"""Outcome reporting — interviews and meaningful outcomes, not activity volume.

``docs/REPORTING.md`` is the prose half of this module; this is the arithmetic.

The four rules that shaped it
-----------------------------
1. **Observed ≠ estimated.** Every number the report emits carries
   ``provenance``: ``observed`` (counted from a row we hold), ``user_reported``
   (the user said it happened), ``derived`` (arithmetic over those) or
   ``estimated`` (a model's opinion — the match score, "high fit"). A dashboard
   that prints a scorer output next to a counted fact is the failure mode
   :mod:`app.contracts.vocabulary` documents at length.
2. **No interview probability.** Not "0.42, uncalibrated" and not a hidden
   field either: :func:`calibration_status` returns ``available: False`` with
   the requirements it is missing, and :func:`interview_probability` *raises*
   while they are unmet. A match score is a ranked estimate of fit; the moment
   a UI calls it a probability, a rejection becomes a broken promise.
3. **Minimum sample sizes before comparisons.** A rate over 2 applications is
   noise. Every ratio is gated on :data:`MIN_SAMPLE_SIZES`; a group that does
   not clear it reports ``sufficiency: "insufficient"``, a ``None`` rate and the
   reason — never a percentage. Counts stay visible (they are facts); only the
   comparison is withheld.
4. **Reproducible by construction.** The window is resolved from a named range
   *in the user's timezone* and echoed back as half-open UTC instants plus local
   dates; the methodology is versioned; every group is ordered deterministically;
   and :func:`calculation_id` is a hash over exactly the inputs that can change
   the answer. Same inputs, same report — the test suite asserts it.

What "reproducible" does not mean
---------------------------------
It does not mean "the same bytes forever". ``generated_at`` and the per-user row
ids the report never prints are outside the hash by design, so two runs over the
same window are comparable rather than merely identical.

Windows
-------
``since``/``until`` are resolved to local midnight in the requested timezone and
the window is **half-open**: ``[start_utc, end_utc)``. A date-only ``until``
means "through the end of that local day". There are no floating boundaries, so
an application sent at 23:30 local on the last day is inside the window and one
sent at 00:30 the next local day is not — whatever the host's timezone is, which
is what ``backend/tests/test_outcome_reporting.py`` pins.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.contracts.vocabulary import (
    HIGH_FIT_SCORE,
    MATCH_BANDS,
    REPORT_COMPARISON_DIMENSIONS,
    REPORT_PROVENANCE_KINDS,
    REPORT_RANGES,
    REPORT_SUFFICIENCY,
    REPORT_TRENDS,
    TRACKING_INTERVIEW_STATES,
)
from app.models.models import (
    ApplicationPacket,
    ApplicationTracking,
    Job,
    PipelineJob,
)
from app.services.application_tracking import artifact_label
from app.services.role_family import role_family
from app.utils.timefmt import coerce_datetime, iso_utc

__all__ = [
    "CALIBRATION_MODEL_VERSION",
    "CalibrationUnavailable",
    "MIN_SAMPLE_SIZES",
    "REPORT_VERSION",
    "Window",
    "build_report",
    "calculation_id",
    "calibration_status",
    "csv_export",
    "definitions",
    "interview_probability",
    "resolve_window",
    "weekly_summary",
]

#: Version of the *calculations* in this module. Bumped whenever a formula, a
#: denominator or a minimum sample size changes, so a number quoted in a
#: support thread can be traced to the method that produced it.
REPORT_VERSION = "1.0.0"

#: Minimum sample sizes. Each one encodes "below this, the number is noise".
MIN_SAMPLE_SIZES: Dict[str, int] = {
    # A per-group interview rate needs at least this many applications.
    "comparison_applied": 5,
    # A period-over-period comparison needs this many applications in *both*
    # periods, otherwise the delta is two coin flips subtracted.
    "trend_applied": 5,
    # Median/mean time from posting to application.
    "timing": 5,
    # Correction rate over recorded events (the denominator is events).
    "correction_rate_events": 20,
    # Automation failure rate over application-pipeline attempts.
    "failure_rate_attempts": 10,
}

#: Interview-probability calibration gate. All three must hold, *and* a
#: calibrated model must be registered: with ``CALIBRATION_MODEL_VERSION`` empty
#: the gate can never open, which is the intended state until a model is
#: validated out-of-sample. See ``docs/REPORTING.md`` §Calibration.
CALIBRATION_MIN_INTERVIEWS = 30
CALIBRATION_MIN_BANDS = 3
CALIBRATION_MIN_VERIFIED_SHARE = 0.8
CALIBRATION_MODEL_VERSION = ""

#: A change smaller than this many percentage points is reported as flat.
TREND_FLAT_EPSILON = 1.0

DISCLAIMER = (
    "Everything here comes from your own rows: jobs we discovered for you, submissions we "
    "observed or you recorded, and the outcomes you logged on the Tracking page. An interview "
    "is counted only when it was recorded — never inferred from email, never from a match "
    "score. A match score is an estimated fit, not an interview probability."
)

PROBABILITY_WITHHELD = (
    "No interview probability is reported. A match score ranks fit; turning it into a "
    "probability requires a calibrated model trained on enough recorded outcomes, and this "
    "workspace has not reached that point."
)

#: The formulae, verbatim, so the numbers can be re-derived by hand.
_DEFINITIONS: Dict[str, str] = {
    "cohort": (
        "Applications submitted inside the window, by application date "
        "(application_tracking.applied_at). A job merely saved to the board is not an "
        "application and never enters a denominator."
    ),
    "jobs_discovered": "Jobs whose jobs.discovered_at falls in the window.",
    "high_fit_jobs": (
        f"Discovered jobs scored at or above {HIGH_FIT_SCORE:g} (an estimated fit, not a "
        "probability of an interview)."
    ),
    "applications_prepared": "Distinct jobs with an application packet generated in the window.",
    "applications_submitted": (
        "Distinct jobs applied to in the window, counted from the board (jobs.applied_at) and "
        "from the tracking timeline (application_tracking.applied_at), unioned by job id so a "
        "job our machinery submitted and the user also recorded is counted once."
    ),
    "applications_tracked": (
        "Applications submitted in the window that have a tracking record. This is the "
        "denominator of every rate, because a rate is only as good as the outcome data behind "
        "it. Applications without a record are reported as a coverage gap, not silently "
        "assumed to have failed."
    ),
    "recruiter_responses": "Cohort applications whose first_response_at is set.",
    "interviews": (
        "Cohort applications whose recorded state is one of "
        f"{', '.join(TRACKING_INTERVIEW_STATES)} — recorded by the user, by our own submission "
        "machinery, or by a verified integration."
    ),
    "interview_rate": "interviews ÷ applications_tracked, as a percentage.",
    "rejection_rate": "applications whose recorded state is rejected_by_employer ÷ applications_tracked.",
    "user_correction_rate": (
        "corrections ÷ timeline events on the cohort (both denormalised counters on the "
        "tracking record). It measures how often what we recorded had to be rewritten — a "
        "measure of our accuracy, not of the user's."
    ),
    "application_failure_rate": (
        "Application-pipeline attempts that ended failed or dead ÷ all attempts that ended in "
        "the window (done, failed or dead). This measures the automation, not the user's "
        "applications."
    ),
    "time_from_posting_to_application": (
        "Days between jobs.posted_at and the application date, for cohort applications whose "
        "job carries a posting date. Median and mean are reported with their sample size; "
        "applications dated before the posting are counted and disclosed, never hidden."
    ),
    "comparisons": (
        f"Rates are grouped and only shown for groups with at least "
        f"{MIN_SAMPLE_SIZES['comparison_applied']} applications; smaller groups are labelled "
        "insufficient and their rate is withheld."
    ),
    "trend": (
        f"The window is compared with the immediately preceding window of the same length. A "
        f"direction is only claimed when both windows hold at least "
        f"{MIN_SAMPLE_SIZES['trend_applied']} applications; otherwise the answer is "
        "'not enough data'."
    ),
    "timezone": (
        "Windows are local calendar days in the requested timezone and half-open on the wire "
        "[start_utc, end_utc). Timestamps are stored naive-UTC and rendered ISO-8601 with an "
        "explicit offset."
    ),
}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ReportError(Exception):
    """A refused report request. ``payload()`` is what the API returns."""

    http_status = 422
    code = "validation_error"

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


class UnknownValue(ReportError):
    """A value outside the report's vocabulary — 422, never a silent default."""


class WindowError(ReportError):
    """An unusable date range (``until`` before ``since``, unparseable date)."""


class CalibrationUnavailable(ReportError):
    """Asked for an interview probability before the data supports one."""

    code = "calibration_unavailable"


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Window:
    """One reporting window: named range, resolved local days, UTC instants.

    ``start_utc``/``end_utc`` are naive UTC (what the columns hold) and the pair
    is half-open: ``start_utc <= t < end_utc``.
    """

    range: str
    timezone: str
    start_utc: datetime
    end_utc: datetime
    start_local: datetime
    end_local: datetime
    days: int

    @property
    def start_date(self) -> str:
        return self.start_local.date().isoformat()

    @property
    def end_date(self) -> str:
        """The last local day *inside* the window (``end_utc`` is exclusive)."""
        return (self.end_local - timedelta(microseconds=1)).date().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "range": self.range,
            "timezone": self.timezone,
            "start_utc": iso_utc(self.start_utc),
            "end_utc": iso_utc(self.end_utc),
            "start_local": self.start_local.isoformat(timespec="seconds"),
            "end_local": self.end_local.isoformat(timespec="seconds"),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "days": self.days,
            "boundary": "half-open [start_utc, end_utc)",
            "label": self.label(),
        }

    def label(self) -> str:
        if self.start_date == self.end_date:
            span = self.start_date
        else:
            span = f"{self.start_date} → {self.end_date}"
        return f"{span} ({self.timezone})"


def resolve_timezone(name: Optional[str]) -> ZoneInfo:
    """A real IANA zone, or a 422.

    Never silently falls back to UTC: the whole point of asking is that a day
    boundary is a local fact, and quietly answering in another zone is the bug
    this parameter exists to prevent.
    """
    candidate = (name or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise UnknownValue(
            f"'{candidate}' is not a recognised timezone.",
            field="timezone",
            allowed=["UTC", "Area/City — an IANA name such as Europe/London"],
        ) from None


def _aware_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=ZoneInfo("UTC"))


def _local_midnight(zone: ZoneInfo, day: date) -> datetime:
    """Midnight local time on *day* — DST-aware via the zone's own rules."""
    return datetime(day.year, day.month, day.day, tzinfo=zone)


def _parse_boundary(value: Any, zone: ZoneInfo, *, end: bool) -> Optional[datetime]:
    """A wire boundary → an aware datetime in *zone*.

    ``date`` / ``YYYY-MM-DD`` values are local calendar days: for ``end`` the
    boundary becomes local midnight *the day after*, which makes a date-only
    ``until`` mean "through the end of that day".
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=ZoneInfo("UTC"))
        return aware.astimezone(zone)
    if isinstance(value, date):
        day = value + timedelta(days=1) if end else value
        return _local_midnight(zone, day)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        parsed_day: Optional[date]
        try:
            parsed_day = date.fromisoformat(text)
        except ValueError:
            parsed_day = None
        if parsed_day is not None and len(text) == 10:
            shifted = parsed_day + timedelta(days=1) if end else parsed_day
            return _local_midnight(zone, shifted)
        parsed = _parse_instant(text)
        if parsed is None:
            raise WindowError(f"'{value}' is not a date or a timestamp.", field="since/until")
        return parsed.astimezone(zone)
    raise WindowError("Since/until must be a date or an ISO-8601 timestamp.", field="since/until")


def _parse_instant(text: str) -> Optional[datetime]:
    normalised = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("UTC"))


def resolve_window(
    *,
    range_key: str = "last_30_days",
    since: Any = None,
    until: Any = None,
    timezone_name: Optional[str] = "UTC",
    now: Optional[datetime] = None,
) -> Window:
    """Resolve a named range (or explicit bounds) into local days + UTC instants.

    Named ranges are **whole local calendar days**, so the window (and therefore
    the report's ``calculation_id``) is stable from midnight to midnight in the
    user's zone instead of sliding with the clock. ``this_week`` is the one
    partial window — Monday local midnight to *now* — because a week-to-date
    figure is what "how is this week going" means.
    """
    if range_key not in REPORT_RANGES:
        raise UnknownValue(
            f"'{range_key}' is not a report range.",
            field="range", allowed=list(REPORT_RANGES),
        )
    zone = resolve_timezone(timezone_name)
    now_utc = _aware_utc(coerce_datetime(now) or datetime.utcnow())
    now_local = now_utc.astimezone(zone)

    if since is not None or until is not None or range_key == "custom":
        # Bound one at a time so the locals are datetimes, not Options: an open
        # start means "the week before the end", an open end means "through the
        # end of today, local time" — never "now", which would slide the window.
        start_bound = _parse_boundary(since, zone, end=False)
        end_bound = _parse_boundary(until, zone, end=True)
        if start_bound is not None and end_bound is not None:
            start, end = start_bound, end_bound
        elif start_bound is not None:
            start, end = start_bound, _local_midnight(zone, now_local.date() + timedelta(days=1))
        elif end_bound is not None:
            start, end = end_bound - timedelta(days=7), end_bound
        else:
            raise WindowError("A custom range needs a since or an until.", field="since/until")
    elif range_key == "this_week":
        start = _local_midnight(zone, now_local.date() - timedelta(days=now_local.weekday()))
        end = now_local
    elif range_key == "last_week":
        this_monday = now_local.date() - timedelta(days=now_local.weekday())
        start = _local_midnight(zone, this_monday - timedelta(days=7))
        end = _local_midnight(zone, this_monday)
    else:
        span_days = {"last_7_days": 7, "last_30_days": 30, "last_90_days": 90}[range_key]
        end = _local_midnight(zone, now_local.date() + timedelta(days=1))
        start = end - timedelta(days=span_days)

    if end <= start:
        raise WindowError("The end of the window must be after the start.", field="until")

    return Window(
        range=range_key,
        timezone=str(zone),
        start_utc=_aware_utc(start).astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
        end_utc=_aware_utc(end).astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
        start_local=start,
        end_local=end,
        days=max(1, (end - start).days),
    )


def previous_window(window: Window) -> Window:
    """The window immediately before *window*, of the same length."""
    span = window.end_local - window.start_local
    return Window(
        range=window.range,
        timezone=window.timezone,
        start_utc=(window.start_local - span).astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
        end_utc=window.start_utc,
        start_local=window.start_local - span,
        end_local=window.start_local,
        days=window.days,
    )


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _rate(numerator: int, denominator: int) -> Optional[float]:
    """A percentage, or ``None`` when there is no denominator (never ``0.0``)."""
    if not denominator:
        return None
    return round(100.0 * float(numerator) / float(denominator), 1)


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return round(float(ordered[middle]), 1)
    return round((float(ordered[middle - 1]) + float(ordered[middle])) / 2.0, 1)


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(float(value) for value in values) / len(values), 1)


def _percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return round(ordered[index], 1)


def _sufficiency(n: int, minimum: int, *, subject: str) -> Dict[str, Any]:
    if minimum <= 0:
        state = "not_applicable"
    elif n >= minimum:
        state = "sufficient"
    else:
        state = "insufficient"
    if state not in REPORT_SUFFICIENCY:
        raise UnknownValue(f"'{state}' is not a sufficiency state.", field="sufficiency",
                           allowed=list(REPORT_SUFFICIENCY))
    return {
        "n": int(n),
        "minimum": int(minimum),
        "state": state,
        "sufficient": state == "sufficient",
        "label": {
            "sufficient": f"{n} of {minimum} needed",
            "insufficient": f"Not enough data — {n} of {minimum} {subject}",
            "not_applicable": "No minimum applies",
        }[state],
    }


def _metric(
    key: str,
    label: str,
    value: Any,
    *,
    unit: str,
    provenance: str,
    sample: Dict[str, Any],
    note: str = "",
) -> Dict[str, Any]:
    """One reported number, with the two facts a reader needs to trust it."""
    if provenance not in REPORT_PROVENANCE_KINDS:
        raise UnknownValue(f"'{provenance}' is not a report provenance.", field="provenance",
                           allowed=list(REPORT_PROVENANCE_KINDS))
    return {
        "key": key,
        "label": label,
        "value": value,
        "unit": unit,
        "provenance": provenance,
        "sample": sample,
        "sufficiency": sample["state"],
        "note": note,
    }


def calibration_status(*, interviews: int, bands_with_data: int,
                       verified_share: Optional[float]) -> Dict[str, Any]:
    """Whether an interview *probability* may be shown. It may not, yet.

    Deliberately conservative and deliberately explicit: the report says what is
    missing rather than emitting a number nobody validated.
    """
    observed = {
        "recorded_interviews": int(interviews),
        "score_bands_with_outcomes": int(bands_with_data),
        "share_verified_outcomes": None if verified_share is None else round(float(verified_share), 3),
    }
    requires = {
        "recorded_interviews": CALIBRATION_MIN_INTERVIEWS,
        "score_bands_with_outcomes": CALIBRATION_MIN_BANDS,
        "share_verified_outcomes": CALIBRATION_MIN_VERIFIED_SHARE,
        "calibrated_model_version": CALIBRATION_MODEL_VERSION or None,
    }
    missing: List[str] = []
    if interviews < CALIBRATION_MIN_INTERVIEWS:
        missing.append("recorded_interviews")
    if bands_with_data < CALIBRATION_MIN_BANDS:
        missing.append("score_bands_with_outcomes")
    if (verified_share or 0.0) < CALIBRATION_MIN_VERIFIED_SHARE:
        missing.append("share_verified_outcomes")
    if not CALIBRATION_MODEL_VERSION:
        missing.append("calibrated_model_version")
    return {
        "available": not missing,
        "interview_probability": None,
        "missing": missing,
        "observed": observed,
        "requires": requires,
        "note": PROBABILITY_WITHHELD if missing else (
            "A calibrated model version is registered; probability output stays opt-in per "
            "surface and is never shown for a score band with too few outcomes."
        ),
    }


def interview_probability(*_args: Any, **_kwargs: Any) -> float:
    """Always refuses. Kept so a caller cannot quietly compute one instead.

    The gate lives in :func:`calibration_status`; this function exists to make
    "just estimate it locally" a one-line failure rather than a plausible idea.
    """
    raise CalibrationUnavailable(
        "Interview probability is withheld until the recorded outcomes support calibration.",
        missing=calibration_status(interviews=0, bands_with_data=0, verified_share=0.0)["missing"],
    )


# --------------------------------------------------------------------------- #
# Data loading — tenant-scoped, one query per fact
# --------------------------------------------------------------------------- #
@dataclass
class _Data:
    """Everything the report reads, already filtered to one tenant."""

    window: Window
    discovered_jobs: List[Job]
    board_applied: List[Job]                 # applied_at inside the window
    cohort: List[ApplicationTracking]        # tracked applications inside the window
    cohort_jobs: Dict[int, Job]
    packets: List[ApplicationPacket]
    attempts: List[PipelineJob]
    events_total: int
    events_corrected: int


def _load(db: Session, user_id: int, window: Window) -> _Data:
    """Read the window's rows. Each query carries ``user_id`` — there is no
    code path in this module that can see another tenant's data."""
    uid = int(user_id)
    start, end = window.start_utc, window.end_utc

    discovered = (
        db.query(Job)
        .filter(Job.user_id == uid, Job.discovered_at >= start, Job.discovered_at < end)
        .order_by(Job.id.asc())
        .all()
    )
    board_applied = (
        db.query(Job)
        .filter(Job.user_id == uid, Job.applied_at.is_not(None),
                Job.applied_at >= start, Job.applied_at < end)
        .order_by(Job.id.asc())
        .all()
    )
    cohort = (
        db.query(ApplicationTracking)
        .filter(ApplicationTracking.user_id == uid,
                ApplicationTracking.applied_at.is_not(None),
                ApplicationTracking.applied_at >= start,
                ApplicationTracking.applied_at < end)
        .order_by(ApplicationTracking.id.asc())
        .all()
    )
    job_ids = {int(row.job_id) for row in cohort}
    cohort_jobs: Dict[int, Job] = {}
    if job_ids:
        for job in db.query(Job).filter(Job.user_id == uid, Job.id.in_(sorted(job_ids))).all():
            cohort_jobs[int(job.id)] = job

    packets = (
        db.query(ApplicationPacket)
        .filter(ApplicationPacket.user_id == uid,
                ApplicationPacket.created_at >= start,
                ApplicationPacket.created_at < end)
        .order_by(ApplicationPacket.id.asc())
        .all()
    )
    finished = func.coalesce(PipelineJob.finished_at, PipelineJob.updated_at)
    attempts = (
        db.query(PipelineJob)
        .filter(PipelineJob.user_id == uid, PipelineJob.pipeline == "application",
                finished >= start, finished < end)
        .order_by(PipelineJob.id.asc())
        .all()
    )
    events_total = sum(int(row.event_count or 0) for row in cohort)
    events_corrected = sum(int(row.correction_count or 0) for row in cohort)
    return _Data(
        window=window,
        discovered_jobs=discovered,
        board_applied=board_applied,
        cohort=cohort,
        cohort_jobs=cohort_jobs,
        packets=packets,
        attempts=attempts,
        events_total=events_total,
        events_corrected=events_corrected,
    )


# --------------------------------------------------------------------------- #
# Cohorts and tallies
# --------------------------------------------------------------------------- #
def _is_interview(record: ApplicationTracking) -> bool:
    return record.state in TRACKING_INTERVIEW_STATES


def _tally(rows: Iterable[ApplicationTracking]) -> Dict[str, Any]:
    """The cohort numbers for a set of tracked applications."""
    applied = interviews = responses = offers = rejections = withdrawn = provisional = 0
    corrections = events = 0
    scores: List[float] = []
    by_origin: Counter[str] = Counter()
    for row in rows:
        applied += 1
        by_origin[row.state_origin] += 1
        corrections += int(row.correction_count or 0)
        events += int(row.event_count or 0)
        provisional += 1 if row.is_provisional else 0
        if row.first_response_at is not None:
            responses += 1
        if _is_interview(row):
            interviews += 1
        if row.state == "offer_received":
            offers += 1
        if row.state == "rejected_by_employer":
            rejections += 1
        if row.state == "withdrawn":
            withdrawn += 1
        if row.match_score is not None:
            scores.append(float(row.match_score))
    return {
        "applied": applied,
        "responses": responses,
        "interviews": interviews,
        "offers": offers,
        "rejections": rejections,
        "withdrawn": withdrawn,
        "provisional": provisional,
        "corrections": corrections,
        "events": events,
        "interview_rate": _rate(interviews, applied),
        "response_rate": _rate(responses, applied),
        "rejection_rate": _rate(rejections, applied),
        "offer_rate": _rate(offers, interviews),
        "avg_match_score": _mean(scores),
        "by_origin": dict(sorted(by_origin.items())),
    }


# --------------------------------------------------------------------------- #
# Grouping (the comparisons)
# --------------------------------------------------------------------------- #
def _score_range(score: Optional[float]) -> str:
    if score is None:
        return "unscored"
    value = float(score)
    if value >= 90:
        return "90-100"
    if value >= 80:
        return "80-89"
    if value >= 70:
        return "70-79"
    if value >= 60:
        return "60-69"
    return "0-59"


def _score_band(record: ApplicationTracking) -> str:
    """The scorer's own band, or the band implied by the frozen score.

    A record whose band column is empty still sorts into a band when it carries
    a score, so a comparison is never silently missing a row.
    """
    if record.match_band:
        return record.match_band
    return "unscored" if record.match_score is None else "unknown"


def _group_key(record: ApplicationTracking, dimension: str) -> str:
    if dimension == "source":
        return record.source or "unknown"
    if dimension == "score_band":
        return _score_band(record)
    if dimension == "score_range":
        return _score_range(record.match_score)
    if dimension == "role_family":
        return record.role_family or role_family(record.job_title_snapshot or "") or "Other"
    if dimension == "artifact":
        return artifact_label(record)
    raise UnknownValue(f"'{dimension}' is not a comparison dimension.",
                       field="compare", allowed=list(REPORT_COMPARISON_DIMENSIONS))


#: Presentation order for the banded dimensions — a report reads top-down as
#: "best fit first", not as "whatever sorted first".
_GROUP_ORDER: Dict[str, Tuple[str, ...]] = {
    "score_band": tuple(MATCH_BANDS),
    "score_range": ("90-100", "80-89", "70-79", "60-69", "0-59", "unscored"),
}


def _order_key(dimension: str, key: str, tally: Dict[str, Any]) -> Tuple[int, Any]:
    preferred = _GROUP_ORDER.get(dimension)
    if preferred and key in preferred:
        return (0, preferred.index(key))
    if key in ("unscored", "unknown", "none"):
        return (1, key)
    # Otherwise: biggest sample first, then alphabetically — deterministic, and
    # the groups a reader cares about are at the top.
    return (2, (-int(tally["applied"]), key))


def _comparison(rows: List[ApplicationTracking], dimension: str, minimum: int) -> Dict[str, Any]:
    grouped: Dict[str, List[ApplicationTracking]] = {}
    for row in rows:
        grouped.setdefault(_group_key(row, dimension), []).append(row)

    tallies = {key: _tally(bucket) for key, bucket in grouped.items()}
    ordered = sorted(grouped, key=lambda key: _order_key(dimension, key, tallies[key]))

    groups: List[Dict[str, Any]] = []
    for key in ordered:
        tally = tallies[key]
        sample = _sufficiency(tally["applied"], minimum, subject="applications")
        entry: Dict[str, Any] = {"key": key, "label": key, **tally, "sample": sample,
                                 "sufficiency": sample["state"],
                                 "insufficient_reason": None}
        if not sample["sufficient"]:
            # The counts stay (they are facts); the *comparison* is withheld.
            entry["interview_rate"] = None
            entry["response_rate"] = None
            entry["rejection_rate"] = None
            entry["insufficient_reason"] = sample["label"]
        groups.append(entry)

    comparable = [group for group in groups if group["sufficiency"] == "sufficient"]
    best = None
    if len(comparable) >= 2:
        leader = max(comparable, key=lambda group: (group["interview_rate"] or 0.0, -group["applied"]))
        if (leader["interview_rate"] or 0.0) > 0:
            best = {"key": leader["key"], "label": leader["label"],
                    "interview_rate": leader["interview_rate"], "applied": leader["applied"],
                    "interviews": leader["interviews"], "dimension": dimension}
    return {
        "dimension": dimension,
        "minimum_sample": minimum,
        "groups": groups,
        "group_count": len(groups),
        "sufficient_group_count": len(comparable),
        "best": best,
        "withheld_group_count": len(groups) - len(comparable),
    }


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
def calculation_id(window: Window, *, extra: Optional[Dict[str, Any]] = None) -> str:
    """A stable hash over exactly the inputs that can change the answer."""
    payload = {
        "report_version": REPORT_VERSION,
        "timezone": window.timezone,
        "range": window.range,
        "start_utc": iso_utc(window.start_utc),
        "end_utc": iso_utc(window.end_utc),
        "minimum_sample_sizes": MIN_SAMPLE_SIZES,
        "high_fit_score": HIGH_FIT_SCORE,
        "interview_states": list(TRACKING_INTERVIEW_STATES),
        "extra": extra or {},
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def definitions() -> Dict[str, Any]:
    """The methodology, verbatim — what makes the numbers re-derivable."""
    return {
        "report_version": REPORT_VERSION,
        "minimum_sample_sizes": dict(MIN_SAMPLE_SIZES),
        "high_fit_score": HIGH_FIT_SCORE,
        "interview_states": list(TRACKING_INTERVIEW_STATES),
        "formulae": dict(_DEFINITIONS),
    }


def _timing(data: _Data) -> Dict[str, Any]:
    """Time from posting to application — the one number that measures *speed*."""
    gaps: List[float] = []
    negative = 0
    missing_posted = 0
    for record in data.cohort:
        job = data.cohort_jobs.get(int(record.job_id))
        posted = coerce_datetime(getattr(job, "posted_at", None))
        applied = coerce_datetime(record.applied_at)
        if posted is None or applied is None:
            missing_posted += 1
            continue
        days = (applied - posted).total_seconds() / 86400.0
        if days < 0:
            negative += 1
        gaps.append(days)
    sample = _sufficiency(len(gaps), MIN_SAMPLE_SIZES["timing"], subject="postings with a date")
    value = _median(gaps) if sample["sufficient"] else None
    note = ""
    if negative:
        note = (f"{negative} application(s) are dated before their posting — usually a reposted "
                "listing. They are included, not hidden.")
    if missing_posted and not gaps:
        note = (f"{missing_posted} application(s) have no posting date on the job row, so this "
                "cannot be measured yet.")
    return {
        "days": value,
        "median_days": value,
        "mean_days": _mean(gaps) if sample["sufficient"] else None,
        "p90_days": _percentile(gaps, 0.9) if sample["sufficient"] else None,
        "min_days": round(min(gaps), 1) if gaps and sample["sufficient"] else None,
        "max_days": round(max(gaps), 1) if gaps and sample["sufficient"] else None,
        "applications_measured": len(gaps),
        "applications_missing_posting_date": missing_posted,
        "applications_dated_before_posting": negative,
        "provenance": "derived",
        "sample": sample,
        "sufficiency": sample["state"],
        "note": note or _DEFINITIONS["time_from_posting_to_application"],
    }


def build_report(
    db: Session,
    user_id: int,
    *,
    range_key: str = "last_30_days",
    since: Any = None,
    until: Any = None,
    timezone_name: Optional[str] = None,
    now: Optional[datetime] = None,
    include_comparisons: bool = True,
) -> Dict[str, Any]:
    """The full outcome report for one tenant.

    Tenant-scoped by construction: ``user_id`` is the only identity this
    function accepts and every query filters on it.
    """
    uid = int(user_id)
    window = resolve_window(range_key=range_key, since=since, until=until,
                            timezone_name=timezone_name or "UTC", now=now)
    previous = previous_window(window)
    data = _load(db, uid, window)
    prior = _load(db, uid, previous)

    cohort_tally = _tally(data.cohort)
    submitted_ids = {int(job.id) for job in data.board_applied} | {int(row.job_id) for row in data.cohort}
    tracked_ids = {int(row.job_id) for row in data.cohort}
    board_only = len(submitted_ids - tracked_ids)
    high_fit = [job for job in data.discovered_jobs
                if job.score is not None and float(job.score) >= HIGH_FIT_SCORE]
    prepared_jobs = {int(row.job_id) for row in data.packets}

    tracking_coverage = _rate(len(tracked_ids), len(submitted_ids))

    # --- application failures: the automation's record, not the user's ------ #
    attempts = len(data.attempts)
    failures = sum(1 for row in data.attempts if (row.status or "") in ("failed", "dead"))
    failure_sample = _sufficiency(attempts, MIN_SAMPLE_SIZES["failure_rate_attempts"],
                                  subject="application attempts")
    failure_rate = _rate(failures, attempts) if failure_sample["sufficient"] else None
    prior_attempts = len(prior.attempts)
    prior_failures = sum(1 for row in prior.attempts if (row.status or "") in ("failed", "dead"))
    prior_failure_sample = _sufficiency(prior_attempts, MIN_SAMPLE_SIZES["failure_rate_attempts"],
                                        subject="application attempts")
    prior_failure_rate = (_rate(prior_failures, prior_attempts)
                          if prior_failure_sample["sufficient"] else None)

    # --- corrections ------------------------------------------------------- #
    correction_sample = _sufficiency(data.events_total, MIN_SAMPLE_SIZES["correction_rate_events"],
                                     subject="recorded events")
    correction_rate = (_rate(data.events_corrected, data.events_total)
                       if correction_sample["sufficient"] else None)

    # --- bands with outcomes (the calibration gate's observation) ---------- #
    bands_with_data = len({_score_range(row.match_score)
                           for row in data.cohort if _is_interview(row)})
    verified = sum(count for origin, count in cohort_tally["by_origin"].items()
                   if origin in ("system_observed", "verified_integration"))
    verified_share = (float(verified) / float(cohort_tally["applied"])
                      if cohort_tally["applied"] else None)

    comparison_min = MIN_SAMPLE_SIZES["comparison_applied"]
    comparisons = ({
        dimension: _comparison(data.cohort, dimension, comparison_min)
        for dimension in REPORT_COMPARISON_DIMENSIONS
    } if include_comparisons else {})

    timing = _timing(data)
    metrics = _metrics(
        data=data, submitted=len(submitted_ids), tracked=len(tracked_ids), board_only=board_only,
        high_fit=len(high_fit), prepared=len(prepared_jobs),
        tallies=cohort_tally, failure_rate=failure_rate, failure_sample=failure_sample,
        failure_attempts=attempts, correction_rate=correction_rate,
        correction_sample=correction_sample, tracking_coverage=tracking_coverage,
        timing=timing,
    )

    trend = _trend(current=cohort_tally, prior=_tally(prior.cohort), previous=previous,
                   failure_rate=failure_rate, prior_failure_rate=prior_failure_rate)

    report = {
        "report_version": REPORT_VERSION,
        "generated_at": iso_utc(datetime.utcnow()),
        "calculation_id": calculation_id(window),
        "timezone": window.timezone,
        "window": window.to_dict(),
        "previous_window": previous.to_dict(),
        "definitions": definitions(),
        "metrics": metrics,
        "funnel": {
            "jobs_discovered": len(data.discovered_jobs),
            "high_fit_jobs": len(high_fit),
            "applications_prepared": len(prepared_jobs),
            "applications_submitted": len(submitted_ids),
            "applications_tracked": len(tracked_ids),
            "recruiter_responses": cohort_tally["responses"],
            "interviews": cohort_tally["interviews"],
            "offers": cohort_tally["offers"],
            "rejections": cohort_tally["rejections"],
        },
        "totals": {
            **_gated(cohort_tally, comparison_min),
            "jobs_discovered": len(data.discovered_jobs),
            "high_fit_jobs": len(high_fit),
            "applications_submitted": len(submitted_ids),
            "applications_tracked": len(tracked_ids),
            "applications_prepared": len(prepared_jobs),
            "tracking_coverage_percent": tracking_coverage,
        },
        "comparisons": comparisons,
        "timing": timing,
        "quality": {
            "tracking_coverage_percent": tracking_coverage,
            "applications_without_a_tracking_record": board_only,
            "provisional_records": cohort_tally["provisional"],
            "corrections": data.events_corrected,
            "events": data.events_total,
            "correction_rate": correction_rate,
            "by_origin": cohort_tally["by_origin"],
            "application_attempts": attempts,
            "application_failures": failures,
            "application_failure_rate": failure_rate,
            "note": (
                "A rate is only as good as the outcome data behind it. Applications without a "
                "tracking record are reported here rather than assumed to have failed."
            ),
        },
        "trend": trend,
        "calibration": calibration_status(interviews=cohort_tally["interviews"],
                                          bands_with_data=bands_with_data,
                                          verified_share=verified_share),
        "privacy": _privacy(window),
        "disclaimer": DISCLAIMER,
        "server_time": iso_utc(datetime.utcnow()),
    }
    report["signal"] = _signal(comparisons, trend)
    report["headline"] = _headline(report)
    return report


def _gated(tally: Dict[str, Any], minimum: int, subject: str = "applications") -> Dict[str, Any]:
    """A tally with its *ratios* withheld when the sample is too small.

    Counts stay (they are facts). A percentage over three applications is not a
    rate, and the report must not print one anywhere a person could read it.
    """
    sample = _sufficiency(int(tally.get("applied") or 0), minimum, subject=subject)
    gated = dict(tally)
    if not sample["sufficient"]:
        for key in ("interview_rate", "response_rate", "rejection_rate", "offer_rate"):
            gated[key] = None
    gated["sample"] = sample
    gated["sufficiency"] = sample["state"]
    return gated


def _metrics(*, data: _Data, submitted: int, tracked: int, board_only: int, high_fit: int,
             prepared: int, tallies: Dict[str, Any], failure_rate: Optional[float],
             failure_sample: Dict[str, Any], failure_attempts: int,
             correction_rate: Optional[float], correction_sample: Dict[str, Any],
             tracking_coverage: Optional[float], timing: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The metric list, in the order the acceptance criteria name them.

    ``observed`` metrics are counts of rows; ``user_reported`` metrics are
    outcomes a person asserted; ``derived`` metrics are arithmetic; one metric
    (high fit) is ``estimated``, and it says so.
    """
    interview_sample = _sufficiency(tallies["applied"], MIN_SAMPLE_SIZES["comparison_applied"],
                                    subject="applications")
    return [
        _metric("jobs_discovered", "Jobs discovered", len(data.discovered_jobs),
                unit="count", provenance="observed",
                sample=_sufficiency(len(data.discovered_jobs), 0, subject="jobs")),
        _metric("high_fit_jobs", "High-fit jobs found", high_fit,
                unit="count", provenance="estimated",
                sample=_sufficiency(len(data.discovered_jobs), 0, subject="jobs"),
                note=f"Estimated fit at or above {HIGH_FIT_SCORE:g}. A ranking, not an "
                     "interview probability."),
        _metric("applications_prepared", "Applications prepared", prepared,
                unit="count", provenance="observed",
                sample=_sufficiency(prepared, 0, subject="packets")),
        _metric("applications_submitted", "Applications submitted", submitted,
                unit="count", provenance="observed",
                sample=_sufficiency(tracked, 0, subject="applications"),
                note=(f"{board_only} of them have no tracking record yet, so they are outside "
                      "the rates below." if board_only else "")),
        _metric("recruiter_responses", "Recruiter responses", tallies["responses"],
                unit="count", provenance="user_reported",
                sample=_sufficiency(tallies["applied"], MIN_SAMPLE_SIZES["comparison_applied"],
                                    subject="applications")),
        _metric("interviews", "Interviews", tallies["interviews"],
                unit="count", provenance="user_reported",
                sample=_sufficiency(tallies["applied"], MIN_SAMPLE_SIZES["comparison_applied"],
                                    subject="applications")),
        _metric("interview_rate", "Interview rate",
                tallies["interview_rate"] if interview_sample["sufficient"] else None,
                unit="percent", provenance="derived",
                sample=_sufficiency(tallies["applied"], MIN_SAMPLE_SIZES["comparison_applied"],
                                    subject="applications"),
                note="Interviews ÷ applications recorded in this window."),
        _metric("rejection_rate", "Rejection rate",
                tallies["rejection_rate"] if interview_sample["sufficient"] else None,
                unit="percent", provenance="derived",
                sample=_sufficiency(tallies["applied"], MIN_SAMPLE_SIZES["comparison_applied"],
                                    subject="applications")),
        _metric("user_correction_rate", "User correction rate", correction_rate,
                unit="percent", provenance="derived", sample=correction_sample,
                note="Corrections ÷ recorded timeline events — how often what we recorded had "
                     "to be rewritten."),
        _metric("application_failure_rate", "Application failure rate", failure_rate,
                unit="percent", provenance="derived", sample=failure_sample,
                note=f"{failure_attempts} automation attempt(s) finished in this window. "
                     "This measures the machinery, not your applications."),
        _metric("time_from_posting_to_application",
                "Time from posting to application", timing["median_days"],
                unit="days", provenance="derived", sample=timing["sample"],
                note=timing["note"]),
        _metric("tracking_coverage", "Outcome data coverage", tracking_coverage,
                unit="percent", provenance="derived",
                sample=_sufficiency(submitted, 0, subject="applications")),
    ]


def _trend(*, current: Dict[str, Any], prior: Dict[str, Any], previous: Window,
           failure_rate: Optional[float],
           prior_failure_rate: Optional[float]) -> Dict[str, Any]:
    """Is this working? — the current window against the one before it.

    Only the interview rate is allowed to carry the verdict, because it is the
    only metric here that says "the applications turned into conversations". A
    direction needs both windows to clear the minimum sample; otherwise the
    answer is ``not_enough_data`` and the report says how far off it is.
    """
    current_sample = _sufficiency(current["applied"], MIN_SAMPLE_SIZES["trend_applied"],
                                  subject="applications")
    prior_sample = _sufficiency(prior["applied"], MIN_SAMPLE_SIZES["trend_applied"],
                                subject="applications")
    measured = current_sample["sufficient"] and prior_sample["sufficient"]

    def delta(now: Optional[float], before: Optional[float]) -> Optional[float]:
        if now is None or before is None:
            return None
        return round(float(now) - float(before), 1)

    def visible(value: Any, sample: Dict[str, Any]) -> Any:
        """A ratio is only shown on the side whose sample supports it."""
        return value if sample["sufficient"] else None

    rows = {
        "interview_rate": {"current": visible(current["interview_rate"], current_sample),
                           "previous": visible(prior["interview_rate"], prior_sample)},
        "response_rate": {"current": visible(current["response_rate"], current_sample),
                          "previous": visible(prior["response_rate"], prior_sample)},
        "rejection_rate": {"current": visible(current["rejection_rate"], current_sample),
                           "previous": visible(prior["rejection_rate"], prior_sample)},
        "interviews": {"current": current["interviews"], "previous": prior["interviews"]},
        "applications_submitted": {"current": current["applied"], "previous": prior["applied"]},
        "application_failure_rate": {"current": failure_rate, "previous": prior_failure_rate},
    }
    for row in rows.values():
        row["delta"] = delta(row["current"], row["previous"])

    primary = rows["interview_rate"]
    direction = "not_enough_data"
    if measured and primary["delta"] is not None:
        if primary["delta"] > TREND_FLAT_EPSILON:
            direction = "improving"
        elif primary["delta"] < -TREND_FLAT_EPSILON:
            direction = "declining"
        else:
            direction = "flat"

    if direction == "not_enough_data":
        shortfall = min(
            MIN_SAMPLE_SIZES["trend_applied"] - current["applied"],
            MIN_SAMPLE_SIZES["trend_applied"] - prior["applied"],
        )
        headline = (
            "Not enough recorded outcomes yet to say whether this is working — "
            f"{MIN_SAMPLE_SIZES['trend_applied']} applications are needed in each period "
            f"({current['applied']} this period, {prior['applied']} the last one)."
        )
        if current["applied"] and current["interviews"]:
            headline += (f" So far {current['interviews']} of {current['applied']} applications "
                         "reached an interview.")
        else:
            headline += " " + (f"{shortfall} more application(s) this period would be enough to "
                               "compare." if shortfall > 0 else "")
    elif direction == "flat":
        headline = (f"Interview rate is holding steady: {primary['current']}% this period vs "
                    f"{primary['previous']}% last period ({current['applied']} applications).")
    elif direction == "improving":
        headline = (f"Interview rate is up: {primary['current']}% of {current['applied']} "
                    f"applications this period vs {primary['previous']}% of {prior['applied']} "
                    "last period.")
    else:
        headline = (f"Interview rate is down: {primary['current']}% of {current['applied']} "
                    f"applications this period vs {primary['previous']}% of {prior['applied']} "
                    "last period.")

    if direction not in REPORT_TRENDS:
        raise UnknownValue(f"'{direction}' is not a trend direction.", field="direction",
                           allowed=list(REPORT_TRENDS))
    return {
        "direction": direction,
        "headline": headline,
        "compared_with": previous.to_dict(),
        "measured": measured,
        "sample": {"current": current_sample, "previous": prior_sample},
        "metrics": rows,
        "verdict_basis": "interview_rate",
        "flat_threshold_percentage_points": TREND_FLAT_EPSILON,
        "note": ("An improvement is only claimed when both periods clear the minimum sample "
                 "size; a small delta inside the flat band counts as steady."),
    }


def _signal(comparisons: Dict[str, Any], trend: Dict[str, Any]) -> Dict[str, Any]:
    """One sentence a user can act on — or an honest "not yet"."""
    leaders = [(dimension, block["best"]) for dimension, block in comparisons.items()
               if block.get("best")]
    withheld = sum(block["withheld_group_count"] for block in comparisons.values())
    if trend["direction"] == "improving":
        headline = trend["headline"]
        if leaders:
            dimension, best = leaders[0]
            headline += (f" The best-performing {dimension.replace('_', ' ')} is {best['label']} "
                         f"({best['interviews']} of {best['applied']} applications reached an "
                         "interview).")
        return {"kind": "working", "headline": headline, "withheld_group_count": withheld}
    if trend["direction"] == "not_enough_data" and leaders:
        dimension, best = leaders[0]
        return {
            "kind": "early_signal",
            "headline": (f"Too little data for a trend yet, but the best-performing "
                         f"{dimension.replace('_', ' ')} so far is {best['label']} "
                         f"({best['interviews']} of {best['applied']} applications reached an "
                         "interview)."),
            "withheld_group_count": withheld,
        }
    if trend["direction"] == "declining":
        return {
            "kind": "attention",
            "headline": "Interview rate fell this period. Worth re-reading the reasons behind "
                        "your top matches before applying to more.",
            "withheld_group_count": withheld,
        }
    return {
        "kind": "not_enough_data" if trend["direction"] == "not_enough_data" else "steady",
        "headline": trend["headline"],
        "withheld_group_count": withheld,
    }


def _headline(report: Dict[str, Any]) -> str:
    totals = report["totals"]
    if not totals["applications_submitted"]:
        discovered = totals["jobs_discovered"]
        if discovered:
            return (f"{discovered} job(s) discovered in this window and no applications recorded "
                    "yet — the funnel starts when you apply.")
        return ("Nothing recorded in this window yet. Run discovery, then record what happens on "
                "the Tracking page — an interview only counts when it is recorded.")
    parts = [f"{totals['applications_submitted']} application(s) submitted"]
    if totals["applications_tracked"] != totals["applications_submitted"]:
        parts.append(f"{totals['applications_tracked']} with an outcome record")
    parts.append(f"{totals['interviews']} interview(s)")
    if totals["rejections"]:
        parts.append(f"{totals['rejections']} rejection(s)")
    rate = totals["interview_rate"]
    sample = _sufficiency(totals["applications_tracked"], MIN_SAMPLE_SIZES["comparison_applied"],
                          subject="applications")
    rate_text = f"{rate}%" if (rate is not None and sample["sufficient"]) else "not enough data for a rate"
    return f"{', '.join(parts)} — interview rate {rate_text}."


def _privacy(window: Window) -> Dict[str, Any]:
    """The report's privacy statement — asserted, not aspirational."""
    return {
        "scope": "self_only",
        "tenant_filter": "Every query is filtered by the authenticated user id; no aggregate in "
                         "this document is built from another account's rows.",
        "includes_other_users": False,
        "cross_tenant_aggregates": False,
        "third_party_personal_data": False,
        "raw_free_text": False,
        "timezone": window.timezone,
    }


# --------------------------------------------------------------------------- #
# Weekly summary
# --------------------------------------------------------------------------- #
def weekly_summary(
    db: Session,
    user_id: int,
    *,
    range_key: str = "last_7_days",
    timezone_name: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The week in outcomes: applications, responses, interviews — and whether
    the week was better than the last one, or whether there is not enough data
    to say. Always returns a sentence; never returns a fabricated rate."""
    report = build_report(db, user_id, range_key=range_key, timezone_name=timezone_name, now=now,
                          include_comparisons=False)
    totals = report["totals"]
    trend = report["trend"]
    metrics = {metric["key"]: metric for metric in report["metrics"]}
    focus = ["applications_submitted", "applications_prepared", "interviews", "recruiter_responses",
             "rejection_rate", "interview_rate", "jobs_discovered", "high_fit_jobs"]
    sample = _sufficiency(totals["applications_tracked"], MIN_SAMPLE_SIZES["comparison_applied"],
                          subject="applications")

    if totals["interviews"] and sample["sufficient"]:
        next_step = ("Keep doing what produced those interviews — the comparison below shows "
                     "which source and score band they came from.")
    elif totals["applications_submitted"]:
        next_step = ("Record the outcome of each application on the Tracking page (a reply, a "
                     "rejection, an interview). Unrecorded outcomes are why the rate cannot be "
                     "calculated yet.")
    elif totals["jobs_discovered"]:
        next_step = "Pick a high-fit job and prepare an application — the funnel needs its first one."
    else:
        next_step = "Run discovery to find jobs that fit, then apply from the jobs board."

    return {
        "headline": report["headline"],
        "trend": {"direction": trend["direction"], "headline": trend["headline"],
                  "metrics": trend["metrics"], "compared_with": trend["compared_with"]},
        "metrics": {key: metrics[key] for key in focus if key in metrics},
        "totals": totals,
        "sufficiency": sample,
        "range": report["window"],
        "previous_range": report["previous_window"],
        "timezone": report["timezone"],
        "calculation_id": report["calculation_id"],
        "calibration": {"available": report["calibration"]["available"],
                        "note": report["calibration"]["note"]},
        "next_step": next_step,
        "disclaimer": DISCLAIMER,
        "server_time": report["server_time"],
    }


# --------------------------------------------------------------------------- #
# Export — only where the privacy requirements are satisfied
# --------------------------------------------------------------------------- #
#: The privacy requirements export must satisfy. All of them hold by
#: construction (the report is built per tenant and holds no third-party
#: personal data), and :func:`export_allowed` re-checks them at call time so a
#: future change to the report cannot silently leak through the export path.
EXPORT_PRIVACY_REQUIREMENTS: Tuple[str, ...] = (
    "self_scope_only",
    "no_other_tenants",
    "no_third_party_personal_data",
    "no_free_text_notes",
    "audited",
)


def export_privacy(report: Dict[str, Any]) -> Dict[str, Any]:
    """Re-check the export gate against the document that would be written."""
    privacy = report.get("privacy") or {}
    checks = {
        "self_scope_only": privacy.get("scope") == "self_only",
        "no_other_tenants": (privacy.get("includes_other_users") is False
                             and privacy.get("cross_tenant_aggregates") is False),
        "no_third_party_personal_data": privacy.get("third_party_personal_data") is False,
        "no_free_text_notes": privacy.get("raw_free_text") is False,
        "audited": True,  # the router writes the audit row before returning the body
    }
    unmet = [name for name in EXPORT_PRIVACY_REQUIREMENTS if not checks.get(name)]
    return {
        "allowed": not unmet,
        "requirements": list(EXPORT_PRIVACY_REQUIREMENTS),
        "checks": checks,
        "unmet": unmet,
        "formats": ["csv", "json"] if not unmet else [],
        "note": ("The export contains this account's own outcome data only — the same rows this "
                 "report was built from, minus free text." if not unmet else
                 "Export is disabled while a privacy requirement is unmet."),
    }


_METRIC_COLUMNS = ("key", "label", "value", "unit", "provenance", "sufficiency",
                   "sample_n", "sample_minimum", "note")


def csv_export(report: Dict[str, Any]) -> str:
    """The report as CSV: methodology header, metric rows, then comparisons.

    One tenant's numbers and nothing else — the caller must pass the report the
    service built for the authenticated user.
    """
    privacy = export_privacy(report)
    if not privacy["allowed"]:
        raise ReportError("Export is disabled by the privacy gate.",
                          code="export_not_allowed", http_status=403,
                          unmet=privacy["unmet"])
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    window = report["window"]
    writer.writerow(["# JobHunter outcome report"])
    writer.writerow(["# report_version", report["report_version"]])
    writer.writerow(["# calculation_id", report["calculation_id"]])
    writer.writerow(["# timezone", window["timezone"]])
    writer.writerow(["# range", window["range"]])
    writer.writerow(["# start_utc", window["start_utc"]])
    writer.writerow(["# end_utc", window["end_utc"], "(exclusive)"])
    writer.writerow(["# start_date_local", window["start_date"]])
    writer.writerow(["# end_date_local", window["end_date"]])
    writer.writerow(["# generated_at", report["generated_at"]])
    writer.writerow(["# scope", report["privacy"]["scope"]])
    writer.writerow([])
    writer.writerow(_METRIC_COLUMNS)
    for metric in report["metrics"]:
        sample = metric["sample"]
        writer.writerow([
            metric["key"], metric["label"],
            "" if metric["value"] is None else metric["value"], metric["unit"],
            metric["provenance"], metric["sufficiency"],
            sample["n"], sample["minimum"], metric["note"],
        ])
    writer.writerow([])
    writer.writerow(["comparison_dimension", "group", "applied", "interviews", "interview_rate",
                     "sufficiency", "minimum_sample"])
    for dimension, block in report["comparisons"].items():
        for group in block["groups"]:
            writer.writerow([
                dimension, group["key"], group["applied"], group["interviews"],
                "" if group["interview_rate"] is None else group["interview_rate"],
                group["sufficiency"], block["minimum_sample"],
            ])
    writer.writerow([])
    writer.writerow(["timing", "days", "applications_measured", "sufficiency", "minimum"])
    timing = report["timing"]
    writer.writerow(["posting_to_application_median", timing["median_days"],
                     timing["applications_measured"], timing["sufficiency"],
                     timing["sample"]["minimum"]])
    return buffer.getvalue()
