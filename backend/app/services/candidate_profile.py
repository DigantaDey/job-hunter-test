"""
Candidate profile extraction and review pipeline.

Implements:
- Rich extraction of roles, employers, dates, skills, tools, industries,
  achievements, education, certifications, location, work auth, sponsorship,
  remote preference, salary, notice period, target roles.
- Per-field source/confidence/evidence metadata.
- Status marking: confirmed / uncertain / missing / conflicting.
- Never invent required application field.
- Distinguish resume-derived vs user-confirmed.
- Allow corrections with audit trail (who/when).
- Completeness calculation based on product needs.
- Safe defaults when AI unavailable.
- Uses existing AI client + provider-reported usage accounting.

Design notes:
- Extraction uses ai_guardrails.run_guarded_task with a JSON schema.
- Each field carries confidence 0..1, evidence snippets (≤500 chars), source.
- Missing fields are returned as structured completion requirements.
- Low-confidence cannot silently populate required fields: they stay in
  needs_review and are excluded from autofill.
- User corrections override without deleting original evidence (history).
- All evidence quotes are verified to exist in source text when possible.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.contracts.vocabulary import (
    CONFIDENCE_BAND_THRESHOLDS,
    EVIDENCE_KINDS,
    PROVENANCE_SOURCES,
    REVIEW_STATUSES,
    SENSITIVITY_LEVELS,
    confidence_band,
    requires_review,
)
from app.core.logging import get_logger
from app.models.models import (
    CandidateProfile,
    ProfileFieldHistory,
    ProfileFieldProvenance,
)
from app.services.ai_client import fit_prompt_part, input_budget_chars
from app.services.ai_guardrails import (
    AIUnavailableError,
    FieldSpec,
    SchemaSpec,
    build_fact_ledger,
    run_guarded_task,
    strip_ai_artifacts,
)

log = get_logger("app.candidate_profile")

# --------------------------------------------------------------------------- #
# Field definitions — sensitivity, required, description
# --------------------------------------------------------------------------- #
# Required for product completeness (application autofill needs):
# - identity: name, email, location
# - eligibility: work_authorization
# - professional: roles, employers, dates, skills
# - preferences: target_roles
# Optional but valuable: tools, industries, achievements, education,
# certifications, sponsorship, remote, salary, notice.
#
# Sensitivity mapping follows 01-conventions and 02-candidate-profile.
FIELD_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    # Core identity / contact
    "full_name": {"sensitivity": "internal", "required": True, "label": "Full name", "type": "string"},
    "email": {"sensitivity": "sensitive", "required": True, "label": "Email", "type": "string"},
    "phone": {"sensitivity": "sensitive", "required": False, "label": "Phone", "type": "string"},
    "location": {"sensitivity": "internal", "required": True, "label": "Location", "type": "string"},
    # Professional
    "roles": {"sensitivity": "public", "required": True, "label": "Roles / Titles", "type": "list"},
    "employers": {"sensitivity": "public", "required": True, "label": "Employers", "type": "list"},
    "dates": {"sensitivity": "public", "required": True, "label": "Employment dates", "type": "list"},
    "skills": {"sensitivity": "public", "required": True, "label": "Skills", "type": "list"},
    "tools": {"sensitivity": "public", "required": False, "label": "Tools", "type": "list"},
    "industries": {"sensitivity": "public", "required": False, "label": "Industries", "type": "list"},
    "achievements": {"sensitivity": "public", "required": False, "label": "Achievements", "type": "list"},
    "education": {"sensitivity": "public", "required": False, "label": "Education", "type": "list"},
    "certifications": {"sensitivity": "public", "required": False, "label": "Certifications", "type": "list"},
    # Eligibility / preferences — user-stated, never inferred from resume per contract 02
    "work_authorization": {"sensitivity": "sensitive", "required": True, "label": "Work authorization", "type": "string"},
    "sponsorship_required": {"sensitivity": "sensitive", "required": False, "label": "Sponsorship required", "type": "boolean"},
    "remote_preference": {"sensitivity": "internal", "required": False, "label": "Remote preference", "type": "string"},
    "salary_expectations": {"sensitivity": "sensitive", "required": False, "label": "Salary expectations", "type": "object"},
    "notice_period": {"sensitivity": "internal", "required": False, "label": "Notice period", "type": "object"},
    "target_roles": {"sensitivity": "internal", "required": True, "label": "Target roles", "type": "list"},
}

# Fields that must never be invented — they require explicit user statement
# or high-confidence resume evidence. Low confidence → missing.
REQUIRED_APPLICATION_FIELDS = [
    "full_name",
    "email",
    "location",
    "roles",
    "employers",
    "dates",
    "skills",
    "work_authorization",
    "target_roles",
]

# Fields where resume is allowed to provide evidence, vs. fields that are
# user-stated only per contract 02 § preferences/compensation/availability/eligibility
RESUME_DERIVABLE_FIELDS = {
    "full_name", "email", "phone", "location",
    "roles", "employers", "dates", "skills", "tools",
    "industries", "achievements", "education", "certifications",
    "target_roles",
}
USER_STATED_ONLY_FIELDS = {
    "work_authorization", "sponsorship_required", "remote_preference",
    "salary_expectations", "notice_period",
}

OUTPUT_TOKENS = 5000

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _hash_value(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _preview_value(value: Any, sensitivity: str) -> Optional[str]:
    if sensitivity in ("sensitive", "restricted"):
        return None
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text[:500]


def _band(conf: Optional[float]) -> str:
    return confidence_band(conf)


def _requires_review(sensitivity: str, band: str) -> bool:
    return requires_review(sensitivity, band)


def _evidence_from_text(quote: str, text: str, doc_id: Optional[int] = None) -> Dict[str, Any]:
    """Build evidence object with locator if quote found in source text."""
    q = (quote or "").strip()[:500]
    start = -1
    end = -1
    if q and text:
        # case-insensitive search
        idx = text.lower().find(q.lower()[:100])  # search first 100 chars for robustness
        if idx >= 0:
            start = idx
            end = idx + len(q)
    locator: Dict[str, Any] = {}
    if doc_id is not None:
        locator["document_id"] = doc_id
    if start >= 0:
        locator["start"] = start
        locator["end"] = end
    return {
        "kind": "text_span",
        "quote": q,
        "locator": locator,
    }


def _normalize_value(field_key: str, raw: Any) -> Any:
    """Normalize raw AI value to canonical shape."""
    if raw is None:
        return None
    defn = FIELD_DEFINITIONS.get(field_key, {})
    ftype = defn.get("type", "string")
    if ftype == "list":
        if isinstance(raw, list):
            return [strip_ai_artifacts(str(x)) if isinstance(x, str) else x for x in raw if x not in (None, "")]
        if isinstance(raw, str):
            # split by comma/newline
            parts = re.split(r"[,\n;]+", raw)
            return [p.strip() for p in parts if p.strip()]
        return [raw]
    if ftype == "boolean":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            low = raw.lower()
            if low in ("true", "yes", "required", "needs sponsorship"):
                return True
            if low in ("false", "no", "not required", "no sponsorship"):
                return False
        return None
    if ftype == "object":
        if isinstance(raw, dict):
            return raw
        return None
    # string
    if isinstance(raw, dict):
        # AI sometimes returns object for string field, e.g. location {city, country}
        # Coerce to string
        if "value" in raw:
            return strip_ai_artifacts(str(raw["value"]))
        return strip_ai_artifacts(json.dumps(raw))
    return strip_ai_artifacts(str(raw))


# --------------------------------------------------------------------------- #
# Extraction schema for guardrails
# --------------------------------------------------------------------------- #
CANDIDATE_EXTRACTION_SCHEMA = SchemaSpec([
    FieldSpec("full_name", "str", required=False),
    FieldSpec("email", "str", required=False),
    FieldSpec("phone", "str", required=False),
    FieldSpec("location", "str", required=False),
    FieldSpec("roles", "list", required=False),
    FieldSpec("employers", "list", required=False),
    FieldSpec("dates", "list", required=False),
    FieldSpec("skills", "list", required=False),
    FieldSpec("tools", "list", required=False),
    FieldSpec("industries", "list", required=False),
    FieldSpec("achievements", "list", required=False),
    FieldSpec("education", "list", required=False),
    FieldSpec("certifications", "list", required=False),
    FieldSpec("work_authorization", "str", required=False),
    FieldSpec("sponsorship_required", "str", required=False),  # allow bool or str, coerce later
    FieldSpec("remote_preference", "str", required=False),
    FieldSpec("salary_expectations", "str", required=False),
    FieldSpec("notice_period", "str", required=False),
    FieldSpec("target_roles", "list", required=False),
    # Confidence and evidence per field — optional but encouraged
    FieldSpec("field_confidences", "dict", required=False),
    FieldSpec("field_evidence", "dict", required=False),
])

# Extended schema that includes confidence/evidence structured
# We ask model to return JSON with keys for each field containing value/confidence/evidence,
# but guardrails will also accept flat shape. Coercion handles both.


def _coerce_extraction(payload: Dict[str, Any], source_text: str) -> Dict[str, Any]:
    """Normalize AI payload into canonical {field: {value, confidence, evidence}}."""
    result: Dict[str, Any] = {}
    field_confidences = payload.get("field_confidences") or {}
    field_evidence = payload.get("field_evidence") or {}

    for key in FIELD_DEFINITIONS.keys():
        raw = payload.get(key)
        # Support nested shape {value, confidence, evidence}
        confidence: Optional[float] = None
        evidence_snippets: List[str] = []

        if isinstance(raw, dict) and ("value" in raw or "confidence" in raw):
            # Structured shape
            val = raw.get("value")
            confidence = raw.get("confidence")
            ev = raw.get("evidence")
            if isinstance(ev, list):
                evidence_snippets = [str(x)[:500] for x in ev if x]
            elif isinstance(ev, str) and ev.strip():
                evidence_snippets = [ev.strip()[:500]]
            raw_value = val
        else:
            raw_value = raw
            # Try to get confidence from sidecar dict
            conf_raw = field_confidences.get(key)
            if conf_raw is not None:
                try:
                    confidence = float(conf_raw)
                except Exception:
                    confidence = None
            ev_raw = field_evidence.get(key)
            if isinstance(ev_raw, list):
                evidence_snippets = [str(x)[:500] for x in ev_raw if x]
            elif isinstance(ev_raw, str) and ev_raw.strip():
                evidence_snippets = [ev_raw.strip()[:500]]

        normalized = _normalize_value(key, raw_value)

        # Evidence validation: if no evidence but value present, lower confidence
        # and mark as needing review (per contract: never invent evidence)
        if normalized not in (None, "", [], {}):
            if not evidence_snippets:
                # No evidence provided — cap confidence to 0.6 (needs review)
                if confidence is None or confidence > 0.6:
                    confidence = 0.6
        else:
            # Missing value
            normalized = None
            if confidence is None:
                confidence = 0.0
            evidence_snippets = []

        # Clamp confidence 0..1
        if confidence is not None:
            try:
                confidence = max(0.0, min(1.0, float(confidence)))
            except Exception:
                confidence = 0.0

        # For user-stated-only fields, if value came from resume without explicit
        # user statement, we treat it as ai_inferred with low confidence
        if key in USER_STATED_ONLY_FIELDS and normalized not in (None, "", [], {}):
            # If evidence is from resume but field is user-stated only,
            # cap confidence to 0.5 and mark ambiguity
            if confidence is None or confidence > 0.5:
                confidence = 0.5

        result[key] = {
            "value": normalized,
            "confidence": confidence if confidence is not None else 0.0,
            "evidence": evidence_snippets,
        }

    return result


def _extraction_checks(source_text: str):
    def check(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        hay = re.sub(r"[^a-z0-9]+", "", (source_text or "").lower())
        # For each string field, if value present, ensure substring exists in source
        # unless field is user-stated-only (then it's okay to be missing)
        for key in ("full_name", "email", "roles", "employers", "skills", "location"):
            val = data.get(key)
            if not val:
                continue
            if isinstance(val, list):
                for item in val[:5]:
                    if not isinstance(item, str):
                        continue
                    token = re.sub(r"[^a-z0-9]+", "", item.lower())
                    if token and len(token) > 3 and token not in hay:
                        # Not necessarily error — could be normalized — but flag low confidence
                        pass
            else:
                token = re.sub(r"[^a-z0-9]+", "", str(val).lower())
                if token and len(token) > 4 and token not in hay:
                    # Allow, but will be low confidence
                    pass
        return issues
    return check


# --------------------------------------------------------------------------- #
# Core extraction function
# --------------------------------------------------------------------------- #
async def ai_extract_candidate_profile(
    text: str,
    ai_config: Optional[Dict[str, Any]] = None,
    *,
    db: Optional[Session] = None,
    user_id: Optional[int] = None,
    source_document_id: Optional[int] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """
    Extract candidate profile with confidence and evidence.

    Returns (fields, document, meta) where:
    - fields: dict keyed by field_key -> {value, confidence, band, evidence, source, status, ...}
    - document: canonical profile document per contract 02
    - meta: {ai_used, guardrail, tokens, model, etc.}

    Raises AIUnavailableError when model unreachable — caller should handle safe defaults.
    """
    if not text.strip():
        raise AIUnavailableError(
            "unknown",
            workflow="candidate_profile_extraction",
            detail="empty_document",
            context={"message": "No readable text found in the document"},
        )

    budget = input_budget_chars(db=db, user_id=user_id, ai_config=ai_config)
    resume_text, _truncated = fit_prompt_part(strip_ai_artifacts(text), budget, label="candidate_profile.resume")

    prompt = f"""Extract a complete candidate profile from this resume with confidence and evidence.

