"""Settings validation, secret handling and outreach compliance gates."""
from __future__ import annotations

from app.core.security import decrypt_secret
from app.models.models import Email, SettingsModel


def test_settings_defaults_are_returned(client, auth):
    data = client.get("/api/settings", headers=auth).json()
    for category in ("ai", "scraping", "general", "application", "email", "funding", "compliance", "workflows"):
        assert category in data
    assert data["email"]["dry_run"] is True
    assert data["ai"]["api_key_set"] is False


def test_unknown_settings_keys_are_rejected(client, auth):
    assert client.put("/api/settings", json={"nope": {"x": 1}}, headers=auth).status_code == 400
    assert client.put("/api/settings", json={"ai": {"nonsense": 1}}, headers=auth).status_code == 400
    assert client.put("/api/settings", json={"ai": {"rpm": "not-a-number"}}, headers=auth).status_code == 400


def test_settings_update_applies_and_updates_rate_limiter(client, auth, db, owner):
    response = client.put("/api/settings", json={"ai": {"rpm": 42}, "general": {"strict_skeleton": True}},
                          headers=auth)
    assert response.status_code == 200, response.text
    from app.core.rate_limiter import rate_limiter

    assert rate_limiter.rpm == 42
    user = db.query(SettingsModel).filter(SettingsModel.category == "ai", SettingsModel.key == "rpm").first()
    assert user.value == 42


def test_smtp_password_is_encrypted_at_rest_and_never_returned(client, auth, db):
    client.put("/api/settings", json={"email": {"password": "super-secret-app-password", "host": "smtp.example.com"}},
               headers=auth)
    row = db.query(SettingsModel).filter(SettingsModel.category == "email", SettingsModel.key == "password").first()
    assert row is not None
    assert "super-secret-app-password" not in str(row.value)

    me = client.get("/api/auth/me", headers=auth).json()
    assert me["settings"]["email"]["password_set"] is True
    assert "password" not in me["settings"]["email"]


def test_ai_workflow_config_validation_and_masking(client, auth):
    assert client.post("/api/ai/config", json={"not_a_workflow": {"model": "x"}}, headers=auth).status_code == 400
    ok = client.post("/api/ai/config",
                     json={"resume_gen": {"base_url": "https://api.example.com/v1", "model": "gpt-4o",
                                          "api_key": "sk-secret"}},
                     headers=auth)
    assert ok.status_code == 200, ok.text

    listing = client.get("/api/ai/config", headers=auth).json()
    override = listing["overrides"]["resume_gen"]
    assert override["model"] == "gpt-4o"
    assert override["api_key"] == "" and override["api_key_set"] is True

    from app.services.ai_client import resolve_config

    assert resolve_config("resume_gen")["model"] == "gpt-4o"
    assert resolve_config("resume_gen")["api_key"] == "sk-secret"

    assert client.delete("/api/ai/config/resume_gen", headers=auth).status_code == 200
    assert resolve_config("resume_gen")["api_key"] == ""


def test_ai_status_reports_offline_without_key(client, auth):
    status = client.get("/api/settings/ai/status", headers=auth).json()
    assert status["online"] is False
    assert status["configured"] is False
    assert status["reason"] == "no_api_key"
    assert "breakers" in status and "usage" in status


def test_consent_is_recorded_with_timestamps(client, auth):
    before = client.get("/api/account/consent", headers=auth).json()
    assert before["granted"]["outreach"] is False
    response = client.post("/api/account/consent", json={"outreach": True}, headers=auth)
    assert response.status_code == 200
    after = client.get("/api/account/consent", headers=auth).json()
    assert after["granted"]["outreach"] is True
    assert after["consents"]["outreach_accepted_at"]


def test_email_draft_never_sends_without_explicit_enablement(client, full_consent, uploaded_resume, db):
    draft = client.post("/api/emails/generate", json={"company": "FinCo", "founder": True}, headers=full_consent)
    assert draft.status_code == 200, draft.text
    email_id = draft.json()["email"]["id"]
    assert draft.json()["email"]["status"] == "pending_approval"

    report = client.post(f"/api/emails/{email_id}/check", headers=full_consent).json()
    assert "smtp_not_configured" in report["blockers"]
    assert report["ok"] is False

    send = client.post(f"/api/emails/{email_id}/send", json={}, headers=full_consent).json()
    assert send["sent"] is False
    assert send["blocked"] is True
    assert client.get(f"/api/emails/{email_id}", headers=full_consent) is not None


