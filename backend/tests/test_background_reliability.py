"""
Reliability, operational visibility and recovery for the background workflows.

These tests pin the four things the operational contract promises:

1. **Every background failure is classified** — transient / permanent /
   user-action / unknown — and lands in the matching queue state, with a
   bounded reason an operator can group by.
2. **Metrics stay low-cardinality** — no user, job or session identifier can
   become a label value, and an unmapped value collapses to ``other`` rather
   than minting a permanent series.
3. **User-action-required state expires safely** — parked work is never
   retried, always has a deadline, and the deadline survives a restart because
   it is stored on the row.
4. **Logs redact secrets and personal data** on every format.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

import pytest

from app.core import metrics
from app.core.logging import JsonFormatter, TextFormatter
from app.core.redaction import redact_fields, redact_text
from app.models.models import (
    ApplicationAction,
    ApplicationSession,
    Job,
    PipelineJob,
    User,
    UserInputRequest,
)
from app.services import reliability
from app.services.job_queue import claim, enqueue, fail, needs_input, recover_stalled
from app.services.reliability import (
    FAILURE_CODES,
    KIND_PERMANENT,
    KIND_TRANSIENT,
    KIND_UNKNOWN,
    KIND_USER_ACTION,
    METRIC_CATALOG,
    OUTCOME_DEAD,
    OUTCOME_INPUT,
    OUTCOME_PAUSE,
    OUTCOME_RETRY,
    PermanentJobError,
    UserActionRequired,
    apply_failure,
    bounded_label,
    classify_failure,
    count,
    expire_user_actions,
    note_dedupe,
    user_action_deadline,
)


def _user(db) -> User:
    return db.query(User).order_by(User.id).first()


def _job(db, user: User, *, title: str = "Backend Engineer") -> Job:
    job = Job(user_id=user.id, title=title, company="Acme", description="Python, FastAPI",
              url="https://boards.example.com/1", source="greenhouse", status="discovered",
              dedupe_key=f"acme:{title}")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _snapshot_value(key: str) -> float:
    return float(metrics.snapshot().get(key, 0) or 0)


# --------------------------------------------------------------------------- #
# 1. Retry classification
# --------------------------------------------------------------------------- #
class TestRetryClassification:
    """Every background job has a retry classification (acceptance criterion 1)."""

    def test_timeout_is_transient_and_retryable(self):
        decision = classify_failure(TimeoutError("provider timed out"))
        assert decision.outcome == OUTCOME_RETRY
        assert decision.kind == KIND_TRANSIENT
        assert decision.code == "timeout"
        assert decision.retryable is True

    def test_connection_error_is_transient(self):
        decision = classify_failure(ConnectionResetError("reset by peer"))
        assert (decision.kind, decision.code) == (KIND_TRANSIENT, "connection")

    def test_value_error_is_permanent_validation(self):
        # Re-running the identical payload produces the identical rejection, so
        # three retries would be three identical failures.
        decision = classify_failure(ValueError("profile_missing"))
        assert decision.outcome == OUTCOME_DEAD
        assert decision.kind == KIND_PERMANENT
        assert decision.code == "validation"

    def test_bug_is_permanent_and_named_as_one(self):
        for exc in (TypeError("x"), AttributeError("y"), KeyError("z")):
            decision = classify_failure(exc)
            assert decision.outcome == OUTCOME_DEAD
            assert decision.code == "bug", type(exc).__name__

    def test_missing_row_sentinel_is_permanent(self):
        decision = classify_failure(RuntimeError("job_missing"))
        assert decision.outcome == OUTCOME_DEAD
        assert decision.code == "not_found"

    def test_unrecognised_exception_defaults_to_retryable(self):
        decision = classify_failure(Exception("something nobody predicted"))
        assert decision.outcome == OUTCOME_RETRY
        assert decision.kind == KIND_UNKNOWN
        assert decision.code == "unknown"

    def test_user_action_required_is_never_retried(self):
        decision = classify_failure(UserActionRequired("captcha on the portal",
                                                       action_kind="captcha"))
        assert decision.outcome == OUTCOME_INPUT
        assert decision.kind == KIND_USER_ACTION
        assert decision.action_kind == "captcha"
        assert decision.retryable is False

    def test_permanent_job_error_dead_letters_at_once(self):
        decision = classify_failure(PermanentJobError("the posting was withdrawn",
                                                      code="not_found"))
        assert decision.outcome == OUTCOME_DEAD
        assert decision.code == "not_found"

    def test_source_error_codes_map_onto_the_queue(self):
        from app.services.sources.base import SourceError

        assert classify_failure(SourceError("429", code="rate_limited")).code == "rate_limited"
        assert classify_failure(SourceError("429", code="rate_limited")).kind == KIND_TRANSIENT
        assert classify_failure(SourceError("401", code="auth")).kind == KIND_PERMANENT
        assert classify_failure(SourceError("gated", code="gated")).code == "unsupported"

    def test_cancellation_is_never_turned_into_a_retry(self):
        import asyncio

        decision = classify_failure(asyncio.CancelledError())
        assert decision.outcome == OUTCOME_DEAD
        assert decision.code == "cancelled"

    def test_every_code_comes_from_the_bounded_vocabulary(self):
        exceptions = [
            TimeoutError("t"), ConnectionError("c"), ValueError("v"), TypeError("t"),
            RuntimeError("job_missing"), Exception("?"), UserActionRequired("captcha"),
            PermanentJobError("nope"), KeyboardInterrupt(), NotImplementedError(),
        ]
        for exc in exceptions:
            assert classify_failure(exc).code in FAILURE_CODES, exc

    def test_message_is_redacted_before_it_is_stored(self):
        decision = classify_failure(RuntimeError("failed with sk-proj-ABCDEF1234567890"))
        assert "sk-proj-ABCDEF1234567890" not in decision.message
        assert "sk-***" in decision.message

    def test_transient_ai_error_pauses_instead_of_failing(self):
        from app.services.ai_client import REASON_TIMEOUT, AIClientError

        exc = AIClientError("provider timed out", status=504, reason=REASON_TIMEOUT)
        decision = classify_failure(exc)
        assert decision.outcome == OUTCOME_PAUSE
        assert decision.kind == KIND_TRANSIENT
        assert decision.code == "ai_transient"

    def test_blocked_ai_error_dead_letters(self):
        from app.services.ai_client import REASON_NO_API_KEY, AIClientError

        decision = classify_failure(
            AIClientError("no api key configured", reason=REASON_NO_API_KEY))
        assert decision.outcome == OUTCOME_DEAD
        assert decision.code == "ai_blocked"


class TestApplyFailure:
    """The classifier's decision is what the queue actually does."""

    def test_permanent_failure_dead_letters_and_labels_the_row(self, db, owner):
        user = _user(db)
        item = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="perm:1")
        before = _snapshot_value('jobhunter_queue_dead_total{pipeline="discovery"}')

        outcome = apply_failure(db, item, ValueError("profile_missing"))

        db.expire_all()
        row = db.get(PipelineJob, item.id)
        assert outcome == "dead"
        assert row.status == "dead"
        assert row.payload["failure"]["kind"] == KIND_PERMANENT
        assert row.payload["failure"]["code"] == "validation"
        assert _snapshot_value('jobhunter_queue_dead_total{pipeline="discovery"}') > before
        assert _snapshot_value(
            'jobhunter_job_failures_total{code="validation",kind="permanent",pipeline="discovery"}'
        ) >= 1

    def test_transient_failure_requeues_with_budget_left(self, db, owner):
        user = _user(db)
        item = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="trans:1")
        outcome = apply_failure(db, item, TimeoutError("slow"))
        db.expire_all()
        row = db.get(PipelineJob, item.id)
        assert outcome == "retrying"
        assert row.status == "queued"
        assert row.attempts == 1
        assert row.scheduled_at > datetime.utcnow()

    def test_user_action_failure_parks_without_spending_an_attempt(self, db, owner):
        user = _user(db)
        item = enqueue(db, user_id=user.id, pipeline="application", dedupe_key="ua:1")
        outcome = apply_failure(db, item, UserActionRequired("captcha", action_kind="captcha"))
        db.expire_all()
        row = db.get(PipelineJob, item.id)
        assert outcome == "needs_input"
        assert row.status == "needs_input"
        assert row.attempts == 0, "a pause for the user must not spend failure budget"
        assert row.payload["result"]["action_kind"] == "captcha"