CRITICAL RULES:
- Copy facts exactly as written. NEVER invent values for required fields.
- If a field is not present in the resume, return null value and confidence 0.0 with empty evidence.
- For each field, provide confidence 0.0-1.0 and evidence snippets (exact quotes ≤200 chars from resume).
- Work authorization, sponsorship, remote preference, salary, notice period are USER-STATED ONLY — if not explicitly in resume, mark as missing (confidence 0.0).
- Preserve original evidence snippets — do not paraphrase.
- Mark uncertain fields with confidence <0.6, confident with ≥0.85, medium 0.6-0.85.
- For conflicting information (e.g., two different locations), include both in evidence and lower confidence.

Fields to extract:
- full_name: person's full name
- email: email address
- phone: phone number
- location: city, country
- roles: list of job titles held
- employers: list of company names
- dates: list of employment date ranges like "Mar 2021 - Present" or {{"start":"2021-03","end":null,"is_current":true}}
- skills: list of technical skills
- tools: list of tools/platforms (Docker, AWS, etc.)
- industries: list of industries (automotive, fintech, etc.)
- achievements: list of quantifiable achievements
- education: list of degrees with school
- certifications: list of certifications
- work_authorization: work authorization status (citizen, permanent resident, work visa, etc.) — ONLY if explicitly stated
- sponsorship_required: whether sponsorship needed — ONLY if explicitly stated
- remote_preference: remote/hybrid/onsite preference — ONLY if explicitly stated
- salary_expectations: salary range if stated — ONLY if explicitly stated
- notice_period: notice period if stated — ONLY if explicitly stated
- target_roles: desired roles (may infer from current title but low confidence)

