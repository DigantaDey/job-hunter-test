"""Application tracking — lifecycle, outcome feedback, timeline and reporting.

The task's acceptance criteria, one test group each:

* the user can update an application status
  (:func:`test_status_update_appends_an_event_and_moves_the_projection`);
* the user can add an interview event (:func:`test_user_can_add_an_interview`);
* the timeline is visible from the application detail page
  (:func:`test_detail_document_is_self_contained`);
* reports calculate application→interview conversion
  (:func:`test_report_calculates_application_to_interview_conversion`);
* status transitions, invalid transitions, manual corrections and duplicate
  events are all covered (the groups below).

Plus the two honesty rules the layer exists for: an interview is never inferred
(:func:`test_an_email_can_never_record_an_interview`) and a system observation is
never presented as the user's claim
(:func:`test_system_observed_and_user_reported_stay_apart`).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.contracts.vocabulary import (
    APPLICATION_TRACKING_STATES,
    TRACKING_INTERVIEW_STATES,
    TRACKING_TERMINAL_STATES,
)
from app.models.models import (
    ApplicationPacket,
    ApplicationTracking,
    ApplicationTrackingEvent,
    Job,
    MatchResult,
    Notification,
    User,
)
from app.services import application_tracking as tracking
from app.services.auto_scheduler import AutoScheduler

API = "/api/application-tracking"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def user(db, owner) -> User:
    row = db.query(User).order_by(User.id).first()
    assert row is not None
    return row


@pytest.fixture()
def job(db, user) -> Job:
    row = Job(
        user_id=user.id, title="Senior Backend Engineer", company="FinCo",
        company_name_normalized="finco", description="Python, FastAPI, PostgreSQL.",
        url="https://jobs.lever.co/finco/1", source="lever", dedupe_key="lever:finco:1",
        status="discovered", score=78.0, score_source="ai",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture()
def other_job(db, user) -> Job:
    row = Job(
        user_id=user.id, title="Data Analyst", company="OtherCo",
        company_name_normalized="otherco", description="SQL, dashboards.",
        url="https://remoteok.com/2", source="remoteok", dedupe_key="remoteok:otherco:2",
        status="discovered", score=61.0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _open(client, auth, job_id: int, **body) -> dict:
    response = client.post(API, json={"job_id": job_id, **body}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture()
def tracked(client, auth, job) -> dict:
    """A record opened through the API, in ``not_applied``."""
    return _open(client, auth, job.id)


def _record(db, user, job_id: int) -> ApplicationTracking:
    """The row as the *database* has it.

    The API writes on its own session, so an instance already in this session's
    identity map would happily return pre-request values — expiring first is what
    keeps these assertions about stored state rather than about a cache.
    """
    db.expire_all()
    row = tracking.for_job(db, user.id, job_id)
    assert row is not None, "expected a tracking record"
    return row


def _events(db, user, job_id: int) -> list[ApplicationTrackingEvent]:
    record = _record(db, user, job_id)
    return (
        db.query(ApplicationTrackingEvent)
        .filter(ApplicationTrackingEvent.tracking_id == record.id)
        .order_by(ApplicationTrackingEvent.sequence.asc())
        .all()
    )


def _match(db, user, job, *, score: float = 82.0, band: str = "good",
           source: str = "ai", version: str = "1.2.0") -> MatchResult:
    row = MatchResult(
        user_id=user.id, job_id=job.id, profile_sha256="p" * 64,
        job_description_sha256="j" * 64, scorer="hybrid", scorer_version=version,
        score=score, band=band, score_source=source, is_current=True,
        staleness="fresh", computed_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _packet(db, user, job, *, version: int = 3) -> ApplicationPacket:
    row = ApplicationPacket(
        user_id=user.id, job_id=job.id, version=version, status="approved", is_current=True,
        jd_hash="h" * 64, jd_version=1, job_title=job.title, company=job.company,
        generated_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# Opening a record — idempotent, snapshot-taking
# --------------------------------------------------------------------------- #
def test_opening_tracking_is_idempotent_and_snapshots_the_job(client, auth, db, user, job):
    first = _open(client, auth, job.id)
    assert first["duplicate"] is False
    assert first["document"]["state"] == "not_applied"
    assert first["document"]["attribution"]["source"] == "lever"
    assert first["document"]["attribution"]["role_family"] == "Backend Engineer"
    assert first["document"]["snapshots"]["match"]["score"] == 78.0

    second = _open(client, auth, job.id)
    assert second["duplicate"] is True
    assert second["document"]["tracking_id"] == first["document"]["tracking_id"]
    # One record, one opening event — not two rows and not a second event.
    assert db.query(ApplicationTracking).filter(ApplicationTracking.user_id == user.id).count() == 1
    assert len(_events(db, user, job.id)) == 1


def test_opening_an_already_applied_job_records_it_as_applied(client, auth, db, user, job):
    """The board's own fact is not thrown away when tracking starts."""
    job.status = "applied"
    job.applied_at = datetime.utcnow() - timedelta(days=3)
    db.commit()

    opened = _open(client, auth, job.id)
    document = opened["document"]
    assert document["state"] == "applied"
    assert document["timestamps"]["applied_at"] is not None
    # The claim came from the board, not from the person pressing the button, so
    # the record says system_observed — otherwise ``by_origin`` in the report
    # would credit the user with a fact the product already had.
    assert document["state_origin"] == "system_observed"
    event = _events(db, user, job.id)[0]
    assert event.state_to == "applied"
    assert event.origin == "system_observed"
    assert event.payload["already_applied"] is True


def test_the_projection_backfills_a_date_the_board_was_missing(client, auth, db, user, job):
    """A legacy board row can say ``applied`` and know nothing about when."""
    job.status = "applied"
    job.applied_at = None
    db.commit()

    opened = _open(client, auth, job.id, state="applied")
    db.refresh(job)
    assert job.status == "applied"
    assert job.applied_at is not None
    assert opened["document"]["timestamps"]["applied_at"] is not None
    # The person said it, so the origin stays theirs — only the date was borrowed.
    assert opened["document"]["state_origin"] == "user_reported"


def test_opening_with_a_source_override_attributes_the_application(client, auth, db, user, job):
    opened = _open(client, auth, job.id, source="referral", channel="manual_user")
    assert opened["document"]["attribution"]["source"] == "referral"
    assert opened["document"]["attribution"]["channel"] == "manual_user"


