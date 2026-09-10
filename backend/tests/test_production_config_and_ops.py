"""Production guardrails: config validation and the backup script."""
from __future__ import annotations

import sqlite3
import sys

import pytest

from app.core.config import Settings


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


def test_production_config_accepts_a_hardened_setup():
    config = _production()
    assert config.is_production is True
    assert config.cors_origin_list == ["https://app.example.com"]
    assert config.allowed_host_list == ["app.example.com"]


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"secret_key": "change-me"}, "SECRET_KEY"),
        ({"secret_key": "short"}, "SECRET_KEY"),
        ({"encryption_key": "", "vault_key": ""}, "ENCRYPTION_KEY"),
        ({"encryption_key": "change-me-please"}, "ENCRYPTION_KEY"),
        ({"cors_origins": "*"}, "CORS_ORIGINS"),
        ({"auth_required": False}, "AUTH_REQUIRED"),
        ({"database_url": "sqlite:///./prod.db"}, "SQLite"),
    ],
)
def test_production_config_refuses_insecure_values(overrides, expected):
    with pytest.raises(ValueError) as excinfo:
        _production(**overrides)
    assert expected in str(excinfo.value)


def test_sqlite_is_allowed_when_explicitly_accepted():
    config = _production(database_url="sqlite:///./prod.db", allow_sqlite_in_prod=True)
    assert config.is_production is True


def test_real_email_requires_compliance_configuration():
    with pytest.raises(ValueError) as excinfo:
        _production(email_sending_enabled=True, email_dry_run=False)
    message = str(excinfo.value)
    assert "EMAIL_SMTP_HOST" in message
    assert "EMAIL_POSTAL_ADDRESS" in message
    assert "EMAIL_UNSUBSCRIBE_BASE_URL" in message

    configured = _production(
        email_sending_enabled=True,
        email_dry_run=False,
        email_smtp_host="smtp.example.com",
        email_postal_address="1 Test Street, Testville",
        email_unsubscribe_base_url="https://app.example.com",
    )
    assert configured.email_real_sending is True


def test_development_defaults_stay_permissive():
    config = Settings(environment="development")
    assert config.is_production is False
    assert config.email_real_sending is False
    assert config.autofill_enabled is False
    assert config.allow_synthetic_funding_data is False


def test_public_settings_never_contains_secrets():
    config = _production(ai_api_key="sk-super-secret", email_smtp_password="hunter2")
    payload = config.public_settings()
    assert payload["ai_configured"] is True
    flat = repr(payload)
    assert "sk-super-secret" not in flat
    assert "hunter2" not in flat


def test_backup_script_creates_and_prunes(tmp_path, monkeypatch):
    from scripts import backup as backup_script

    database = tmp_path / "jobhunter.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE demo(id integer primary key, value text)")
    connection.execute("INSERT INTO demo(value) VALUES ('kept')")
    connection.commit()
    connection.close()

    out_dir = tmp_path / "backups"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setattr("app.core.config.settings.database_url", f"sqlite:///{database}")

    assert backup_script.main(["--out", str(out_dir), "--keep", "2"]) == 0
    created = sorted(out_dir.glob("jobhunter-*.db"))
    assert len(created) == 1

    # The copy is a real, readable database with the data intact.
    copied = sqlite3.connect(created[0])
    assert copied.execute("SELECT value FROM demo").fetchone()[0] == "kept"
    copied.close()

    # Pruning keeps only the requested number of generations.
    for _ in range(3):
        assert backup_script.main(["--out", str(out_dir), "--keep", "2"]) == 0
    assert len(list(out_dir.glob("jobhunter-*.db"))) == 2


def test_backup_script_fails_loudly_for_missing_database(tmp_path, monkeypatch):
    from scripts import backup as backup_script

    monkeypatch.setattr("app.core.config.settings.database_url",
                        f"sqlite:///{tmp_path / 'nope' / 'missing.db'}")
    assert backup_script.main(["--out", str(tmp_path / "out")]) == 1