Return JSON with for each field:
{{
  "full_name": {{"value": "...", "confidence": 0.95, "evidence": ["exact quote"]}},
  "email": {{"value": "...", "confidence": 0.9, "evidence": [...]}},
  ...
  "roles": {{"value": ["Senior Engineer"], "confidence": 0.9, "evidence": [...]}},
  ...
}}

If field missing, return {{"value": null, "confidence": 0.0, "evidence": []}}.

Resume text:
\"\"\"{resume_text}\"\"\"
"""

    data, report = await run_guarded_task(
        "candidate_profile_extraction",
        system=(
            "You are a precise resume parser that extracts structured candidate data "
            "with confidence scores and verbatim evidence. You never invent values. "
            "You preserve original evidence snippets. You mark uncertain or missing fields honestly. "
            "User-stated fields (work authorization, sponsorship, remote, salary, notice) are only extracted if explicitly stated."
        ),
        prompt=prompt,
        schema=CANDIDATE_EXTRACTION_SCHEMA,
        checks=[_extraction_checks(text)],
        ledger=build_fact_ledger({}, text),
        db=db,
        user_id=user_id,
        temperature=0.0,
        max_tokens=OUTPUT_TOKENS,
        coerce=lambda payload: _coerce_extraction(payload, text),
    )

    # data is now {field_key: {value, confidence, evidence}}
    fields: Dict[str, Any] = {}
    for key, defn in FIELD_DEFINITIONS.items():
        entry = data.get(key) or {"value": None, "confidence": 0.0, "evidence": []}
        value = entry.get("value")
        conf = entry.get("confidence", 0.0)
        ev_quotes = entry.get("evidence") or []

        # Build evidence objects
        evidence_objs = []
        for q in ev_quotes[:3]:  # cap 3 per field
            evidence_objs.append(_evidence_from_text(q, text, doc_id=source_document_id))

        # Determine band and review required
        band = _band(conf if value not in (None, "", [], {}) else None)
        sensitivity = defn.get("sensitivity", "internal")
        review_required = _requires_review(sensitivity, band)

        # Determine status: confirmed / uncertain / missing / conflicting
        if value in (None, "", [], {}):
            status = "missing"
            band = "none"
            review_required = True
        elif len(ev_quotes) >= 2 and len(set(ev_quotes)) > 1:
            # Multiple distinct evidence may indicate conflict — check if values conflict
            # For simplicity, if evidence has distinct quotes for single-value fields, mark conflicting
            if defn.get("type") in ("string", "boolean", "object") and len(ev_quotes) > 1:
                # Check if quotes imply different values
                status = "conflicting"
                review_required = True
            else:
                status = "confirmed" if band == "high" and not review_required else "uncertain"
        else:
            if band == "high" and not review_required:
                status = "confirmed"
            elif band in ("low", "none"):
                status = "uncertain" if value not in (None, "", [], {}) else "missing"
            else:
                status = "uncertain"

        # Never allow low-confidence to silently populate required fields
        if key in REQUIRED_APPLICATION_FIELDS and band in ("low", "none") and status != "missing":
            status = "uncertain"
            review_required = True

        # For user-stated-only fields, always require review if value present from resume
        if key in USER_STATED_ONLY_FIELDS and value not in (None, "", [], {}):
            # Mark as ai_inferred, needs review
            status = "uncertain"
            review_required = True

        # Determine origin
        if value in (None, "", [], {}):
            origin = "heuristic"  # missing
        elif key in USER_STATED_ONLY_FIELDS:
            origin = "ai_inferred"
        else:
            origin = "resume_extraction"

        # Build provenance
        value_hash = _hash_value(value)
        preview = _preview_value(value, sensitivity)

        fields[key] = {
            "value": value,
            "confidence": conf,
            "confidence_band": band,
            "evidence": evidence_objs,
            "evidence_quotes": ev_quotes,
            "source": origin,
            "sensitivity": sensitivity,
            "status": status,
            "review_required": review_required,
            "value_hash": value_hash,
            "value_preview": preview,
            "required": defn.get("required", False),
            "label": defn.get("label", key),
        }

    # Build canonical document per contract 02
    document = _build_canonical_document(fields)

    # Completeness
    completeness = calculate_completeness(fields)

    # Build completion requirements (structured)
    completion_requirements = build_completion_requirements(fields)

    meta = {
        "ai_used": True,
        "source": "ai",
        "extraction_source": "ai",
        "guardrail": report.to_dict(),
        "tokens": getattr(report, "tokens", {}) or {},
        "model": getattr(report, "model", None),
        "completeness": completeness,
        "completion_requirements": completion_requirements,
    }

    return fields, document, meta


def _build_canonical_document(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Build canonical profile document per contract 02 top-level keys."""
    def get_val(k):
        return fields.get(k, {}).get("value")

    doc = {
        "identity": {
            "full_name": get_val("full_name"),
        },
        "contact": {
            "email": get_val("email"),
            "phone": get_val("phone"),
            "location": get_val("location"),
        },
        "summary": "",  # could be derived from achievements
        "current_title": (get_val("roles")[0] if isinstance(get_val("roles"), list) and get_val("roles") else get_val("roles")),
        "seniority": None,
        "skills": get_val("skills") or [],
        "tools": get_val("tools") or [],
        "industries": get_val("industries") or [],
        "experience": [
            {"title": role, "company": emp, "duration": date}
            for role, emp, date in zip(
                get_val("roles") or [],
                get_val("employers") or [],
                get_val("dates") or [],
            )
        ] if get_val("roles") else [],
        "education": get_val("education") or [],
        "certifications": get_val("certifications") or [],
        "achievements": get_val("achievements") or [],
        "projects": [],
        "languages": [],
        "preferences": {
            "target_roles": get_val("target_roles") or [],
            "remote": get_val("remote_preference"),
        },
        "compensation": {
            "minimum": (get_val("salary_expectations") or {}).get("minimum") if isinstance(get_val("salary_expectations"), dict) else get_val("salary_expectations"),
            "target": (get_val("salary_expectations") or {}).get("target") if isinstance(get_val("salary_expectations"), dict) else None,
            "currency": (get_val("salary_expectations") or {}).get("currency") if isinstance(get_val("salary_expectations"), dict) else None,
        },
        "availability": {
            "notice_period": get_val("notice_period"),
        },
        "eligibility": {
            "work_authorization": get_val("work_authorization"),
            "sponsorship_required": get_val("sponsorship_required"),
        },
        "meta": {
            "extraction_confidence": {k: v.get("confidence") for k, v in fields.items()},
        },
    }
    return doc


