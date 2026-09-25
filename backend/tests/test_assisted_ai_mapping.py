"""Tests for AI-assisted field mapping during live assisted passes.

Covers:
* ``ai_map_unknown_fields`` filters obvious/secret fields out of the prompt,
  rejects unknown keys, accepts profile_key extras, and never maps a field
  back to a secret/credential key even if the AI misbehaves.
* ``apply_ai_hints_to_observation`` stamps ``ai_mapped_key`` on hinted fields.
* Classifier picks up ``ai_mapped_key`` as a candidate and treats it like a
  declared hint for clean fields, but *still* refuses to fill secrets/MFA/legal
  fields even if the AI mislabels them.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from app.services import assisted_fill as af
from app.services.field_classifier import candidate_matches, classify_field


def _ensure_ai_shims(monkeypatch):
    import app.services.ai_client as ai_client_mod
    if not hasattr(ai_client_mod, "fit_prompt_part"):
        monkeypatch.setattr(ai_client_mod, "fit_prompt_part",
                            lambda s, _b, label=None: (s, True), raising=False)
    if not hasattr(ai_client_mod, "input_budget_chars"):
        monkeypatch.setattr(ai_client_mod, "input_budget_chars", lambda: 8000, raising=False)


def _fake_chat(monkeypatch, response):
    import app.services.ai_client as ai_client_mod
    async def fake(_kind, _prompt, **_kw):
        return response
    monkeypatch.setattr(ai_client_mod, "chat_completion", fake, raising=False)
    _ensure_ai_shims(monkeypatch)


# --------------------------------------------------------------------------- #
# Budget filter
# --------------------------------------------------------------------------- #

def test_looks_obvious_skips_standard_fields():
    assert af._looks_obvious("firstName", "First Name") is True
    assert af._looks_obvious("emailAddress", "Email Address") is True
    assert af._looks_obvious("qX", "Have you ever been convicted of a felony?") is False


# --------------------------------------------------------------------------- #
# ai_map_unknown_fields
# --------------------------------------------------------------------------- #

def test_ai_maps_only_nonobvious_nonsecret_fields(monkeypatch):
    sent: Dict[str, str] = {}

    import app.services.ai_client as ai_client_mod

    async def chat(kind, prompt, **kw):
        sent["kind"] = kind
        sent["prompt"] = prompt
        # Misbehaving AI tries to map a password field too — must be ignored.
        return {"mapping": {"q1": "firstName", "password": "firstName"}}

    monkeypatch.setattr(ai_client_mod, "chat_completion", chat, raising=False)
    _ensure_ai_shims(monkeypatch)

    obs = {"fields": [
        {"name": "firstName", "label": "First Name", "type": "text"},       # obvious -> skipped
        {"name": "password", "label": "Password", "type": "password"},      # secret  -> skipped
        {"name": "q1", "label": "How should we address you?", "type": "text"},
        {"name": "no_label", "type": "text"},                               # no label -> skipped
    ]}
    out = asyncio.run(af.ai_map_unknown_fields(obs, profile_keys=[], profile_sample={}))
    assert out == {"q1": "firstName"}
    # Only q1 was sent inside the Fields payload (firstName appearing in the
    # allowed-keys list is fine — that's the vocabulary, not a field).
    fields_section = sent["prompt"].split("Fields:", 1)[1]
    assert '"name": "q1"' in fields_section
    assert '"name": "firstName"' not in fields_section
    assert '"name": "password"' not in fields_section
    assert "How should we address you" in fields_section
    assert sent["kind"] == "form_detect_live"


def test_ai_rejects_keys_outside_allowed(monkeypatch):
    _fake_chat(monkeypatch, {"mapping": {"q1": "not_a_real_key"}})
    obs = {"fields": [{"name": "q1", "label": "Weird one", "type": "text"}]}
    assert asyncio.run(af.ai_map_unknown_fields(obs, profile_keys=[], profile_sample={})) == {}


def test_ai_accepts_extra_profile_keys(monkeypatch):
    _fake_chat(monkeypatch, {"mapping": {"q1": "custom_q_1"}})
    obs = {"fields": [{"name": "q1", "label": "Weird one", "type": "text"}]}
    out = asyncio.run(af.ai_map_unknown_fields(
        obs, profile_keys=["custom_q_1"], profile_sample={}))
    assert out == {"q1": "custom_q_1"}


def test_ai_rejects_hallucinated_field_names(monkeypatch):
    """If the AI returns a name we didn't send it, drop it."""
    _fake_chat(monkeypatch, {"mapping": {"nonexistent": "firstName",
                                         "q1": "firstName"}})
    obs = {"fields": [{"name": "q1", "label": "How should we address you?", "type": "text"}]}
    out = asyncio.run(af.ai_map_unknown_fields(obs, profile_keys=[], profile_sample={}))
    assert out == {"q1": "firstName"}


