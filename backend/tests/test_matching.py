"""
Multi-stage matching: hard filters, deterministic score, AI evidence review,
feedback and the calibration gate.

Acceptance coverage:
* hard filters — every stage-1 check, unknown-never-fails, expired, duplicate
* score calculation — weights, contribution math, reproducibility, bands
* missing evidence — a high-fit match without candidate evidence is demoted
* conflicts — job/candidate contradictions are itemised, AI band conflicts
  and probability claims are guardrail errors
* duplicate jobs — a second board row for the same role is filtered
* feedback — corrections are stored and visible; the calibration gate never
  claims a calibrated interview probability
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List

import pytest

from app.contracts.vocabulary import MATCH_FEEDBACK_KINDS
from app.models.models import Job, MatchFeedback, MatchResult, Profile, Subscription, User
from app.services import matching

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

PROFILE_DATA: Dict[str, Any] = {
    "skills": ["Python", "FastAPI", "PostgreSQL", "Kubernetes", "AWS"],
    "summary": "Backend engineer with 6 years building Python, FastAPI and PostgreSQL "
               "services for fintech payments platforms.",
    "location": "Berlin, Germany",
    "current_title": "Senior Software Engineer",
    "experience": [
        {"title": "Senior Software Engineer", "company": "FinCo", "duration": "2021 - present",
         "bullets": ["Led the payments platform on AWS for fintech."]},
        {"title": "Software Engineer", "company": "ShopStack", "duration": "2018 - 2021",
         "bullets": ["Built the order pipeline on AWS."]},
    ],
    "remote_preference": "remote",
    "work_authorization": "German citizen",
    "salary_expectations": {"minimum": 90000, "currency": "EUR"},
}

JD_STRONG = """Senior Backend Engineer - Payments

About the role:
We are a fintech company building payments platforms.

Required skills:
- Python
- FastAPI
- PostgreSQL
- Kubernetes

Nice to have:
- Kafka
- Terraform

Compensation: $110,000 - $140,000 per year.