# --------------------------------------------------------------------------- #
# 2. Cardinality
# --------------------------------------------------------------------------- #
class TestMetricCardinality:
    """Metrics must avoid high-cardinality user or job labels."""

    FORBIDDEN_LABELS = {
        "user_id", "user", "uid", "email", "job_id", "job", "session_id", "session",
        "tenant", "account", "url", "host", "path", "message", "error", "reason_text",
        "request_id", "resume_id", "packet_id", "document_id",
    }

    def test_no_metric_declares_an_identifier_label(self):
        offenders = {
            name: sorted(set(spec.get("labels") or {}) & self.FORBIDDEN_LABELS)
            for name, spec in METRIC_CATALOG.items()
            if set(spec.get("labels") or {}) & self.FORBIDDEN_LABELS
        }
        assert offenders == {}

    def test_unknown_label_values_collapse_to_other(self):
        assert bounded_label("jobhunter_discovery_jobs_found_total", "source",
                             "user-42-secret-board") == "other"
        assert bounded_label("jobhunter_discovery_jobs_found_total", "source",
                             "greenhouse") == "greenhouse"

    def test_an_exception_message_cannot_become_a_label(self):
        # The whole point of ``code`` being a closed vocabulary: a provider can
        # put anything in an exception message, and a label per message is a
        # series per message.
        code = classify_failure(Exception("user 7 failed on https://acme.example/x?y=1")).code
        assert code in FAILURE_CODES
        assert "acme" not in code and "7" != code

    def test_every_catalog_entry_is_well_formed(self):
        for name, spec in METRIC_CATALOG.items():
            assert name.startswith("jobhunter_"), name
            assert spec["type"] in ("counter", "histogram", "gauge"), name
            assert spec.get("help"), name
            assert spec.get("labels") is not None, name

    def test_histograms_are_registered_as_histograms(self):
        for name, spec in METRIC_CATALOG.items():
            if spec["type"] == "histogram":
                assert name in metrics._HISTOGRAM_NAMES, name

    def test_help_text_is_published_for_every_metric(self):
        from app.core.metrics import _HELP

        missing = [name for name in METRIC_CATALOG if name not in _HELP]
        assert missing == []

    def test_count_coerces_labels_at_runtime(self):
        count("jobhunter_onboarding_total", stage="session", outcome="user-99@example.com")
        key = 'jobhunter_onboarding_total{outcome="other",stage="session"}'
        assert _snapshot_value(key) >= 1
        assert not any("example.com" in series for series in metrics.snapshot())