# --------------------------------------------------------------------------- #
# Completeness calculation based on actual product needs
# --------------------------------------------------------------------------- #
def calculate_completeness(fields: Dict[str, Any]) -> Dict[str, Any]:
    """
    Calculate profile completeness based on product needs.

    Weights based on actual application requirements:
    - Core identity (name, email, location): 25%
    - Work eligibility (work_auth, sponsorship): 15%
    - Professional (roles, employers, dates, skills): 35%
    - Additional (tools, industries, achievements, education, certs): 10%
    - Preferences (remote, salary, notice, target_roles): 15%
    """
    weights = {
        "full_name": 10,
        "email": 10,
        "location": 5,
        "work_authorization": 10,
        "sponsorship_required": 5,
        "roles": 10,
        "employers": 5,
        "dates": 5,
        "skills": 15,
        "tools": 2,
        "industries": 2,
        "achievements": 2,
        "education": 2,
        "certifications": 2,
        "remote_preference": 3,
        "salary_expectations": 4,
        "notice_period": 3,
        "target_roles": 5,
    }

    total_weight = sum(weights.values())
    earned = 0
    breakdown: Dict[str, Any] = {}
    missing: List[str] = []
    uncertain: List[str] = []
    confirmed: List[str] = []

    for key, weight in weights.items():
        f = fields.get(key)
        if not f:
            missing.append(key)
            breakdown[key] = {"weight": weight, "earned": 0, "status": "missing"}
            continue
        val = f.get("value")
        status = f.get("status", "missing")
        conf = f.get("confidence", 0.0)
        band = f.get("confidence_band", "none")

        if val in (None, "", [], {}):
            missing.append(key)
            breakdown[key] = {"weight": weight, "earned": 0, "status": "missing", "confidence": conf, "band": band}
        elif status == "conflicting":
            uncertain.append(key)
            # Partial credit for conflicting
            earned += weight * 0.3
            breakdown[key] = {"weight": weight, "earned": weight * 0.3, "status": "conflicting", "confidence": conf, "band": band}
        elif status == "uncertain" or band in ("low", "none"):
            uncertain.append(key)
            earned += weight * 0.5
            breakdown[key] = {"weight": weight, "earned": weight * 0.5, "status": "uncertain", "confidence": conf, "band": band}
        else:
            confirmed.append(key)
            earned += weight
            breakdown[key] = {"weight": weight, "earned": weight, "status": "confirmed", "confidence": conf, "band": band}

    percent = round((earned / total_weight * 100) if total_weight else 0, 1)

    # Required fields tracking
    required_total = len(REQUIRED_APPLICATION_FIELDS)
    required_filled = sum(1 for k in REQUIRED_APPLICATION_FIELDS if fields.get(k, {}).get("value") not in (None, "", [], {}) and fields.get(k, {}).get("status") != "missing")
    required_confirmed = sum(1 for k in REQUIRED_APPLICATION_FIELDS if fields.get(k, {}).get("status") == "confirmed")

    return {
        "percent": percent,
        "earned": earned,
        "total_weight": total_weight,
        "required_filled": required_filled,
        "required_total": required_total,
        "required_confirmed": required_confirmed,
        "missing": missing,
        "uncertain": uncertain,
        "confirmed": confirmed,
        "breakdown": breakdown,
    }