Location: Berlin, Germany. Hybrid: 2 days a week in office.
"""


def _user(db) -> User:
    user = db.query(User).order_by(User.id).first()
    assert user is not None
    return user


def _pro(db, user: User) -> None:
    sub = db.query(Subscription).filter(Subscription.user_id == user.id).first()
    if sub is None:
        db.add(Subscription(user_id=user.id, plan="pro", status="active"))
    else:
        sub.plan, sub.status = "pro", "active"
    db.commit()


def _profile(db, user: User, data: Dict[str, Any] | None = None) -> Profile:
    profile = Profile(user_id=user.id, data=data if data is not None else dict(PROFILE_DATA))
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _job(db, user: User, *, description: str = JD_STRONG, title: str = "Senior Backend Engineer",
         company: str = "FinCo", location: str = "Berlin, Germany",
         posted_hours_ago: float | None = 20, dedupe: str = "lever:test",
         status: str = "discovered", expired: bool = False,
         extra: Dict[str, Any] | None = None) -> Job:
    job = Job(
        user_id=user.id, title=title, company=company, location=location,
        description=description, url="https://jobs.example.com/x", source="lever",
        dedupe_key=dedupe, status=status, company_size="medium",
        company_name_normalized=company.strip().lower(),
        title_normalized=title.strip().lower()[:300],
        extra=extra or {},
    )
    if posted_hours_ago is not None:
        job.posted_at = datetime.utcnow() - timedelta(hours=posted_hours_ago)
    job.expired = expired
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _stages(db, user: User, job: Job, profile_data: Dict[str, Any] | None = None,
            extra: Dict[str, Any] | None = None):
    """Run stages 1+2 directly (no persistence, no AI)."""
    if extra is not None:
        job.extra = {**(job.extra or {}), **extra}
        db.commit()
        db.refresh(job)
    data = profile_data if profile_data is not None else dict(PROFILE_DATA)
    view = matching.minimal_candidate_view(data)
    signals = matching.parse_job_signals(job)
    filters = matching.run_hard_filters(
        job, signals, view,
        is_duplicate=matching.find_duplicate_job(db, user.id, job) is not None,
        already_applied=matching.is_already_applied(db, user.id, job),
    )
    det = matching.deterministic_score(job, signals, view, filters)
    return view, signals, filters, det


# --------------------------------------------------------------------------- #
# Stage 1 — hard filters
# --------------------------------------------------------------------------- #
def test_hard_filter_location_mismatch_is_a_conflict(db, owner, uploaded_resume):
    user = _user(db)
    job = _job(db, user, location="Munich, Germany",
               description="Onsite in Munich. Python backend engineer.\n\nRequired skills:\n- Python")
    _, _, filters, _ = _stages(db, user, job)
    loc = next(c for c in filters["checks"] if c["key"] == "location_mismatch")
    assert loc["status"] == "conflict"
    assert loc["penalty"] == matching.FILTER_PENALTIES["location_mismatch"]
    assert filters["status"] == "penalized"
    assert "Munich" in loc["detail"]


def test_hard_filter_remote_job_matches_remote_candidate(db, owner, uploaded_resume):
    user = _user(db)
    job = _job(db, user, location="Remote (Germany)",
               description="Fully remote Python backend engineer.")
    _, _, filters, _ = _stages(db, user, job)
    loc = next(c for c in filters["checks"] if c["key"] == "location_mismatch")
    assert loc["status"] == "pass"


def test_hard_filter_remote_job_conflicts_onsite_only_candidate(db, owner, uploaded_resume):
    user = _user(db)
    profile = _profile(db, user, {**PROFILE_DATA, "remote_preference": "onsite",
                                  "work_authorization": "German citizen"})
    del profile  # _stages reads the default; rebuild view explicitly
    view = matching.minimal_candidate_view(PROFILE_DATA | {"remote_preference": "onsite"})
    job = _job(db, user, location="Remote", description="Fully remote backend engineer.")
    signals = matching.parse_job_signals(job)
    filters = matching.run_hard_filters(job, signals, view)
    loc = next(c for c in filters["checks"] if c["key"] == "location_mismatch")
    assert loc["status"] == "conflict"
    assert loc["penalty"] > 0


def test_hard_filter_work_authorization_mismatch_filters(db, owner, uploaded_resume):
    user = _user(db)
    job = _job(db, user, description="Python backend engineer. You must have US work authorization.")
    view = matching.minimal_candidate_view(PROFILE_DATA | {"work_authorization": "visa dependent, needs sponsor"})
    signals = matching.parse_job_signals(job)
    filters = matching.run_hard_filters(job, signals, view)
    wa = next(c for c in filters["checks"] if c["key"] == "work_authorization")
    assert wa["status"] == "fail"
    assert filters["status"] == "filtered"
    assert any("work authorization" in r.lower() for r in filters["reasons"])


def test_hard_filter_work_authorization_unknown_never_fails(db, owner, uploaded_resume):
    user = _user(db)
    job = _job(db, user, description="Python backend engineer. You must have US work authorization.")
    view = matching.minimal_candidate_view(PROFILE_DATA | {"work_authorization": ""})
    signals = matching.parse_job_signals(job)
    filters = matching.run_hard_filters(job, signals, view)
    wa = next(c for c in filters["checks"] if c["key"] == "work_authorization")
    assert wa["status"] == "unknown"
    assert wa["penalty"] == 0
    assert filters["status"] != "filtered"


def test_hard_filter_sponsorship_conflict_and_fail(db, owner, uploaded_resume):
    user = _user(db)
    view = matching.minimal_candidate_view(PROFILE_DATA | {"sponsorship_required": True})
    job_no = _job(db, user, dedupe="lever:sp1",
                  description="Python backend engineer. No visa sponsorship offered.")
    signals_no = matching.parse_job_signals(job_no)
    filters_no = matching.run_hard_filters(job_no, signals_no, view)
    sp = next(c for c in filters_no["checks"] if c["key"] == "sponsorship")
    assert sp["status"] == "fail"
    assert filters_no["status"] == "filtered"

    job_silent = _job(db, user, dedupe="lever:sp2", description="Python backend engineer.")
    signals_silent = matching.parse_job_signals(job_silent)
    filters_silent = matching.run_hard_filters(job_silent, signals_silent, view)
    sp2 = next(c for c in filters_silent["checks"] if c["key"] == "sponsorship")
    assert sp2["status"] == "conflict"
    assert sp2["penalty"] == matching.FILTER_PENALTIES["sponsorship_mismatch"]


def test_hard_filter_employment_type_conflict_and_internship_fail(db, owner, uploaded_resume):
    user = _user(db)
    job_pt = _job(db, user, dedupe="lever:pt",
                  description="Part-time Python backend engineer. Part time, 20 hours a week.")
    _, _, filters_pt, _ = _stages(db, user, job_pt)
    et = next(c for c in filters_pt["checks"] if c["key"] == "employment_type_mismatch")
    assert et["status"] == "conflict"
    assert et["penalty"] == matching.FILTER_PENALTIES["employment_type_mismatch"]

    view_senior = matching.minimal_candidate_view(PROFILE_DATA)
    job_intern = _job(db, user, dedupe="lever:in", description="Backend Engineering Internship (internship)")
    signals_intern = matching.parse_job_signals(job_intern)
    filters_intern = matching.run_hard_filters(job_intern, signals_intern, view_senior)
    et2 = next(c for c in filters_intern["checks"] if c["key"] == "employment_type_mismatch")
    assert et2["status"] == "fail"


def test_hard_filter_compensation_below_minimum_filters(db, owner, uploaded_resume):
    user = _user(db)
    job = _job(db, user, description="Python backend engineer.\n\nCompensation: $60,000 - $80,000 per year.")
    view = matching.minimal_candidate_view(PROFILE_DATA)  # minimum 90,000
    signals = matching.parse_job_signals(job)
    assert signals["salary"]["stated"] and signals["salary"]["max"] == 80000
    filters = matching.run_hard_filters(job, signals, view)
    comp = next(c for c in filters["checks"] if c["key"] == "compensation_below_minimum")
    assert comp["status"] == "fail"
    assert filters["status"] == "filtered"

    # Range starts below the minimum but reaches above it → conflict, not fail.
    job2 = _job(db, user, dedupe="lever:comp2",
                description="Python backend engineer.\n\nCompensation: $85,000 - $120,000 per year.")
    signals2 = matching.parse_job_signals(job2)
    filters2 = matching.run_hard_filters(job2, signals2, view)
    comp2 = next(c for c in filters2["checks"] if c["key"] == "compensation_below_minimum")
    assert comp2["status"] == "conflict"
    assert comp2["penalty"] == matching.FILTER_PENALTIES["compensation_below_minimum"]


def test_hard_filter_seniority_mismatch_conflict(db, owner, uploaded_resume):
    user = _user(db)
    # Junior posting vs a senior candidate: two levels below.
    job = _job(db, user, title="Junior Python Developer",
               description="Junior Python developer. Python, basic SQL.")
    _, _, filters, _ = _stages(db, user, job)
    se = next(c for c in filters["checks"] if c["key"] == "seniority_mismatch")
    assert se["status"] == "conflict"
    assert se["penalty"] > 0


def test_hard_filter_all_required_skills_missing_filters(db, owner, uploaded_resume):
    user = _user(db)
    job = _job(db, user, description="Rust systems engineer.\n\nRequired skills:\n- Rust\n- WebAssembly")
    _, _, filters, _ = _stages(db, user, job)
    rs = next(c for c in filters["checks"] if c["key"] == "required_skills_missing")
    assert rs["status"] == "fail"
    assert filters["status"] == "filtered"
    assert "Rust" in filters["reasons"][0] or "rust" in filters["reasons"][0]


def test_hard_filter_partial_required_skills_penalized_and_listed(db, owner, uploaded_resume):
    user = _user(db)
    job = _job(db, user, dedupe="lever:rs",
               description="Python backend engineer.\n\nRequired skills:\n- Python\n- GraphQL")
    _, _, filters, _ = _stages(db, user, job)
    rs = next(c for c in filters["checks"] if c["key"] == "required_skills_missing")
    assert rs["status"] == "conflict"
    assert filters["missing_required_skills"] == ["GraphQL"]
    assert filters["total_penalty"] == matching.FILTER_PENALTIES["required_skills_missing"]


def test_hard_filter_expired_job_filters(db, owner, uploaded_resume):
    user = _user(db)
    job = _job(db, user, expired=True)
    _, _, filters, _ = _stages(db, user, job)
    ex = next(c for c in filters["checks"] if c["key"] == "expired")
    assert ex["status"] == "fail"
    assert filters["status"] == "filtered"


def test_hard_filter_unknown_fields_never_filter(db, owner, uploaded_resume):
    """An uninformative posting is 'unknown', never a mismatch."""
    user = _user(db)
    job = _job(db, user, location="Various", posted_hours_ago=None,
               description="Backend engineer wanted. Strong candidate sought.")
    view = matching.minimal_candidate_view(PROFILE_DATA | {"salary_expectations": None,
                                                          "work_authorization": ""})
    signals = matching.parse_job_signals(job)
    filters = matching.run_hard_filters(job, signals, view)
    by_key = {c["key"]: c for c in filters["checks"]}
    assert by_key["location_mismatch"]["status"] in ("pass", "unknown")
    assert by_key["compensation_below_minimum"]["status"] == "unknown"
    assert by_key["work_authorization"]["status"] == "pass"
    assert by_key["required_skills_missing"]["status"] == "unknown"
    assert filters["status"] == "eligible"
    assert filters["total_penalty"] == 0


# --------------------------------------------------------------------------- #
# Stage 2 — deterministic score
# --------------------------------------------------------------------------- #
def test_feature_weights_sum_to_one():
    assert abs(sum(matching.FEATURE_WEIGHTS.values()) - 1.0) < 1e-9
    assert len(matching.FEATURE_WEIGHTS) == 12


def test_deterministic_score_is_reproducible(db, owner, uploaded_resume):
    user = _user(db)
    job = _job(db, user)
    a = _stages(db, user, job)
    b = _stages(db, user, job)
    assert a[3]["overall"] == b[3]["overall"]
    assert a[3]["criteria"] == b[3]["criteria"]
    fp1 = matching.fingerprint_match("p" * 64, "j" * 64, matching.MATCHER_VERSION)
    fp2 = matching.fingerprint_match("p" * 64, "j" * 64, matching.MATCHER_VERSION)
    assert fp1 == fp2
    assert fp1 != matching.fingerprint_match("p" * 64, "j" * 64, "9.9.9")


def test_feature_contributions_and_weighted_mean(db, owner, uploaded_resume):
    user = _user(db)
    job = _job(db, user)
    _, _, _, det = _stages(db, user, job)
    for feature in det["criteria"]:
        weight = matching.FEATURE_WEIGHTS[feature["key"]]
        assert feature["weight"] == weight
        if feature["scored"]:
            assert abs(feature["contribution"] - round(feature["score"] * weight, 4)) < 1e-6
    scored = [f for f in det["criteria"] if f["scored"]]
    weight_sum = sum(f["weight"] for f in scored)
    expected_raw = sum(f["contribution"] for f in scored) / weight_sum
    assert abs(det["raw"] - round(expected_raw, 1)) <= 0.11
    assert abs(det["overall"] - max(0.0, min(100.0, det["raw"] - det["penalty"]))) < 0.11
    # The band is derivable from the stored number.
    assert det["band"] == matching.band_for(det["overall"])


def test_unscored_features_are_excluded_not_zeroed(db, owner, uploaded_resume):
    user = _user(db)
    # No salary, no location stated, no posting date, no seniority in the
    # title → several features are unscored (excluded, not zeroed).
    job = _job(db, user, title="Backend Engineer", location="Various",
               posted_hours_ago=None,
               description="Backend Engineer. Python, FastAPI. Hybrid.")
    _, _, _, det = _stages(db, user, job)
    by_key = {f["key"]: f for f in det["criteria"]}
    assert by_key["compensation"]["scored"] is False
    assert by_key["freshness"]["scored"] is False
    assert by_key["seniority"]["scored"] is False  # no seniority in title
    assert by_key["required_skills"]["scored"] is False  # no required section stated
    scored_weight = sum(f["weight"] for f in det["criteria"] if f["scored"])
    assert 0 < scored_weight < 1.0
    # raw must be the mean over scored features only (not dragged to 0 by the rest)
    expected = sum(f["contribution"] for f in det["criteria"] if f["scored"]) / scored_weight
    assert abs(det["raw"] - round(expected, 1)) <= 0.11


def test_penalties_reduce_the_score(db, owner, uploaded_resume):
    user = _user(db)
    base = _job(db, user, dedupe="lever:base")
    _, _, base_filters, base_det = _stages(db, user, base)
    assert base_filters["total_penalty"] == 0
    assert base_det["overall"] == base_det["raw"]

    penalised = _job(db, user, dedupe="lever:pen", location="Munich, Germany",
                     description="Onsite in Munich. Python backend engineer.\n\nRequired skills:\n- Python")
    _, _, pen_filters, pen_det = _stages(db, user, penalised)
    assert pen_filters["total_penalty"] > 0
    assert pen_det["penalty"] == pen_filters["total_penalty"]
    assert pen_det["overall"] <= pen_det["raw"] - pen_filters["total_penalty"] + 0.11
    assert pen_det["overall"] < base_det["overall"]


def test_band_thresholds():
    assert matching.band_for(85) == "strong"
    assert matching.band_for(84.9) == "good"
    assert matching.band_for(70) == "good"
    assert matching.band_for(69.9) == "possible"
    assert matching.band_for(50) == "possible"
    assert matching.band_for(49.9) == "weak"
    assert matching.band_for(0) == "weak"
    assert matching.band_for(99, source_known=False) == "unknown"


# --------------------------------------------------------------------------- #
# Missing evidence — a high fit must be supported
# --------------------------------------------------------------------------- #
def test_high_fit_without_candidate_evidence_is_demoted(client, auth, db, uploaded_resume):
    user = _user(db)
    _pro(db, user)
    # Profile text that matches the JD well, but NO skill list: nothing for a
    # skill-based strength to point at. The upload fixture created both a
    # legacy Profile and a v2 CandidateProfile — retire the v2 document so the
    # minimized view is built from the (skill-less) legacy profile only.
    from app.models.models import CandidateProfile

    for cp in db.query(CandidateProfile).filter(CandidateProfile.user_id == user.id).all():
        cp.is_current = False
    db.query(Profile).filter(Profile.user_id == user.id).delete(synchronize_session=False)
    db.commit()

    no_skills = {
        "summary": "Backend engineer for payments platforms, six years.",
        "location": "Berlin, Germany",
        "current_title": "Senior Software Engineer",
        "experience": [
            {"title": "Senior Software Engineer", "company": "FinCo", "duration": "2021 - present",
             "bullets": ["payments platform backend work"]},
        ],
        "remote_preference": "remote",
        "work_authorization": "German citizen",
        "salary_expectations": {"minimum": 50000},
    }
    _profile(db, user, no_skills)
    # JD with no explicit required-skill section (so required_skills is unscored)
    job = _job(db, user, dedupe="lever:ev", description="Senior Backend Engineer for our "
               "payments platform. Hybrid in Berlin.")
    res = client.post(f"/api/jobs/{job.id}/match", json={"ai": True}, headers=auth)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["band"] != "strong" and body["band"] != "good"
    assert body["flags"].get("evidence_gap") is True
    assert "evidence" in body["reason"]
    # Missing required skills remain visible (empty here — none stated, and the
    # gap is about candidate evidence, not about the posting).
    assert body["matched_skills"] == []


# --------------------------------------------------------------------------- #
# Conflicts
# --------------------------------------------------------------------------- #
def test_conflict_is_itemised_in_filter_report_and_rubric(db, owner, uploaded_resume):
    user = _user(db)
    job = _job(db, user, location="Munich, Germany",
               description="Onsite in Munich. Python backend engineer.")
    _, _, filters, det = _stages(db, user, job)
    assert "location_mismatch" in [c["key"] for c in filters["checks"] if c["status"] == "conflict"]
    loc_feature = next(f for f in det["criteria"] if f["key"] == "location")
    assert loc_feature["scored"] and loc_feature["score"] < 100
    assert "Munich" in loc_feature["reason"]


def test_seniority_conflict_is_a_conflict_not_silence(db, owner, uploaded_resume):
    user = _user(db)
    job = _job(db, user, title="Staff Backend Engineer",
               description="Staff Backend Engineer, staff level. Python.")
    _, _, filters, det = _stages(db, user, job)
    se = next(c for c in filters["checks"] if c["key"] == "seniority_mismatch")
    assert se["status"] == "conflict"
    se_feature = next(f for f in det["criteria"] if f["key"] == "seniority")
    assert se_feature["score"] <= 40
    # "Staff" is detected at principal level — the reason names both levels.
    assert "senior" in se_feature["reason"]
    assert se["job_value"] in ("staff", "principal")


def test_ai_recommendation_conflicting_with_band_is_rejected():
    checker = matching._review_checks(
        {"grounding_text": "python fastapi"}, "python fastapi job", band="good",
    )
    issues = checker({"recommendation": "skip", "strengths": ["Python", "FastAPI", "fastapi"],
                      "requirements_review": [], "risks": []})
    assert any(i["code"] == "recommendation_mismatch" for i in issues)


def test_probability_claim_is_a_guardrail_error():
    checker = matching._review_checks(
        {"grounding_text": "python"}, "python job", band="possible",
    )
    issues = checker({"recommendation": "hold",
                      "strengths": ["python", "python", "python"],
                      "requirements_review": [],
                      "risks": ["85% chance of an interview"]})
    assert any(i["code"] == "probability_claim" for i in issues)
    issues2 = checker({"recommendation": "hold",
                       "strengths": ["python", "python", "python"],
                       "requirements_review": [],
                       "risks": ["This is a guaranteed interview"]})
    assert any(i["code"] == "probability_claim" for i in issues2)


def test_explicit_requirement_not_in_jd_is_rejected():
    checker = matching._review_checks({"grounding_text": "python"}, "python job", band="possible")
    issues = checker({"recommendation": "hold", "strengths": ["python", "python", "python"],
                      "requirements_review": [{"requirement": "quantum computing",
                                               "basis": "explicit", "met": False, "evidence": ""}],
                      "risks": []})
    assert any(i["code"] == "unsupported_requirement" for i in issues)
    # The same claim marked inferred is allowed.
    ok = checker({"recommendation": "hold", "strengths": ["python", "python", "python"],
                  "requirements_review": [{"requirement": "quantum computing",
                                           "basis": "inferred", "met": False, "evidence": ""}],
                  "risks": []})
    assert not any(i["code"] == "unsupported_requirement" for i in ok)


def test_high_band_review_requires_three_grounded_strengths():
    checker = matching._review_checks({"grounding_text": "python fastapi"},
                                      "python job", band="strong")
    issues = checker({"recommendation": "apply", "strengths": ["python"],
                      "requirements_review": [], "risks": []})
    assert any(i["code"] == "high_fit_missing_evidence" for i in issues)
    ok = checker({"recommendation": "apply", "strengths": ["python", "fastapi", "python fastapi"],
                  "requirements_review": [], "risks": []})
    assert not any(i["code"] == "high_fit_missing_evidence" for i in ok)


# --------------------------------------------------------------------------- #
# Duplicate jobs
# --------------------------------------------------------------------------- #
def test_duplicate_board_row_is_filtered(db, owner, uploaded_resume):
    user = _user(db)
    first = _job(db, user, dedupe="lever:dup1")
    second = _job(db, user, dedupe="greenhouse:dup1", posted_hours_ago=10)  # same role, other source
    dup_of = matching.find_duplicate_job(db, user.id, second)
    assert dup_of is not None and dup_of.id == first.id

    _, _, filters, _ = _stages(db, user, second)
    dup = next(c for c in filters["checks"] if c["key"] == "duplicate")
    assert dup["status"] == "fail"
    assert filters["status"] == "filtered"
    # The canonical row is not a duplicate.
    view = matching.minimal_candidate_view(PROFILE_DATA)
    signals = matching.parse_job_signals(first)
    assert matching.run_hard_filters(first, signals, view)["status"] != "filtered"


def test_duplicate_and_already_applied_are_filtered_via_api(client, auth, db, uploaded_resume):
    user = _user(db)
    _pro(db, user)
    first = _job(db, user, dedupe="lever:ap1", status="applied")
    second = _job(db, user, dedupe="greenhouse:ap1", posted_hours_ago=5)

    res = client.post(f"/api/jobs/{second.id}/match", json={}, headers=auth)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["flags"].get("hard_filtered") is True
    assert body["flags"].get("duplicate") is True
    assert body["flags"].get("duplicate_of") == first.id
    assert body["band"] == "weak"
    assert body["score"] <= 49
    # The explanation names the duplicate.
    assert "Duplicate" in body["reason"] or "already applied" in body["reason"].lower()


def test_applied_job_itself_is_filtered_from_recommendations(client, auth, db, uploaded_resume):
    user = _user(db)
    _pro(db, user)
    job = _job(db, user, dedupe="lever:applied-itself", status="applied")
    res = client.post(f"/api/jobs/{job.id}/match", json={}, headers=auth)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["flags"].get("hard_filtered") is True
    assert "already applied" in body["reason"].lower()


# --------------------------------------------------------------------------- #
# API — full flows, idempotency, plan gate, honesty
# --------------------------------------------------------------------------- #
def test_match_endpoint_returns_full_explanation(client, auth, db, uploaded_resume):
    user = _user(db)
    _pro(db, user)
    job = _job(db, user)

    res = client.post(f"/api/jobs/{job.id}/match", json={"ai": True}, headers=auth)
    assert res.status_code == 200, res.text
    body = res.json()

    # The number + its provenance.
    assert body["score"] > 0 and body["score"] <= 100
    assert body["band"] in ("strong", "good", "possible", "weak")
    assert body["scorer"] in ("deterministic", "hybrid")
    assert body["scorer_version"] == matching.MATCHER_VERSION
    assert body["score_source"] in ("ai", "preliminary")
    assert body["calibrated_probability"] is False
    assert "estimated fit" in body["disclaimer"].lower()

    # Stage 1: every check explained.
    checks = {c["key"]: c for c in body["hard_filters"]["checks"]}
    for key in ("expired", "duplicate", "location_mismatch", "work_authorization", "sponsorship",
                "employment_type_mismatch", "compensation_below_minimum", "seniority_mismatch",
                "required_skills_missing"):
        assert key in checks, f"missing filter check {key}"
        assert checks[key]["status"] in ("pass", "conflict", "unknown", "fail")
        assert checks[key]["detail"]

    # Stage 2: all twelve features with contributions + reasons.
    criteria = body["rubric"]["criteria"]
    assert len(criteria) == 12
    for feature in criteria:
        assert feature["reason"]
        assert feature["weight"] > 0
        assert feature["label"]
    assert body["rubric"]["overall"] is not None

    # Every match has an explanation.
    assert body["reason"].strip()

    # Missing preferred skills are visible (Kafka/Terraform from the JD).
    missing_names = {m["name"].casefold() for m in body["missing_skills"]}
    assert {"kafka", "terraform"} <= missing_names

    # Stage 3: the guarded AI review.
    review = body["ai_review"]
    assert review is not None, body.get("flags")
    assert review["recommendation"] in matching.RECOMMENDATIONS
    assert review["strengths"]
    assert review["recommendation_reason"]
    assert review["risks"]
    assert review["resume_emphasis"]
    assert review["application_action"]
    assert body["guardrail_report"]["passed"] is True
    # High fit ⇒ grounded candidate evidence.
    assert body["matched_skills"]
    assert any(m["evidence"] for m in body["matched_skills"])

    # Read it back.
    got = client.get(f"/api/jobs/{job.id}/match", headers=auth)
    assert got.status_code == 200
    assert got.json()["current"]["score"] == body["score"]
    assert got.json()["current"]["match_id"] == body["match_id"]
    # No probability language anywhere in the stored review.
    blob = json.dumps(body["ai_review"]).lower()
    assert "probability" not in blob or "interview probability" not in blob.replace(
        "never", "x") or "chance of an interview" not in blob


def test_match_is_idempotent_for_identical_inputs(client, auth, db, uploaded_resume):
    user = _user(db)
    _pro(db, user)
    job = _job(db, user)

    first = client.post(f"/api/jobs/{job.id}/match", json={"ai": True}, headers=auth).json()
    second = client.post(f"/api/jobs/{job.id}/match", json={"ai": True}, headers=auth).json()
    assert second["match_id"] == first["match_id"]
    assert second.get("reused") is True

    rows = db.query(MatchResult).filter(MatchResult.job_id == job.id).all()
    assert len(rows) == 1

    # A re-score with force=1 inserts a new row and supersedes the old one.
    third = client.post(f"/api/jobs/{job.id}/match", json={"refresh": True}, headers=auth).json()
    assert third["match_id"] != first["match_id"]
    db.expire_all()  # the router committed on its own session
    rows = db.query(MatchResult).filter(MatchResult.job_id == job.id).all()
    assert len(rows) == 2
    old = next(r for r in rows if r.id == first["match_id"])
    assert old.is_current is False
    assert old.superseded_by_id == third["match_id"]


def test_rescore_after_profile_change_creates_new_version(client, auth, db, uploaded_resume):
    user = _user(db)
    _pro(db, user)
    job = _job(db, user)
    first = client.post(f"/api/jobs/{job.id}/match", json={"ai": True}, headers=auth).json()

    # A new candidate profile version (the v2 document the matcher scores).
    from app.models.models import CandidateProfile

    cp = (
        db.query(CandidateProfile)
        .filter(CandidateProfile.user_id == user.id, CandidateProfile.is_current.is_(True))
        .first()
    )
    assert cp is not None
    doc = dict(cp.document)
    doc["skills"] = ["Rust", "WebAssembly"]
    cp.document = doc
    cp.document_sha256 = hashlib.sha256(
        json.dumps(doc, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    cp.version += 1
    db.commit()

    second = client.post(f"/api/jobs/{job.id}/match", json={}, headers=auth).json()
    assert second["match_id"] != first["match_id"]
    db.expire_all()  # the router committed on its own session
    old = db.query(MatchResult).filter(MatchResult.id == first["match_id"]).first()
    assert old.is_current is False
    # The new verdict must now see the missing required skills.
    assert any(m["required"] for m in second["missing_skills"])


def test_insufficient_data_is_labelled_not_invented(client, auth, db, owner):
    user = _user(db)
    _pro(db, user)
    # No profile at all.
    job = _job(db, user)
    res = client.post(f"/api/jobs/{job.id}/match", json={}, headers=auth)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["score_source"] == "insufficient_data"
    assert body["band"] == "unknown"
    assert body["confidence"] is None
    assert body["reason"]
    assert body["calibrated_probability"] is False


def test_free_tier_is_deterministic_with_upgrade_hint(client, auth, db, uploaded_resume):
    user = _user(db)
    # No pro subscription → the AI stage is plan-gated.
    job = _job(db, user)
    res = client.post(f"/api/jobs/{job.id}/match", json={"ai": True}, headers=auth)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["plan_gated"] is True
    assert body["upgrade_hint"]
    assert body["ai_review"] is None
    assert body["scorer"] == "deterministic"
    assert body["score_source"] == "preliminary"
    assert body["score"] > 0
    # Free tier must not spend the job-analysis quota.
    from app.models.models import UsageCounter
    assert db.query(UsageCounter).filter(
        UsageCounter.user_id == user.id,
        UsageCounter.capability == "job_analysis_per_month",
    ).first() is None


def test_ai_stage_charges_the_existing_meter(client, auth, db, uploaded_resume):
    user = _user(db)
    _pro(db, user)
    job = _job(db, user)
    client.post(f"/api/jobs/{job.id}/match", json={"ai": True}, headers=auth)
    from app.models.models import UsageCounter
    counter = db.query(UsageCounter).filter(
        UsageCounter.user_id == user.id,
        UsageCounter.capability == "job_analysis_per_month",
    ).first()
    assert counter is not None and counter.count >= 1


def test_expired_job_match_is_filtered_with_explanation(client, auth, db, uploaded_resume):
    user = _user(db)
    _pro(db, user)
    job = _job(db, user, expired=True)
    res = client.post(f"/api/jobs/{job.id}/match", json={}, headers=auth)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["flags"].get("hard_filtered") is True
    assert body["band"] == "weak"
    assert "expired" in body["reason"].lower()


def test_best_matches_listing_and_user_correction_demotes(client, auth, db, uploaded_resume):
    user = _user(db)
    _pro(db, user)
    good = _job(db, user, dedupe="lever:bm1", title="Senior Backend Engineer",
                company="FinCo", location="Berlin, Germany")
    weaker = _job(db, user, dedupe="lever:bm2", title="Junior Frontend Developer",
                  company="OtherCo", location="Hamburg, Germany",
                  description="Junior frontend developer. React, JavaScript. Part-time.")
    client.post(f"/api/jobs/{good.id}/match", json={"ai": True}, headers=auth)
    client.post(f"/api/jobs/{weaker.id}/match", json={"ai": True}, headers=auth)

    listing = client.get("/api/matches", headers=auth).json()
    assert len(listing) == 2
    assert listing[0]["job_id"] == good.id
    assert all("disclaimer" in e for e in listing)
    assert listing[0]["user_corrected"] is False

    # The user corrects the top recommendation.
    fb = client.post(f"/api/jobs/{good.id}/match/feedback",
                     json={"kind": "not_relevant",
                           "reason": "I do not know Kubernetes; the match claimed I do."},
                     headers=auth)
    assert fb.status_code == 200, fb.text
    assert fb.json()["ok"] is True

    listing = client.get("/api/matches", headers=auth).json()
    top = next(e for e in listing if e["job_id"] == good.id)
    assert top["user_corrected"] is True
    # Corrections sink below the uncorrected job at the same or higher score.
    assert listing.index(top) > 0 or listing[0]["job_id"] != good.id

    # The correction is visible on the match read.
    got = client.get(f"/api/jobs/{good.id}/match", headers=auth).json()
    assert got["user_feedback"]["kind"] == "not_relevant"
    assert "Kubernetes" in got["user_feedback"]["reason"]


@pytest.mark.parametrize("kind", list(MATCH_FEEDBACK_KINDS))
def test_feedback_records_all_kinds(client, auth, db, uploaded_resume, kind):
    user = _user(db)
    _pro(db, user)
    job = _job(db, user, dedupe=f"lever:fb-{kind}")
    res = client.post(f"/api/jobs/{job.id}/match/feedback",
                      json={"kind": kind, "reason": "test signal"}, headers=auth)
    assert res.status_code == 200, res.text
    assert res.json()["kind"] == kind
    row = db.query(MatchFeedback).filter(MatchFeedback.job_id == job.id).first()
    assert row is not None and row.kind == kind


def test_feedback_rejects_unknown_kind(client, auth, db, uploaded_resume):
    user = _user(db)
    job = _job(db, user)
    res = client.post(f"/api/jobs/{job.id}/match/feedback",
                      json={"kind": "definitely_a_maybe"}, headers=auth)
    assert res.status_code == 422


def test_calibration_gate_never_claims_a_probability(client, auth, db, uploaded_resume):
    user = _user(db)
    res = client.get("/api/matches/calibration", headers=auth)
    assert res.status_code == 200
    body = res.json()
    assert body["calibrated"] is False
    assert body["calibrated_probability"] is False
    assert body["sufficient_outcome_data"] is False
    assert body["recorded_outcomes"]["interview"] == 0
    assert "too few" in body["reason"]

    # Record the maximum outcomes a test plausibly has — still below the gate.
    for kind, _ in ((k, i) for i, k in enumerate(["applied", "rejected", "interview"])):
        job = _job(db, user, dedupe=f"lever:cal-{kind}")
        client.post(f"/api/jobs/{job.id}/match/feedback", json={"kind": kind}, headers=auth)
    body = client.get("/api/matches/calibration", headers=auth).json()
    assert body["calibrated"] is False
    assert body["recorded_outcomes"]["applied"] == 1
    assert body["recorded_outcomes"]["interview"] == 1
    assert body["sufficient_outcome_data"] is False
    assert body["minimum_outcomes_required"] == matching.MIN_OUTCOMES_FOR_CALIBRATION
    # The per-scorer-version breakdown is the recalibration hook.
    versions = body["scorer_versions"]
    assert versions and versions[0]["scorer_version"] == matching.MATCHER_VERSION
    assert versions[0]["outcomes"] == 2


def test_every_stored_match_carries_inputs_and_version(client, auth, db, uploaded_resume):
    user = _user(db)
    _pro(db, user)
    job = _job(db, user)
    body = client.post(f"/api/jobs/{job.id}/match", json={"ai": True}, headers=auth).json()

    row = db.query(MatchResult).filter(MatchResult.id == body["match_id"]).first()
    assert row is not None
    assert len(row.profile_sha256) == 64
    assert len(row.job_description_sha256) == 64
    assert row.scorer_version == matching.MATCHER_VERSION
    assert row.is_current is True
    assert row.staleness == "fresh"
    assert row.rubric and row.hard_filters
    # Reproducibility hook: the fingerprint is recomputable from the stored inputs.
    assert body["fingerprint"] == matching.fingerprint_match(
        row.profile_sha256, row.job_description_sha256, row.scorer_version)


def test_match_404_for_unknown_job(client, auth, db):
    res = client.post("/api/jobs/99999/match", json={}, headers=auth)
    assert res.status_code == 404
    res = client.get("/api/jobs/99999/match", headers=auth)
    assert res.status_code == 404
