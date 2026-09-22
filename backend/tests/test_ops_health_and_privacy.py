"""Health/readiness, metrics, logs, ops status, and GDPR export/delete."""
from __future__ import annotations

import os
import subprocess
import sys

from app.models.models import AuditLog, Email, Job, JobEvent, PipelineJob, Resume, User, VaultEntry
from tests.conftest import pdf_bytes


def test_health_endpoints(client):
    assert client.get("/api/health").json()["status"] == "ok"
    live = client.get("/api/health/live")
    assert live.status_code == 200 and live.json()["status"] == "alive"

    ready = client.get("/api/health/ready")
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"]["database"] == "ok"


def test_meta_lists_production_capabilities(client):
    meta = client.get("/api/meta").json()
    assert meta["version"].startswith("2.")
    for feature in ("multi_user_auth_rbac", "durable_pipeline_queue", "audit_trail",
                    "gdpr_export_delete", "alembic_migrations"):
        assert feature in meta["features"]


def test_metrics_are_exposed_after_traffic(client, auth):
    client.get("/api/jobs", headers=auth)
    payload = client.get("/api/metrics").text
    assert "jobhunter_http_requests_total" in payload
    assert "jobhunter_http_request_duration_seconds_bucket" in payload
    assert "jobhunter_info" in payload


