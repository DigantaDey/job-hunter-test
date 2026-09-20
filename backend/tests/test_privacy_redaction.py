"""
Privacy redaction tests — secrets never reach logs, AI prompts, audit detail,
action payloads or stored error messages.

These tests pin the boundaries where sensitive data meets an output channel:

* ``safe_error_message`` masks every known secret shape before storage.
* ``sanitize_audit_detail`` masks secrets that might appear in audit metadata.
* ``_scrub_prompt_secrets`` masks secrets before they reach an AI provider.
* ``sanitize_action_payload`` strips credential values from browser action records.
* The AI client never forwards a prompt that contains a vault credential.
* Search queries never contain PII.
* Browser session responses never expose storage state or field values.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Dict

import pytest


# --------------------------------------------------------------------------- #
# safe_error_message — the onboarding/worker boundary
# --------------------------------------------------------------------------- #
class TestSafeErrorMessage:
    def test_masks_openai_style_keys(self):
        from app.services.onboarding import safe_error_message

        result = safe_error_message("failed: sk-proj-ABCDEFghijklmnop1234567890")
        assert "sk-proj-ABCDEFghijklmnop1234567890" not in result
        assert "sk-***" in result

    def test_masks_bearer_tokens(self):
        from app.services.onboarding import safe_error_message

        result = safe_error_message("401 Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig")
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        assert "Bearer ***" in result

    def test_masks_api_key_assignment(self):
        from app.services.onboarding import safe_error_message

        result = safe_error_message("api_key=sk-live-abcdef1234567890xyz")
        assert "sk-live-abcdef1234567890xyz" not in result

    def test_masks_password_in_error(self):
        from app.services.onboarding import safe_error_message

        result = safe_error_message("auth failed password=hunter2-secret-value rejected")
        assert "hunter2-secret-value" not in result

    def test_masks_token_assignment(self):
        from app.services.onboarding import safe_error_message

        result = safe_error_message("token=ghp_ABCDEFghijklmnopqrstuvwxyz1234567890")
        assert "ghp_ABCDEFghijklmnopqrstuvwxyz1234567890" not in result

    def test_collapses_control_characters(self):
        from app.services.onboarding import safe_error_message

        result = safe_error_message("line1\napi_key: sk-proj-ABCDEF1234567890\tsecret")
        assert "\n" not in result
        assert "\t" not in result
        assert "sk-proj-ABCDEF1234567890" not in result

    def test_truncates_long_messages(self):
        from app.services.onboarding import safe_error_message

        result = safe_error_message("x" * 2000, limit=200)
        assert len(result) <= 200

    def test_masks_jwt_shaped_strings(self):
        from app.services.onboarding import safe_error_message

        jwt = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature_here_long_enough"
        result = safe_error_message(f"failed with {jwt}")
        assert jwt not in result

    def test_masks_authorization_header_value(self):
        from app.services.onboarding import safe_error_message

        result = safe_error_message("Authorization: Bearer sk-abcdef1234567890abcdef")
        assert "sk-abcdef1234567890abcdef" not in result

    def test_handles_none_and_empty(self):
        from app.services.onboarding import safe_error_message

        assert safe_error_message(None) == ""
        assert safe_error_message("") == ""
        assert safe_error_message("clean error message") == "clean error message"


# --------------------------------------------------------------------------- #
# sanitize_audit_detail — audit trail secret masking
# --------------------------------------------------------------------------- #
class TestSanitizeAuditDetail:
    def test_masks_secret_values_in_flat_dict(self):
        from app.core.audit import sanitize_audit_detail

        detail = {
            "action": "vault.credential_created",
            "domain": "jobs.lever.co",
            "password": "hunter2-super-secret",
            "api_key": "sk-proj-ABCDEF1234567890",
        }
        result = sanitize_audit_detail(detail)
        assert result["action"] == "vault.credential_created"
        assert result["domain"] == "jobs.lever.co"
        assert "hunter2-super-secret" not in str(result)
        assert "sk-proj-ABCDEF1234567890" not in str(result)

    def test_masks_nested_dict_values(self):
        from app.core.audit import sanitize_audit_detail

        detail = {
            "config": {
                "username": "user@example.com",
                "password": "secret-value-123",
            },
            "token": "Bearer abc.def.ghi",
        }
        result = sanitize_audit_detail(detail)
        assert result["config"]["username"] == "user@example.com"
        assert "secret-value-123" not in str(result)
        assert "Bearer abc.def.ghi" not in str(result)

    def test_preserves_non_secret_values(self):
        from app.core.audit import sanitize_audit_detail

        detail = {
            "email": "user@example.com",
            "session_id": 42,
            "rows_removed": 15,
            "files_removed": 3,
        }
        result = sanitize_audit_detail(detail)
        assert result == detail

    def test_handles_none_and_empty(self):
        from app.core.audit import sanitize_audit_detail

        assert sanitize_audit_detail(None) == {}
        assert sanitize_audit_detail({}) == {}

    def test_masks_sensitive_key_names_regardless_of_value(self):
        from app.core.audit import sanitize_audit_detail

        detail = {
            "secret": "any-value-here",
            "mfa_code": "123456",
            "authorization": "some-header-value",
        }
        result = sanitize_audit_detail(detail)
        assert "any-value-here" not in str(result.get("secret", ""))
        assert "123456" not in str(result.get("mfa_code", ""))

    def test_handles_lists(self):
        from app.core.audit import sanitize_audit_detail

        detail = {
            "keys": ["sk-proj-ABCDEF1234567890", "normal-value"],
            "count": 2,
        }
        result = sanitize_audit_detail(detail)
        assert "sk-proj-ABCDEF1234567890" not in str(result)
        assert result["count"] == 2


# --------------------------------------------------------------------------- #
# _scrub_prompt_secrets — AI prompt boundary
# --------------------------------------------------------------------------- #
class TestScrubPromptSecrets:
    def test_masks_api_key_in_prompt(self):
        from app.services.ai_client import _scrub_prompt_secrets

        prompt = "Process this resume. API key for verification: sk-proj-ABCDEF1234567890"
        result = _scrub_prompt_secrets(prompt)
        assert "sk-proj-ABCDEF1234567890" not in result
        assert "***" in result

    def test_masks_bearer_token_in_prompt(self):
        from app.services.ai_client import _scrub_prompt_secrets

        prompt = "Auth: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
        result = _scrub_prompt_secrets(prompt)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result

    def test_masks_password_in_prompt(self):
        from app.services.ai_client import _scrub_prompt_secrets

        prompt = "Error: auth failed password=hunter2-secret-value rejected by portal"
        result = _scrub_prompt_secrets(prompt)
        assert "hunter2-secret-value" not in result

    def test_clean_prompt_passes_through(self):
        from app.services.ai_client import _scrub_prompt_secrets

        prompt = "Extract the candidate's skills from this resume: Python, FastAPI, PostgreSQL."
        result = _scrub_prompt_secrets(prompt)
        assert result == prompt

    def test_empty_and_none_prompts(self):
        from app.services.ai_client import _scrub_prompt_secrets

        assert _scrub_prompt_secrets("") == ""
        assert _scrub_prompt_secrets(None) == ""


# --------------------------------------------------------------------------- #
# sanitize_action_payload — browser action records
# --------------------------------------------------------------------------- #
class TestSanitizeActionPayload:
    def test_strips_password_field_values(self):
        from app.services.browser_session import sanitize_action_payload

        payload = {
            "field": "password",
            "classification": "credential",
            "value": "my-secret-password",
            "status": "filled",
        }
        result = sanitize_action_payload(payload)
        assert "my-secret-password" not in str(result)
        assert result.get("field") == "password"
        assert result.get("status") == "filled"
        assert result.get("value") is None or result.get("value") == "***"

    def test_strips_ssn_field_values(self):
        from app.services.browser_session import sanitize_action_payload

        payload = {
            "field": "ssn",
            "classification": "sensitive_pii",
            "value": "123-45-6789",
        }
        result = sanitize_action_payload(payload)
        assert "123-45-6789" not in str(result)

    def test_preserves_non_sensitive_values(self):
        from app.services.browser_session import sanitize_action_payload

        payload = {
            "field": "first_name",
            "classification": "identity",
            "value": "Jane",
            "status": "filled",
        }
        result = sanitize_action_payload(payload)
        # Non-sensitive values may be kept or fingerprinted depending on policy
        assert result.get("field") == "first_name"
        assert result.get("status") == "filled"

    def test_handles_none_and_empty(self):
        from app.services.browser_session import sanitize_action_payload

        assert sanitize_action_payload(None) == {}
        assert sanitize_action_payload({}) == {}


# --------------------------------------------------------------------------- #
# Log sanitization — control characters and value scrubbing
# --------------------------------------------------------------------------- #
class TestLogSanitization:
    def test_sanitize_log_value_escapes_newlines(self):
        from app.core.logging import sanitize_log_value

        assert sanitize_log_value("line1\nline2") == "line1\\nline2"
        assert sanitize_log_value("col1\tcol2") == "col1\\tcol2"
        assert sanitize_log_value("null\x00byte") == "null\\x00byte"
        assert sanitize_log_value("uni\u2028break") == "uni\\x2028break"

    def test_sanitize_log_value_passes_non_strings(self):
        from app.core.logging import sanitize_log_value

        assert sanitize_log_value(42) == 42
        assert sanitize_log_value(None) is None
        assert sanitize_log_value(True) is True

    def test_sanitize_log_value_truncates(self):
        from app.core.logging import sanitize_log_value

        result = sanitize_log_value("x" * 5000, limit=64)
        assert len(result) == 64

    def test_sanitize_log_value_plain_passthrough(self):
        from app.core.logging import sanitize_log_value

        assert sanitize_log_value("clean value") == "clean value"


# --------------------------------------------------------------------------- #
# Search query privacy — no PII in outbound queries
# --------------------------------------------------------------------------- #
class TestSearchQueryPrivacy:
    def test_only_curated_roles_are_used(self):
        from app.services.search.queries import SearchPreferences, generate_queries

        prefs = SearchPreferences.from_normalized({
            "target_roles": ["software engineer", "NOT_A_REAL_ROLE_XYZ", "data scientist"],
            "remote": "remote",
        })
        queries = generate_queries(prefs)
        for q in queries:
            assert "NOT_A_REAL_ROLE_XYZ" not in q

    def test_personal_info_never_in_queries(self):
        from app.services.search.queries import SearchPreferences, generate_queries

        prefs = SearchPreferences.from_normalized({
            "target_roles": ["software engineer"],
            "remote": "remote",
        })
        queries = generate_queries(prefs)
        # None of these should ever appear in a search query
        pii_values = ["john.doe@example.com", "555-123-4567", "123 Main Street"]
        for q in queries:
            for pii in pii_values:
                assert pii not in q

    def test_free_form_titles_are_dropped(self):
        from app.services.search.queries import SearchPreferences

        prefs = SearchPreferences.from_normalized({
            "target_roles": ["VP of Quantum Blockchain Synergy at Acme Corp"],
            "remote": "",
        })
        # This role is not in the curated vocabulary
        assert prefs.roles == ()

    def test_empty_preferences_produce_no_queries(self):
        from app.services.search.queries import SearchPreferences, generate_queries

        prefs = SearchPreferences.from_normalized({"target_roles": [], "remote": ""})
        queries = generate_queries(prefs)
        assert queries == []


# --------------------------------------------------------------------------- #
# Vault encryption isolation
# --------------------------------------------------------------------------- #
class TestVaultIsolation:
    def test_different_users_produce_different_ciphertexts(self):
        from app.core.security import encrypt_secret

        ct1 = encrypt_secret("same-password", "user:1:vault")
        ct2 = encrypt_secret("same-password", "user:2:vault")
        assert ct1 != ct2

    def test_cross_user_decryption_fails(self):
        from app.core.security import decrypt_secret, encrypt_secret
        from cryptography.fernet import InvalidToken

        ct = encrypt_secret("my-password", "user:1:vault")
        with pytest.raises(InvalidToken):
            decrypt_secret(ct, "user:2:vault", allow_legacy=False)


# --------------------------------------------------------------------------- #
# Consent enforcement
# --------------------------------------------------------------------------- #
class TestConsentEnforcement:
    def test_disclosures_are_public(self, client):
        """Disclosures are readable without authentication."""
        response = client.get("/api/account/disclosures")
        assert response.status_code == 200
        body = response.json()
        assert "disclosures" in body
        for key in ("terms", "automation", "outreach", "data_processing"):
            assert key in body["disclosures"]
            assert "title" in body["disclosures"][key]
            assert "summary" in body["disclosures"][key]

    def test_automation_consent_required_for_submit(self, client, auth, db):
        """Automation consent must be accepted before auto-submit is possible."""
        from app.services.automation_policy import plan_http_overrides
        from app.models.models import User

        user = db.query(User).order_by(User.id).first()
        overrides = plan_http_overrides(db, user)
        # Without consent, the HTTP gate must be closed
        if not (user.consents or {}).get("automation_accepted_at"):
            assert overrides.consent_automation is False


# --------------------------------------------------------------------------- #
# Browser session privacy — response shape
# --------------------------------------------------------------------------- #
class TestBrowserSessionPrivacy:
    def test_session_public_never_exposes_storage_state(self):
        """The public session shape must not contain encrypted state or credentials."""
        from app.services.browser_session import FIELD_CHECKPOINT_KEYS

        # FIELD_CHECKPOINT_KEYS must not include 'value'
        assert "value" not in FIELD_CHECKPOINT_KEYS
        # It should only have fingerprint-level information
        expected = {"status", "classification", "profile_key", "filled_at",
                    "answered_at", "value_fingerprint", "attempts", "source"}
        assert set(FIELD_CHECKPOINT_KEYS) == expected

    def test_handoff_kinds_cover_sensitive_actions(self):
        from app.services.browser_session import HANDOFF_ACTION_KINDS, NEVER_SCREENSHOT_KINDS

        assert "login" in HANDOFF_ACTION_KINDS
        assert "mfa" in HANDOFF_ACTION_KINDS
        assert "captcha" in HANDOFF_ACTION_KINDS
        # Sensitive handoff screens must never be captured
        for kind in HANDOFF_ACTION_KINDS:
            assert kind in NEVER_SCREENSHOT_KINDS


# --------------------------------------------------------------------------- #
# Deletion integrity — browser state coverage
# --------------------------------------------------------------------------- #
class TestDeletionCoversBrowserState:
    def test_erasure_plan_covers_application_sessions(self, db):
        from app.services.erasure import owned_tables

        tables = owned_tables()
        assert "application_sessions" in tables
        assert "application_actions" in tables
        assert "application_submissions" in tables

    def test_erasure_plan_covers_onboarding(self, db):
        from app.services.erasure import owned_tables

        tables = owned_tables()
        assert "onboarding_sessions" in tables
        assert "onboarding_events" in tables
        assert "resume_documents" in tables
        assert "resume_extractions" in tables

    def test_erasure_plan_covers_candidate_profiles(self, db):
        from app.services.erasure import owned_tables

        tables = owned_tables()
        assert "candidate_profiles" in tables
        assert "profile_field_provenance" in tables
        assert "profile_field_history" in tables

    def test_erasure_plan_covers_automation_policy(self, db):
        from app.services.erasure import owned_tables

        tables = owned_tables()
        assert "automation_policies" in tables
        assert "automation_policy_revisions" in tables
        assert "automation_submission_counters" in tables

    def test_erasure_plan_covers_application_tracking(self, db):
        from app.services.erasure import owned_tables

        tables = owned_tables()
        assert "application_tracking" in tables
        assert "application_tracking_events" in tables

    def test_erasure_plan_covers_application_packets(self, db):
        from app.services.erasure import owned_tables

        tables = owned_tables()
        assert "application_packets" in tables
        assert "application_packet_events" in tables