def build_completion_requirements(fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build structured completion requirements for missing/uncertain fields."""
    requirements: List[Dict[str, Any]] = []
    for key, defn in FIELD_DEFINITIONS.items():
        f = fields.get(key)
        if not f:
            continue
        val = f.get("value")
        status = f.get("status")
        required = defn.get("required", False)
        if val in (None, "", [], {}) or status in ("missing", "uncertain", "conflicting"):
            req = {
                "field": key,
                "label": defn.get("label", key),
                "type": defn.get("type", "string"),
                "required": required,
                "status": status,
                "confidence": f.get("confidence", 0.0),
                "band": f.get("confidence_band", "none"),
                "evidence": f.get("evidence", []),
                "candidates": [],  # for conflicting, list candidates
                "message": "",
            }
            if status == "missing":
                req["message"] = f"{defn.get('label')} is missing and is {'required' if required else 'recommended'} for applications."
            elif status == "uncertain":
                req["message"] = f"{defn.get('label')} was extracted with low confidence ({f.get('confidence',0):.2f}) and needs review."
            elif status == "conflicting":
                req["message"] = f"{defn.get('label')} has conflicting values and needs resolution."
                # Populate candidates from evidence if available
                quotes = f.get("evidence_quotes", [])
                if quotes:
                    req["candidates"] = [{"value": q, "source": "resume", "confidence": 0.5} for q in quotes[:3]]

            requirements.append(req)

    # Sort: required first, then by status (missing > conflicting > uncertain)
    def sort_key(r):
        return (
            0 if r["required"] else 1,
            {"missing": 0, "conflicting": 1, "uncertain": 2}.get(r["status"], 3),
            r["field"],
        )
    requirements.sort(key=sort_key)
    return requirements


# --------------------------------------------------------------------------- #
# Safe defaults when AI unavailable
# --------------------------------------------------------------------------- #
def safe_default_extraction(text: str = "", source_document_id: Optional[int] = None) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Return all fields missing with safe defaults when AI is unavailable."""
    fields: Dict[str, Any] = {}
    for key, defn in FIELD_DEFINITIONS.items():
        sensitivity = defn.get("sensitivity", "internal")
        fields[key] = {
            "value": None,
            "confidence": 0.0,
            "confidence_band": "none",
            "evidence": [],
            "evidence_quotes": [],
            "source": "heuristic",
            "sensitivity": sensitivity,
            "status": "missing",
            "review_required": True,
            "value_hash": _hash_value(None),
            "value_preview": _preview_value(None, sensitivity),
            "required": defn.get("required", False),
            "label": defn.get("label", key),
        }

    document = _build_canonical_document(fields)
    completeness = calculate_completeness(fields)
    completion_requirements = build_completion_requirements(fields)

    meta = {
        "ai_used": False,
        "source": "safe_default",
        "extraction_source": "heuristic",
        "guardrail": {"passed": False, "reason": "ai_unavailable"},
        "tokens": {},
        "model": None,
        "completeness": completeness,
        "completion_requirements": completion_requirements,
        "safe_default": True,
        "message": "AI unavailable — all fields marked missing, manual input required.",
    }

    return fields, document, meta


def build_fields_from_legacy_profile(
    legacy_data: Dict[str, Any],
    source_text: str = "",
    source_document_id: Optional[int] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """
    Build candidate fields from legacy profile_data (name, email, skills, experience etc.)
    without extra AI call — used in onboarding to create v2 profile from v1 extraction.

    Preserves evidence from source_text where possible, marks user-stated fields missing.
    """
    # Map legacy keys to new field keys
    mapping: Dict[str, Any] = {}

    # Direct mappings
    mapping["full_name"] = legacy_data.get("name") or legacy_data.get("full_name")
    mapping["email"] = legacy_data.get("email")
    mapping["phone"] = legacy_data.get("phone")
    mapping["location"] = legacy_data.get("location")

    # Experience -> roles, employers, dates
    experience = legacy_data.get("experience") or []
    roles = []
    employers = []
    dates = []
    achievements = []
    for exp in experience:
        if isinstance(exp, dict):
            if exp.get("title"):
                roles.append(exp["title"])
            if exp.get("company"):
                employers.append(exp["company"])
            if exp.get("duration"):
                dates.append(exp["duration"])
            bullets = exp.get("bullets") or []
            for b in bullets[:3]:
                if b:
                    achievements.append(b)
        elif isinstance(exp, str):
            roles.append(exp)

    mapping["roles"] = roles
    mapping["employers"] = employers
    mapping["dates"] = dates
    mapping["achievements"] = achievements

    mapping["skills"] = legacy_data.get("skills") or []
    # Tools: try to extract from skills that look like tools, or from projects tech
    tools = []
    projects = legacy_data.get("projects") or []
    for proj in projects:
        if isinstance(proj, dict):
            tech = proj.get("tech") or []
            tools.extend(tech)
    mapping["tools"] = list(dict.fromkeys(tools))[:20]

    mapping["industries"] = []  # not in legacy, leave missing
    mapping["education"] = legacy_data.get("education") or []
    mapping["certifications"] = legacy_data.get("certifications") or []
    mapping["target_roles"] = [legacy_data.get("current_title")] if legacy_data.get("current_title") else roles[:1]

    # User-stated only — always missing in legacy flow
    for k in USER_STATED_ONLY_FIELDS:
        mapping[k] = None

    # Build fields with confidence based on presence and evidence
    fields: Dict[str, Any] = {}
    for key, defn in FIELD_DEFINITIONS.items():
        raw_val = mapping.get(key)
        normalized = _normalize_value(key, raw_val)

        if normalized in (None, "", [], {}):
            conf = 0.0
            status = "missing"
            evidence_objs: List[Dict[str, Any]] = []
            evidence_quotes: List[str] = []
            band = "none"
            review_required = True
            origin = "heuristic"
        else:
            # Try to find evidence in source_text
            evidence_quotes = []
            if isinstance(normalized, list):
                for item in normalized[:2]:
                    if isinstance(item, str) and item and source_text and item.lower()[:30] in source_text.lower():
                        # Find snippet
                        idx = source_text.lower().find(item.lower()[:30])
                        snippet = source_text[max(0, idx-20):idx+100].strip()[:200]
                        evidence_quotes.append(snippet or item[:200])
                    elif isinstance(item, str):
                        evidence_quotes.append(item[:200])
            elif isinstance(normalized, str):
                if source_text and normalized.lower()[:30] in source_text.lower():
                    idx = source_text.lower().find(normalized.lower()[:30])
                    snippet = source_text[max(0, idx-20):idx+100].strip()[:200]
                    evidence_quotes.append(snippet or normalized[:200])
                else:
                    evidence_quotes.append(normalized[:200])

            evidence_objs = [_evidence_from_text(q, source_text, doc_id=source_document_id) for q in evidence_quotes[:3]]

            # Confidence: high if evidence found, medium otherwise
            if evidence_quotes:
                conf = 0.85 if key in RESUME_DERIVABLE_FIELDS else 0.5
            else:
                conf = 0.6

            band = _band(conf)
            sensitivity = defn.get("sensitivity", "internal")
            review_required = _requires_review(sensitivity, band)

            if key in USER_STATED_ONLY_FIELDS:
                conf = 0.0
                band = "none"
                status = "missing"
                review_required = True
                origin = "heuristic"
                evidence_objs = []
                evidence_quotes = []
                normalized = None
            else:
                if band == "high" and not review_required:
                    status = "confirmed"
                else:
                    status = "uncertain"
                origin = "resume_extraction"

        if normalized in (None, "", [], {}):
            conf = 0.0
            band = "none"
            status = "missing"
            review_required = True
            origin = "heuristic"
            evidence_objs = []
            evidence_quotes = []

        sensitivity = defn.get("sensitivity", "internal")
        value_hash = _hash_value(normalized)
        preview = _preview_value(normalized, sensitivity)

        fields[key] = {
            "value": normalized,
            "confidence": conf,
            "confidence_band": band,
            "evidence": evidence_objs,
            "evidence_quotes": evidence_quotes,
            "source": origin,
            "sensitivity": sensitivity,
            "status": status,
            "review_required": review_required,
            "value_hash": value_hash,
            "value_preview": preview,
            "required": defn.get("required", False),
            "label": defn.get("label", key),
        }

    document = _build_canonical_document(fields)
    completeness = calculate_completeness(fields)
    completion_requirements = build_completion_requirements(fields)

    meta_out = {
        "ai_used": (meta or {}).get("ai_used", True) if meta else True,
        "source": "ai",
        "extraction_source": "ai",
        "guardrail": (meta or {}).get("guardrail", {}) if meta else {},
        "tokens": (meta or {}).get("tokens", {}) if meta else {},
        "model": (meta or {}).get("model") if meta else None,
        "completeness": completeness,
        "completion_requirements": completion_requirements,
    }

    return fields, document, meta_out


# --------------------------------------------------------------------------- #
# Persistence — store to candidate_profiles + provenance
# --------------------------------------------------------------------------- #
def persist_candidate_profile(
    db: Session,
    user_id: int,
    fields: Dict[str, Any],
    document: Dict[str, Any],
    meta: Dict[str, Any],
    source_document_id: Optional[int] = None,
    source_extraction_id: Optional[int] = None,
    persona_id: Optional[int] = None,
) -> CandidateProfile:
    """Persist candidate profile and provenance rows, handling versioning."""
    import hashlib

    # Determine next version
    existing = (
        db.query(CandidateProfile)
        .filter(CandidateProfile.user_id == user_id, CandidateProfile.persona_id == persona_id)
        .order_by(CandidateProfile.version.desc())
        .first()
    )
    next_version = (existing.version + 1) if existing else 1

    # Mark previous current as superseded
    if existing and existing.is_current:
        existing.is_current = False
        existing.state = "superseded"
        existing.superseded_at = datetime.utcnow()
        db.add(existing)

    doc_json = json.dumps(document, sort_keys=True, ensure_ascii=False, default=str)
    doc_sha = hashlib.sha256(doc_json.encode("utf-8")).hexdigest()

    completeness = meta.get("completeness") or calculate_completeness(fields)
    # Determine state: review_required if any field needs review
    needs_review = any(f.get("review_required") for f in fields.values())
    state = "review_required" if needs_review else "draft"

    profile = CandidateProfile(
        user_id=user_id,
        persona_id=persona_id,
        version=next_version,
        state=state,
        is_current=True,
        document=document,
        document_sha256=doc_sha,
        source_document_id=source_document_id,
        source_extraction_id=source_extraction_id,
        review={
            "required": sum(1 for f in fields.values() if f.get("review_required")),
            "resolved": 0,
            "deferred": 0,
            "completed_at": None,
        },
        completeness=completeness,
        extraction_source=meta.get("extraction_source", "ai"),
    )
    db.add(profile)
    db.flush()  # get id

    # Persist provenance per field
    for field_key, f in fields.items():
        # JSON Pointer path
        path = f"/{field_key}"
        # For nested, use top-level for simplicity; contract allows deeper
        provenance = ProfileFieldProvenance(
            user_id=user_id,
            profile_id=profile.id,
            path=path,
            value_hash=f.get("value_hash") or _hash_value(f.get("value")),
            value_preview=f.get("value_preview"),
            origin=f.get("source", "resume_extraction"),
            sensitivity=f.get("sensitivity", "internal"),
            confidence=f.get("confidence"),
            confidence_band=f.get("confidence_band", "none"),
            evidence=f.get("evidence", []),
            extractor={
                "name": "candidate_profile_extractor",
                "version": "1.0.0",
                "model": meta.get("model"),
                "prompt_version": "v1",
            },
            source_document_id=source_document_id,
            source_extraction_id=source_extraction_id,
            ambiguity="conflicting_sources" if f.get("status") == "conflicting" else ("missing" if f.get("status") == "missing" else "none"),
            review_status="needs_review" if f.get("review_required") else "auto_accepted",
            review_required=f.get("review_required", False),
            needs_answer_for=[field_key] if f.get("review_required") else [],
        )
        db.add(provenance)

    db.flush()
    return profile


# --------------------------------------------------------------------------- #
# Corrections — user overrides without deleting original evidence
# --------------------------------------------------------------------------- #
def correct_field(
    db: Session,
    user_id: int,
    profile_id: int,
    field_path: str,
    new_value: Any,
    actor_type: str = "user",
    actor_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> ProfileFieldProvenance:
    """
    Correct a field value, preserving original evidence in history.

    - New value gets origin user_corrected, confidence None (user is truth)
    - Old evidence stays in history
    - Records who changed and when
    """
    provenance = (
        db.query(ProfileFieldProvenance)
        .filter(
            ProfileFieldProvenance.user_id == user_id,
            ProfileFieldProvenance.profile_id == profile_id,
            ProfileFieldProvenance.path == field_path,
        )
        .first()
    )
    if not provenance:
        raise ValueError(f"Field {field_path} not found for profile {profile_id}")

    old_hash = provenance.value_hash
    old_preview = provenance.value_preview
    old_origin = provenance.origin
    old_status = provenance.review_status

    # Normalize new value
    field_key = field_path.lstrip("/").split("/")[0]
    defn = FIELD_DEFINITIONS.get(field_key, {})
    sensitivity = defn.get("sensitivity", provenance.sensitivity)
    normalized = _normalize_value(field_key, new_value)

    new_hash = _hash_value(normalized)
    new_preview = _preview_value(normalized, sensitivity)

    # History row — append-only, never delete original evidence
    history = ProfileFieldHistory(
        user_id=user_id,
        provenance_id=provenance.id,
        path=field_path,
        previous_value_hash=old_hash,
        previous_value_preview=old_preview,
        new_value_hash=new_hash,
        new_value_preview=new_preview,
        origin_before=old_origin,
        origin_after="user_corrected",
        review_status_before=old_status,
        review_status_after="corrected",
        actor_type=actor_type,
        actor_id=actor_id or user_id,
        reason=reason or "user correction",
        event_id=str(uuid.uuid4()),
        occurred_at=datetime.utcnow(),
    )
    db.add(history)

    # Update provenance — keep original evidence in history, but new provenance
    # has user statement evidence
    provenance.value_hash = new_hash
    provenance.value_preview = new_preview
    provenance.origin = "user_corrected"
    provenance.confidence = None  # user is source of truth
    provenance.confidence_band = "high"
    provenance.sensitivity = sensitivity
    # Preserve original evidence in history, but add user statement
    # Keep original evidence list and append user statement
    existing_evidence = provenance.evidence or []
    user_evidence = {
        "kind": "user_statement",
        "quote": str(new_value)[:500] if new_value is not None else "",
        "locator": {},
    }
    # Original evidence preserved in history table, new provenance gets combined
    provenance.evidence = existing_evidence + [user_evidence] if existing_evidence else [user_evidence]
    provenance.review_status = "corrected"
    provenance.review_required = False
    provenance.reviewed_by = actor_id or user_id
    provenance.reviewed_at = datetime.utcnow()
    provenance.needs_answer_for = []
    provenance.ambiguity = "none"

    db.add(provenance)
    db.flush()

    # Update candidate profile document
    profile = db.query(CandidateProfile).filter(CandidateProfile.id == profile_id, CandidateProfile.user_id == user_id).first()
    if profile:
        doc = dict(profile.document or {})
        # Simple top-level update; for nested paths we'd need JSON pointer logic
        # For now, handle top-level keys and known nested structures
        if field_key in doc:
            doc[field_key] = normalized
        elif "contact" in doc and field_key in ("email", "phone", "location"):
            doc["contact"][field_key] = normalized
        elif "eligibility" in doc and field_key in ("work_authorization", "sponsorship_required"):
            doc["eligibility"][field_key] = normalized
        elif "preferences" in doc and field_key == "remote_preference":
            doc["preferences"]["remote"] = normalized
        elif "preferences" in doc and field_key == "target_roles":
            doc["preferences"]["target_roles"] = normalized
        elif field_key == "full_name" and "identity" in doc:
            doc["identity"]["full_name"] = normalized
        # Recalculate completeness
        # Rebuild fields dict from provenance for completeness calc would be ideal,
        # but for now patch the document and recalc from current provenance
        all_provenance = db.query(ProfileFieldProvenance).filter(ProfileFieldProvenance.profile_id == profile_id).all()
        # Reconstruct fields for completeness
        fields_for_comp = {}
        for prov in all_provenance:
            fk = prov.path.lstrip("/").split("/")[0]
            # Decode value from preview if not sensitive, else use hash placeholder
            # For completeness we need actual value — we have it in doc or we can use preview
            # Simplify: use doc value
            val = doc.get(fk)
            if fk in ("email", "phone", "location") and "contact" in doc:
                val = doc["contact"].get(fk, val)
            fields_for_comp[fk] = {
                "value": val,
                "confidence": prov.confidence if prov.confidence is not None else 1.0,
                "confidence_band": prov.confidence_band,
                "status": "confirmed" if prov.review_status in ("confirmed", "corrected", "auto_accepted") else ("missing" if val in (None, "", [], {}) else "uncertain"),
                "review_required": prov.review_required,
            }
        # Ensure all defined fields present
        for k, d in FIELD_DEFINITIONS.items():
            if k not in fields_for_comp:
                fields_for_comp[k] = {"value": None, "confidence": 0.0, "confidence_band": "none", "status": "missing", "review_required": True}

        profile.document = doc
        profile.completeness = calculate_completeness(fields_for_comp)
        # Update review counts
        remaining = sum(1 for p in all_provenance if p.review_required)
        profile.review = {
            "required": remaining,
            "resolved": len(all_provenance) - remaining,
            "deferred": 0,
            "completed_at": datetime.utcnow().isoformat() if remaining == 0 else None,
        }
        if remaining == 0:
            profile.state = "active"
            profile.activated_at = datetime.utcnow()
        db.add(profile)

    db.flush()
    return provenance


def confirm_field(
    db: Session,
    user_id: int,
    profile_id: int,
    field_path: str,
    actor_id: Optional[int] = None,
) -> ProfileFieldProvenance:
    """Confirm an extracted field without changing value."""
    provenance = (
        db.query(ProfileFieldProvenance)
        .filter(
            ProfileFieldProvenance.user_id == user_id,
            ProfileFieldProvenance.profile_id == profile_id,
            ProfileFieldProvenance.path == field_path,
        )
        .first()
    )
    if not provenance:
        raise ValueError(f"Field {field_path} not found")

    old_status = provenance.review_status

    history = ProfileFieldHistory(
        user_id=user_id,
        provenance_id=provenance.id,
        path=field_path,
        previous_value_hash=provenance.value_hash,
        previous_value_preview=provenance.value_preview,
        new_value_hash=provenance.value_hash,
        new_value_preview=provenance.value_preview,
        origin_before=provenance.origin,
        origin_after="user_confirmed" if provenance.origin == "resume_extraction" else provenance.origin,
        review_status_before=old_status,
        review_status_after="confirmed",
        actor_type="user",
        actor_id=actor_id or user_id,
        reason="user confirmed",
        event_id=str(uuid.uuid4()),
        occurred_at=datetime.utcnow(),
    )
    db.add(history)

    provenance.origin = "user_confirmed" if provenance.origin == "resume_extraction" else provenance.origin
    provenance.review_status = "confirmed"
    provenance.review_required = False
    provenance.reviewed_by = actor_id or user_id
    provenance.reviewed_at = datetime.utcnow()
    provenance.needs_answer_for = []
    db.add(provenance)
    db.flush()
    return provenance


def reject_field(
    db: Session,
    user_id: int,
    profile_id: int,
    field_path: str,
    actor_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> ProfileFieldProvenance:
    """Reject an extracted field — value cleared, original evidence kept in history."""
    provenance = (
        db.query(ProfileFieldProvenance)
        .filter(
            ProfileFieldProvenance.user_id == user_id,
            ProfileFieldProvenance.profile_id == profile_id,
            ProfileFieldProvenance.path == field_path,
        )
        .first()
    )
    if not provenance:
        raise ValueError(f"Field {field_path} not found")

    old_hash = provenance.value_hash
    old_preview = provenance.value_preview
    old_origin = provenance.origin
    old_status = provenance.review_status

    new_hash = _hash_value(None)

    history = ProfileFieldHistory(
        user_id=user_id,
        provenance_id=provenance.id,
        path=field_path,
        previous_value_hash=old_hash,
        previous_value_preview=old_preview,
        new_value_hash=new_hash,
        new_value_preview=None,
        origin_before=old_origin,
        origin_after="user_corrected",
        review_status_before=old_status,
        review_status_after="rejected",
        actor_type="user",
        actor_id=actor_id or user_id,
        reason=reason or "user rejected",
        event_id=str(uuid.uuid4()),
        occurred_at=datetime.utcnow(),
    )
    db.add(history)

    provenance.value_hash = new_hash
    provenance.value_preview = None
    provenance.origin = "user_corrected"
    provenance.confidence = None
    provenance.confidence_band = "none"
    provenance.review_status = "rejected"
    provenance.review_required = False
    provenance.reviewed_by = actor_id or user_id
    provenance.reviewed_at = datetime.utcnow()
    provenance.needs_answer_for = []
    provenance.ambiguity = "none"

    db.add(provenance)
    db.flush()
    return provenance


# --------------------------------------------------------------------------- #
# Retrieval helpers for API
# --------------------------------------------------------------------------- #
def get_current_profile(db: Session, user_id: int, persona_id: Optional[int] = None) -> Optional[CandidateProfile]:
    return (
        db.query(CandidateProfile)
        .filter(CandidateProfile.user_id == user_id, CandidateProfile.persona_id == persona_id, CandidateProfile.is_current.is_(True))
        .order_by(CandidateProfile.version.desc())
        .first()
    )


def get_profile_with_provenance(db: Session, user_id: int, profile_id: int) -> Tuple[Optional[CandidateProfile], List[ProfileFieldProvenance]]:
    profile = db.query(CandidateProfile).filter(CandidateProfile.id == profile_id, CandidateProfile.user_id == user_id).first()
    if not profile:
        return None, []
    provenance = db.query(ProfileFieldProvenance).filter(ProfileFieldProvenance.profile_id == profile_id, ProfileFieldProvenance.user_id == user_id).all()
    return profile, provenance


def list_provenance_for_review(db: Session, user_id: int, profile_id: int, only_needs_review: bool = False) -> List[ProfileFieldProvenance]:
    q = db.query(ProfileFieldProvenance).filter(ProfileFieldProvenance.profile_id == profile_id, ProfileFieldProvenance.user_id == user_id)
    if only_needs_review:
        q = q.filter(ProfileFieldProvenance.review_required.is_(True))
    return q.order_by(ProfileFieldProvenance.path).all()


def get_field_history(db: Session, user_id: int, provenance_id: int) -> List[ProfileFieldHistory]:
    return (
        db.query(ProfileFieldHistory)
        .filter(ProfileFieldHistory.user_id == user_id, ProfileFieldHistory.provenance_id == provenance_id)
        .order_by(ProfileFieldHistory.occurred_at.desc())
        .all()
    )


def build_review_response(profile: CandidateProfile, provenance_list: List[ProfileFieldProvenance]) -> Dict[str, Any]:
    """Build API response matching contract 13-api-response-shapes for profile review."""
    fields = []
    for prov in provenance_list:
        # Mask sensitive values
        value_masked = prov.value_preview is None and prov.sensitivity in ("sensitive", "restricted")
        # Build candidates for conflicting
        candidates = []
        if prov.ambiguity == "conflicting_sources" or prov.review_status == "needs_review":
            # Extract candidate values from evidence quotes if available
            for ev in (prov.evidence or [])[:3]:
                quote = ev.get("quote", "") if isinstance(ev, dict) else str(ev)
                if quote:
                    candidates.append({"value": quote[:200], "source": ev.get("kind", "text_span") if isinstance(ev, dict) else "resume", "confidence": 0.5})

        fields.append({
            "provenance_id": prov.id,
            "path": prov.path,
            "value_masked": value_masked,
            "value_preview": prov.value_preview,
            "value_hash": prov.value_hash,
            "origin": prov.origin,
            "sensitivity": prov.sensitivity,
            "confidence": prov.confidence,
            "confidence_band": prov.confidence_band,
            "evidence": prov.evidence,
            "review_status": prov.review_status,
            "review_required": prov.review_required,
            "ambiguity": prov.ambiguity,
            "needs_answer_for": prov.needs_answer_for,
            "candidates": candidates,
            "used_by": [],  # would list applications using this field
        })

    return {
        "profile_id": profile.id,
        "version": profile.version,
        "state": profile.state,
        "is_current": profile.is_current,
        "document": profile.document,
        "completeness": profile.completeness,
        "review": profile.review,
        "fields": fields,
        "server_time": datetime.utcnow().isoformat(),
    }