def test_metrics_token_protection(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "metrics_token", "s3cret")
    assert client.get("/api/metrics").status_code == 401
    assert client.get("/api/metrics?metrics_token=s3cret").status_code == 200
    assert client.get("/api/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_logs_are_tenant_scoped_and_clearable(client, auth, member_auth, db):
    from app.utils.logger import log_error

    owner = db.query(User).filter(User.email == "owner@example.com").first()
    member = db.query(User).filter(User.email == "member@example.com").first()
    log_error(db, "discovery", "owner failure", user_id=owner.id)
    log_error(db, "discovery", "member failure", user_id=member.id)

    owner_logs = client.get("/api/logs", headers=auth).json()
    assert [row["message"] for row in owner_logs] == ["owner failure"]
    assert client.get("/api/logs?level=info", headers=auth).json() == []
    assert client.delete("/api/logs", headers=auth).json()["deleted"] == 1


def test_ops_status_reports_components(client, auth):
    body = client.get("/api/ops/status", headers=auth).json()
    assert body["database"]["database"] == "ok"
    assert "queues" in body and "discovery" in body["queues"]
    assert body["workers"]["concurrency"] >= 1
    assert "ai" in body and "rate_limiter" in body["ai"]
    # The bounded outbound caches are observable: a worker sweeping thousands of
    # distinct URLs must not grow them, and this is where that is checked.
    outbound = body["outbound"]
    assert outbound["http_cache"]["max_entries"] >= 1
    assert outbound["http_cache"]["entries"] <= outbound["http_cache"]["max_entries"]
    assert outbound["http_cache"]["host_state"]["entries"] <= outbound["http_cache"]["host_state"]["max_entries"]
    assert outbound["dns_cache"]["entries"] <= outbound["dns_cache"]["max_entries"]


def test_retry_dead_resets_both_budgets(client, auth, db):
    """``POST /api/ops/queue/retry-dead`` gives a dead item a fresh start on
    BOTH counters (v2.2.2): the failure budget (``attempts``) and the
    AI-outage pause budget (``payload.paused_count``).

    Resetting only ``attempts`` re-queued a pause-capped item with
    ``AI_PAUSE_MAX + 1`` pauses already banked, so its very next outage
    dead-lettered it again and the operator's retry looked like a no-op.
    """
    from app.services.job_queue import AI_PAUSE_MAX, enqueue, fail, pause

    owner = db.query(User).filter(User.email == "owner@example.com").first()

    # One item dead on failures, one dead on the pause cap.
    exhausted = enqueue(db, user_id=owner.id, pipeline="discovery", max_attempts=1,
                        dedupe_key="retry-dead:failures")
    assert fail(db, exhausted, "boom") == "dead"
    parked = enqueue(db, user_id=owner.id, pipeline="ai", max_attempts=3,
                     payload={"task": "tag_resume"}, dedupe_key="retry-dead:pauses")
    parked.payload = {**(parked.payload or {}), "paused_count": AI_PAUSE_MAX}
    db.commit()
    assert pause(db, parked, "ai_transient_outage") == "dead"
    db.expire_all()
    assert db.query(PipelineJob).filter(PipelineJob.status == "dead").count() == 2

    assert client.post("/api/ops/queue/retry-dead", headers=auth).json()["requeued"] == 2

    db.expire_all()
    for item in db.query(PipelineJob).all():
        assert item.status == "queued"
        assert item.attempts == 0, "the failure budget is fresh"
        assert item.error == ""
        assert "paused_count" not in (item.payload or {}), "the pause budget is fresh too"

    # …and the pause-capped item can actually sit out another outage now.
    parked = db.query(PipelineJob).filter(PipelineJob.dedupe_key == "retry-dead:pauses").one()
    assert pause(db, parked, "ai_transient_outage") == "paused"
    assert parked.attempts == 0
    assert (parked.payload or {}).get("paused_count") == 1


def test_runtime_and_settings_runtime_are_safe(client, auth):
    body = client.get("/api/settings/runtime", headers=auth).json()
    assert body["platform"]["environment"] == "test"
    assert body["platform"]["email_dry_run"] is True
    assert body["autofill"]["available"] is False
    assert any(row["id"] == "sec_edgar" for row in body["funding_providers"])

    assert client.get("/api/settings/runtime").status_code == 401


def test_account_runtime_includes_sources(client, auth):
    body = client.get("/api/account/runtime", headers=auth).json()
    ids = {row["id"] for row in body["sources"]}
    assert {"greenhouse", "lever", "linkedin"} <= ids
    assert body["autofill_runtime"]["available"] is False
    assert "assisted_apply_runtime" in body


def test_audit_trail_is_available(client, auth):
    client.post("/api/vault", json={"domain": "x.example.com", "username": "u@example.com",
                                    "password": "password-1234"}, headers=auth)
    rows = client.get("/api/account/audit", headers=auth).json()
    assert any(row["action"] == "vault.credential_created" for row in rows)


def test_usage_counters(client, auth, db, uploaded_resume):
    body = client.get("/api/account/billing-usage", headers=auth).json()
    assert body["resumes"] >= 1
    assert "jobs" in body and "emails" in body


def test_consent_disclosures_are_public(client):
    body = client.get("/api/account/disclosures").json()
    for key in ("terms", "automation", "outreach", "data_processing"):
        assert key in body["disclosures"]
        assert body["disclosures"][key]["summary"]


def test_gdpr_export_contains_everything_owned(client, full_consent, db, uploaded_resume):
    job = db.query(Job).first()
    if job is None:
        job = Job(user_id=db.query(User).first().id, title="T", company="C", dedupe_key="x", description="d")
        db.add(job)
        db.commit()
    client.post("/api/vault", json={"domain": "x.example.com", "username": "u@example.com",
                                    "password": "password-1234"}, headers=full_consent)
    export = client.get("/api/account/export", headers=full_consent)
    assert export.status_code == 200
    assert "attachment" in export.headers["content-disposition"]
    body = export.json()
    assert body["account"]["email"] == "owner@example.com"
    assert body["resumes"] and body["vault"][0]["password"] == "password-1234"
    assert body["audit"]
    # settings exports must never leak the SMTP password in clear
    assert all(row["value"] != "password-1234" for row in body["settings"])


def test_account_deletion_requires_email_confirmation(client, auth):
    assert client.delete("/api/account", headers=auth).status_code == 400
    assert client.delete("/api/account?confirm=wrong@example.com", headers=auth).status_code == 400


def test_account_deletion_removes_all_data_and_files(client, auth, db, uploaded_resume):
    user = db.query(User).filter(User.email == "owner@example.com").first()
    resume_path = db.query(Resume).filter(Resume.user_id == user.id).first().filepath
    db.add(Job(user_id=user.id, title="J", company="C", dedupe_key="k", description="d"))
    client.post("/api/vault", json={"domain": "x.example.com", "username": "u@example.com",
                                    "password": "password-1234"}, headers=auth)
    db.commit()

    response = client.delete("/api/account?confirm=owner@example.com", headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True

    assert db.query(User).filter(User.email == "owner@example.com").count() == 0
    for model in (Job, Resume, VaultEntry, JobEvent, PipelineJob, Email):
        assert db.query(model).filter(getattr(model, "user_id", None) == user.id).count() == 0 if hasattr(model, "user_id") else True
    assert not os.path.exists(resume_path)
    # The deletion itself is still auditable (with no user attached).
    assert db.query(AuditLog).filter(AuditLog.action == "account.deleted").count() == 1


def test_health_ready_reports_migration_state(client):
    body = client.get("/api/health/ready").json()
    assert "migrations" in body["checks"]


def test_alembic_migrations_apply_to_a_fresh_database(tmp_path):
    """The migration chain must build the whole schema from scratch."""
    db_path = tmp_path / "migration.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db_path}",
        "ALEMBIC_DATABASE_URL": f"sqlite:///{db_path}",
        "ENVIRONMENT": "test",
        "SECRET_KEY": "test-secret-key-that-is-long-enough-1234567890",
        "ENCRYPTION_KEY": "test-encryption-key-1234567890-abcdefghij",
    }
    backend = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                            cwd=backend, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    import sqlite3

    connection = sqlite3.connect(db_path)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for expected in ("users", "jobs", "job_events", "audit_logs", "pipeline_jobs", "email_opt_outs",
                     "refresh_tokens", "api_keys", "scheduled_runs", "alembic_version"):
        assert expected in tables

    # The v2.2 auto-mode history table is described by the chain itself (not just
    # by ``create_all``), including the columns the scheduler and the Settings
    # card read.
    columns = {row[1] for row in connection.execute("PRAGMA table_info(scheduled_runs)")}
    assert {"user_id", "workflow", "cycle_bucket", "triggered_at", "state", "queue_job_id",
            "job_id", "reason", "meta"} <= columns, columns

    # Idempotent: a second upgrade is a no-op.
    again = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                           cwd=backend, env=env, capture_output=True, text=True)
    assert again.returncode == 0, again.stderr
    connection.close()

    # And reversible: rolling the scheduled_runs revision back drops the table
    # cleanly, and upgrading again rebuilds it. A migration that cannot be
    # undone is a release that cannot be rolled back. Pinned to the *revision*,
    # not to ``-1``: since v2.2.5 the chain continues past this migration, and
    # a relative step then undoes the wrong one (the same stale-``-1`` bug the
    # funding history test documents).
    down = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "b2c3d4e5f6a7"],
                          cwd=backend, env=env, capture_output=True, text=True)
    assert down.returncode == 0, down.stderr
    connection = sqlite3.connect(db_path)
    after_down = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    connection.close()
    assert "scheduled_runs" not in after_down, after_down

    up = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                        cwd=backend, env=env, capture_output=True, text=True)
    assert up.returncode == 0, up.stderr


