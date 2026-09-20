"""Outcome reporting — interviews and meaningful outcomes, not activity volume.

One test group per acceptance criterion, and each one pins the *honest* edge
rather than the happy path:

* **date ranges** — the window is half-open, so an application at the last
  instant is in and one at the boundary is out
  (:func:`test_window_boundaries_are_half_open`);
* **timezone boundaries** — the same instant lands on different local days in
  different zones, and the resolved window says which
  (:func:`test_a_day_boundary_is_local_not_utc`);
* **empty data** — a brand-new account gets a full document with ``None`` rates
  and a sentence, not a crash and not a 0%
  (:func:`test_empty_data_is_an_answer_not_a_crash`);
* **minimum sample sizes** — a group under the threshold has its rate withheld
  and labelled (:func:`test_a_group_below_the_minimum_sample_is_labelled`);
* **tenant safety** — one account's report never contains another's rows
  (:func:`test_reports_are_tenant_scoped`), and neither does the export;
* plus the two claims the product is not allowed to make: an interview
  probability before calibration
  (:func:`test_no_interview_probability_is_reported_before_calibration`) and an
  improvement that the sample cannot support
  (:func:`test_an_improvement_is_not_claimed_from_a_small_sample`).

Reproducibility is pinned by
:func:`test_the_same_inputs_produce_the_same_numbers`: same window, same rows,
same report — with ``generated_at`` the only field allowed to move.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.contracts.vocabulary import (
    REPORT_COMPARISON_DIMENSIONS,
    REPORT_PROVENANCE_KINDS,
    REPORT_RANGES,
    REPORT_SUFFICIENCY,
    TRACKING_INTERVIEW_STATES,
)
from app.models.models import (
    ApplicationPacket,
    ApplicationTracking,
    AuditLog,
    Job,
    PipelineJob,
    User,
)
from app.services import application_tracking as tracking

API = "/api/analytics/outcomes"
MIN = 5  # MIN_SAMPLE_SIZES["comparison_applied"] — the comparisons' floor


# --------------------------------------------------------------------------- #
# Fixtures & builders
# --------------------------------------------------------------------------- #
@pytest.fixture()
def user(db, owner) -> User:
    row = db.query(User).order_by(User.id).first()
    assert row is not None
    row.timezone = "UTC"
    db.commit()
    return row


@pytest.fixture()
def other_user(db, member) -> User:
    row = db.query(User).filter(User.email == "member@example.com").first()
    assert row is not None
    row.timezone = "UTC"
    db.commit()
    return row


def _job(db, user, *, title="Senior Backend Engineer", company="FinCo", source="lever",
         score=82.0, applied_at=None, posted_at=None, discovered_at=None, key=None) -> Job:
    row = Job(
        user_id=user.id, title=title, company=company,
        company_name_normalized=company.lower(), description="Python, FastAPI, PostgreSQL.",
        url=f"https://jobs.example/{company}", source=source,
        dedupe_key=key or f"{source}:{company}:{title}",
        status="applied" if applied_at else "discovered", score=score,
        discovered_at=discovered_at or datetime.utcnow(),
        applied_at=applied_at, posted_at=posted_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _track(db, user, job, *, applied_at, state="applied", source="lever",
           artifact_kind="packet", artifact_version=3, band="strong",
           score=82.0, responded=False, interview_at=None, role_family=None) -> ApplicationTracking:
    row = ApplicationTracking(
        user_id=user.id, job_id=job.id, state=state, phase="post_application",
        state_origin="user_reported", source=source,
        applied_at=applied_at,
        first_response_at=(applied_at + timedelta(days=1)) if responded else None,
        interview_scheduled_at=interview_at,
        match_score=score, match_band=band, artifact_kind=artifact_kind,
        artifact_version=artifact_version,
        role_family=role_family or "Backend Engineer",
        job_title_snapshot=job.title, company_snapshot=job.company,
        event_count=4, correction_count=1,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _cohort(db, user, *, count: int, interviews: int, days_ago: int = 3,
            source: str = "lever", band: str = "strong", score: float = 82.0) -> None:
    """``count`` tracked applications, ``interviews`` of them hired-to-interview."""
    for index in range(count):
        job = _job(db, user, key=f"{source}:{band}:{index}",
                   applied_at=datetime.utcnow() - timedelta(days=days_ago))
        _track(db, user, job, applied_at=job.applied_at, source=source, band=band, score=score,
               state="interview_scheduled" if index < interviews else "applied",
               interview_at=(job.applied_at + timedelta(days=2)) if index < interviews else None)


def _totals(body: dict) -> dict:
    return body["totals"]


# --------------------------------------------------------------------------- #
# Windows: date ranges
# --------------------------------------------------------------------------- #
def test_window_boundaries_are_half_open(client, auth, db, user):
    """An application on the first day is in; one at the end instant is out."""
    window_start = datetime(2026, 9, 1, 0, 0, 0)   # UTC, and the account is UTC
    inside = _job(db, user, key="inside", applied_at=window_start)
    _track(db, user, inside, applied_at=window_start)
    boundary = _job(db, user, key="boundary", applied_at=datetime(2026, 9, 30, 0, 0, 0))
    _track(db, user, boundary, applied_at=boundary.applied_at)
    before = _job(db, user, key="before", applied_at=window_start - timedelta(seconds=1))
    _track(db, user, before, applied_at=before.applied_at)

    body = client.get(
        f"{API}?range=custom&since=2026-09-01&until=2026-09-29",
        headers=auth,
    )
    assert body.status_code == 200, body.text
    payload = body.json()

    assert payload["window"]["start_utc"] == "2026-09-01T00:00:00Z"
    # A date-only `until` means "through that local day": the exclusive instant
    # is the next local midnight.
    assert payload["window"]["end_utc"] == "2026-09-30T00:00:00Z"
    assert payload["window"]["end_date"] == "2026-09-29"
    assert payload["window"]["boundary"] == "half-open [start_utc, end_utc)"
    assert _totals(payload)["applications_tracked"] == 1, (
        "the boundary application must fall outside, the first-day one inside"
    )


def test_a_day_boundary_is_local_not_utc(client, auth, db, user):
    """23:30 in Tokyo is the *next* UTC day; the window has to agree with the user.

    The two applications below are one hour apart in UTC and land on different
    Tokyo days. In UTC both are on 30 September; in Asia/Tokyo the first is on
    the 30th and the second on the 1st — so a window of 30 September in Tokyo
    holds exactly one of them.
    """
    late = _job(db, user, key="late", applied_at=datetime(2026, 9, 30, 14, 30))   # 23:30 JST
    _track(db, user, late, applied_at=late.applied_at)
    after_midnight = _job(db, user, key="after", applied_at=datetime(2026, 9, 30, 15, 30))  # 00:30 JST
    _track(db, user, after_midnight, applied_at=after_midnight.applied_at)

    utc = client.get(f"{API}?range=custom&since=2026-09-30&until=2026-09-30"
                     "&timezone=UTC", headers=auth).json()
    tokyo = client.get(f"{API}?range=custom&since=2026-09-30&until=2026-09-30"
                       "&timezone=Asia/Tokyo", headers=auth).json()

    assert utc["window"]["start_utc"] == "2026-09-30T00:00:00Z"
    assert utc["window"]["end_utc"] == "2026-10-01T00:00:00Z"
    assert tokyo["window"]["start_utc"] == "2026-09-29T15:00:00Z"
    assert tokyo["window"]["end_utc"] == "2026-09-30T15:00:00Z"

    assert _totals(utc)["applications_tracked"] == 2, "UTC still has 30 September open"
    assert _totals(tokyo)["applications_tracked"] == 1, (
        "the application sent 00:30 JST belongs to the next Tokyo day"
    )
    assert tokyo["timezone"] == "Asia/Tokyo"


def test_the_named_ranges_are_whole_local_days(client, auth, user):
    for name in REPORT_RANGES:
        if name == "custom":
            continue
        body = client.get(f"{API}?range={name}&timezone=Europe/London", headers=auth)
        assert body.status_code == 200, f"{name}: {body.text}"
        payload = body.json()
        assert payload["window"]["timezone"] == "Europe/London"
        assert payload["window"]["end_local"].endswith("+01:00") or \
               payload["window"]["end_local"].endswith("+00:00")
        assert payload["window"]["days"] >= 1
        assert payload["window"]["start_local"].endswith("T00:00:00+01:00") or \
               payload["window"]["start_local"].endswith("T00:00:00+00:00")

    # The rolling ranges are stable within a day (they start at local midnight),
    # which is what makes two reports comparable.
    today = client.get(f"{API}?range=last_7_days&timezone=Europe/London", headers=auth).json()
    assert today["window"]["start_local"].endswith("T00:00:00+01:00") or \
           today["window"]["start_local"].endswith("T00:00:00+00:00")


def test_an_unknown_range_or_timezone_is_a_422(client, auth):
    bad_range = client.get(f"{API}?range=fortnight", headers=auth)
    assert bad_range.status_code == 422
    assert bad_range.json()["detail"]["code"] == "validation_error"
    bad_zone = client.get(f"{API}?timezone=Mars/Olympus_Mons", headers=auth)
    assert bad_zone.status_code == 422
    assert bad_zone.json()["detail"]["field"] == "timezone"

    backwards = client.get(f"{API}?range=custom&since=2026-09-10&until=2026-09-01", headers=auth)
    assert backwards.status_code == 422


def test_the_previous_window_is_the_same_length_and_ends_where_this_one_starts(client, auth):
    payload = client.get(f"{API}?range=last_30_days", headers=auth).json()
    assert payload["previous_window"]["end_utc"] == payload["window"]["start_utc"]
    assert payload["previous_window"]["days"] == payload["window"]["days"]


# --------------------------------------------------------------------------- #
# Empty data
# --------------------------------------------------------------------------- #
def test_empty_data_is_an_answer_not_a_crash(client, auth):
    payload = client.get(f"{API}?range=last_30_days", headers=auth).json()

    assert _totals(payload)["applications_submitted"] == 0
    assert _totals(payload)["interview_rate"] is None
    for metric in payload["metrics"]:
        assert metric["sufficiency"] in REPORT_SUFFICIENCY
        if metric["unit"] == "percent":
            assert metric["value"] is None or metric["sample"]["sufficient"] is False

    # Nothing is compared, nothing is claimed, and the user is told what to do.
    assert payload["comparisons"]["source"]["groups"] == []
    assert payload["comparisons"]["source"]["best"] is None
    assert payload["trend"]["direction"] == "not_enough_data"
    assert "Nothing recorded in this window yet" in payload["headline"]
    assert payload["calibration"]["available"] is False
    assert payload["timing"]["days"] is None
    assert payload["timing"]["sample"]["state"] == "insufficient"
    assert payload["funnel"] == {
        "jobs_discovered": 0, "high_fit_jobs": 0, "applications_prepared": 0,
        "applications_submitted": 0, "applications_tracked": 0, "recruiter_responses": 0,
        "interviews": 0, "offers": 0, "rejections": 0,
    }


def test_an_empty_week_still_says_something(client, auth):
    body = client.get(f"{API}/weekly", headers=auth)
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["headline"]
    assert payload["trend"]["direction"] == "not_enough_data"
    assert payload["sufficiency"]["state"] == "insufficient"
    assert payload["next_step"]
    assert payload["metrics"]["interview_rate"]["value"] is None


def test_the_weekly_summary_is_the_same_calculation_as_the_dashboard(client, auth, db, user):
    """Two surfaces, one number: the dashboard's week and the report's week."""
    _cohort(db, user, count=1, interviews=0)

    weekly = client.get(f"{API}/weekly", headers=auth).json()
    dashboard = client.get("/api/me/dashboard", headers=auth).json()["weekly_report"]

    assert dashboard["applications_submitted"] == weekly["totals"]["applications_submitted"] == 1
    assert dashboard["timezone"] == weekly["timezone"] == "UTC"
    assert dashboard["window"] == weekly["range"]
    assert set(dashboard["window"]) >= {"start_utc", "end_utc", "timezone", "boundary"}
    assert dashboard["trend_direction"] == weekly["trend"]["direction"]


# --------------------------------------------------------------------------- #
# Minimum sample sizes
# --------------------------------------------------------------------------- #
def test_a_group_below_the_minimum_sample_is_labelled(client, auth, db, user):
    """Four applications is not a conversion rate; it is four applications."""
    _cohort(db, user, count=MIN - 1, interviews=2, source="remoteok")

    payload = client.get(f"{API}?range=last_30_days", headers=auth).json()
    group = payload["comparisons"]["source"]["groups"][0]

    assert group["key"] == "remoteok"
    assert group["applied"] == MIN - 1
    assert group["interviews"] == 2, "the count is a fact and stays visible"
    assert group["sufficiency"] == "insufficient"
    assert group["interview_rate"] is None, "no percentage over four applications"
    assert group["sample"] == {"n": MIN - 1, "minimum": MIN, "state": "insufficient",
                              "sufficient": False,
                              "label": f"Not enough data — {MIN - 1} of {MIN} applications"}
    assert group["insufficient_reason"] == group["sample"]["label"]
    assert payload["comparisons"]["source"]["withheld_group_count"] == 1
    assert payload["comparisons"]["source"]["best"] is None, "no best-of-one"
    # The window's own headline rate is gated by the same rule — and so is the
    # raw tally, so no surface can print a percentage the sample cannot carry.
    rate = next(metric for metric in payload["metrics"] if metric["key"] == "interview_rate")
    assert rate["value"] is None
    assert rate["sufficiency"] == "insufficient"
    assert payload["totals"]["interview_rate"] is None
    assert payload["totals"]["interviews"] == 2, "the count is still a fact"


def test_the_minimum_sample_clears_and_the_rate_appears(client, auth, db, user):
    _cohort(db, user, count=MIN, interviews=2, source="remoteok")

    payload = client.get(f"{API}?range=last_30_days", headers=auth).json()
    group = payload["comparisons"]["source"]["groups"][0]
    assert group["sufficiency"] == "sufficient"
    assert group["interview_rate"] == 40.0
    assert group["insufficient_reason"] is None
    assert payload["comparisons"]["source"]["withheld_group_count"] == 0

    # A single group is not a comparison: "best" needs a rival.
    assert payload["comparisons"]["source"]["best"] is None
    _cohort(db, user, count=MIN, interviews=4, source="linkedin")
    payload = client.get(f"{API}?range=last_30_days", headers=auth).json()
    best = payload["comparisons"]["source"]["best"]
    assert best and best["key"] == "linkedin" and best["interview_rate"] == 80.0
    assert payload["signal"]["kind"] in ("early_signal", "not_enough_data", "working", "steady")


def test_every_comparison_dimension_is_gated_and_present(client, auth, db, user):
    _cohort(db, user, count=MIN, interviews=1)
    payload = client.get(f"{API}?range=last_30_days", headers=auth).json()

    assert set(payload["comparisons"]) == set(REPORT_COMPARISON_DIMENSIONS)
    for dimension, block in payload["comparisons"].items():
        assert block["minimum_sample"] == MIN
        for group in block["groups"]:
            if not group["sample"]["sufficient"]:
                assert group["interview_rate"] is None, dimension
            assert group["sufficiency"] in REPORT_SUFFICIENCY


# --------------------------------------------------------------------------- #
# Estimates vs observations, and the probability gate
# --------------------------------------------------------------------------- #
def test_every_number_says_whether_it_was_observed_or_estimated(client, auth, db, user):
    _cohort(db, user, count=MIN, interviews=2)
    payload = client.get(f"{API}?range=last_30_days", headers=auth).json()

    provenance = {metric["key"]: metric["provenance"] for metric in payload["metrics"]}
    assert set(provenance.values()) <= set(REPORT_PROVENANCE_KINDS)
    assert provenance["jobs_discovered"] == "observed"
    assert provenance["applications_submitted"] == "observed"
    assert provenance["interviews"] == "user_reported"
    assert provenance["interview_rate"] == "derived"
    assert provenance["high_fit_jobs"] == "estimated", (
        "a match score is a model's opinion; the report says so"
    )
    high_fit = next(m for m in payload["metrics"] if m["key"] == "high_fit_jobs")
    assert "probability" in high_fit["note"]
    assert payload["definitions"]["formulae"]["interview_rate"]
    assert payload["definitions"]["report_version"] == payload["report_version"]


def test_no_interview_probability_is_reported_before_calibration(client, auth, db, user):
    _cohort(db, user, count=MIN + 3, interviews=3)
    payload = client.get(f"{API}?range=last_30_days", headers=auth).json()

    calibration = payload["calibration"]
    assert calibration["available"] is False
    assert calibration["interview_probability"] is None
    assert "recorded_interviews" in calibration["missing"]
    assert "calibrated_model_version" in calibration["missing"]
    assert calibration["observed"]["recorded_interviews"] == 3
    assert calibration["observed"]["score_bands_with_outcomes"] >= 1
    assert "probability" in calibration["note"]

    # …and the guard a future caller would reach for refuses rather than guesses.
    from app.services.outcome_report import CalibrationUnavailable, interview_probability

    with pytest.raises(CalibrationUnavailable):
        interview_probability(score=88.0, band="strong")

    # Nothing anywhere in the document is a probability.
    blob = json.dumps(payload).lower()
    assert "interview_probability" in blob  # the explicit *absence* is stated
    assert payload["calibration"]["interview_probability"] is None


def test_high_fit_uses_one_threshold_across_the_product(client, auth, db, user):
    from app.contracts.vocabulary import HIGH_FIT_SCORE
    from app.services import user_dashboard

    assert user_dashboard.STRONG_MATCH_SCORE == HIGH_FIT_SCORE
    _job(db, user, key="borderline", score=HIGH_FIT_SCORE, discovered_at=datetime.utcnow())
    _job(db, user, key="just-below", score=HIGH_FIT_SCORE - 0.1, discovered_at=datetime.utcnow())

    payload = client.get(f"{API}?range=last_7_days", headers=auth).json()
    assert payload["funnel"]["jobs_discovered"] == 2
    assert payload["funnel"]["high_fit_jobs"] == 1


# --------------------------------------------------------------------------- #
# The trend: is this working?
# --------------------------------------------------------------------------- #
def test_an_improvement_is_not_claimed_from_a_small_sample(client, auth, db, user):
    _cohort(db, user, count=2, interviews=2, days_ago=3)
    _cohort(db, user, count=2, interviews=0, days_ago=10, source="remoteok")

    payload = client.get(f"{API}?range=last_7_days", headers=auth).json()
    trend = payload["trend"]
    assert trend["direction"] == "not_enough_data", "2 applications is not evidence"
    assert trend["measured"] is False
    assert "Not enough recorded outcomes" in trend["headline"]
    assert trend["metrics"]["interview_rate"] == {"current": None, "previous": None, "delta": None}
    assert payload["signal"]["kind"] in ("not_enough_data", "early_signal")


def test_an_improvement_is_claimed_when_both_periods_carry_the_sample(client, auth, db, user):
    _cohort(db, user, count=MIN, interviews=4, days_ago=3)                       # this week
    _cohort(db, user, count=MIN, interviews=1, days_ago=10, source="remoteok")   # last week

    payload = client.get(f"{API}?range=last_7_days", headers=auth).json()
    trend = payload["trend"]
    assert trend["measured"] is True
    assert trend["direction"] == "improving"
    assert trend["metrics"]["interview_rate"]["current"] == 80.0
    assert trend["metrics"]["interview_rate"]["previous"] == 20.0
    assert trend["metrics"]["interview_rate"]["delta"] == 60.0
    assert trend["verdict_basis"] == "interview_rate"
    assert "up" in trend["headline"]
    assert payload["signal"]["kind"] == "working"


def test_a_decline_is_reported_as_a_decline(client, auth, db, user):
    _cohort(db, user, count=MIN, interviews=0, days_ago=3)
    _cohort(db, user, count=MIN, interviews=3, days_ago=10, source="remoteok")

    payload = client.get(f"{API}?range=last_7_days", headers=auth).json()
    assert payload["trend"]["direction"] == "declining"
    assert payload["signal"]["kind"] == "attention"


# --------------------------------------------------------------------------- #
# The rest of the required metrics
# --------------------------------------------------------------------------- #
def test_the_funnel_counts_preparation_submission_and_outcomes(client, auth, db, user):
    job = _job(db, user, applied_at=datetime.utcnow() - timedelta(days=2), key="funnel-1")
    db.add(ApplicationPacket(user_id=user.id, job_id=job.id, version=1, status="approved",
                             job_title=job.title, company=job.company))
    db.commit()
    _track(db, user, job, applied_at=job.applied_at, state="rejected_by_employer", responded=True)

    payload = client.get(f"{API}?range=last_7_days", headers=auth).json()
    funnel = payload["funnel"]
    assert funnel["applications_prepared"] == 1
    assert funnel["applications_submitted"] == 1
    assert funnel["applications_tracked"] == 1
    assert funnel["recruiter_responses"] == 1
    assert funnel["interviews"] == 0
    assert funnel["rejections"] == 1
    assert payload["quality"]["correction_rate"] is None, (
        "four events is below the correction-rate minimum sample"
    )
    assert payload["quality"]["events"] == 4


def test_an_application_the_board_knows_about_without_a_record_is_a_coverage_gap(client, auth, db, user):
    """Never assumed to have failed — reported as a gap in the data."""
    _job(db, user, applied_at=datetime.utcnow() - timedelta(days=1), key="board-only")
    payload = client.get(f"{API}?range=last_7_days", headers=auth).json()

    assert payload["funnel"]["applications_submitted"] == 1
    assert payload["funnel"]["applications_tracked"] == 0
    assert payload["quality"]["applications_without_a_tracking_record"] == 1
    assert payload["quality"]["tracking_coverage_percent"] == 0.0
    submitted = next(m for m in payload["metrics"] if m["key"] == "applications_submitted")
    assert "no tracking record" in submitted["note"]


def test_correction_and_failure_rates_appear_once_their_sample_clears(client, auth, db, user):
    _cohort(db, user, count=MIN, interviews=1)
    # Correction rate needs 20 recorded events; the cohort above seeds 4 each.
    for row in db.query(ApplicationTracking).filter(ApplicationTracking.user_id == user.id).all():
        row.event_count = 5
        row.correction_count = 1
    for index in range(10):
        db.add(PipelineJob(user_id=user.id, pipeline="application", status="failed",
                           dedupe_key=f"attempt-{index}", finished_at=datetime.utcnow() - timedelta(days=1)))
    db.commit()

    payload = client.get(f"{API}?range=last_7_days", headers=auth).json()
    assert payload["quality"]["events"] == MIN * 5
    assert payload["quality"]["correction_rate"] == 20.0
    assert payload["quality"]["application_attempts"] == 10
    assert payload["quality"]["application_failures"] == 10
    assert payload["quality"]["application_failure_rate"] == 100.0

    metric = next(m for m in payload["metrics"] if m["key"] == "application_failure_rate")
    assert metric["value"] == 100.0
    assert metric["sufficiency"] == "sufficient"
    assert metric["provenance"] == "derived"


def test_time_from_posting_to_application_is_measured_and_disclosed(client, auth, db, user):
    now = datetime.utcnow()
    for index in range(MIN):
        job = _job(db, user, key=f"posted-{index}",
                   applied_at=now - timedelta(days=1),
                   posted_at=now - timedelta(days=1 + (index + 1) * 2))
        _track(db, user, job, applied_at=job.applied_at)
    payload = client.get(f"{API}?range=last_7_days", headers=auth).json()
    timing = payload["timing"]
    assert timing["sample"]["n"] == MIN
    assert timing["sufficiency"] == "sufficient"
    assert timing["median_days"] == 6.0     # 2, 4, 6, 8, 10 → median 6
    assert timing["mean_days"] == 6.0

    # An application dated before its posting is disclosed, not hidden.
    job = _job(db, user, key="repost", applied_at=now - timedelta(days=1),
               posted_at=now)
    _track(db, user, job, applied_at=job.applied_at)
    payload = client.get(f"{API}?range=last_7_days", headers=auth).json()
    assert payload["timing"]["applications_dated_before_posting"] == 1
    assert "before their posting" in payload["timing"]["note"]


# --------------------------------------------------------------------------- #
# Reproducibility
# --------------------------------------------------------------------------- #
def test_the_same_inputs_produce_the_same_numbers(client, auth, db, user):
    _cohort(db, user, count=MIN, interviews=2)
    query = f"{API}?range=last_30_days&timezone=Asia/Tokyo"

    first = client.get(query, headers=auth).json()
    second = client.get(query, headers=auth).json()

    assert first["calculation_id"] == second["calculation_id"]
    for field in ("generated_at", "server_time"):
        first.pop(field)
        second.pop(field)
    assert first == second, "same window, same rows, same report"

    other = client.get(f"{API}?range=last_7_days&timezone=Asia/Tokyo", headers=auth).json()
    assert other["calculation_id"] != first["calculation_id"], (
        "a different window is a different calculation"
    )
    assert first["definitions"]["minimum_sample_sizes"] == other["definitions"]["minimum_sample_sizes"]


def test_the_group_order_is_stable(client, auth, db, user):
    _cohort(db, user, count=MIN, interviews=1, source="lever")
    _cohort(db, user, count=MIN, interviews=1, source="remoteok")
    _cohort(db, user, count=MIN, interviews=1, source="adzuna", band="good", score=72.0)

    first = [g["key"] for g in client.get(f"{API}?range=last_30_days", headers=auth)
             .json()["comparisons"]["score_band"]["groups"]]
    second = [g["key"] for g in client.get(f"{API}?range=last_30_days", headers=auth)
              .json()["comparisons"]["score_band"]["groups"]]
    assert first == second
    # Bands read best-fit-first, with the unscored tail last.
    assert first.index("strong") < first.index("good")


# --------------------------------------------------------------------------- #
# Tenancy and privacy
# --------------------------------------------------------------------------- #
def test_reports_are_tenant_scoped(client, auth, member_auth, db, user, other_user):
    _cohort(db, user, count=MIN, interviews=4, source="lever")
    _cohort(db, other_user, count=MIN, interviews=0, source="secretboard")

    mine = client.get(f"{API}?range=last_30_days", headers=auth).json()
    theirs = client.get(f"{API}?range=last_30_days", headers=member_auth).json()

    assert _totals(mine)["interviews"] == 4
    assert _totals(theirs)["interviews"] == 0
    assert {g["key"] for g in mine["comparisons"]["source"]["groups"]} == {"lever"}
    assert {g["key"] for g in theirs["comparisons"]["source"]["groups"]} == {"secretboard"}
    assert "secretboard" not in json.dumps(mine)
    assert "lever" not in json.dumps(theirs)
    assert mine["privacy"]["scope"] == "self_only"
    assert mine["privacy"]["includes_other_users"] is False
    assert mine["privacy"]["cross_tenant_aggregates"] is False


def test_the_export_gate_checks_the_document_it_would_write(client, auth, db, user):
    from app.services.outcome_report import EXPORT_PRIVACY_REQUIREMENTS, export_privacy

    _cohort(db, user, count=MIN, interviews=2)
    payload = client.get(f"{API}?range=last_30_days", headers=auth).json()

    gate = export_privacy(payload)
    assert gate["allowed"] is True
    assert gate["unmet"] == []
    assert gate["formats"] == ["csv", "json"]

    tampered = json.loads(json.dumps(payload))
    tampered["privacy"]["includes_other_users"] = True
    refused = export_privacy(tampered)
    assert refused["allowed"] is False
    assert "no_other_tenants" in refused["unmet"]
    assert set(refused["requirements"]) == set(EXPORT_PRIVACY_REQUIREMENTS)


def test_the_export_is_csv_with_the_methodology_and_only_this_tenants_rows(client, auth, db, user):
    _cohort(db, user, count=MIN, interviews=2, source="lever")

    response = client.get(f"{API}/export?format=csv&range=last_30_days", headers=auth)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    body = response.text
    assert "# report_version" in body
    assert "# timezone,UTC" in body
    assert "# end_utc" in body and "(exclusive)" in body
    assert "# scope,self_only" in body
    assert "interview_rate" in body
    assert "comparison_dimension" in body
    assert "Not enough data" not in body or "insufficient" in body


def test_the_export_is_audited_before_the_body_is_produced(client, auth, db, user):
    _cohort(db, user, count=MIN, interviews=2)
    response = client.get(f"{API}/export?format=json&range=last_30_days", headers=auth)
    assert response.status_code == 200
    assert response.json()["privacy"]["scope"] == "self_only"

    rows = db.query(AuditLog).filter(AuditLog.action == "analytics.report_exported").all()
    assert len(rows) == 1, "downloading your outcome data is an auditable action"
    assert rows[0].user_id == user.id


def test_cross_tenant_exports_are_impossible_by_construction(client, auth, member_auth, db,
                                                             user, other_user):
    _cohort(db, user, count=MIN, interviews=3, source="lever")
    _cohort(db, other_user, count=MIN, interviews=0, source="secretboard")

    mine = client.get(f"{API}/export?format=csv", headers=auth).text
    theirs = client.get(f"{API}/export?format=csv", headers=member_auth).text

    assert "lever" in mine and "secretboard" not in mine
    assert "secretboard" in theirs and "lever" not in theirs


def test_an_unsupported_export_format_is_refused(client, auth):
    response = client.get(f"{API}/export?format=xlsx", headers=auth)
    assert response.status_code == 422
    assert response.json()["detail"]["allowed"] == ["csv", "json"]


# --------------------------------------------------------------------------- #
# The reads stay honest about what they are
# --------------------------------------------------------------------------- #
def test_the_reports_require_authentication(client):
    assert client.get(API).status_code == 401
    assert client.get(f"{API}/weekly").status_code == 401
    assert client.get(f"{API}/export").status_code == 401


def test_the_weekly_read_only_covers_a_week(client, auth):
    response = client.get(f"{API}/weekly?range=last_90_days", headers=auth)
    assert response.status_code == 422
    assert response.json()["detail"]["allowed"] == ["last_7_days", "this_week"]


def test_the_report_disclaims_what_it_is_not(client, auth, db, user):
    _cohort(db, user, count=MIN, interviews=2)
    payload = client.get(f"{API}?range=last_30_days", headers=auth).json()
    assert "never inferred from email" in payload["disclaimer"]
    assert "not an interview probability" in payload["disclaimer"]
    assert set(payload["definitions"]["interview_states"]) == set(TRACKING_INTERVIEW_STATES)
    assert payload["privacy"]["third_party_personal_data"] is False


def test_the_tracking_report_still_answers_the_same_question(client, auth, db, user):
    """The older grouped read is untouched by this layer (regression guard)."""
    _cohort(db, user, count=MIN, interviews=2)
    body = client.get("/api/application-tracking/report?group_by=source", headers=auth)
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["totals"]["applied"] == MIN
    assert payload["totals"]["interviews"] == 2
    assert payload["totals"]["application_to_interview_rate"] == 40.0
    assert "artifact" in payload["groups"][0]["key"] or payload["groups"][0]["key"] == "lever"


def test_the_report_uses_the_users_stored_timezone_by_default(client, auth, db, user):
    user.timezone = "Pacific/Auckland"
    db.commit()

    payload = client.get(f"{API}?range=last_7_days", headers=auth).json()
    assert payload["timezone"] == "Pacific/Auckland"
    assert payload["window"]["timezone"] == "Pacific/Auckland"

    # An unusable stored zone falls back to UTC rather than blanking the page,
    # and the weekly dashboard read survives it too.
    user.timezone = "Not/AZone"
    db.commit()
    assert client.get(f"{API}?range=last_7_days", headers=auth).status_code == 422
    dashboard = client.get("/api/me/dashboard", headers=auth)
    assert dashboard.status_code == 200
    assert dashboard.json()["weekly_report"]["timezone"] == "UTC"


def test_an_out_of_vocabulary_origin_is_never_silently_counted(client, auth, db, user):
    """An origin the vocabulary does not know is visible, not folded into a rate."""
    job = _job(db, user, applied_at=datetime.utcnow() - timedelta(days=1), key="weird")
    record = _track(db, user, job, applied_at=job.applied_at)
    record.state_origin = "telepathy"
    db.commit()

    payload = client.get(f"{API}?range=last_7_days", headers=auth).json()
    assert payload["quality"]["by_origin"] == {"telepathy": 1}
    assert payload["calibration"]["observed"]["share_verified_outcomes"] == 0.0


def test_reporting_helpers_are_pure():
    """The windows are a pure function of (range, tz, now) — the point of them."""
    from app.services.outcome_report import previous_window, resolve_window

    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    tokyo = resolve_window(range_key="last_7_days", timezone_name="Asia/Tokyo", now=now)
    assert tokyo.start_local.isoformat() == "2026-09-14T00:00:00+09:00"
    assert tokyo.end_local.isoformat() == "2026-09-21T00:00:00+09:00"
    assert tokyo.start_utc == datetime(2026, 9, 13, 15, 0)
    assert tokyo.end_utc == datetime(2026, 9, 20, 15, 0)
    assert tokyo.days == 7
    assert tokyo.start_date == "2026-09-14" and tokyo.end_date == "2026-09-20"

    prior = previous_window(tokyo)
    assert prior.end_utc == tokyo.start_utc
    assert prior.start_utc == tokyo.start_utc - timedelta(days=7)
    assert prior.timezone == tokyo.timezone

    # Week-to-date is the one partial window, and it starts on Monday.
    monday = datetime(2026, 9, 16, 9, 30, tzinfo=timezone.utc)  # a Wednesday
    week = resolve_window(range_key="this_week", timezone_name="UTC", now=monday)
    assert week.start_local.isoformat() == "2026-09-14T00:00:00+00:00"
    last_week = resolve_window(range_key="last_week", timezone_name="UTC", now=monday)
    assert last_week.start_date == "2026-09-07"
    assert last_week.end_date == "2026-09-13"

    # DST is the zone's business, not ours: London's 30-day window crosses the
    # October change and the UTC instants shift with it.
    autumn = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    london = resolve_window(range_key="last_30_days", timezone_name="Europe/London", now=autumn)
    assert london.end_local.isoformat() == "2026-11-02T00:00:00+00:00"
    assert london.start_local.utcoffset() == timedelta(hours=1), (
        "the first day of the window is still BST"
    )
    assert london.start_utc.astimezone(ZoneInfo("UTC")).hour == 23


def test_the_artifact_comparison_groups_by_the_frozen_snapshot(client, auth, db, user):
    _cohort(db, user, count=MIN, interviews=3)
    for index in range(MIN):
        job = _job(db, user, key=f"nofile-{index}", applied_at=datetime.utcnow() - timedelta(days=2))
        _track(db, user, job, applied_at=job.applied_at, artifact_kind="none",
               artifact_version=None, source="remoteok", state="applied")

    payload = client.get(f"{API}?range=last_30_days", headers=auth).json()
    groups = {g["key"]: g for g in payload["comparisons"]["artifact"]["groups"]}
    assert groups["packet:v3"]["interviews"] == 3
    assert groups["packet:v3"]["interview_rate"] == 60.0
    assert groups["none"]["interviews"] == 0
    assert groups["none"]["sufficiency"] == "sufficient"


def test_the_comparison_summary_reports_what_was_withheld(client, auth, db, user):
    _cohort(db, user, count=MIN, interviews=2, source="lever")
    _cohort(db, user, count=2, interviews=1, source="tinyboard")

    payload = client.get(f"{API}?range=last_30_days", headers=auth).json()
    block = payload["comparisons"]["source"]
    assert block["group_count"] == 2
    assert block["sufficient_group_count"] == 1
    assert block["withheld_group_count"] == 1
    # The withheld group is still listed, with its reason attached.
    tiny = next(g for g in block["groups"] if g["key"] == "tinyboard")
    assert tiny["insufficient_reason"] and tiny["interview_rate"] is None
    assert payload["signal"]["withheld_group_count"] == 1