def test_ai_refuses_to_map_a_field_to_a_secret_key(monkeypatch):
    """Even if the AI returns {"q1": "password"}, we refuse — passwords only
    flow via the vault-credential path, never through an AI suggestion."""
    _fake_chat(monkeypatch, {"mapping": {"q1": "password"}})
    obs = {"fields": [{"name": "q1", "label": "Account password", "type": "text"}]}
    out = asyncio.run(af.ai_map_unknown_fields(obs, profile_keys=[], profile_sample={}))
    assert out == {}


def test_ai_returns_empty_when_all_fields_filtered(monkeypatch):
    """If every field is obvious or secret, we don't even call the AI."""
    import app.services.ai_client as ai_client_mod
    called = {"n": 0}

    async def chat(*a, **kw):
        called["n"] += 1
        return {"mapping": {}}

    monkeypatch.setattr(ai_client_mod, "chat_completion", chat, raising=False)
    _ensure_ai_shims(monkeypatch)
    obs = {"fields": [
        {"name": "firstName", "label": "First Name", "type": "text"},
        {"name": "password", "type": "password", "label": "Password"},
    ]}
    assert asyncio.run(af.ai_map_unknown_fields(obs, profile_keys=[], profile_sample={})) == {}
    assert called["n"] == 0


def test_ai_survives_malformed_ai_response(monkeypatch):
    _fake_chat(monkeypatch, "not a json object")
    obs = {"fields": [{"name": "q1", "label": "How should we address you?", "type": "text"}]}
    assert asyncio.run(af.ai_map_unknown_fields(obs, profile_keys=[], profile_sample={})) == {}


# --------------------------------------------------------------------------- #
# apply_ai_hints_to_observation
# --------------------------------------------------------------------------- #

def test_apply_ai_hints_stamps_fields():
    obs = {"fields": [
        {"name": "q1", "label": "x", "type": "text"},
        {"name": "q2", "label": "y", "type": "text"},
    ]}
    out = af.apply_ai_hints_to_observation(obs, {"q1": "firstName"})
    assert out is not obs
    assert out["fields"][0]["ai_mapped_key"] == "firstName"
    assert "ai_mapped_key" not in out["fields"][1]


def test_apply_ai_hints_preserves_non_field_keys():
    obs = {"url": "https://example.com", "fields": []}
    out = af.apply_ai_hints_to_observation(obs, {})
    assert out["url"] == "https://example.com"


# --------------------------------------------------------------------------- #
# Classifier integration
# --------------------------------------------------------------------------- #

def test_classifier_surfaces_ai_hint_as_candidate():
    field = {"name": "q1", "label": "How should we address you?", "type": "text",
             "ai_mapped_key": "firstName"}
    matches = candidate_matches(field)
    assert any(level == "ai_mapped" and key == "firstName" for key, level in matches)


def test_classifier_autofills_clean_field_with_ai_hint():
    field = {"name": "q1", "label": "How should we address you?", "type": "text",
             "ai_mapped_key": "firstName"}
    v = classify_field(field, value="Alice")
    assert v.profile_key == "firstName"
    assert v.classification in ("fill", "fill_required", "canonical")
    assert v.action in ("fill", "fill_required", "autofill")


def test_classifier_refuses_password_despite_ai_hint():
    """Password inputs must be gated through the credential path even if the
    AI mislabels them as a profile field."""
    field = {"name": "pwd", "label": "Password", "type": "password",
             "ai_mapped_key": "firstName"}
    v = classify_field(field, value="Alice")
    assert v.classification not in ("fill", "fill_required", "answered", "canonical")
    assert v.action not in ("fill", "fill_required", "autofill")


def test_classifier_refuses_mfa_despite_ai_hint():
    field = {"name": "mfa", "label": "Verification code", "type": "text",
             "ai_mapped_key": "firstName"}
    v = classify_field(field, value="Alice")
    assert v.action not in ("fill", "fill_required", "autofill")
    assert v.classification not in ("fill", "fill_required", "canonical")


def test_classifier_refuses_legal_signature_despite_ai_hint():
    field = {"name": "sig", "label": "Type your legal signature", "type": "text",
             "signature": True, "ai_mapped_key": "firstName"}
    v = classify_field(field, value="Alice")
    assert v.action not in ("fill", "fill_required", "autofill")
    assert v.classification not in ("fill", "fill_required", "canonical")
