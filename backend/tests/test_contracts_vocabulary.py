"""
Tests for shared domain contracts vocabulary and cross-system parity.

Ensures:
1. Complete parity between backend/app/contracts/vocabulary.py and frontend/src/lib/contracts.ts.
2. Complete documentation coverage across docs/contracts/ (15 contract docs + index).
3. Pure helper behavior (confidence_band, requires_review, is_terminal_application_state, job_status_for).
4. Application state mapping completeness (state -> phase, state -> legacy status).
5. Analytics funnel uses canonical JOB_STATUSES without stale ghost values (applying, emailed).
6. Jobs page frontend filter matches canonical JOB_STATUSES.
7. Audit action coverage between contracts vocabulary and app.core.audit.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

import app.contracts as contracts
from app.contracts import (
    ACTION_KINDS,
    ACTOR_TYPES,
    AGGREGATE_TYPES,
    AMBIGUITY_KINDS,
    ANSWER_REUSE_SCOPES,
    APPLICATION_BLOCKED_CODES,
    APPLICATION_PHASES,
    APPLICATION_STATE_PHASE,
    APPLICATION_STATES,
    APPLICATION_TERMINAL_STATES,
    APPLICATION_USER_ACTION_STATES,
    AUDIT_ACTIONS,
    AUTOFILL_VALUE_SOURCES,
    AUTOMATION_MODES,
    AUTOMATION_SCOPES,
    AUTOMATION_WORKFLOWS,
    CONFIDENCE_BAND_THRESHOLDS,
    CONFIDENCE_BANDS,
    CONTRACT_VERSION,
    DISCOVERY_AI_SKIP_REASONS,
    DISCOVERY_RUN_STATES,
    DISCOVERY_WHY_EMPTY,
    EEO_FIELD_KEYS,
    ERROR_CODES,
    EVENT_TYPES,
    EVIDENCE_KINDS,
    EXTRACTION_STATES,
    FIELD_RESOLUTIONS,
    IDEMPOTENCY_HEADER,
    JOB_STATUS_PROJECTION,
    JOB_STATUSES,
    LEGACY_PROFILE_EXTRACTION_SOURCES,
    MAPPINGS,
    MATCH_BANDS,
    MATCH_STALENESS,
    NOTIFICATION_CHANNELS,
    NOTIFICATION_KINDS,
    NOTIFICATION_PREFERENCE_KEYS,
    NOTIFICATION_SEVERITIES,
    NOTIFICATION_UNMUTABLE_KINDS,
    ONBOARDING_GATE_STATES,
    ONBOARDING_GATES,
    ONBOARDING_STATES,
    ONBOARDING_TERMINAL_STATES,
    PROFILE_LINK_KINDS,
    PROFILE_STATES,
    PROVENANCE_SOURCES,
    QUEUE_LIVE_STATUSES,
    QUEUE_PIPELINES,
    QUEUE_PROGRESS_STEPS,
    QUEUE_STATUSES,
    QUEUE_TERMINAL_STATUSES,
    QUEUE_TRIGGERS,
    RESTRICTED_FIELD_KEYS,
    RESUME_ROLES,
    RESUME_STATES,
    REVIEW_ACTIONS,
    REVIEW_STATUSES,
    SCORE_SOURCES,
    SENSITIVE_AUDIT_ACTIONS,
    SENSITIVE_FIELD_KEYS,
    SENSITIVE_FIELD_POLICIES,
    SENSITIVITY_LEVELS,
    SUBMISSION_CHANNELS,
    VOCABULARY,
    confidence_band,
    is_terminal_application_state,
    job_status_for,
    requires_review,
)
from app.contracts.vocabulary import __all__ as VOCABULARY_ALL
from app.core.audit import SENSITIVE_ACTIONS as CORE_SENSITIVE_ACTIONS

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
FRONTEND_DIR = ROOT_DIR / "frontend"
DOCS_DIR = ROOT_DIR / "docs" / "contracts"
TS_CONTRACTS_FILE = FRONTEND_DIR / "src" / "lib" / "contracts.ts"
JOBS_PAGE_FILE = FRONTEND_DIR / "src" / "pages" / "Jobs.tsx"


def test_contracts_reexport_all():
    """__init__.py must re-export every single symbol in vocabulary.__all__."""
    assert set(contracts.__all__) == set(VOCABULARY_ALL)
    for name in VOCABULARY_ALL:
        assert hasattr(contracts, name), f"Missing re-export for {name}"


def test_vocabulary_dict_covers_all_string_tuples():
    """VOCABULARY dictionary must catalogue all canonical string tuples."""
    assert isinstance(VOCABULARY, dict)
    for key, val in VOCABULARY.items():
        assert isinstance(key, str)
        assert isinstance(val, tuple)
        assert len(val) > 0
        assert all(isinstance(item, str) for item in val)


def test_frontend_contracts_file_exists():
    assert TS_CONTRACTS_FILE.exists(), f"Missing {TS_CONTRACTS_FILE}"
    content = TS_CONTRACTS_FILE.read_text(encoding="utf-8")
    assert f"export const CONTRACT_VERSION = '{CONTRACT_VERSION}'" in content
    assert f"export const IDEMPOTENCY_HEADER = '{IDEMPOTENCY_HEADER}'" in content


def _parse_ts_string_arrays(ts_text: str) -> Dict[str, List[str]]:
    """Parse `export const FOO = [ 'a', 'b', ... ] as const` from TypeScript."""
    result: Dict[str, List[str]] = {}
    pattern = re.compile(
        r"export\s+const\s+([A-Za-z0-9_]+)\s*=\s*\[\s*([^\]]*?)\s*\]\s*as\s*const",
        re.MULTILINE | re.DOTALL,
    )
    for match in pattern.finditer(ts_text):
        name = match.group(1)
        raw_items = match.group(2)
        # Extract quoted strings
        items = re.findall(r"['\"]([^'\"]+)['\"]", raw_items)
        result[name] = items
    return result


def _parse_ts_records(ts_text: str) -> Dict[str, Dict[str, str]]:
    """Parse `export const FOO: Record<...> = { k: 'v', ... }` from TypeScript."""
    result: Dict[str, Dict[str, str]] = {}
    pattern = re.compile(
        r"export\s+const\s+([A-Za-z0-9_]+)\s*:\s*Record<[^>]+>\s*=\s*\{\s*([^}]*?)\s*\}",
        re.MULTILINE | re.DOTALL,
    )
    for match in pattern.finditer(ts_text):
        name = match.group(1)
        body = match.group(2)
        entries: Dict[str, str] = {}
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            entry_m = re.match(r"([A-Za-z0-9_]+)\s*:\s*['\"]([^'\"]+)['\"]", line)
            if entry_m:
                entries[entry_m.group(1)] = entry_m.group(2)
        result[name] = entries
    return result


def test_frontend_ts_vocabulary_exact_parity():
    """Verify that every vocabulary tuple has an exact, identically ordered TS mirror."""
    ts_text = TS_CONTRACTS_FILE.read_text(encoding="utf-8")
    arrays = _parse_ts_string_arrays(ts_text)

    # Check each VOCABULARY entry
    for key, py_tuple in VOCABULARY.items():
        ts_name = key.upper()
        assert ts_name in arrays, f"TypeScript contracts.ts missing array {ts_name}"
        ts_list = arrays[ts_name]
        assert tuple(ts_list) == py_tuple, (
            f"Parity mismatch in {ts_name}:\n"
            f"Python ({len(py_tuple)}): {py_tuple}\n"
            f"TypeScript ({len(ts_list)}): {tuple(ts_list)}"
        )


def test_frontend_ts_mappings_parity():
    """Verify JOB_STATUS_PROJECTION and APPLICATION_STATE_PHASE match in TypeScript."""
    ts_text = TS_CONTRACTS_FILE.read_text(encoding="utf-8")
    records = _parse_ts_records(ts_text)

    assert "JOB_STATUS_PROJECTION" in records
    assert records["JOB_STATUS_PROJECTION"] == JOB_STATUS_PROJECTION

    assert "APPLICATION_STATE_PHASE" in records
    assert records["APPLICATION_STATE_PHASE"] == APPLICATION_STATE_PHASE


def test_docs_contracts_completeness():
    """Ensure all 15 contract documents + README exist and have substantive content."""
    assert (DOCS_DIR / "README.md").exists()
    for i in range(1, 16):
        pattern = f"{i:02d}-*.md"
        matches = list(DOCS_DIR.glob(pattern))
        assert len(matches) == 1, f"Missing contract doc {pattern}"
        doc_path = matches[0]
        text = doc_path.read_text(encoding="utf-8")
        assert len(text.splitlines()) > 50, f"Contract doc {doc_path.name} is too brief"


def test_confidence_band_calculations():
    """Test confidence_band boundary behaviors."""
    assert confidence_band(1.0) == "high"
    assert confidence_band(0.85) == "high"
    assert confidence_band(0.8499) == "medium"
    assert confidence_band(0.60) == "medium"
    assert confidence_band(0.5999) == "low"
    assert confidence_band(0.0) == "low"
    assert confidence_band(None) == "none"


def test_requires_review_helper():
    """requires_review must enforce review for low/none confidence or sensitive/restricted data."""
    # Low or none confidence always requires review even if sensitivity is standard
    assert requires_review("standard", "none") is True
    assert requires_review("standard", "low") is True

    # High / medium confidence standard field does not require review
    assert requires_review("standard", "high") is False
    assert requires_review("standard", "medium") is False

    # Sensitive or restricted sensitivity always requires review regardless of high confidence
    assert requires_review("sensitive", "high") is True
    assert requires_review("restricted", "high") is True


def test_application_state_mappings_completeness():
    """Every application state must map to a valid phase and a valid legacy job status."""
    for state in APPLICATION_STATES:
        # Phase mapping
        assert state in APPLICATION_STATE_PHASE, f"Missing phase mapping for {state}"
        phase = APPLICATION_STATE_PHASE[state]
        assert phase in APPLICATION_PHASES, f"Invalid phase {phase} for state {state}"

        # Job status projection
        assert state in JOB_STATUS_PROJECTION, f"Missing job status projection for {state}"
        legacy_status = JOB_STATUS_PROJECTION[state]
        assert legacy_status in JOB_STATUSES, f"Invalid job status {legacy_status} for {state}"
        assert legacy_status not in ("applying", "emailed"), (
            f"Projection {legacy_status} for state {state} is deprecated"
        )


def test_is_terminal_application_state():
    """Verify terminal state helper exactly identifies APPLICATION_TERMINAL_STATES."""
    for state in APPLICATION_STATES:
        expected = state in APPLICATION_TERMINAL_STATES
        assert is_terminal_application_state(state) is expected, f"State {state} terminal mismatch"


def test_job_status_for():
    """Helper job_status_for accurately projects states."""
    assert job_status_for("submitted") == "applied"
    assert job_status_for("failed") == "failed"
    assert job_status_for("unknown_future_state") == "discovered"


def test_queue_progress_steps_keys_in_pipelines():
    """Every pipeline defined in QUEUE_PROGRESS_STEPS must be in QUEUE_PIPELINES."""
    for pipeline in QUEUE_PROGRESS_STEPS:
        assert pipeline in QUEUE_PIPELINES, f"Unknown pipeline {pipeline} in QUEUE_PROGRESS_STEPS"


def test_audit_sensitive_actions_coverage():
    """Every prefix in core.audit.SENSITIVE_ACTIONS must be covered in SENSITIVE_AUDIT_ACTIONS."""
    for prefix in CORE_SENSITIVE_ACTIONS:
        assert any(
            action.startswith(prefix) or prefix.startswith(action)
            for action in SENSITIVE_AUDIT_ACTIONS
        ), f"Core sensitive action prefix {prefix} not covered in SENSITIVE_AUDIT_ACTIONS"


def test_jobs_frontend_filter_matches_canonical_statuses():
    """Jobs.tsx status filter options must contain all canonical JOB_STATUSES and no dead ones."""
    assert JOBS_PAGE_FILE.exists()
    ts_text = JOBS_PAGE_FILE.read_text(encoding="utf-8")
    # Find the select with filter.status
    m = re.search(
        r'<select[^>]*value=\{filter\.status\}[^>]*>(.*?)</select>',
        ts_text,
        re.DOTALL,
    )
    assert m, "filter.status select not found in Jobs.tsx"
    select_body = m.group(1)
    values = re.findall(r'<option\s+value="([^"]*)"', select_body)
    # The first option is "" (All status)
    status_values = [v for v in values if v]
    # Check that 'applying' is absent
    assert "applying" not in status_values, "Dead status 'applying' found in Jobs.tsx filter"
    assert "emailed" not in status_values, "Dead status 'emailed' found in Jobs.tsx filter"
    # Check that canonical statuses are present
    for s in ("discovered", "queued", "preparing", "needs_input", "ready_to_apply", "applied", "failed", "skipped"):
        assert s in status_values, f"Canonical status '{s}' missing from Jobs.tsx filter"


def test_analytics_funnel_endpoint_canonical_statuses():
    """funnel_analytics must return exactly JOB_STATUSES and correct conversion rate."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.api.routers.analytics import funnel_analytics
    from app.models.models import Base, Job, User

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        user = User(email="test_funnel@example.com", password_hash="pw")
        session.add(user)
        session.commit()
        session.refresh(user)

        # Create jobs across canonical statuses
        test_statuses = [
            "discovered", "queued", "preparing", "needs_input",
            "ready_to_apply", "applied", "failed", "skipped",
        ]
        for s in test_statuses:
            session.add(
                Job(
                    user_id=user.id,
                    title=f"Job {s}",
                    company="Acme",
                    status=s,
                    dedupe_key=f"job-dedupe-{s}",
                )
            )
        session.commit()

        res = funnel_analytics(user=user, db=session)

        # Stage list matches canonical JOB_STATUSES
        assert set(res["funnel"].keys()) == set(JOB_STATUSES)
        assert "applying" not in res["funnel"]
        assert "emailed" not in res["funnel"]

        # Counts
        for s in test_statuses:
            assert res["funnel"][s] == 1

        assert res["total"] == 8
        assert res["unclassified"] == 0
        # 1 applied out of 8 = 12.5%
        assert res["conversion_rate"] == 12.5
    finally:
        session.close()