def test_migrated_schema_builds_every_column_the_models_declare(tmp_path):
    """``create_all`` (the suite) and the Alembic chain (production) must agree.

    A column that exists only in ``app/models/models.py`` is invisible to every
    test, because the test schema is built from that same metadata — and fatal in
    a deployed install, where the first ``INSERT`` through the ORM names a column
    the database never got. That is exactly how ``pipeline_jobs.reclaim_count``
    shipped (migration ``a7b8c9d0e1f2`` closed it), and it is the same blind spot
    the deletion bugs hid behind: the suite describes *a* database rather than
    reading the one production runs.
    """
    db_path = tmp_path / "drift.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db_path}",
        "ALEMBIC_DATABASE_URL": f"sqlite:///{db_path}",
        "ENVIRONMENT": "test",
        "SECRET_KEY": "test-secret-key-that-is-long-enough-1234567890",
        "ENCRYPTION_KEY": "test-encryption-key-1234567890-abcdefghij",
    }
    backend = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                            cwd=backend, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    import sqlite3

    from app.db import Base
    from app.models import models  # noqa: F401  (registers the metadata)

    connection = sqlite3.connect(db_path)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    drift: dict[str, list[str]] = {}
    for name, table in Base.metadata.tables.items():
        if name not in tables:
            drift[name] = ["<table missing>"]
            continue
        built = {row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')}
        missing = sorted(set(table.columns.keys()) - built)
        if missing:
            drift[name] = missing
    connection.close()
    assert not drift, f"the migration chain does not build what the models declare: {drift}"