# --------------------------------------------------------------------------- #
# 3. The metric families the brief asks for
# --------------------------------------------------------------------------- #
class TestRequiredMetricsExist:
    """Each required measurement has a metric, declared and reachable."""

    REQUIRED = {
        "onboarding started/completed/failed": "jobhunter_onboarding_total",
        "resume extraction duration": "jobhunter_resume_extraction_seconds",
        "discovery jobs found": "jobhunter_discovery_jobs_found_total",
        "discovery source failure rate": "jobhunter_source_fetch_outcomes_total",
        "search provider usage": "jobhunter_search_provider_requests_total",
        "match generation duration": "jobhunter_match_generation_seconds",
        "high-fit recommendation count": "jobhunter_high_fit_matches_total",
        "application preparation success": "jobhunter_application_prep_total",
        "autofill success/failure": "jobhunter_autofill_failures_total",
        "CAPTCHA/MFA pauses": "jobhunter_user_action_pauses_total",
        "user-action completion time": "jobhunter_user_action_wait_seconds",
        "auto-submit success/failure": "jobhunter_auto_submit_total",
        "duplicate prevention": "jobhunter_dedupe_hits_total",
        "interview events": "jobhunter_interview_events_total",
    }

    def test_every_required_family_is_in_the_catalog(self):
        missing = {label: name for label, name in self.REQUIRED.items()
                   if name not in METRIC_CATALOG}
        assert missing == {}

    def test_required_metrics_are_documented(self):
        from pathlib import Path

        doc = Path(__file__).resolve().parents[2] / "docs" / "OBSERVABILITY.md"
        text = doc.read_text(encoding="utf-8")
        missing = [name for name in self.REQUIRED.values() if name not in text]
        assert missing == [], f"undocumented metrics: {missing}"

    def test_required_metrics_are_wired_into_the_workflows(self):
        """The metric is written by the module that owns the work, not just declared."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "app"
        owners = {
            "jobhunter_onboarding_total": "services/onboarding.py",
            "jobhunter_resume_extraction_seconds": "services/onboarding.py",
            "jobhunter_discovery_jobs_found_total": "services/discovery.py",
            "jobhunter_discovery_run_seconds": "services/discovery.py",
            "jobhunter_source_fetch_outcomes_total": "services/sources/__init__.py",
            "jobhunter_search_provider_requests_total": "services/search/__init__.py",
            "jobhunter_match_generation_seconds": "services/matching.py",
            "jobhunter_high_fit_matches_total": "services/matching.py",
            "jobhunter_application_prep_total": "services/apply_flow.py",
            "jobhunter_autofill_failures_total": "services/reliability.py",
            "jobhunter_user_action_pauses_total": "services/browser_session.py",
            "jobhunter_user_action_wait_seconds": "services/browser_session.py",
            "jobhunter_auto_submit_total": "services/apply_flow.py",
            "jobhunter_dedupe_hits_total": "services/job_queue.py",
            "jobhunter_interview_events_total": "services/application_tracking.py",
            "jobhunter_browser_session_passes_seconds": "services/assisted_fill.py",
            "jobhunter_artifact_generation_total": "services/resume_service.py",
            "jobhunter_notifications_total": "api/routers/notifications.py",
            "jobhunter_reports_total": "services/outcome_report.py",
        }
        # The user-action metrics are written through reliability's helpers
        # (``note_user_action_*``) rather than as literals at each call site, so
        # either form satisfies the contract.
        aliases = {
            "jobhunter_user_action_pauses_total": "note_user_action_pause",
            "jobhunter_user_action_wait_seconds": "note_user_action_outcome",
        }
        missing = {}
        for metric, path in owners.items():
            body = (root / path).read_text(encoding="utf-8")
            if metric not in body and aliases.get(metric, metric) not in body:
                missing[metric] = path
        assert missing == {}


# --------------------------------------------------------------------------- #
# 4. User-action-required expiry
# --------------------------------------------------------------------------- #
class TestUserActionExpiry:
    """Every user-action-required state expires safely, and never retries."""

    def test_parking_a_row_stores_a_deadline_that_survives_a_restart(self, db, owner):
        user = _user(db)
        item = enqueue(db, user_id=user.id, pipeline="application", dedupe_key="park:1")
        needs_input(db, item, reason="2 fields need your input",
                    result={"status": "needs_input", "action_kind": "unknown_field"})
        db.expire_all()
        row = db.get(PipelineJob, item.id)
        # Stored on the row (not computed from "now + TTL" at sweep time) so a
        # restart, a redeploy and a second worker all read the same moment.
        assert row.payload["user_action"]["expires_at"]
        assert user_action_deadline(row) > datetime.utcnow()

    def test_an_unexpired_row_is_left_alone(self, db, owner):
        user = _user(db)
        item = enqueue(db, user_id=user.id, pipeline="application", dedupe_key="park:2")
        needs_input(db, item, reason="waiting", result={"status": "needs_input"})
        expired = expire_user_actions(db)
        db.expire_all()
        assert expired["queue"] == 0
        assert db.get(PipelineJob, item.id).status == "needs_input"

    def test_an_expired_row_is_closed_not_retried(self, db, owner):
        user = _user(db)
        item = enqueue(db, user_id=user.id, pipeline="application", dedupe_key="park:3")
        needs_input(db, item, reason="waiting", result={"status": "needs_input"})
        db.expire_all()
        row = db.get(PipelineJob, item.id)
        row.payload = {**row.payload,
                       "user_action": {**row.payload["user_action"],
                                       "expires_at": (datetime.utcnow() - timedelta(minutes=1))
                                       .isoformat(timespec="seconds") + "Z"}}
        db.commit()
        attempts_before = row.attempts

        expired = expire_user_actions(db)

        db.expire_all()
        row = db.get(PipelineJob, item.id)
        assert expired["queue"] == 1
        assert row.status == "dead", "expiry closes the row; it must not re-queue it"
        assert row.scheduled_at <= datetime.utcnow() + timedelta(seconds=1)
        assert "user_action_expired" in row.error
        assert row.attempts == attempts_before, "expiry is not a failure"
        assert _snapshot_value('jobhunter_user_action_expiry_total{scope="queue"}') >= 1

    def test_the_user_is_told_their_run_expired(self, db, owner):
        from app.models.models import Notification

        user = _user(db)
        item = enqueue(db, user_id=user.id, pipeline="application", dedupe_key="park:4")
        needs_input(db, item, reason="waiting", result={"status": "needs_input"})
        db.expire_all()
        row = db.get(PipelineJob, item.id)
        row.payload = {**row.payload,
                       "user_action": {**row.payload["user_action"],
                                       "expires_at": (datetime.utcnow() - timedelta(minutes=1))
                                       .isoformat(timespec="seconds") + "Z"}}
        db.commit()

        expire_user_actions(db)

        notices = db.query(Notification).filter(Notification.user_id == user.id).all()
        assert any(n.kind == "automation_failed" for n in notices)

    def test_a_stale_question_row_expires(self, db, owner):
        user = _user(db)
        job = _job(db, user)
        request = UserInputRequest(user_id=user.id, job_id=job.id,
                                   fields=[{"name": "salary", "required": True}],
                                   status="pending",
                                   created_at=datetime.utcnow() - timedelta(days=30))
        db.add(request)
        db.commit()

        expired = expire_user_actions(db)

        db.expire_all()
        assert expired["input_request"] == 1
        assert db.get(UserInputRequest, request.id).status == "expired"

    def test_a_stale_browser_action_expires_and_records_the_wait(self, db, owner):
        user = _user(db)
        job = _job(db, user)
        session = ApplicationSession(user_id=user.id, job_id=job.id, state="awaiting_user",
                                     phase="awaiting_user", created_at=datetime.utcnow(),
                                     expires_at=datetime.utcnow() + timedelta(minutes=5))
        db.add(session)
        db.flush()
        action = ApplicationAction(user_id=user.id, job_id=job.id, session_id=session.id,
                                   kind="captcha", status="pending", dedupe_key="s:captcha",
                                   created_at=datetime.utcnow() - timedelta(minutes=10),
                                   expires_at=datetime.utcnow() - timedelta(minutes=1))
        db.add(action)
        db.commit()

        expired = expire_user_actions(db)

        db.expire_all()
        assert expired["action"] == 1
        assert db.get(ApplicationAction, action.id).status == "expired"
        assert _snapshot_value(
            'jobhunter_user_action_outcomes_total{kind="captcha",outcome="expired"}') >= 1
        assert _snapshot_value(
            'jobhunter_user_action_wait_seconds_count{kind="captcha",outcome="expired"}') >= 1

    def test_the_sweep_is_idempotent(self, db, owner):
        user = _user(db)
        item = enqueue(db, user_id=user.id, pipeline="application", dedupe_key="park:5")
        needs_input(db, item, reason="waiting", result={"status": "needs_input"})
        db.expire_all()
        row = db.get(PipelineJob, item.id)
        row.payload = {**row.payload,
                       "user_action": {**row.payload["user_action"],
                                       "expires_at": (datetime.utcnow() - timedelta(minutes=1))
                                       .isoformat(timespec="seconds") + "Z"}}
        db.commit()

        first = expire_user_actions(db)
        second = expire_user_actions(db)

        assert first["queue"] == 1
        assert second["queue"] == 0, "a second sweep must find nothing to do"


# --------------------------------------------------------------------------- #
# 5. Duplicate execution is safe
# --------------------------------------------------------------------------- #
class TestDuplicateExecution:
    def test_a_second_enqueue_is_refused_and_counted(self, db, owner):
        user = _user(db)
        first = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="dup:1")
        before = _snapshot_value('jobhunter_dedupe_hits_total{scope="queue"}')
        second = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="dup:1")
        assert first is not None and second is None
        assert _snapshot_value('jobhunter_dedupe_hits_total{scope="queue"}') == before + 1

    def test_two_workers_claiming_one_row_yield_exactly_one_winner(self, db, owner):
        user = _user(db)
        enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="dup:2")
        winners = [claim(db, pipelines=["discovery"]) for _ in range(2)]
        assert sum(1 for item in winners if item is not None) == 1

    def test_a_replay_of_a_finished_extraction_is_counted_as_a_dedupe(self):
        # The idempotency guard in the onboarding handler reports a replay;
        # this pins the metric name it uses so the boundary stays verifiable.
        note_dedupe("extraction")
        assert _snapshot_value('jobhunter_dedupe_hits_total{scope="extraction"}') >= 1


# --------------------------------------------------------------------------- #
# 6. Worker restart preserves state
# --------------------------------------------------------------------------- #
class TestRestartSafety:
    def test_a_crashed_items_lease_is_reclaimed_without_spending_attempts(self, db, owner):
        user = _user(db)
        item = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="restart:1")
        claimed = claim(db, pipelines=["discovery"], lease_seconds=60)
        assert claimed is not None and claimed.id == item.id
        attempts_before = claimed.attempts
        # The worker died: the lease is now in the past and nobody holds the row.
        claimed.lease_expires_at = datetime.utcnow() - timedelta(seconds=5)
        db.commit()

        recovered = recover_stalled(db, pipelines=["discovery"], safety_margin_seconds=0)

        db.expire_all()
        row = db.get(PipelineJob, item.id)
        assert recovered == 1
        assert row.status == "queued", "the work comes back after a crash"
        assert row.attempts == attempts_before, "a crash is not a handler failure"
        assert row.reclaim_count == 1

    def test_a_stalled_row_past_its_reclaim_budget_dead_letters(self, db, owner):
        user = _user(db)
        item = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="restart:2")
        claimed = claim(db, pipelines=["discovery"], lease_seconds=60)
        claimed.lease_expires_at = datetime.utcnow() - timedelta(seconds=5)
        # The crash-loop guard fires when the row has *already* been reclaimed
        # up to the budget: a job that dies every time it is picked up must not
        # bounce forever.
        claimed.reclaim_count = 1
        db.commit()
        recover_stalled(db, pipelines=["discovery"], safety_margin_seconds=0, max_reclaims=1)
        db.expire_all()
        assert db.get(PipelineJob, item.id).status == "dead"

    def test_a_requeued_row_runs_exactly_once_more(self, db, owner):
        """Re-execution after recovery must not double-enqueue the work."""
        user = _user(db)
        enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="restart:3")
        claimed = claim(db, pipelines=["discovery"], lease_seconds=60)
        claimed.lease_expires_at = datetime.utcnow() - timedelta(seconds=5)
        db.commit()
        recover_stalled(db, pipelines=["discovery"], safety_margin_seconds=0)
        again = enqueue(db, user_id=user.id, pipeline="discovery", dedupe_key="restart:3")
        assert again is None, "the live row is reused, never duplicated"
        assert db.query(PipelineJob).filter(
            PipelineJob.dedupe_key == "restart:3").count() == 1


# --------------------------------------------------------------------------- #
# 7. Log redaction
# --------------------------------------------------------------------------- #
class TestLogRedaction:
    def _record(self, message: str, *args: object, **extra) -> logging.LogRecord:
        record = logging.LogRecord("app.test", logging.WARNING, "test.py", 1, message,
                                   args or None, None)
        for key, value in extra.items():
            setattr(record, key, value)
        return record

    @pytest.mark.parametrize("secret", [
        "sk-proj-ABCDEFghijklmnop1234567890",
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
        "password=hunter2-secret-value",
        "token=ghp_ABCDEFghijklmnopqrstuvwxyz1234567890",
    ])
    def test_secrets_never_reach_either_format(self, secret):
        record = self._record("provider rejected the call: %s", secret)
        for formatter in (JsonFormatter(), TextFormatter()):
            rendered = formatter.format(record)
            assert secret.split("=")[-1].split()[-1] not in rendered, formatter.__class__.__name__

    def test_personal_data_is_masked_in_the_message(self):
        record = self._record("mailed sam@example.com about +44 20 7946 0958")
        rendered = JsonFormatter().format(record)
        assert "sam@example.com" not in rendered
        assert "s***@example.com" in rendered
        assert "7946 0958" not in rendered

    def test_sensitive_extra_fields_are_masked_by_key_name(self):
        record = self._record("handoff ready", handoff={"mfa_code": "481920"})
        payload = json.loads(JsonFormatter().format(record))
        assert payload["handoff"]["mfa_code"] == "***"

    def test_diagnostics_are_not_collateral_damage(self):
        # An operator still needs the client IP, the date and the error code.
        record = self._record("blocked 192.168.0.1 on 2026-09-20 error_code=ai_blocked")
        rendered = TextFormatter().format(record)
        assert "192.168.0.1" in rendered
        assert "2026-09-20" in rendered
        assert "ai_blocked" in rendered

    def test_redaction_is_idempotent_and_never_raises(self):
        once = redact_text("token=abc123 sk-live-abcdefghij")
        assert redact_text(once) == once
        assert redact_text(None) is None
        assert redact_fields({"user_id": 7, "detail": {"to": "a@b.example"}})["user_id"] == 7

    def test_the_three_other_boundaries_share_one_pattern_list(self):
        from app.core.audit import _AUDIT_SECRET_VALUE_PATTERNS
        from app.core.redaction import SECRET_PATTERNS
        from app.services.ai_client import _PROMPT_SECRET_PATTERNS
        from app.services.onboarding import _SECRET_PATTERNS

        canonical = tuple(pattern for pattern, _ in SECRET_PATTERNS)
        assert _AUDIT_SECRET_VALUE_PATTERNS == canonical
        assert _PROMPT_SECRET_PATTERNS == canonical
        assert _SECRET_PATTERNS == SECRET_PATTERNS


# --------------------------------------------------------------------------- #
# 8. Failure visibility
# --------------------------------------------------------------------------- #
class TestFailureVisibility:
    def test_a_permanent_failure_records_one_bounded_reason(self, db, owner):
        user = _user(db)
        item = enqueue(db, user_id=user.id, pipeline="email", dedupe_key="vis:1")
        apply_failure(db, item, RuntimeError("email_missing"))
        key = 'jobhunter_job_failures_total{code="not_found",kind="permanent",pipeline="email"}'
        assert _snapshot_value(key) >= 1

    def test_the_failure_reason_is_stored_where_the_ui_reads_it(self, db, owner):
        user = _user(db)
        item = enqueue(db, user_id=user.id, pipeline="funding", dedupe_key="vis:2")
        apply_failure(db, item, PermissionError("nope"))
        db.expire_all()
        row = db.get(PipelineJob, item.id)
        assert row.error, "the user-facing error text is stored"
        assert row.payload["failure"]["outcome"] in (
            "dead", "retry", "pause", "needs_input")


# --------------------------------------------------------------------------- #
# 9. The registry reports on itself
# --------------------------------------------------------------------------- #
class TestRegistrySelfMetrics:
    """Cardinality is only manageable if the cap is observable. These are the
    gauges that answer "which metric is growing?" and "has anything been
    evicted?" — asserted through the real endpoints, not a direct call."""

    def test_the_exposition_carries_the_self_metrics_as_gauges(self, client, auth):
        client.get("/api/jobs", headers=auth)
        text = client.get("/api/metrics").text
        assert "# TYPE jobhunter_registry_series gauge" in text
        assert "# TYPE jobhunter_registry_evicted_series gauge" in text
        assert 'jobhunter_registry_series{metric="jobhunter_http_requests_total"}' in text

    def test_a_series_is_named_after_a_metric_never_after_a_user_or_job(
            self, client, auth):
        client.get("/api/jobs", headers=auth)
        text = client.get("/api/metrics").text
        labelled = [line for line in text.splitlines()
                    if line.startswith("jobhunter_registry_series{")]
        assert labelled, "at least one series is published"
        for line in labelled:
            metric = line.split('metric="', 1)[1].split('"', 1)[0]
            assert metric.startswith("jobhunter_"), metric
            # The label set is exactly one label — nothing to leak into.
            assert line.split("{", 1)[1].split("}", 1)[0].count("=") == 1

    def test_ops_status_exposes_the_same_numbers_as_json(self, client, auth):
        client.get("/api/jobs", headers=auth)
        # Warm both endpoints first. The scrape is itself an HTTP request, so
        # the *first* one registers the {endpoint="/api/metrics"} series after
        # any snapshot taken before it — comparing those would be off by one by
        # construction. Once warm, a scrape adds no new series and the two
        # views must agree exactly.
        client.get("/api/metrics")
        client.get("/api/ops/status", headers=auth)
        registry = client.get("/api/ops/status", headers=auth).json()["edge"]["registry"]
        assert registry["max_series_per_metric"] >= 16
        assert registry["series"] >= 1
        entry = registry["by_metric"]["jobhunter_http_requests_total"]
        assert entry["series"] >= 1 and entry["evicted"] >= 0
        # The two views agree, or one of them is lying.
        text = client.get("/api/metrics").text
        exposed = int(text.split(
            'jobhunter_registry_series{metric="jobhunter_http_requests_total"} ', 1)[1]
            .split("\n", 1)[0].split()[0])
        assert exposed == entry["series"]