def test_outreach_consent_blocks_send(client, auth, uploaded_resume, db):
    draft = client.post("/api/emails/generate", json={"company": "FinCo"}, headers=auth).json()
    email_id = draft["email"]["id"]
    report = client.post(f"/api/emails/{email_id}/check", headers=auth).json()
    assert "outreach_consent_missing" in report["blockers"]
    assert "terms_not_accepted" in report["blockers"]
    blocked = client.post(f"/api/emails/{email_id}/send", json={}, headers=auth).json()
    assert blocked["sent"] is False


def test_dry_run_reports_no_transmission(client, full_consent, uploaded_resume, monkeypatch):
    from app.core.config import settings

    client.put("/api/settings",
               json={"email": {"host": "smtp.example.com", "username": "me@example.com", "password": "app-pass",
                               "postal_address": "1 Test Street"}},
               headers=full_consent)
    monkeypatch.setattr(settings, "email_sending_enabled", True)
    monkeypatch.setattr(settings, "email_dry_run", True)

    email_id = client.post("/api/emails/generate", json={"company": "FinCo"}, headers=full_consent).json()["email"]["id"]
    result = client.post(f"/api/emails/{email_id}/send", json={}, headers=full_consent).json()
    assert result["dry_run"] is True
    assert result["sent"] is False


def test_real_send_requires_compliance_configuration(client, full_consent, uploaded_resume, monkeypatch):
    from app.core.config import settings

    client.put("/api/settings",
               json={"email": {"host": "smtp.example.com", "username": "me@example.com", "password": "app-pass"}},
               headers=full_consent)
    monkeypatch.setattr(settings, "email_sending_enabled", True)
    monkeypatch.setattr(settings, "email_dry_run", False)
    monkeypatch.setattr(settings, "email_postal_address", "")

    email_id = client.post("/api/emails/generate", json={"company": "FinCo"}, headers=full_consent).json()["email"]["id"]
    report = client.post(f"/api/emails/{email_id}/check", headers=full_consent).json()
    assert "postal_address_missing" in report["blockers"]
    assert client.post(f"/api/emails/{email_id}/send", json={}, headers=full_consent).json()["sent"] is False


def test_suppression_list_blocks_and_can_be_removed(client, full_consent, uploaded_resume, db):
    draft = client.post("/api/emails/generate", json={"company": "FinCo"}, headers=full_consent).json()
    email_id, recipient = draft["email"]["id"], draft["email"]["to"]

    added = client.post("/api/emails/suppressions", json={"email": recipient}, headers=full_consent)
    assert added.status_code == 200
    report = client.post(f"/api/emails/{email_id}/check", headers=full_consent).json()
    assert "recipient_suppressed" in report["blockers"]

    send = client.post(f"/api/emails/{email_id}/send", json={}, headers=full_consent).json()
    assert send["blocked"] is True
    assert send["compliance"]["blockers"] == ["recipient_suppressed"] or "recipient_suppressed" in send["compliance"]["blockers"]

    listing = client.get("/api/emails/suppressions", headers=full_consent).json()
    assert listing and listing[0]["email"] == recipient.lower()
    assert client.delete(f"/api/emails/suppressions/{listing[0]['id']}", headers=full_consent).status_code == 200


def test_unsubscribe_token_suppresses_recipient(client, full_consent, uploaded_resume, db):
    draft = client.post("/api/emails/generate", json={"company": "FinCo"}, headers=full_consent).json()
    email_id = draft["email"]["id"]
    row = db.query(Email).filter(Email.id == email_id).first()

    page = client.get(f"/api/track/unsubscribe/{row.unsubscribe_token}")
    assert page.status_code == 200
    assert "unsubscribed" in page.text.lower()

    report = client.post(f"/api/emails/{email_id}/check", headers=full_consent).json()
    assert "recipient_suppressed" in report["blockers"]


def test_open_pixel_records_events(client, full_consent, uploaded_resume, db):
    email_id = client.post("/api/emails/generate", json={"company": "FinCo"}, headers=full_consent).json()["email"]["id"]
    row = db.query(Email).filter(Email.id == email_id).first()
    pixel = client.get(f"/api/track/open/{row.tracking_token}.png")
    assert pixel.status_code == 200
    assert pixel.headers["content-type"] == "image/gif"
    db.refresh(row)
    assert row.opens == 1
    events = client.get(f"/api/emails/{email_id}/events", headers=full_consent).json()
    assert any(event["kind"] == "open" for event in events)


def test_webhook_bounce_suppresses(client, full_consent, uploaded_resume, db):
    draft = client.post("/api/emails/generate", json={"company": "FinCo"}, headers=full_consent).json()
    recipient = draft["email"]["to"]
    response = client.post("/api/track/events", json={"event": "hard_bounce", "email": recipient})
    assert response.status_code == 200
    assert response.json()["handled"] is True
    suppressions = client.get("/api/emails/suppressions", headers=full_consent).json()
    assert any(row["email"] == recipient.lower() for row in suppressions)