def test_job_lookup_reports_an_untracked_job_instead_of_creating_one(client, auth, db, user, job):
    response = client.get(f"{API}/job/{job.id}", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is False
    assert body["actions"][0]["key"] == "open"
    assert body["snapshot"]["role_family"] == "Backend Engineer"
    assert tracking.for_job(db, user.id, job.id) is None


# --------------------------------------------------------------------------- #
# Status transitions
# --------------------------------------------------------------------------- #
def test_status_update_appends_an_event_and_moves_the_projection(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    assert db.refresh(job) is None and job.status == "applied"
    # The projection carries the date too: a board row that says "applied" with no
    # ``applied_at`` is dropped by every "applied this week" filter.
    assert job.applied_at is not None

    response = client.post(f"{API}/{_record(db, user, job.id).id}/status",
                           json={"state": "recruiter_response", "note": "They emailed back"},
                           headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state_changed"] is True
    assert body["duplicate"] is False
    assert body["document"]["state"] == "recruiter_response"
    assert body["document"]["state_label"] == "Recruiter responded"
    assert body["document"]["state_origin"] == "user_reported"
    assert body["document"]["note"] == "They emailed back"

    events = _events(db, user, job.id)
    assert [row.event_type for row in events][-1] == "application.response_received"
    assert events[-1].state_from == "applied"
    assert events[-1].state_to == "recruiter_response"
    assert events[-1].origin == "user_reported"
    assert events[-1].actor_type == "user"
    assert events[-1].actor_label == user.email
    # The legacy board column follows the canonical state (contracts/07 §5).
    db.refresh(job)
    assert job.status == "applied"
    assert events[-1].sequence == len(events), "sequence must be gapless and 1-based"


def test_happy_path_walks_the_whole_lifecycle(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    slot = (datetime.utcnow() + timedelta(days=4)).replace(microsecond=0)

    for path, body in (
        (f"{API}/{record_id}/response", {"channel": "email", "summary": "Would you like to talk?"}),
        (f"{API}/{record_id}/interview", {"interview_at": slot.isoformat() + "Z", "format": "video",
                                          "round_name": "Hiring manager"}),
        (f"{API}/{record_id}/interview-complete", {"self_assessment": "went_well",
                                                   "next_step": "Panel next week"}),
        (f"{API}/{record_id}/offer", {}),
    ):
        response = client.post(path, json=body, headers=auth)
        assert response.status_code == 200, f"{path}: {response.text}"
        assert response.json()["state_changed"] is True

    document = client.get(f"{API}/{record_id}", headers=auth).json()
    assert document["state"] == "offer_received"
    assert document["phase"] == "closed"
    assert document["job_status"] == "applied"
    timestamps = document["timestamps"]
    for key in ("applied_at", "first_response_at", "interview_scheduled_at", "interview_at",
                "interview_completed_at", "outcome_at", "closed_at"):
        assert timestamps[key], f"{key} was never stamped"
    assert document["durations_days"]["applied_to_interview"] is not None
    # The timeline tells the story in order, with one row per fact.
    kinds = [row["event_type"] for row in document["timeline"]]
    assert kinds == [
        "application.tracking_opened",
        "application.response_received",
        "application.interview_reported",
        "application.interview_completed",
        "application.offer_received",
    ]
    assert [row["sequence"] for row in document["timeline"]] == [1, 2, 3, 4, 5]
    assert all(row["origin"] == "user_reported" for row in document["timeline"])


def test_invalid_transition_is_refused_with_the_states_that_are_allowed(client, auth, db, user, job):
    opened = _open(client, auth, job.id)
    record_id = opened["document"]["tracking_id"]

    response = client.post(f"{API}/{record_id}/status", json={"state": "interview_scheduled"}, headers=auth)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_state_transition"
    assert detail["state_from"] == "not_applied"
    assert detail["allowed"] == ["applied", "withdrawn"]
    # Nothing was written: the timeline still has exactly the opening event.
    assert len(_events(db, user, job.id)) == 1
    assert _record(db, user, job.id).state == "not_applied"


def test_a_terminal_outcome_refuses_transitions(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    assert client.post(f"{API}/{record_id}/rejection", json={"reason": "Not a fit"},
                       headers=auth).status_code == 200

    for state in ("applied", "interview_scheduled", "offer_received"):
        response = client.post(f"{API}/{record_id}/status", json={"state": state}, headers=auth)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "invalid_state_transition"
        assert response.json()["detail"]["allowed"] == []
    assert _record(db, user, job.id).state == "rejected_by_employer"
    assert _record(db, user, job.id).closed_at is not None
    db.refresh(job)
    assert job.status == "rejected"


def test_a_state_outside_the_vocabulary_is_422(client, auth, db, user, job):
    record_id = _open(client, auth, job.id)["document"]["tracking_id"]
    response = client.post(f"{API}/{record_id}/status", json={"state": "hired_maybe"}, headers=auth)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "validation_error"
    assert set(response.json()["detail"]["allowed"]) == set(APPLICATION_TRACKING_STATES)


def test_a_self_loop_records_a_second_interview_and_a_second_reply(client, auth, db, user, job):
    """A reschedule and a follow-up reply are facts, not duplicates."""
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    first = (datetime.utcnow() + timedelta(days=2)).replace(microsecond=0)
    second = (datetime.utcnow() + timedelta(days=9)).replace(microsecond=0)

    assert client.post(f"{API}/{record_id}/interview",
                       json={"interview_at": first.isoformat()}, headers=auth).status_code == 200
    rescheduled = client.post(f"{API}/{record_id}/interview",
                              json={"interview_at": second.isoformat(), "note": "They moved it"},
                              headers=auth)
    assert rescheduled.status_code == 200, rescheduled.text
    assert rescheduled.json()["duplicate"] is False
    document = rescheduled.json()["document"]
    assert document["state"] == "interview_scheduled"
    assert document["timestamps"]["interview_at"].startswith(second.strftime("%Y-%m-%dT%H:%M"))
    interviews = [row for row in _events(db, user, job.id)
                  if row.event_type == "application.interview_reported"]
    assert len(interviews) == 2


def test_the_projection_never_downgrades_a_status_the_queue_owns(db, user, job):
    """A tracker reset must not erase an automation that is mid-run."""
    record, _created = tracking.open_tracking(db, user=user, job=job)
    job.status = "preparing"
    db.commit()

    tracking.change_state(db, user=user, record=record, state="not_applied", is_correction=True,
                          correction_reason="opened by mistake")
    db.refresh(job)
    assert job.status == "preparing"

    # …but "it went out" always wins, whatever the queue thinks.
    tracking.change_state(db, user=user, record=record, state="applied", is_correction=True,
                          correction_reason="it was submitted after all")
    db.refresh(job)
    assert job.status == "applied"


# --------------------------------------------------------------------------- #
# "I got an interview"
# --------------------------------------------------------------------------- #
def test_user_can_add_an_interview(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    slot = (datetime.utcnow() + timedelta(days=5)).replace(microsecond=0)

    response = client.post(
        f"{API}/{record_id}/interview",
        json={"interview_at": slot.isoformat() + "Z", "format": "video",
              "round_name": "Technical screen", "interviewer_role": "Staff Engineer",
              "note": "Bring the payments case study",
              "follow_up_at": (slot + timedelta(days=2)).isoformat() + "Z",
              "follow_up_note": "Send the thank-you note"},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state_changed"] is True
    document = body["document"]
    assert document["state"] == "interview_scheduled"
    assert document["state_label"] == "Interview scheduled"
    assert document["state_origin"] == "user_reported"
    assert document["note"] == "Bring the payments case study"
    assert document["follow_up"]["pending"] is True
    assert document["follow_up"]["note"] == "Send the thank-you note"

    event = _events(db, user, job.id)[-1]
    assert event.event_type == "application.interview_reported"
    assert event.origin == "user_reported"
    assert event.severity == "success"
    assert event.payload["format"] == "video"
    assert event.payload["round"] == "Technical screen"
    assert event.payload["interview_at"].startswith(slot.strftime("%Y-%m-%dT%H:%M"))
    # The user's own words are the evidence for a user-reported fact.
    assert event.evidence[0]["kind"] == "user_statement"
    assert event.follow_up_at is not None
    # The note is prose about the user's own application — never a field value.
    assert "password" not in (event.note or "").lower()


def test_an_interview_format_outside_the_vocabulary_is_422(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    response = client.post(f"{API}/{record_id}/interview", json={"format": "telepathy"}, headers=auth)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "validation_error"


def test_completing_an_interview_keeps_the_round_and_the_follow_up(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    slot = (datetime.utcnow() - timedelta(days=1)).replace(microsecond=0)
    client.post(f"{API}/{record_id}/interview", json={"interview_at": slot.isoformat()}, headers=auth)

    response = client.post(f"{API}/{record_id}/interview-complete",
                           json={"self_assessment": "mixed", "next_step": "Take-home task",
                                 "follow_up_at": (datetime.utcnow() + timedelta(days=3)).isoformat()},
                           headers=auth)
    assert response.status_code == 200, response.text
    document = response.json()["document"]
    assert document["state"] == "interview_completed"
    assert document["timestamps"]["interview_completed_at"] is not None
    assert document["follow_up"]["pending"] is True
    event = _events(db, user, job.id)[-1]
    assert event.event_type == "application.interview_completed"
    assert event.payload["self_assessment"] == "mixed"
    assert event.payload["next_step"] == "Take-home task"


def test_an_interview_can_be_backfilled_with_the_date_it_happened(client, auth, db, user, job):
    """A fact reported late is stored late, and says so."""
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    slot = datetime.utcnow() - timedelta(days=21)

    response = client.post(f"{API}/{record_id}/interview",
                           json={"interview_at": slot.isoformat(),
                                 "occurred_at": slot.isoformat()}, headers=auth)
    assert response.status_code == 200, response.text
    event = _events(db, user, job.id)[-1]
    assert event.occurred_at.date() == slot.date()
    assert event.recorded_at > event.occurred_at, "recorded_at must stay the storage time"
    rendered = response.json()["event"]
    assert rendered["late_reported"] is True


# --------------------------------------------------------------------------- #
# Recruiter response, rejection, withdrawal, notes
# --------------------------------------------------------------------------- #
def test_a_recruiter_response_is_a_response_and_not_an_interview(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    response = client.post(f"{API}/{record_id}/response",
                           json={"channel": "linkedin", "summary": "Let's schedule something"},
                           headers=auth)
    assert response.status_code == 200, response.text
    document = response.json()["document"]
    assert document["state"] == "recruiter_response"
    assert document["state"] not in TRACKING_INTERVIEW_STATES
    assert document["timestamps"]["first_response_at"] is not None
    assert document["timestamps"]["interview_scheduled_at"] is None


def test_a_response_channel_outside_the_vocabulary_is_422(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    response = client.post(f"{API}/{record_id}/response", json={"channel": "carrier_pigeon"}, headers=auth)
    assert response.status_code == 422


def test_withdrawal_records_the_reason_and_closes_the_record(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    response = client.post(f"{API}/{record_id}/withdraw",
                           json={"reason": "Accepted another offer", "note": "Told them on the call"},
                           headers=auth)
    assert response.status_code == 200, response.text
    document = response.json()["document"]
    assert document["state"] == "withdrawn"
    assert document["state"] in TRACKING_TERMINAL_STATES
    assert document["transitions"]["terminal"] is True
    db.refresh(job)
    assert job.status == "skipped"
    event = _events(db, user, job.id)[-1]
    assert event.event_type == "application.withdrawn"
    assert event.payload["reason"] == "Accepted another offer"


def test_notes_change_nothing_but_the_timeline(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    before = _record(db, user, job.id).state

    response = client.post(f"{API}/{record_id}/note",
                           json={"note": "Recruiter mentioned a second round in October"}, headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state_changed"] is False
    assert body["document"]["state"] == before
    assert body["document"]["note"] == "Recruiter mentioned a second round in October"
    event = _events(db, user, job.id)[-1]
    assert event.event_type == "application.note_added"
    assert event.state_to is None, "a note is not a transition"
    assert event.payload == {"note_present": True, "note_length": 45}


def test_an_empty_note_is_refused(client, auth, db, user, job):
    record_id = _open(client, auth, job.id)["document"]["tracking_id"]
    assert client.post(f"{API}/{record_id}/note", json={"note": "   "},
                       headers=auth).status_code == 422


# --------------------------------------------------------------------------- #
# Manual corrections
# --------------------------------------------------------------------------- #
def test_a_correction_needs_a_reason(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    response = client.post(f"{API}/{record_id}/correction", json={"state": "not_applied", "reason": ""},
                           headers=auth)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "correction_reason_required"
    assert _record(db, user, job.id).state == "applied"


def test_a_correction_keeps_the_wrong_event_and_names_it(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    slot = (datetime.utcnow() + timedelta(days=2)).replace(microsecond=0)
    client.post(f"{API}/{record_id}/interview", json={"interview_at": slot.isoformat()}, headers=auth)
    mistaken = _events(db, user, job.id)[-1]
    assert mistaken.state_to == "interview_scheduled"

    response = client.post(
        f"{API}/{record_id}/correction",
        json={"state": "recruiter_response", "reason": "That was a screening call, not an interview",
              "correction_of_event_id": mistaken.id},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    document = response.json()["document"]
    assert document["state"] == "recruiter_response"
    assert document["counts"]["corrections"] == 1
    # History is preserved: the wrong row is still there, unedited.
    db.refresh(mistaken)
    assert mistaken.state_to == "interview_scheduled"
    assert mistaken.is_correction is False

    correction = _events(db, user, job.id)[-1]
    assert correction.event_type == "application.state_corrected"
    assert correction.is_correction is True
    assert correction.correction_of_event_id == mistaken.id
    assert correction.correction_reason == "That was a screening call, not an interview"
    assert correction.severity == "warning"
    assert correction.payload["correction_of_sequence"] == mistaken.sequence
    assert correction.payload["corrected_from"] == "interview_scheduled"
    assert correction.payload["corrected_to"] == "recruiter_response"
    # The interview timestamp is history too, and the correction did not fake one.
    assert _record(db, user, job.id).interview_scheduled_at is not None


def test_a_correction_defaults_to_the_last_state_changing_event(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    client.post(f"{API}/{record_id}/note", json={"note": "waiting to hear back"}, headers=auth)

    response = client.post(f"{API}/{record_id}/correction",
                           json={"state": "rejected_by_employer",
                                 "reason": "They replied no last week and I never logged it"},
                           headers=auth)
    assert response.status_code == 200, response.text
    correction = _events(db, user, job.id)[-1]
    # The note is not a state change, so the correction names the row before it.
    named = db.query(ApplicationTrackingEvent).filter(
        ApplicationTrackingEvent.id == correction.correction_of_event_id).one()
    assert named.event_type == "application.tracking_opened"
    assert correction.payload["corrected_from"] == "applied"


def test_a_correction_can_reach_a_state_the_machine_forbids(client, auth, db, user, job):
    """That is the whole point of a correction — and it is audited, not silent."""
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    client.post(f"{API}/{record_id}/rejection", json={"reason": "No fit"}, headers=auth)
    assert client.post(f"{API}/{record_id}/status", json={"state": "applied"},
                       headers=auth).status_code == 409

    response = client.post(f"{API}/{record_id}/correction",
                           json={"state": "interview_scheduled",
                                 "reason": "Mis-clicked: the rejection was for another company"},
                           headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["document"]["state"] == "interview_scheduled"
    assert _record(db, user, job.id).closed_at is None, "a correction out of a closed state re-opens it"
    assert len([row for row in _events(db, user, job.id) if row.is_correction]) == 1


def test_correcting_a_foreign_event_is_404(client, auth, db, user, job, member, member_auth):
    record_id = _open(client, auth, job.id, state="applied")["document"]["tracking_id"]
    their_user = db.query(User).filter(User.email == member["email"]).one()
    theirs = Job(user_id=their_user.id, title="Frontend Engineer", company="Elsewhere",
                 company_name_normalized="elsewhere", description="React.",
                 url="https://jobs.lever.co/elsewhere/7", source="lever",
                 dedupe_key="lever:elsewhere:7", status="discovered")
    db.add(theirs)
    db.commit()
    db.refresh(theirs)

    other = _open(client, member_auth, theirs.id, state="applied")["document"]["tracking_id"]
    their_event = _events(db, their_user, theirs.id)[-1]

    # Naming somebody else's timeline row as the thing being corrected is a 404,
    # not a 403: the row does not exist *for this user*.
    response = client.post(f"{API}/{record_id}/correction",
                           json={"state": "withdrawn", "reason": "wrong row",
                                 "correction_of_event_id": their_event.id}, headers=auth)
    assert response.status_code == 404
    assert client.get(f"{API}/{other}", headers=auth).status_code == 404
    assert _record(db, user, job.id).state == "applied", "nothing moved"


# --------------------------------------------------------------------------- #
# Duplicate events
# --------------------------------------------------------------------------- #
def test_the_same_interview_twice_is_one_event(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    slot = (datetime.utcnow() + timedelta(days=6)).replace(microsecond=0)
    body = {"interview_at": slot.isoformat(), "format": "onsite"}

    first = client.post(f"{API}/{record_id}/interview", json=body, headers=auth)
    second = client.post(f"{API}/{record_id}/interview", json=body, headers=auth)
    assert first.status_code == second.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True, "a double click must not create a second interview"
    assert second.json()["event"]["id"] == first.json()["event"]["id"]
    assert len([row for row in _events(db, user, job.id)
                if row.event_type == "application.interview_reported"]) == 1
    assert _record(db, user, job.id).event_count == 2  # opened + interview


def test_an_idempotency_key_replays_the_first_write(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    headers = {**auth, "Idempotency-Key": "interview-2026-09-25-finco"}
    # A different slot: without the key these would be two events (a reschedule).
    slot_a = (datetime.utcnow() + timedelta(days=2)).replace(microsecond=0).isoformat()
    slot_b = (datetime.utcnow() + timedelta(days=3)).replace(microsecond=0).isoformat()

    first = client.post(f"{API}/{record_id}/interview", json={"interview_at": slot_a}, headers=headers)
    replay = client.post(f"{API}/{record_id}/interview", json={"interview_at": slot_b}, headers=headers)
    assert first.json()["duplicate"] is False
    assert replay.json()["duplicate"] is True
    assert replay.json()["event"]["event_id"] == first.json()["event"]["event_id"]
    assert len([row for row in _events(db, user, job.id)
                if row.event_type == "application.interview_reported"]) == 1


def test_repeating_a_status_change_in_the_same_minute_is_a_duplicate(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    moment = (datetime.utcnow() - timedelta(days=1)).replace(microsecond=0).isoformat()
    body = {"state": "rejected_by_employer", "occurred_at": moment}

    first = client.post(f"{API}/{record_id}/status", json=body, headers=auth)
    again = client.post(f"{API}/{record_id}/status", json=body, headers=auth)
    assert first.json()["state_changed"] is True
    assert again.json()["duplicate"] is True
    assert again.json()["state_changed"] is False
    assert len([row for row in _events(db, user, job.id) if row.state_to == "rejected_by_employer"]) == 1


def test_notes_are_never_deduped(client, auth, db, user, job):
    """Two identical notes are two notes — the dedupe key is None on purpose."""
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    for _ in range(2):
        response = client.post(f"{API}/{record_id}/note", json={"note": "Still waiting"}, headers=auth)
        assert response.status_code == 200
        assert response.json()["duplicate"] is False
    notes = [row for row in _events(db, user, job.id) if row.event_type == "application.note_added"]
    assert len(notes) == 2
    assert all(row.dedupe_key is None for row in notes)


def test_the_sequence_constraint_is_the_backstop_for_a_racing_writer(db, user, job):
    """A row that skips the service cannot reuse a sequence number."""
    from sqlalchemy.exc import IntegrityError

    record, _ = tracking.open_tracking(db, user=user, job=job)
    db.add(ApplicationTrackingEvent(
        event_id="11111111-1111-4111-8111-111111111111", user_id=user.id, tracking_id=record.id,
        job_id=job.id, sequence=1, event_type="application.note_added", origin="user_reported",
        actor_type="user", actor_label=user.email, trigger="user", severity="info",
        message="racing writer", occurred_at=datetime.utcnow(), recorded_at=datetime.utcnow(),
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# --------------------------------------------------------------------------- #
# Origin: system-observed vs user-reported vs inferred
# --------------------------------------------------------------------------- #
def test_system_observed_and_user_reported_stay_apart(client, auth, db, user, job):
    """The automation's submission and the user's memory are different claims."""
    observed = tracking.observe_submission(db, user=user, job=job, channel="automation",
                                           actor_type="system_worker", trigger="auto",
                                           receipt={"external_id": "abc-123", "locator": "confirmation-page"})
    assert observed is not None
    record = _record(db, user, job.id)
    assert record.state == "applied"
    assert record.state_origin == "system_observed"
    event = _events(db, user, job.id)[-1]
    assert event.origin == "system_observed"
    assert event.actor_type == "system_worker"
    assert event.actor_id is None, "a worker is not a person"
    assert event.payload["external_receipt_id"] == "abc-123"
    assert event.evidence[0]["kind"] == "portal_response"
    db.refresh(job)
    assert job.status == "applied" and job.applied_at is not None

    # Observing the same submission again writes nothing.
    assert tracking.observe_submission(db, user=user, job=job, channel="automation") is None
    assert len([row for row in _events(db, user, job.id) if row.event_type == "application.submitted"]) == 1

    # The user reporting an interview afterwards is labelled as the user's claim.
    client.post(f"{API}/{record.id}/interview",
                json={"interview_at": (datetime.utcnow() + timedelta(days=3)).isoformat()},
                headers=auth)
    interview = _events(db, user, job.id)[-1]
    assert interview.origin == "user_reported"
    assert interview.actor_type == "user"
    assert interview.actor_label == user.email
    # The current state's origin is the origin of whoever last claimed it.
    record = _record(db, user, job.id)
    assert record.state == "interview_scheduled"
    assert record.state_origin == "user_reported"
    assert record.state_actor_type == "user"


def test_marking_a_job_applied_by_hand_is_user_reported(client, auth, db, user, job):
    response = client.post(f"/api/jobs/{job.id}/mark-applied?note=Filled+the+portal+myself",
                           headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["tracking_id"] is not None

    record = _record(db, user, job.id)
    assert record.state == "applied"
    assert record.state_origin == "user_reported"
    assert record.channel == "manual_user"
    rows = _events(db, user, job.id)
    submitted = [row for row in rows if row.event_type == "application.submitted"]
    assert len(submitted) == 1
    assert submitted[0].origin == "user_reported"
    assert submitted[0].actor_type == "user"
    assert submitted[0].payload["channel"] == "manual_user"
    # The note the user typed on the way in is kept as a note, not swallowed.
    assert [row for row in rows if row.event_type == "application.note_added"]


def test_an_email_can_never_record_an_interview(db, user, job):
    """Requirement: no interview inferred from email without a verified integration."""
    record, _ = tracking.open_tracking(db, user=user, job=job, state="applied")

    for state in ("interview_scheduled", "interview_completed", "offer_received",
                  "rejected_by_employer"):
        with pytest.raises(tracking.UntrustedOrigin) as excinfo:
            tracking.record_email_suggestion(db, user=user, record=record, suggested_state=state)
        assert excinfo.value.payload()["code"] == "origin_not_trusted"
        assert "user_reported" in excinfo.value.payload()["allowed_origins"]
        # And the same refusal applies to the generic transition path.
        with pytest.raises(tracking.UntrustedOrigin):
            tracking.change_state(db, user=user, record=record, state=state, origin="email_inferred")
    assert record.state == "applied", "nothing moved"
    assert not [row for row in _events(db, user, job.id) if row.origin == "email_inferred"]


def test_an_email_may_only_suggest_a_response_and_stays_provisional(db, user, job):
    record, _ = tracking.open_tracking(db, user=user, job=job, state="applied")

    result = tracking.record_email_suggestion(
        db, user=user, record=record, suggested_state="recruiter_response",
        email_id=42, locator="email:42", confidence=0.62, summary="Would you be free Thursday?",
    )
    event = result["event"]
    assert event.event_type == "application.suggestion_recorded"
    assert event.origin == "email_inferred"
    assert event.is_provisional is True
    assert event.evidence[0]["kind"] == "email_message"
    assert event.payload["suggested_state"] == "recruiter_response"
    # A suggestion does not move the state: the record still says "applied".
    assert record.state == "applied"
    assert record.is_provisional is True
    assert record.provisional_evidence["source"] == "email_inferred"
    # …and the user is told, in words that say it is unconfirmed.
    notice = db.query(Notification).filter(Notification.user_id == user.id).order_by(
        Notification.id.desc()).first()
    assert notice is not None and notice.kind == "application_outcome"
    assert "nothing was recorded as an interview" in notice.body.lower()

    # A repeated suggestion is a duplicate, not a second nudge.
    again = tracking.record_email_suggestion(db, user=user, record=record, email_id=42, locator="email:42")
    assert again["duplicate"] is True
    assert db.query(Notification).filter(Notification.user_id == user.id).count() == 1

    # Confirming turns the inference into the user's own claim.
    confirmed = tracking.confirm_suggestion(db, user=user, record=record)
    assert record.state == "recruiter_response"
    assert record.is_provisional is False
    assert record.state_origin == "user_reported"
    assert confirmed["event"].origin == "user_reported"
    assert confirmed["event"].payload["confirmed_suggestion"] is True


def test_dismissing_a_suggestion_changes_nothing_but_the_flag(db, user, job):
    record, _ = tracking.open_tracking(db, user=user, job=job, state="applied")
    tracking.record_email_suggestion(db, user=user, record=record, locator="email:7")
    assert record.is_provisional is True

    tracking.dismiss_suggestion(db, user=user, record=record, reason="That was a newsletter")
    assert record.is_provisional is False
    assert record.state == "applied"
    dismissed = _events(db, user, job.id)[-1]
    assert dismissed.event_type == "application.state_corrected"
    assert dismissed.payload["dismissed_suggestion"] is True
    assert dismissed.state_to is None, "nothing moved, so nothing is claimed to have moved"


def test_a_verified_integration_may_record_an_interview(db, user, job):
    """The exception to the rule: an integration the user connected and we verified."""
    record, _ = tracking.open_tracking(db, user=user, job=job, state="applied")
    result = tracking.change_state(
        db, user=user, record=record, state="interview_scheduled",
        origin="verified_integration", actor_type="external_provider", trigger="webhook",
        event_type="application.interview_reported",
        message="Interview on your calendar: FinCo, Thursday 14:00.",
        payload={"integration": "calendar", "verified": True},
    )
    assert result["state_changed"] is True
    assert record.state == "interview_scheduled"
    assert record.state_origin == "verified_integration"
    assert _events(db, user, job.id)[-1].actor_type == "external_provider"


# --------------------------------------------------------------------------- #
# Snapshots: source attribution, match score, artefact version
# --------------------------------------------------------------------------- #
def test_the_snapshot_freezes_the_match_score_and_artefact_version(client, auth, db, user, job):
    match = _match(db, user, job, score=88.0, band="strong", version="1.4.0")
    packet = _packet(db, user, job, version=3)

    opened = _open(client, auth, job.id)
    snapshots = opened["document"]["snapshots"]
    assert snapshots["match"] == {"score": 88.0, "band": "strong", "score_source": "ai",
                                  "scorer_version": "1.4.0", "match_id": match.id}
    assert snapshots["artifact"]["kind"] == "packet"
    assert snapshots["artifact"]["id"] == packet.id
    assert snapshots["artifact"]["version"] == 3
    assert snapshots["artifact"]["sha256"] == "h" * 64

    # A later re-score and a new packet version do not rewrite history.
    match.is_current = False
    db.commit()
    _match(db, user, job, score=54.0, band="weak", version="1.5.0")
    _packet(db, user, job, version=4)
    document = client.get(f"{API}/{opened['document']['tracking_id']}", headers=auth).json()
    assert document["snapshots"]["match"]["score"] == 88.0
    assert document["snapshots"]["artifact"]["version"] == 3

    # Refreshing is an explicit act, and it is an event.
    refreshed = client.post(f"{API}/{opened['document']['tracking_id']}/snapshot", headers=auth)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["event"]["event_type"] == "application.snapshot_recorded"
    assert set(refreshed.json()["event"]["payload"]["changed"]) >= {"match_score", "artifact_version"}
    assert refreshed.json()["document"]["snapshots"]["match"]["score"] == 54.0
    # Refreshing again, with nothing to refresh, writes nothing.
    assert client.post(f"{API}/{opened['document']['tracking_id']}/snapshot",
                       headers=auth).json()["event"] is None


def test_a_board_score_without_a_match_row_still_gets_a_band(db, user, job):
    """A report grouped by band must not file scored jobs under "unscored"."""
    job.score = 52.0
    job.score_source = "keyword"
    db.commit()
    snapshot = tracking.build_snapshot(db, user.id, job)
    assert snapshot["match_band"] == "possible"  # contract 06 §3: >= 50
    assert snapshot["match_score"] == 52.0

    # No scorer provenance at all is a different claim: unknown, not "weak".
    job.score_source = None
    db.commit()
    assert tracking.build_snapshot(db, user.id, job)["match_band"] == "unknown"

    # And an unscored job has no invented band.
    job.score = None
    db.commit()
    assert tracking.build_snapshot(db, user.id, job)["match_band"] == ""


def test_source_attribution_can_be_corrected_and_the_old_value_is_kept(client, auth, db, user, job):
    opened = _open(client, auth, job.id, state="applied")
    record_id = opened["document"]["tracking_id"]
    assert opened["document"]["attribution"]["source"] == "lever"

    response = client.post(f"{API}/{record_id}/attribution",
                           json={"source": "referral", "channel": "manual_user",
                                 "reason": "A former colleague referred me; the board id was wrong"},
                           headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["document"]["attribution"]["source"] == "referral"
    assert response.json()["document"]["attribution"]["channel"] == "manual_user"

    event = _events(db, user, job.id)[-1]
    assert event.event_type == "application.attribution_updated"
    assert event.is_correction is True
    assert event.payload["before"] == {"source": "lever", "channel": ""}
    assert event.payload["after"]["source"] == "referral"
    assert event.payload["reason"].startswith("A former colleague")

    # Re-submitting the same attribution is a no-op, not a second event.
    again = client.post(f"{API}/{record_id}/attribution", json={"source": "referral"}, headers=auth)
    assert again.json()["duplicate"] is True


def test_an_unknown_submission_channel_is_refused(client, auth, db, user, job):
    record_id = _open(client, auth, job.id)["document"]["tracking_id"]
    response = client.post(f"{API}/{record_id}/attribution", json={"channel": "carrier_pigeon"},
                           headers=auth)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "validation_error"


# --------------------------------------------------------------------------- #
# Follow-up notifications
# --------------------------------------------------------------------------- #
def _follow_up(client, auth, record_id: int, when: datetime, note: str = "Chase them") -> dict:
    response = client.post(f"{API}/{record_id}/follow-up",
                           json={"follow_up_at": when.isoformat(), "note": note}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_follow_up_is_an_event_and_shows_up_on_the_document(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    due = datetime.utcnow() + timedelta(days=7)

    body = _follow_up(client, auth, record_id, due)
    assert body["document"]["follow_up"]["pending"] is True
    assert body["document"]["follow_up"]["due_at"].endswith("Z")
    assert body["document"]["follow_up"]["overdue"] is False
    assert body["state_changed"] is False, "a reminder is not a status change"
    event = _events(db, user, job.id)[-1]
    assert event.event_type == "application.follow_up_scheduled"
    assert event.follow_up_at is not None
    # Setting the same date again is a duplicate; a new date replaces it.
    assert _follow_up(client, auth, record_id, due)["duplicate"] is True
    moved = _follow_up(client, auth, record_id, due + timedelta(days=3), note="They asked for a week")
    assert moved["duplicate"] is False
    assert moved["event"]["payload"]["replaces"] is not None
    assert _record(db, user, job.id).follow_up_note == "They asked for a week"


def test_a_due_follow_up_notifies_once(db, user, job):
    record, _ = tracking.open_tracking(db, user=user, job=job, state="applied")
    due = datetime.utcnow() - timedelta(hours=2)
    tracking.set_follow_up(db, user=user, record=record, follow_up_at=due, note="Send the thank-you")
    assert record.follow_up_notified_at is None

    swept = tracking.notify_due_follow_ups(db)
    assert swept == {"due": 1, "notified": 1, "skipped": 0}
    notice = db.query(Notification).filter(Notification.user_id == user.id).one()
    assert notice.kind == "application_follow_up"
    assert "FinCo" in notice.title
    assert "Send the thank-you" in notice.body
    assert notice.link == f"/tracking?tracking_id={record.id}"
    assert notice.meta["state"] == "applied"
    assert notice.meta["overdue_days"] == 0
    assert record.follow_up_notified_at is not None

    # A second sweep (a restart, a second worker) must not notify again.
    record.follow_up_notified_at = None
    db.commit()
    again = tracking.notify_due_follow_ups(db)
    assert again["notified"] == 0 and again["skipped"] == 1
    assert db.query(Notification).filter(Notification.user_id == user.id).count() == 1


def test_a_follow_up_that_is_not_due_yet_does_not_notify(db, user, job):
    record, _ = tracking.open_tracking(db, user=user, job=job, state="applied")
    tracking.set_follow_up(db, user=user, record=record,
                           follow_up_at=datetime.utcnow() + timedelta(days=20))
    assert tracking.notify_due_follow_ups(db)["notified"] == 0
    assert db.query(Notification).filter(Notification.user_id == user.id).count() == 0
    # …but it is on the agenda the user can read.
    agenda = tracking.upcoming_follow_ups(db, user.id, days=30)
    assert len(agenda) == 1 and agenda[0]["overdue"] is False
    assert agenda[0]["route"] == f"/tracking?tracking_id={record.id}"


def test_completing_a_follow_up_stops_the_reminder(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    _follow_up(client, auth, record_id, datetime.utcnow() - timedelta(days=1))

    response = client.post(f"{API}/{record_id}/follow-up/complete",
                           json={"note": "Sent the follow-up, they replied"}, headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["document"]["follow_up"]["pending"] is False
    assert _events(db, user, job.id)[-1].event_type == "application.follow_up_completed"
    assert tracking.notify_due_follow_ups(db)["notified"] == 0

    # Cancelling a reminder keeps the event that set it.
    _follow_up(client, auth, record_id, datetime.utcnow() + timedelta(days=2))
    cancelled = client.delete(f"{API}/{record_id}/follow-up", headers=auth)
    assert cancelled.status_code == 200
    assert cancelled.json()["document"]["follow_up"]["due_at"] is None
    assert any(row.event_type == "application.follow_up_scheduled"
               for row in _events(db, user, job.id))


def test_the_follow_ups_read_sweeps_and_returns_the_agenda(client, auth, db, user, job, other_job):
    _open(client, auth, job.id, state="applied")
    _follow_up(client, auth, _record(db, user, job.id).id, datetime.utcnow() - timedelta(hours=5))
    _open(client, auth, other_job.id, state="applied")
    _follow_up(client, auth, _record(db, user, other_job.id).id, datetime.utcnow() + timedelta(days=4))

    response = client.get(f"{API}/follow-ups?days=14", headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sweep"]["notified"] == 1, "the due one is reminded by the read itself"
    assert body["notification_kind"] == "application_follow_up"
    assert [item["overdue"] for item in body["items"]] == [True, False]
    assert body["items"][0]["company"] == "FinCo"
    assert db.query(Notification).filter(Notification.kind == "application_follow_up").count() == 1


def test_the_scheduler_maintenance_pass_notifies_without_auto_mode(db, user, job):
    """A reminder is owed whether or not the user enabled scheduled workflows."""
    import asyncio

    record, _ = tracking.open_tracking(db, user=user, job=job, state="applied")
    tracking.set_follow_up(db, user=user, record=record, follow_up_at=datetime.utcnow() - timedelta(hours=1))
    db.close()

    counts = asyncio.run(AutoScheduler().sweep())
    assert counts["follow_ups"] == 1
    assert db.query(Notification).filter(Notification.kind == "application_follow_up").count() == 1
    # The throttle means the very next sweep does not re-notify.
    assert asyncio.run(AutoScheduler().sweep())["follow_ups"] == 0


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _seed_report_data(db, user, job, other_job) -> dict:
    """A small funnel with every grouping dimension populated.

    =======================  ========  ====  =======  ========  ==============
    job                      source    role  match    artefact  outcome
    =======================  ========  ====  =======  ========  ==============
    ``job`` (FinCo)          lever     BE    88 strong packet:3 applied → interview → offer
    PayCo                    lever     BE    74 good   packet:3 applied → interview
    OtherCo (Data Analyst)   remoteok  DE    52 weak   none     applied → rejection
    GridCo                   remoteok  ML    90 strong none     applied by automation, no outcome
    ``other_job``            remoteok  DE    61 poss.  none     tracked, never applied
    =======================  ========  ====  =======  ========  ==============

    Returns the jobs and records by name so a test can move one of them.
    """
    jobs: dict = {}
    records: dict = {}

    def _add(name: str, job_row: Job) -> ApplicationTracking:
        db.refresh(job_row)
        jobs[name] = job_row
        return job_row

    _match(db, user, job, score=88.0, band="strong")
    _packet(db, user, job, version=3)
    _add("offer", job)
    records["offer"], _ = tracking.open_tracking(db, user=user, job=job, state="applied",
                                                 origin="system_observed", actor_type="system_worker")
    tracking.record_interview(db, user=user, record=records["offer"],
                              interview_at=datetime.utcnow() + timedelta(days=2))
    tracking.record_offer(db, user=user, record=records["offer"])

    interview_job = Job(user_id=user.id, title="Backend Engineer", company="PayCo",
                        company_name_normalized="payco", description="Python.",
                        url="https://jobs.lever.co/payco/9", source="lever",
                        dedupe_key="lever:payco:9", status="applied", score=74.0)
    db.add(interview_job)
    db.commit()
    _match(db, user, interview_job, score=74.0, band="good")
    _packet(db, user, interview_job, version=3)
    _add("interview", interview_job)
    records["interview"], _ = tracking.open_tracking(db, user=user, job=interview_job, state="applied")
    tracking.record_interview(db, user=user, record=records["interview"],
                              interview_at=datetime.utcnow() + timedelta(days=5))

    rejection_job = Job(user_id=user.id, title="Data Analyst", company="OtherCo",
                        company_name_normalized="otherco", description="SQL.",
                        url="https://remoteok.com/3", source="remoteok",
                        dedupe_key="remoteok:otherco:3", status="applied", score=52.0)
    db.add(rejection_job)
    db.commit()
    _match(db, user, rejection_job, score=52.0, band="weak", source="keyword")
    _add("rejection", rejection_job)
    records["rejection"], _ = tracking.open_tracking(db, user=user, job=rejection_job, state="applied")
    tracking.record_rejection(db, user=user, record=records["rejection"],
                              reason="Went with an internal candidate")

    # Applied by our own machinery and nothing since: the funnel's honest
    # "we do not know yet" row, and the one whose origin stays system_observed.
    automation_job = Job(user_id=user.id, title="Machine Learning Engineer", company="GridCo",
                         company_name_normalized="gridco", description="PyTorch, pipelines.",
                         url="https://remoteok.com/4", source="remoteok",
                         dedupe_key="remoteok:gridco:4", status="discovered", score=90.0)
    db.add(automation_job)
    db.commit()
    _match(db, user, automation_job, score=90.0, band="strong")
    _add("automation", automation_job)
    automation_job.status = "applied"
    automation_job.applied_at = datetime.utcnow()
    db.commit()
    tracking.observe_submission(db, user=user, job=automation_job, channel="automation",
                                actor_type="system_worker", trigger="auto")
    records["automation"] = _record(db, user, automation_job.id)

    _add("unapplied", other_job)
    records["unapplied"], _ = tracking.open_tracking(db, user=user, job=other_job)
    return {"jobs": jobs, "records": records}


def test_report_calculates_application_to_interview_conversion(client, auth, db, user, job, other_job):
    _seed_report_data(db, user, job, other_job)

    response = client.get(f"{API}/report?group_by=source", headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    totals = body["totals"]
    # The tracked-never-applied record is out of the denominator by default: a
    # job merely saved to the board cannot dilute a conversion rate.
    assert totals["tracked"] == 4
    assert totals["applied"] == 4
    assert totals["interviews"] == 2
    assert totals["offers"] == 1
    assert totals["rejections"] == 1
    assert totals["application_to_interview_rate"] == 50.0
    assert totals["interview_to_offer_rate"] == 50.0
    assert totals["withdrawn"] == 0
    assert body["disclaimer"], "the report must say what an interview count is"
    assert set(body["interview_definition"]) == set(TRACKING_INTERVIEW_STATES)


def test_a_rate_with_no_applications_is_none_not_zero(client, auth, db, user, job):
    tracking.open_tracking(db, user=user, job=job)  # tracked, never applied
    body = client.get(f"{API}/report", headers=auth).json()
    assert body["totals"]["applied"] == 0
    assert body["totals"]["interviews"] == 0
    assert body["totals"]["application_to_interview_rate"] is None, (
        "no applications is not a 0% conversion rate"
    )
    assert body["groups"] == []


def test_the_report_groups_by_source_score_role_and_artefact(client, auth, db, user, job, other_job):
    _seed_report_data(db, user, job, other_job)

    def groups(dimension: str) -> dict:
        response = client.get(f"{API}/report?group_by={dimension}", headers=auth)
        assert response.status_code == 200, response.text
        return {group["key"]: group for group in response.json()["groups"]}

    by_source = groups("source")
    assert {key: value["applied"] for key, value in by_source.items()} == {"lever": 2, "remoteok": 2}
    assert {key: value["interviews"] for key, value in by_source.items()} == {"lever": 2, "remoteok": 0}
    assert by_source["lever"]["avg_match_score"] == 81.0  # (88 + 74) / 2
    assert by_source["lever"]["offers"] == 1

    by_role = groups("role")
    assert {key: value["interviews"] for key, value in by_role.items()} == {
        "Backend Engineer": 2, "Data Engineer": 0, "ML Engineer": 0,
    }
    assert by_role["Backend Engineer"]["applied"] == 2
    assert by_role["ML Engineer"]["applied"] == 1

    by_band = groups("match_band")
    assert {key: value["tracked"] for key, value in by_band.items()} == {
        "strong": 2, "good": 1, "weak": 1,
    }
    assert {key: value["interviews"] for key, value in by_band.items()} == {
        "strong": 1, "good": 1, "weak": 0,
    }

    by_bucket = groups("score_bucket")
    assert {key: value["tracked"] for key, value in by_bucket.items()} == {
        "90-100": 1, "80-89": 1, "70-79": 1, "0-59": 1,
    }
    assert {key: value["interviews"] for key, value in by_bucket.items()} == {
        "90-100": 0, "80-89": 1, "70-79": 1, "0-59": 0,
    }

    by_artefact = groups("artifact_version")
    assert {key: value["tracked"] for key, value in by_artefact.items()} == {"packet:v3": 2, "none": 2}
    assert {key: value["interviews"] for key, value in by_artefact.items()} == {"packet:v3": 2, "none": 0}

    by_kind = groups("artifact_kind")
    assert {key: value["interviews"] for key, value in by_kind.items()} == {"packet": 2, "none": 0}

    by_state = groups("state")
    assert {key: value["tracked"] for key, value in by_state.items()} == {
        "offer_received": 1, "interview_scheduled": 1, "rejected_by_employer": 1, "applied": 1,
    }

    by_channel = groups("channel")
    assert by_channel["automation"]["applied"] == 1
    assert by_channel["automation"]["by_origin"] == {"system_observed": 1}


def test_the_report_can_be_split_by_origin(client, auth, db, user, job, other_job):
    """A conversion built from memory is not the same claim as an observed one."""
    _seed_report_data(db, user, job, other_job)

    body = client.get(f"{API}/report?group_by=source", headers=auth).json()
    assert body["totals"]["by_origin"] == {"system_observed": 1, "user_reported": 3}

    observed = client.get(f"{API}/report?origin=system_observed", headers=auth).json()
    assert observed["totals"]["applied"] == 1
    assert observed["totals"]["interviews"] == 0, (
        "the automation saw the submission, not the outcome"
    )
    assert observed["totals"]["application_to_interview_rate"] == 0.0
    reported = client.get(f"{API}/report?origin=user_reported", headers=auth).json()
    assert reported["totals"]["applied"] == 3
    assert reported["totals"]["interviews"] == 2

    bad = client.get(f"{API}/report?origin=telepathy", headers=auth)
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "validation_error"


def test_the_report_honours_a_window_and_the_unapplied_flag(client, auth, db, user, job, other_job):
    seeded = _seed_report_data(db, user, job, other_job)
    old = seeded["records"]["rejection"]
    old.applied_at = datetime.utcnow() - timedelta(days=100)
    db.commit()

    everything = client.get(f"{API}/report?include_unapplied=true", headers=auth).json()
    assert everything["totals"]["tracked"] == 5, "the never-applied record is included on request"
    assert everything["totals"]["applied"] == 4
    assert everything["totals"]["rejections"] == 1

    windowed = client.get(
        f"{API}/report?since={(datetime.utcnow() - timedelta(days=30)).isoformat()}", headers=auth
    ).json()
    assert windowed["totals"]["applied"] == 3
    assert windowed["totals"]["rejections"] == 0, "the 100-day-old application is outside the window"
    assert windowed["window"]["since"].endswith("Z")

    # The window is about when the application happened, not when the row was
    # inserted — the record was created today and still falls outside.
    closed = client.get(
        f"{API}/report?until={(datetime.utcnow() - timedelta(days=30)).isoformat()}", headers=auth
    ).json()
    assert closed["totals"]["applied"] == 1
    assert closed["totals"]["rejections"] == 1

    unknown = client.get(f"{API}/report?group_by=astrology", headers=auth)
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["code"] == "validation_error"
    assert "source" in unknown.json()["detail"]["allowed"]


def test_analytics_performance_reads_the_timeline_not_the_score_heuristic(client, auth, db, user, job):
    """The shipped endpoint counted every applied job scoring >=75 as an interview."""
    job.status = "applied"
    job.applied_at = datetime.utcnow()
    job.score = 95.0
    db.commit()

    before = client.get("/api/analytics/performance", headers=auth).json()
    assert before["summary"]["interviews"] == 0, "a high score is not an interview"
    assert before["summary"]["interview_rate"] == 0.0, "one application, no interviews: a real 0%"
    assert before["summary"]["interview_data"] == "none"
    assert before["summary"]["tracked_applications"] == 0
    note = before["summary"]["interview_note"].lower()
    assert "no application outcomes recorded yet" in note
    assert "tracking page" in note, "the note must say where to fix it"

    record, _ = tracking.open_tracking(db, user=user, job=job, state="applied")
    tracking.record_interview(db, user=user, record=record,
                              interview_at=datetime.utcnow() + timedelta(days=2))
    after = client.get("/api/analytics/performance", headers=auth).json()
    assert after["summary"]["interviews"] == 1
    assert after["summary"]["interview_rate"] == 100.0
    assert after["summary"]["interview_data"] == "application_tracking"
    assert after["summary"]["tracking_coverage"] == 100.0
    assert after["roles"][0]["role"] == "Backend Engineer"
    assert after["roles"][0]["interviews"] == 1
    assert after["roles"][0]["interview_rate"] == 100.0
    assert after["conversion"]["totals"]["interviews"] == 1
    assert after["disclaimer"]


def test_analytics_with_nothing_applied_reports_no_rate(client, auth, db, user, job):
    """Empty denominators stay empty instead of becoming a confident zero."""
    body = client.get("/api/analytics/performance", headers=auth).json()
    assert body["summary"]["applied"] == 0
    assert body["summary"]["interview_rate"] is None
    assert body["summary"]["best_role"] is None
    assert body["summary"]["best_role_rate"] is None
    assert all(role["interview_rate"] is None for role in body["roles"])


def test_the_funnel_still_reports_the_board_statuses(client, auth, db, user, job):
    """The tracking layer projects onto ``jobs.status``; it does not replace it."""
    _open(client, auth, job.id, state="applied")
    db.refresh(job)
    assert job.status == "applied"
    funnel = client.get("/api/analytics/funnel", headers=auth).json()
    assert funnel["funnel"]["applied"] == 1


# --------------------------------------------------------------------------- #
# API shape, timeline pagination, tenancy
# --------------------------------------------------------------------------- #
def test_detail_document_is_self_contained(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    client.post(f"{API}/{record_id}/interview",
                json={"interview_at": (datetime.utcnow() + timedelta(days=1)).isoformat()},
                headers=auth)

    document = client.get(f"{API}/{record_id}", headers=auth).json()
    # Labels travel with raw values, so a client cannot word a fact differently.
    assert document["state"] == "interview_scheduled"
    assert document["state_label"] == "Interview scheduled"
    assert document["phase"] == "interviewing"
    assert document["phase_label"] == "Interviewing"
    assert document["job_status"] == "applied"
    assert document["job"]["title"] == "Senior Backend Engineer"
    assert document["server_time"].endswith("Z")
    # The timeline is right there — no second read to render the detail page.
    assert [row["event_type"] for row in document["timeline"]] == [
        "application.tracking_opened", "application.interview_reported",
    ]
    assert document["timeline"][0]["origin"] == "user_reported"
    assert document["timeline"][-1]["state_to_label"] == "Interview scheduled"
    # …and so are the transitions and the buttons.
    assert document["transitions"]["allowed"] == [
        "interview_scheduled", "interview_completed", "offer_received",
        "rejected_by_employer", "withdrawn",
    ]
    keys = {action["key"]: action for action in document["actions"]}
    assert keys["interview"]["disabled"] is False, "a reschedule is allowed"
    assert keys["offer"]["disabled"] is False
    assert keys["response"]["disabled"] is True
    assert keys["response"]["disabled_reason"]
    assert keys["follow_up"]["route"] == f"{API}/{record_id}/follow-up"


def test_the_timeline_paginates_by_sequence(client, auth, db, user, job):
    _open(client, auth, job.id, state="applied")
    record_id = _record(db, user, job.id).id
    for index in range(5):
        client.post(f"{API}/{record_id}/note", json={"note": f"note {index}"}, headers=auth)

    first_page = client.get(f"{API}/{record_id}/events?limit=3", headers=auth).json()
    assert [row["sequence"] for row in first_page["items"]] == [1, 2, 3]
    assert first_page["has_more"] is True
    assert first_page["count"] == 6

    second_page = client.get(f"{API}/{record_id}/events?after_sequence=3&limit=3", headers=auth).json()
    assert [row["sequence"] for row in second_page["items"]] == [4, 5, 6]
    assert second_page["has_more"] is False

    newest = client.get(f"{API}/{record_id}/events?order=desc&limit=2", headers=auth).json()
    assert [row["sequence"] for row in newest["items"]] == [6, 5]


def test_the_list_read_is_a_paginated_envelope(client, auth, db, user, job, other_job):
    _open(client, auth, job.id, state="applied")
    _open(client, auth, other_job.id, state="applied")
    record = _record(db, user, job.id)
    tracking.record_interview(db, user=user, record=record,
                              interview_at=datetime.utcnow() + timedelta(days=1))

    body = client.get(f"{API}?page=1&page_size=1", headers=auth).json()
    assert body["total"] == 2
    assert body["has_more"] is True
    assert len(body["items"]) == 1
    assert body["counts"]["interview_scheduled"] == 1
    assert body["counts"]["applied"] == 1
    assert set(body["states"]) == set(APPLICATION_TRACKING_STATES)

    filtered = client.get(f"{API}?state=interview_scheduled", headers=auth).json()
    assert filtered["total"] == 1
    searched = client.get(f"{API}?q=FinCo", headers=auth).json()
    assert searched["total"] == 1
    unknown = client.get(f"{API}?state=teleported", headers=auth)
    assert unknown.status_code == 422


def test_the_meta_read_carries_the_machine(client, auth):
    body = client.get(f"{API}/meta", headers=auth).json()
    assert body["states"] == list(APPLICATION_TRACKING_STATES)
    assert body["transitions"]["not_applied"] == ["applied", "withdrawn"]
    assert body["transitions"]["rejected_by_employer"] == []
    assert body["interview_states"] == list(TRACKING_INTERVIEW_STATES)
    assert "source" in body["report_group_keys"]
    assert body["state_labels"]["interview_scheduled"] == "Interview scheduled"


def test_cross_tenant_reads_and_writes_are_404(client, auth, member_auth, db, user, job, other_job):
    record_id = _open(client, auth, job.id, state="applied")["document"]["tracking_id"]

    assert client.get(f"{API}/{record_id}", headers=member_auth).status_code == 404
    assert client.get(f"{API}/{record_id}/events", headers=member_auth).status_code == 404
    assert client.get(f"{API}/job/{job.id}", headers=member_auth).status_code == 404
    for path in ("/status", "/interview", "/note", "/correction", "/follow-up", "/attribution"):
        response = client.post(f"{API}/{record_id}{path}",
                               json={"state": "withdrawn", "note": "x", "reason": "nope",
                                     "follow_up_at": datetime.utcnow().isoformat()},
                               headers=member_auth)
        assert response.status_code == 404, f"{path} leaked across tenants"
    # The list and the report are scoped, not filtered afterwards.
    assert client.get(API, headers=member_auth).json()["total"] == 0
    assert client.get(f"{API}/report", headers=member_auth).json()["totals"]["tracked"] == 0


def test_the_endpoints_require_authentication(client):
    assert client.get(API).status_code == 401
    assert client.get(f"{API}/report").status_code == 401
    assert client.get(f"{API}/follow-ups").status_code == 401
    assert client.post(API, json={"job_id": 1}).status_code == 401


# --------------------------------------------------------------------------- #
# Service-level invariants
# --------------------------------------------------------------------------- #
def test_every_event_carries_an_actor_an_origin_and_a_trigger(db, user, job):
    record, _ = tracking.open_tracking(db, user=user, job=job, state="applied")
    tracking.record_interview(db, user=user, record=record,
                              interview_at=datetime.utcnow() + timedelta(days=1))
    tracking.add_note(db, user=user, record=record, note="Prepare the payments story")
    tracking.set_follow_up(db, user=user, record=record, follow_up_at=datetime.utcnow() + timedelta(days=3))
    tracking.correct_state(db, user=user, record=record, state="interview_completed",
                           reason="The screen already happened")

    from app.contracts.vocabulary import ACTOR_TYPES, QUEUE_TRIGGERS, TRACKING_ORIGINS

    events = _events(db, user, job.id)
    assert len(events) == 5
    for row in events:
        assert row.actor_type in ACTOR_TYPES
        assert row.origin in TRACKING_ORIGINS
        assert row.trigger in QUEUE_TRIGGERS
        assert row.actor_label, "an actor always has a label"
        assert len(row.event_id) == 36
    assert [row.sequence for row in events] == [1, 2, 3, 4, 5]
    assert record.event_count == 5 and record.last_sequence == 5
    # A state-changing event's ``state_to`` is the record's state at that point.
    assert events[-1].state_to == record.state


def test_a_state_change_and_its_event_are_one_transaction(db, user, job):
    """The timeline and the row cannot disagree (contracts/09 §7 rule 4)."""
    record, _ = tracking.open_tracking(db, user=user, job=job, state="applied")
    with pytest.raises(tracking.InvalidTransition):
        tracking.change_state(db, user=user, record=record, state="offer_received")
    db.rollback()
    assert record.state == "applied"
    assert len(_events(db, user, job.id)) == 1


def test_transition_table_is_complete_and_matches_the_vocabulary():
    assert set(tracking.TRACKING_TRANSITIONS) == set(APPLICATION_TRACKING_STATES)
    for state, targets in tracking.TRACKING_TRANSITIONS.items():
        assert all(target in APPLICATION_TRACKING_STATES for target in targets), state
        assert state not in targets or state in (
            "recruiter_response", "interview_scheduled", "interview_completed"
        ), f"{state} must not self-loop"
    for terminal in TRACKING_TERMINAL_STATES:
        assert tracking.TRACKING_TRANSITIONS[terminal] == ()
    # Every state has a label and a severity the UI can render.
    assert set(tracking.STATE_LABELS) == set(APPLICATION_TRACKING_STATES)
    assert set(tracking.STATE_SEVERITIES) == set(APPLICATION_TRACKING_STATES)
    assert set(tracking.STATE_EVENT_TYPES) == set(APPLICATION_TRACKING_STATES)