class TestExpositionTyping:
    """Every family in the exposition must carry a ``# TYPE``.

    Before this was derived from the store the samples came from, a metric that
    was not described in a catalog went out with no type at all — so a
    histogram scraped as an untyped series and ``histogram_quantile()`` had
    nothing to work with. The type is inferred, so this cannot regress by
    someone forgetting to describe a new metric.
    """

    def test_no_family_in_the_exposition_is_untyped(self, client, auth, db):
        from app.core import metrics as m

        # Touch one series of each kind through the real helpers.
        m.inc("jobhunter_queue_dead_total", pipeline="email")
        m.set_gauge("jobhunter_queue_depth", 3.0, scope="global")
        m.observe("jobhunter_queue_reclaim_age_seconds", 12.0, {"pipeline": "email"})
        text = client.get("/api/metrics").text

        typed = dict(re.findall(r"^# TYPE (jobhunter_[a-z0-9_]+) (\w+)", text, re.M))
        # A family is every metric name plus its histogram suffixes.
        families = set()
        for line in text.splitlines():
            if line.startswith("jobhunter_"):
                base = re.match(r"(jobhunter_[a-z0-9_]+?)(?:_bucket|_sum|_count)?[{ ]", line)
                families.add(base.group(1))
        untyped = sorted(families - set(typed))
        assert untyped == [], f"families emitted with no # TYPE line: {untyped}"

    def test_the_type_matches_how_the_metric_is_written(self, client):
        from app.core import metrics as m

        m.inc("jobhunter_queue_dead_total", pipeline="email")
        m.set_gauge("jobhunter_queue_depth", 3.0, scope="global")
        m.observe("jobhunter_queue_reclaim_age_seconds", 12.0, {"pipeline": "email"})
        m.observe("jobhunter_resume_extraction_seconds", 1.5, {"kind": "pdf"})
        typed = dict(re.findall(
            r"^# TYPE (jobhunter_[a-z0-9_]+) (\w+)",
            client.get("/api/metrics").text, re.M))
        assert typed["jobhunter_queue_dead_total"] == "counter"
        assert typed["jobhunter_queue_depth"] == "gauge"
        assert typed["jobhunter_queue_reclaim_age_seconds"] == "histogram"
        assert typed["jobhunter_resume_extraction_seconds"] == "histogram"
        assert typed["jobhunter_registry_series"] == "gauge"
        assert typed["jobhunter_info"] == "gauge"
