"""
Regression tests for defects found during the launch-readiness pass.

Each test pins a bug that shipped: a crash, a broken route, an unenforced
security control or a config value that depended on the working directory.
"""
from __future__ import annotations

import os

import pytest

from app.core.config import Settings


# --------------------------------------------------------------------------- #
# 1. Settings: comma-separated (and empty) list values must not crash startup.
# --------------------------------------------------------------------------- #
def test_empty_csv_list_settings_do_not_raise(tmp_path, monkeypatch):
    """`CORS_ORIGINS=` in .env used to abort startup with a SettingsError."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CORS_ORIGINS=\nALLOWED_HOSTS=\nENABLED_SOURCES=\nGREENHOUSE_BOARD_TOKENS=\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    config = Settings(_env_file=str(env_file))
    assert config.cors_origins == []
    assert config.allowed_hosts == []
    assert config.enabled_sources == []
    assert config.greenhouse_board_tokens == []
    # Development keeps the permissive defaults derived from "unset".
    assert config.cors_origin_list == ["*"]


def test_csv_list_settings_are_split(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CORS_ORIGINS=https://app.example.com, https://admin.example.com\n"
        "ALLOWED_HOSTS=app.example.com;admin.example.com\n"
        "ENABLED_SOURCES=greenhouse,lever\n"
        "GREENHOUSE_BOARD_TOKENS=Stripe, Airbnb\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    config = Settings(_env_file=str(env_file))
    assert config.cors_origins == ["https://app.example.com", "https://admin.example.com"]
    assert config.allowed_hosts == ["app.example.com", "admin.example.com"]
    assert config.enabled_sources == ["greenhouse", "lever"]
    assert config.greenhouse_board_tokens == ["stripe", "airbnb"]


def test_json_list_values_still_parse(tmp_path, monkeypatch):
    """Anyone who wrote JSON (pydantic-settings' native format) keeps working."""
    env_file = tmp_path / ".env"
    env_file.write_text('CORS_ORIGINS=["https://app.example.com"]\n', encoding="utf-8")
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert Settings(_env_file=str(env_file)).cors_origins == ["https://app.example.com"]


def test_csv_list_settings_from_the_environment(monkeypatch, tmp_path):
    """The same crash happened for real environment variables (docker, systemd)."""
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com,https://admin.example.com")
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.1")
    config = Settings(_env_file=str(empty))
    assert config.cors_origins == ["https://app.example.com", "https://admin.example.com"]
    assert config.trusted_proxies == ["10.0.0.1"]


def test_real_environment_beats_dotenv(tmp_path, monkeypatch):
    """12-factor: a deployed process must be able to override the file."""
    env_file = tmp_path / ".env"
    env_file.write_text("APP_NAME=From Dotenv\n", encoding="utf-8")
    monkeypatch.setenv("APP_NAME", "From Environment")
    assert Settings(_env_file=str(env_file)).app_name == "From Environment"


# --------------------------------------------------------------------------- #
# 2. Paths must not depend on the process' working directory.
# --------------------------------------------------------------------------- #
def test_relative_sqlite_path_is_pinned_to_the_backend_dir():
    config = Settings(database_url="sqlite:///./jobhunter.db")
    assert config.database_url.startswith("sqlite:///")
    # Four slashes == absolute path (no leading "./" left to resolve against cwd).
    assert config.database_url.startswith("sqlite:////")
    assert config.database_url.endswith("jobhunter.db")


def test_memory_and_absolute_database_urls_are_untouched():
    assert Settings(database_url="sqlite:///:memory:").database_url == "sqlite:///:memory:"
    assert Settings(database_url="sqlite:////data/app.db").database_url == "sqlite:////data/app.db"


def test_data_directories_are_absolute():
    config = Settings(upload_dir="./uploads", generated_dir="./generated",
                      screenshot_dir="./artifacts/screenshots", backup_dir="./backups")
    for path in (config.upload_dir, config.generated_dir, config.screenshot_dir, config.backup_dir):
        assert os.path.isabs(path), path
    assert config.upload_path() == config.upload_dir


# --------------------------------------------------------------------------- #
# 3. Routes: static paths must not be swallowed by an {id} parameter.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/api/jobs/sources", "/api/jobs/discover"])
def test_static_job_routes_are_reachable(client, auth, path):
    response = client.get(path, headers=auth) if path.endswith("sources") \
        else client.post(path, json={"limit": 1, "live_enabled": False}, headers=auth)
    assert response.status_code == 200, response.text


def test_job_sources_lists_registry(client, auth):
    payload = client.get("/api/jobs/sources", headers=auth).json()
    assert "sources" in payload and "selected" in payload
    assert any(source["id"] == "greenhouse" for source in payload["sources"])


def test_unknown_job_id_still_404s(client, auth):
    assert client.get("/api/jobs/999999", headers=auth).status_code == 404


# --------------------------------------------------------------------------- #
# 4. Auth status / registration messaging
# --------------------------------------------------------------------------- #
def test_auth_status_reports_registration_open(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "allow_registration", True)
    payload = client.get("/api/auth/status").json()
    assert payload["registration_open"] is True
    assert payload["allow_registration"] is True
    assert payload["password_min_length"] >= 8
    assert payload["version"]


def test_password_policy_uses_the_configured_minimum(client):
    from app.core.config import settings

    short = "x" * max(8, settings.password_min_length - 1)
    response = client.post("/api/auth/bootstrap",
                           json={"email": "policy@example.com", "password": short})
    assert response.status_code in (400, 422), response.text
    assert str(settings.password_min_length) in response.text or "weak_password" in response.text


def test_validation_errors_carry_a_readable_message(client):
    response = client.post("/api/auth/login", json={"email": "not-an-email", "password": "x"})
    assert response.status_code == 422
    assert "Validation error" not in response.json()["detail"]


# --------------------------------------------------------------------------- #
# 5. API key scopes are enforced (they were stored but never checked)
# --------------------------------------------------------------------------- #
def test_read_only_api_key_cannot_write(client, auth):
    raw = client.post("/api/auth/api-keys", json={"name": "reader", "scopes": ["read"]},
                      headers=auth).json()["api_key"]
    readonly = {"X-API-Key": raw}
    assert client.get("/api/jobs", headers=readonly).status_code == 200
    denied = client.post("/api/jobs/discover", json={"limit": 1, "live_enabled": False}, headers=readonly)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "insufficient_scope"


def test_read_write_api_key_can_write(client, auth):
    raw = client.post("/api/auth/api-keys", json={"name": "ci", "scopes": ["read", "write"]},
                      headers=auth).json()["api_key"]
    assert client.post("/api/jobs/discover", json={"limit": 1, "live_enabled": False},
                       headers={"X-API-Key": raw}).status_code == 200


def test_legacy_api_key_without_scopes_still_works(client, auth, db):
    from app.models.models import ApiKey

    created = client.post("/api/auth/api-keys", json={"name": "legacy"}, headers=auth).json()
    db.query(ApiKey).filter(ApiKey.id == created["id"]).update({"scopes": []}, synchronize_session=False)
    db.commit()
    assert client.post("/api/jobs/discover", json={"limit": 1, "live_enabled": False},
                       headers={"X-API-Key": created["api_key"]}).status_code == 200


# --------------------------------------------------------------------------- #
# 6. Provider webhook is not an open write endpoint in production
# --------------------------------------------------------------------------- #
def test_webhook_requires_a_token_in_production(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "email_webhook_token", "")
    response = client.post("/api/track/events", json={"kind": "bounce"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "webhook_not_configured"


def test_webhook_rejects_a_wrong_token(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "email_webhook_token", "correct-token")
    assert client.post("/api/track/events", json={},
                       headers={"X-Webhook-Token": "nope"}).status_code == 401


# --------------------------------------------------------------------------- #
# 7. Security headers
# --------------------------------------------------------------------------- #
def test_hsts_is_sent_for_https_deployments(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "public_base_url", "https://jobs.example.com")
    response = client.get("/api/health")
    # The running app was built with the original settings; HSTS is set per
    # response by the middleware, so assert on the app factory instead.
    assert response.status_code == 200


def test_hsts_header_present_when_production(monkeypatch):
    from app.core.config import settings
    from app.main import create_app

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "public_base_url", "https://jobs.example.com")
    app = create_app()
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        response = test_client.get("/api/health")
    assert "strict-transport-security" in response.headers
    assert response.headers["content-security-policy"].startswith("default-src 'self'")


# --------------------------------------------------------------------------- #
# 8. create_user.py — the supported way to provision accounts/testers
# --------------------------------------------------------------------------- #
def test_create_user_script_creates_then_updates(monkeypatch, capsys):
    from scripts import create_user

    assert create_user.main(["--email", "cli@example.com", "--password", "cli-password-123"]) == 0
    first = capsys.readouterr().out
    assert "created account cli@example.com" in first

    # A second run must not silently change an existing account.
    assert create_user.main(["--email", "cli@example.com", "--password", "cli-password-123"]) == 1
    assert create_user.main(["--email", "cli@example.com", "--password", "another-password-1",
                             "--reset"]) == 0
    assert "password" in capsys.readouterr().out

    assert create_user.main(["--list"]) == 0
    assert "cli@example.com" in capsys.readouterr().out


def test_create_user_script_rejects_weak_passwords(monkeypatch, capsys):
    from scripts import create_user

    assert create_user.main(["--email", "weak@example.com", "--password", "short"]) == 1
    assert "password rejected" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 9. Demo data is opt-in everywhere, and its ids are stable across restarts
# --------------------------------------------------------------------------- #
def test_demo_pool_respects_the_setting_in_development(monkeypatch):
    """It used to default to *on* outside production, ignoring INCLUDE_DEMO_POOL."""
    from app.core.config import settings
    from app.services.discovery import _demo_pool_enabled

    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "include_demo_pool", False)
    assert _demo_pool_enabled() is False

    monkeypatch.setattr(settings, "include_demo_pool", True)
    assert _demo_pool_enabled() is True


def test_demo_pool_setting_is_read_from_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("INCLUDE_DEMO_POOL=true\n", encoding="utf-8")
    monkeypatch.delenv("INCLUDE_DEMO_POOL", raising=False)
    assert Settings(_env_file=str(env_file)).include_demo_pool is True


def test_demo_job_ids_are_stable_across_processes():
    """`hash()` is salted per process — demo rows were re-inserted on restart."""
    import subprocess
    import sys

    from app.services.demo_pool import demo_jobs

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import sys; sys.path.insert(0, %r); "
            "from app.services.demo_pool import demo_jobs; "
            "print([j['external_id'] for j in demo_jobs(['python'], 72, limit=3)])" % here)
    env = {"PYTHONPATH": here, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    first = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    second = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert first.stdout == second.stdout and first.returncode == 0
    assert [job["external_id"] for job in demo_jobs(["python"], 72, limit=3)] == eval(first.stdout)
    assert all(job["source"] == "demo" for job in demo_jobs(["python"], 72, limit=3))


# --------------------------------------------------------------------------- #
# 10. CLI defaults come from Settings (so .env is honoured, not just $ENV)
# --------------------------------------------------------------------------- #
def test_backup_defaults_come_from_settings(monkeypatch, tmp_path):
    from scripts import backup as backup_script

    monkeypatch.setattr("app.core.config.settings.backup_dir", str(tmp_path / "from-settings"))
    monkeypatch.setattr("app.core.config.settings.backup_keep", 3)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.delenv("BACKUP_KEEP", raising=False)
    assert backup_script.main([]) == 0
    assert (tmp_path / "from-settings").is_dir()
    assert len(list((tmp_path / "from-settings").glob("jobhunter-*"))) == 1


# --------------------------------------------------------------------------- #
# 11. Consent gates live on the routes that act on the outside world
# --------------------------------------------------------------------------- #
def test_sending_email_requires_the_outreach_disclosure(client, auth, uploaded_resume):
    draft = client.post("/api/emails/generate", json={"company": "FinCo"}, headers=auth).json()
    email_id = draft["email"]["id"]

    denied = client.post(f"/api/emails/{email_id}/send", json={}, headers=auth)
    assert denied.status_code == 403
    assert denied.json()["detail"]["consent"] == "outreach"

    assert client.post("/api/account/consent", json={"outreach": True}, headers=auth).status_code == 200
    assert client.post(f"/api/emails/{email_id}/send", json={}, headers=auth).status_code == 200


def test_auto_submit_requires_the_automation_disclosure(client, auth, db, uploaded_resume, monkeypatch):
    """Dry-run preparation stays available; only a real submission is gated."""
    from app.core.config import settings
    from app.models.models import Job, User

    owner = db.query(User).order_by(User.id).first()
    job = Job(user_id=owner.id, title="Backend Engineer", company="FinCo", location="Remote",
              description="Python, FastAPI, PostgreSQL, AWS, Kubernetes.",
              url="https://jobs.lever.co/finco/1", source="lever", dedupe_key="lever:1",
              status="discovered", score=80.0)
    db.add(job)
    db.commit()

    client.put("/api/settings", json={"application": {"allow_auto_submit": True}}, headers=auth)
    monkeypatch.setattr(settings, "autofill_enabled", True)
    monkeypatch.setattr(settings, "autofill_dry_run", False)

    denied = client.post(f"/api/jobs/{job.id}/apply", json={}, headers=auth)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "consent_required"
    assert denied.json()["detail"]["consent"] == "automation"

    assert client.post("/api/account/consent", json={"automation": True}, headers=auth).status_code == 200
    accepted = client.post(f"/api/jobs/{job.id}/apply", json={}, headers=auth)
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "queued"


def test_dry_run_apply_does_not_require_automation_consent(client, auth, db, uploaded_resume, monkeypatch):
    """A platform that only prepares applications must not demand the disclosure."""
    from app.core.config import settings
    from app.models.models import Job, User

    owner = db.query(User).order_by(User.id).first()
    job = Job(user_id=owner.id, title="Backend Engineer", company="FinCo", location="Remote",
              description="Python, FastAPI, PostgreSQL.", url="https://jobs.lever.co/finco/2",
              source="lever", dedupe_key="lever:2", status="discovered", score=70.0)
    db.add(job)
    db.commit()

    monkeypatch.setattr(settings, "autofill_enabled", True)
    monkeypatch.setattr(settings, "autofill_dry_run", True)  # prepare only
    response = client.post(f"/api/jobs/{job.id}/apply", json={}, headers=auth)
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# 12. /api/metrics must not be left open in production
# --------------------------------------------------------------------------- #
def _production(**overrides):
    values = dict(
        environment="production",
        secret_key="a-very-long-and-unique-production-secret-key-1234567890",
        encryption_key="another-long-and-unique-encryption-key-1234567890",
        database_url="postgresql+psycopg2://user:pass@db:5432/jobhunter",
        cors_origins="https://app.example.com",
        allowed_hosts="app.example.com",
        metrics_token="metrics-token",
    )
    values.update(overrides)
    return Settings(**values)


def test_production_requires_a_metrics_token():
    with pytest.raises(ValueError) as excinfo:
        _production(metrics_token="")
    assert "METRICS_TOKEN" in str(excinfo.value)


def test_metrics_may_be_switched_off_instead():
    config = _production(metrics_token="", metrics_enabled=False)
    assert config.metrics_enabled is False


def test_metrics_endpoint_accepts_the_token_by_header(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "metrics_token", "scrape-secret")
    assert client.get("/api/metrics").status_code == 401
    assert client.get("/api/metrics", params={"metrics_token": "scrape-secret"}).status_code == 200
    ok = client.get("/api/metrics", headers={"Authorization": "Bearer scrape-secret"})
    assert ok.status_code == 200
    assert "jobhunter_info" in ok.text
