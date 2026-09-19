"""
Multi-stage job matching — optimized for interview potential, not volume.

The system is deliberately *not* a single opaque AI score. Every match is the
product of three explainable stages, and the stored row carries all three:

Stage 1 — hard filters (deterministic)
    Location / work-authorization / sponsorship / employment-type /
    compensation / seniority mismatches, missing required skills, expired and
    duplicate jobs. Each check is ``pass | conflict | unknown | fail``:
    ``fail`` filters the job out of recommendations, ``conflict`` applies a
    heavy, itemised penalty, ``unknown`` means "not stated" and is *never*
    treated as a fail (an absent answer is not a mismatch).

Stage 2 — deterministic score (reproducible)
    Twelve weighted features (required skills, preferred skills, relevant
    experience, seniority, industry, location, compensation, remote, career
    trajectory, freshness, hiring signal, application friction). Each feature
    stores its weight, 0-100 value, contribution, reason and evidence. The
    overall score is the weighted mean over *scored* features, minus the stage
    1 penalties. Same inputs + same ``MATCHER_VERSION`` ⇒ identical output;
    the row stores the inputs' hashes and the version so the explanation is
    reproducible and future recalibration is possible.

Stage 3 — AI evidence review (guardrailed, optional)
    The model reviews the deterministic result and returns a recommendation,
    evidence-backed strengths, missing requirements (labelled explicit vs
    inferred), risks, resume emphasis and a suggested application action. It
    runs under the shared guardrail contract (:func:`run_guarded_task`): every
    claimed strength must be present in the candidate's own data, every
    "explicit" requirement must appear in the posting, the recommendation must
    follow the deterministic band, and probability-of-interview language is a
    guardrail *error* — the product says "estimated fit", never a calibrated
    interview probability, until enough outcome data exists
    (see :func:`calibration_status`).

Honesty rules enforced here:

* A match is a fact about a pair, computed by a named scorer
  (``docs/contracts/06-match-result.md``) — inputs are versioned, re-scores
  insert, they never edit a score in place.
* High-fit matches require supporting candidate evidence; a score with no
  evidence is demoted and flagged, not trusted.
* Missing required skills are always listed on the row, filtered or not.
* The user can correct a wrong recommendation
  (``match_feedback`` — ``not_relevant`` + reason is the correction path, and
  ``applied``/``rejected``/``interview`` build the calibration dataset).
* Candidate data is minimized: the scoring view carries only what the stages
  consume, and the AI prompt never receives contact details, salary or
  work-authorization specifics.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.contracts.vocabulary import (
    MATCH_FEEDBACK_KINDS,
    MATCH_FEEDBACK_OUTCOME_KINDS,
)
from app.core.logging import get_logger
from app.models.models import Job, MatchFeedback, MatchResult, Persona, Profile
from app.services.ai_guardrails import (
    AIUnavailableError,
    FieldSpec,
    GuardrailError,
    SchemaSpec,
    run_guarded_task,
    strip_ai_artifacts,
)
from app.services.scoring import _detect_seniority, cosine_sim, tf, tokenize

log = get_logger("app.matching")

# --------------------------------------------------------------------------- #
# Scorer identity — what makes scores comparable across time
# --------------------------------------------------------------------------- #

#: Deterministic scorer (stages 1+2) version. A rubric/weight/penalty change
#: is a bump; old rows become ``scorer_upgraded`` and are re-scored on demand.
MATCHER_VERSION = "1.0.0"

#: Stage-3 prompt contract version.
REVIEW_PROMPT_VERSION = "match-review-1.0.0"

#: How many recorded application *outcomes* (applied → rejected/interview) per
#: scorer version are needed before a calibrated interview probability could
#: even be *offered*. Until then the product says "estimated fit" only.
MIN_OUTCOMES_FOR_CALIBRATION = 20

#: The one sentence every match read carries. The score is an estimate.
DISCLAIMER = (
    "Estimated fit from recorded evidence — a recommendation, not a probability "
    "of an interview and never a guarantee."
)

#: Stage-2 weights. They sum to 1.0; the stored row keeps them so the number
#: can be re-derived by anyone with the rubric.
FEATURE_WEIGHTS: Dict[str, float] = {
    "required_skills": 0.22,
    "relevant_experience": 0.16,
    "seniority": 0.10,
    "career_trajectory": 0.10,
    "compensation": 0.08,
    "preferred_skills": 0.06,
    "industry": 0.06,
    "location": 0.06,
    "remote": 0.04,
    "freshness": 0.04,
    "hiring_signal": 0.04,
    "application_friction": 0.04,
}

#: Stage-1 penalties for ``conflict`` checks (``fail`` removes the job).
FILTER_PENALTIES: Dict[str, int] = {
    "location_mismatch": 20,
    "seniority_mismatch": 20,
    "employment_type_mismatch": 25,
    "sponsorship_mismatch": 25,
    "compensation_below_minimum": 10,
    "required_skills_missing": 8,   # per missing required skill, capped below
}
REQUIRED_SKILLS_MAX_PENALTY = 24

#: Band thresholds — ``docs/contracts/06-match-result.md`` §3.
BAND_THRESHOLDS = ((85, "strong"), (70, "good"), (50, "possible"))

#: Stage-3 recommendations and the bands each one is allowed to express.
RECOMMENDATIONS = ("apply", "apply_with_tailoring", "hold", "skip")
RECOMMENDATION_BY_BAND = {
    "strong": ("apply", "apply_with_tailoring"),
    "good": ("apply", "apply_with_tailoring"),
    "possible": ("apply", "apply_with_tailoring", "hold", "skip"),
    "weak": ("hold", "skip"),
    "unknown": RECOMMENDATIONS,
}

#: Language that would turn an estimate into a probability claim.
_PROBABILITY_RE = re.compile(
    r"(\d+(\.\d+)?\s*%|[01]\.\d+\s+(?:chance|probability|likelihood))"
    r"[^.]*\b(interview|offer|callback|screen)\b"
    r"|\b(interview|offer)\b[^.]*\b(\d+(\.\d+)?\s*%|[01]\.\d+\s+(?:chance|probability|likelihood))\b"
    r"|\bguarantee[ds]?\b|\bguaranteed\b|\bassured interview\b",
    re.IGNORECASE,
)

#: Words that never count as skills when mining the JD for required/preferred
#: lists ("5+ years of experience" is not a required skill).
_GENERIC_JD_WORDS = {
    "ability", "advanced", "analytics", "and", "best", "bonus", "candidate",
    "candidates", "collaborate", "communication", "company", "degree", "develop",
    "development", "diploma", "experience", "familiarity", "five", "four",
    "graduate", "helping", "high", "ideally", "including", "knowledge", "least",
    "like", "master", "mentoring", "more", "must", "needs", "offer", "one",
    "plus", "projects", "required", "skills", "strong", "team", "teams",
    "three", "two", "understanding", "using", "use", "various", "work",
    "working", "years", "you", "your", "well", "good", "great", "new", "other",
    "some", "all", "any", "our", "the", "this", "that", "with", "have", "has",
    "will", "able", "tools", "technologies", "technology",
    "systems", "system", "processes", "process", "environments", "environment",
    "domain", "background", "backgrounds", "track", "record", "history",
    "evidence", "demonstrated", "solid", "proven",
}

_REQUIRED_HEADING = re.compile(
    r"^\s*(required|must\s+have|qualifications|what\s+you(?:'ll|\s+will)\s+need|requirements?)\b",
    re.IGNORECASE,
)
_PREFERRED_HEADING = re.compile(
    r"^\s*(nice\s+to\s+have|preferred|preferable|bonus|plus|great\s+if|bonus\s+points)\b",
    re.IGNORECASE,
)

_SALARY_RE = re.compile(
    r"\$\s*([0-9][0-9,]{2,9})\s*[-–—to]+\s*\$\s*([0-9][0-9,]{2,9})"
    r"|\$\s*([0-9][0-9,]{2,9})\s*(?:per|/)\s*(year|month|hour|day)"
    r"\b([0-9][0-9,]{2,9})\b\s*(?:-|–|to)\s*\$\s*([0-9][0-9,]{2,9})"
    r"|([0-9][0-9,]{2,9})\s*[-–—]\s*([0-9][0-9,]{2,9})\s*(?:k|K)\b"
    r"\s*(?:a\s*)?(?:year|annum|per year)?",
    re.IGNORECASE,
)

_SALARY_PER = {"year": 1, "month": 12, "hour": 2080, "day": 260}

INDUSTRY_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "fintech": ("fintech", "payment", "payments", "banking", "bank", "lending", "insurance", "trading", "ledger"),
    "healthcare": ("healthcare", "health tech", "medical", "clinical", "pharma", "biotech", "diagnostic"),
    "e-commerce": ("e-commerce", "ecommerce", "retail", "marketplace", "shopping"),
    "developer_tools": ("developer tools", "dev tools", "developer experience", "devops", "developer platform"),
    "ai_ml": ("machine learning", "deep learning", "natural language", "computer vision", "generative", "llm", "mlops"),
    "gaming": ("gaming", "game development", "game engine", "esports"),
    "logistics": ("logistics", "supply chain", "freight", "shipping", "fulfilment", "fulfillment"),
    "media": ("media", "streaming", "newsroom", "publishing", "content platform"),
    "education": ("education", "edtech", "elearning", "coursework"),
    "energy": ("renewable energy", "solar", "wind farm", "energy trading", "grid"),
    "aerospace_defense": ("aerospace", "aeronautics", "defense", "satellite", "avionics"),
}

EMPLOYMENT_TYPES = ("full_time", "part_time", "contract", "internship", "temporary", "freelance")

#: Location values that are not actually a location — treating them as onsite
#: would invent a mismatch.
_LOCATION_PLACEHOLDERS = {
    "various", "anywhere", "n/a", "na", "tbd", "multiple", "global",
    "various locations", "worldwide", "united states", "usa", "us",
}


def band_for(score: float, source_known: bool = True) -> str:
    """The band for a score (contract 06 §3). ``unknown`` when unscored."""
    if not source_known:
        return "unknown"
    for threshold, label in BAND_THRESHOLDS:
        if score >= threshold:
            return label
    return "weak"


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


def _norm_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9+#.]+", "", str(value or "").lower())


def _int_or(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Candidate view — the minimized slice of the profile the matcher consumes
# --------------------------------------------------------------------------- #

def _candidate_years(exp: List[Dict[str, Any]], summary: str) -> Optional[int]:
    """Estimate total years of experience from recorded durations/years."""
    total = 0
    for entry in exp or []:
        if not isinstance(entry, dict):
            continue
        blob = " ".join(str(entry.get(k) or "") for k in ("duration", "dates", "period"))
        ranges = re.findall(r"\b(19|20)\d{2}\b", blob)
        if len(ranges) >= 2:
            try:
                years = int(ranges[-1]) - int(ranges[0])
                total += max(0, min(years, 40))
                continue
            except ValueError:
                pass
    if total:
        return total
    m = re.search(r"(\d+)\+?\s*years", summary or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


def minimal_candidate_view(profile: Dict[str, Any], persona: Optional[Persona] = None) -> Dict[str, Any]:
    """
    The minimized candidate slice used by all three stages.

    What stays: skills, role titles, durations, location, remote preference,
    stated eligibility, minimum compensation, target roles, industries — the
    facts the stages consume. What never reaches the AI prompt or the stored
    match: name, email, phone, links and free resume prose (server-side
    summaries are capped). This is the "keep candidate profile data
    minimized" rule made concrete.
    """
    profile = profile or {}
    exp_raw = profile.get("experience") or []
    experience: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    for entry in exp_raw:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        duration = str(entry.get("duration") or entry.get("dates") or entry.get("period") or "").strip()
        desc = " ".join(
            str(b) for b in (entry.get("bullets") or ([entry.get("description")] if entry.get("description") else []))
        )[:300]
        experience.append({"title": title, "duration": duration, "description": desc,
                           "company": str(entry.get("company") or "").strip()})
        if title:
            text_parts.append(title)
        if desc:
            text_parts.append(desc)
    skills = [str(s) for s in (profile.get("skills") or []) if str(s).strip()]
    summary = str(profile.get("summary") or "")[:400]
    text_parts.extend(skills)
    if summary:
        text_parts.append(summary)
    current_title = str(profile.get("current_title") or "")
    if current_title:
        text_parts.append(current_title)

    full_text = " ".join(text_parts)
    target_roles: List[str] = []
    if persona is not None and str(persona.target_role or "").strip():
        target_roles.append(str(persona.target_role).strip())
    for role in (profile.get("target_roles") or []):
        role = str(role).strip()
        if role and role not in target_roles:
            target_roles.append(role)

    preferences = profile.get("preferences") or {}
    if isinstance(preferences, dict):
        remote = preferences.get("remote") or preferences.get("remote_preference")
    else:
        remote = None
    remote = str(remote or profile.get("remote_preference") or "").strip() or None

    eligibility = profile.get("eligibility") or {}
    compensation = profile.get("compensation") or {}
    salary_expectations = profile.get("salary_expectations") or {}
    minimum = (
        compensation.get("minimum")
        if isinstance(compensation, dict)
        else None
    ) or (
        salary_expectations.get("minimum")
        if isinstance(salary_expectations, dict)
        else None
    )

    return {
        "skills": skills,
        "current_title": current_title,
        "seniority": _detect_seniority(" ".join([current_title] + [e["title"] for e in experience])) or "unknown",
        "experience": experience,
        "years": _candidate_years(experience, summary),
        "industries": detect_industries(full_text),
        "location": str(profile.get("location") or ""),
        "remote_preference": remote,
        "work_authorization": str(eligibility.get("work_authorization") or profile.get("work_authorization") or ""),
        "sponsorship_required": bool(
            eligibility.get("sponsorship_required")
            if isinstance(eligibility, dict) and "sponsorship_required" in eligibility
            else profile.get("sponsorship_required")
        ),
        "salary_minimum": _int_or(minimum),
        "target_roles": target_roles,
        # Server-side grounding text for the stage-3 checks. The prompt gets a
        # shorter slice of this (skills + titles + capped summary), never the
        # full resume prose.
        "grounding_text": full_text[:8000],
    }


def detect_industries(text: str) -> List[str]:
    low = (text or "").lower()
    return sorted(
        name for name, keywords in INDUSTRY_KEYWORDS.items()
        if any(k in low for k in keywords)
    )


# --------------------------------------------------------------------------- #
# Job signals — what the posting states, deterministically
# --------------------------------------------------------------------------- #

def _is_jd_heading(line: str) -> bool:
    """A section heading in free-form JD text (heuristic, deliberately narrow)."""
    stripped = line.strip()
    if not stripped or len(stripped) > 70:
        return False
    if re.match(r"^\s{0,3}#{1,6}\s+\S", line):
        return True
    # "Required skills:" — short, ends with a colon, no bullet marker.
    if stripped.endswith(":") and not stripped.startswith(("-", "*", "•")):
        return True
    # "Compensation: $110,000 - $140,000 per year." — heading word + colon
    # starting the line (the value continues on the same line).
    if re.match(r"^[A-Z][\w &'/-]{1,40}:\s", stripped) and len(stripped) < 120:
        return True
    if stripped.upper() == stripped and " " in stripped and not stripped.startswith(("-", "*", "•")):
        return True
    return False


def _section_skills(jd: str, heading_re: re.Pattern) -> List[str]:
    """Skill tokens from the bullet lines under a matching heading."""
    skills: List[str] = []
    in_section = False
    for line in (jd or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _is_jd_heading(line):
            in_section = bool(heading_re.match(stripped.rstrip(":")))
            continue
        if not in_section:
            continue
        m = re.match(r"^\s*(?:[-*•+]|\d+[.)])\s*(.+)$", stripped)
        content = m.group(1) if m else stripped
        if len(content) > 120:
            continue
        tokens = tokenize(content)
        for token in tokens:
            if token in _GENERIC_JD_WORDS or len(token) < 3:
                continue
            if re.fullmatch(r"\d+[\d]*", token):
                continue
            if token in skills:
                continue
            # Keep the posting's own casing ("GraphQL", not "graphql") for display.
            casing = re.search(
                rf"(?<![a-z0-9+#]){re.escape(token)}(?![a-z0-9+#])", content, re.IGNORECASE
            )
            skills.append(casing.group(0) if casing else token)
    return skills


def parse_job_signals(job: Job, now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Deterministic read of the structured attributes the stages need.

    Everything is ``None``/``unknown`` when the posting does not state it —
    the filters and features then say "not stated" instead of guessing.
    Source order: ``extra`` structured fields (when a source or the classifier
    already parsed them) → title/location columns → JD text mining.
    """
    now = now or datetime.utcnow()
    extra = dict(job.extra or {})
    raw_role = extra.get("role")
    role: Dict[str, Any] = raw_role if isinstance(raw_role, dict) else {}
    raw_compensation = extra.get("compensation")
    compensation: Dict[str, Any] = raw_compensation if isinstance(raw_compensation, dict) else {}
    jd = job.description or ""
    jd_lower = jd.lower()
    title = job.title or ""
    title_lower = title.lower()
    location = (job.location or "").strip()

    # ---- workplace (remote / hybrid / onsite) ----
    workplace = role.get("workplace") if role.get("workplace") in ("onsite", "hybrid", "remote") else None
    if workplace is None:
        if "hybrid" in jd_lower or "hybrid" in title_lower:
            workplace = "hybrid"
        elif re.search(r"\bremote\b", location) or "fully remote" in jd_lower or "work from anywhere" in jd_lower:
            workplace = "remote"
        elif "on-site" in jd_lower or "on site" in jd_lower or "in office" in jd_lower or "onsite" in title_lower:
            workplace = "onsite"
        elif re.search(r"\bremote\b", jd_lower) and re.search(r"\b(?:days?|times) a week\b", jd_lower):
            workplace = "hybrid"
    if workplace is None:
        workplace = "remote" if re.search(r"\bremote\b", jd_lower) else (
            "onsite" if (location and location.casefold() not in _LOCATION_PLACEHOLDERS) else "unknown"
        )

    # ---- employment type ----
    # ``explicit`` distinguishes a stated type from the full-time default we
    # apply when the posting is silent (silence is not a statement — the
    # filters only penalize explicitly stated non-full-time types).
    employment_type = role.get("employment_type") if role.get("employment_type") in EMPLOYMENT_TYPES else None
    explicit = employment_type is not None
    if employment_type is None:
        for marker, value in (
            ("internship", "internship"), ("intern", "internship"),
            ("part-time", "part_time"), ("part time", "part_time"),
            ("contract basis", "contract"), ("contractor", "contract"),
            ("temporary", "temporary"), ("freelance", "freelance"),
        ):
            if marker in title_lower or (marker in jd_lower and value in ("internship", "part_time", "contract")):
                employment_type = value
                explicit = True
                break
        if employment_type is None:
            employment_type = "full_time"

    # ---- compensation ----
    salary: Dict[str, Any] = {"min": None, "max": None, "currency": "USD", "period": "year", "stated": False}
    if isinstance(compensation.get("min"), (int, float)) or isinstance(compensation.get("max"), (int, float)):
        salary["min"] = _int_or(compensation.get("min"))
        salary["max"] = _int_or(compensation.get("max"))
        salary["currency"] = str(compensation.get("currency") or "USD")
        salary["period"] = str(compensation.get("period") or "year")
        salary["stated"] = True
    else:
        raw_salary = extra.get("salary")
        text = str(raw_salary or "")
        if not text and ("$" in jd or "salary" in jd_lower or "compensation" in jd_lower or "₹" in jd):
            text = jd[:6000]
        for match in _SALARY_RE.finditer(text):
            g = match.groups()
            if g[0] is not None and g[1] is not None:
                lo, hi = int(g[0].replace(",", "")), int(g[1].replace(",", ""))
            elif g[2] is not None and g[3] is not None:
                lo = hi = int(g[2].replace(",", ""))
                period = str(g[3]).lower()
                if period in _SALARY_PER and period != "year":
                    lo = hi = lo * _SALARY_PER[period]
            elif g[4] is not None and g[5] is not None:
                lo, hi = int(g[4].replace(",", "")), int(g[5].replace(",", ""))
            elif g[6] is not None and g[7] is not None:
                lo, hi = int(g[6].replace(",", "")) * 1000, int(g[7].replace(",", "")) * 1000
            else:
                continue
            if lo > hi:
                lo, hi = hi, lo
            if hi < 1000:  # "12-15" is not salary in USD/year
                continue
            salary["min"], salary["max"] = lo, hi
            salary["stated"] = True
            break

    # ---- seniority / years ----
    seniority = role.get("seniority") if role.get("seniority") in (
        "intern", "junior", "mid", "senior", "lead", "principal"
    ) else _detect_seniority(title) or "unknown"
    years_min = _int_or(role.get("years_min"))
    if years_min is None:
        m = re.search(r"(\d+)\s*\+?\s*years? of experience", jd, re.IGNORECASE)
        years_min = int(m.group(1)) if m else None

    # ---- skills (structured first, then section mining) ----
    required_skills = [str(s) for s in (role.get("skills_required") or extra.get("skills_required") or [])] or \
        _section_skills(jd, _REQUIRED_HEADING)
    preferred_skills = [str(s) for s in (role.get("skills_preferred") or extra.get("skills_preferred") or [])] or \
        _section_skills(jd, _PREFERRED_HEADING)

    # ---- eligibility ----
    offers_sponsorship = None
    requires_sponsorship = None
    if re.search(r"(sponsors?|sponsorship available|visa support)", jd_lower):
        offers_sponsorship = True
    if re.search(r"(no\s+(visa\s+)?sponsorship|will not sponsor|no sponsorship)", jd_lower):
        offers_sponsorship = False
        requires_sponsorship = True
    if requires_sponsorship is None and re.search(
        r"(must (?:be |have |hold )?(?:[a-z]{2,4} )?(?:work authorization|authorized to work|be eligible to work)"
        r"|eligible to work in|citizen(ship)? required|right to work)",
        jd_lower,
    ):
        requires_sponsorship = True
    work_authorization_required = role.get("work_authorization_required") or (
        "country-specific" if requires_sponsorship else None
    )

    # ---- industry ----
    industry = str(extra.get("industry") or "").strip() or None
    if not industry:
        found = detect_industries(f"{title} {location} {jd[:4000]}")
        industry = found[0] if found else None

    # ---- hiring signals ----
    signals: List[str] = []
    if re.search(r"\b(?:immediately|urgently|asap)\b", jd_lower):
        signals.append("urgent_language")
    if re.search(r"growing (?:team|engineering)|expanding (?:team|group)", jd_lower):
        signals.append("growing_team")
    if re.search(r"(?:multiple|several) (?:openings|roles|positions)", jd_lower):
        signals.append("multiple_openings")
    if job.posted_at and (now - job.posted_at) <= timedelta(hours=48):
        signals.append("fresh_posting")

    # ---- application friction (what we already know about the form) ----
    raw_forms = extra.get("forms")
    forms: Dict[str, Any] = raw_forms if isinstance(raw_forms, dict) else {}
    friction: Dict[str, Any] = {
        "known": bool(forms),
        "requires_login": bool(forms.get("requires_login")),
        "required_fields": sum(
            1 for f in (forms.get("fields") or [])
            if isinstance(f, dict) and f.get("required")
        ),
        "portal_type": str(forms.get("portal_type") or ""),
        "referral": bool(re.search(r"\breferral\b", jd_lower)),
    }

    # ---- age / identity ----
    posted_at = job.posted_at
    age_hours: Optional[float] = (now - posted_at).total_seconds() / 3600.0 if posted_at else None

    return {
        "workplace": workplace,
        "employment_type": employment_type,
        "employment_type_explicit": explicit,
        "salary": salary,
        "seniority": seniority,
        "years_min": years_min,
        "required_skills": required_skills,
        "preferred_skills": preferred_skills,
        "offers_sponsorship": offers_sponsorship,
        "requires_sponsorship": requires_sponsorship,
        "work_authorization_required": work_authorization_required,
        "industry": industry,
        "hiring_signals": signals,
        "friction": friction,
        "age_hours": age_hours,
        "location": location,
        "title": title,
    }


# --------------------------------------------------------------------------- #
# Stage 1 — hard filters
# --------------------------------------------------------------------------- #

def _check(key: str, status: str, detail: str, *, penalty: int = 0,
           job_value: Any = None, candidate_value: Any = None) -> Dict[str, Any]:
    return {
        "key": key,
        "status": status,  # pass | conflict | unknown | fail
        "penalty": penalty,
        "detail": detail,
        "job_value": job_value,
        "candidate_value": candidate_value,
    }


_SENIORITY_LEVELS = {"intern": 0, "junior": 1, "mid": 2, "senior": 3, "lead": 4, "principal": 5, "unknown": None}


def _skill_token_set(text: str) -> set:
    return {_norm_text(t) for t in tokenize(text or "")}


def _has_skill(cand_tokens: set, skill: str) -> bool:
    """Word-level skill membership — 'go' must not match inside 'google'."""
    return _norm_text(skill) in cand_tokens


def _location_cities(value: str) -> List[str]:
    return [w for w in re.split(r"[,\n;]+", (value or "").strip()) if w.strip() and len(w.strip()) > 2][:8]


def run_hard_filters(
    job: Job,
    signals: Dict[str, Any],
    candidate: Dict[str, Any],
    *,
    is_duplicate: bool = False,
    duplicate_of: Optional[int] = None,
    already_applied: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Stage 1. Returns ``{status: eligible|penalized|filtered, checks: [...],
    total_penalty: int, reasons: [str]}``.

    Rules: an ``unknown`` check never penalizes (a missing answer is not a
    mismatch); a ``conflict`` applies its itemized penalty; a ``fail`` filters
    the job out of recommendations while keeping the full report stored so the
    exclusion is explainable.
    """
    checks: List[Dict[str, Any]] = []

    # ---- expired ----
    if job.expired or (job.expired_at is not None and (now or datetime.utcnow()) >= job.expired_at):
        checks.append(_check("expired", "fail", "The posting is expired/closed.", job_value=True))
    else:
        checks.append(_check("expired", "pass", "Posting is not expired."))

    # ---- duplicate ----
    if is_duplicate:
        checks.append(_check(
            "duplicate", "fail",
            f"Duplicate of job {duplicate_of} (same role, company and location).",
            job_value=job.id,
        ))
    elif already_applied:
        checks.append(_check("duplicate", "fail", "You have already applied to this role."))
    else:
        checks.append(_check("duplicate", "pass", "Not a duplicate on the board."))

    # ---- location ----
    cand_loc = _location_cities(candidate.get("location") or "")
    job_loc = _location_cities(signals.get("location") or "")
    workplace = signals.get("workplace")
    if workplace == "remote":
        if (candidate.get("remote_preference") or "").lower() in ("onsite", "on-site", "in office"):
            checks.append(_check(
                "location_mismatch", "conflict",
                "Job is fully remote but the candidate prefers onsite-only work.",
                penalty=FILTER_PENALTIES["location_mismatch"],
                job_value="remote", candidate_value=candidate.get("remote_preference"),
            ))
        else:
            checks.append(_check("location_mismatch", "pass", "Job is remote — feasible."))
    elif workplace in ("onsite", "hybrid") and cand_loc:
        # Primary (city) first: "Munich, Germany" vs "Berlin, Germany" must not
        # pass on the shared country token. A country/region named by the job
        # that the candidate lives in still passes.
        cand_set = {c.casefold() for c in cand_loc}
        job_set = {c.casefold() for c in job_loc}
        primary_same = cand_loc[0].casefold() == (job_loc[0].casefold() if job_loc else None)
        if primary_same:
            checks.append(_check("location_mismatch", "pass",
                                 f"Same location: {cand_loc[0]}."))
        elif job_loc and job_loc[0].casefold() in cand_set:
            checks.append(_check("location_mismatch", "pass",
                                 f"Job area {job_loc[0]} includes the candidate's location."))
        elif cand_loc[0].casefold() in job_set:
            checks.append(_check("location_mismatch", "pass",
                                 f"Candidate's location {cand_loc[0]} is in the job area."))
        else:
            checks.append(_check(
                "location_mismatch", "conflict",
                f"Job is {workplace} in {job_loc and job_loc[0]}; candidate is based in {cand_loc[0]}.",
                penalty=FILTER_PENALTIES["location_mismatch"],
                job_value=job_loc[0] if job_loc else None, candidate_value=cand_loc[0],
            ))
    else:
        checks.append(_check("location_mismatch", "unknown",
                             "Workplace or candidate location not stated — not treated as a mismatch."))

    # ---- work authorization ----
    job_requires = signals.get("requires_sponsorship")
    cand_auth = (candidate.get("work_authorization") or "").lower()
    if job_requires is True and cand_auth:
        if any(word in cand_auth for word in ("citizen", "permanent resident", "green card", "pr ", "indefinite stay")):
            checks.append(_check("work_authorization", "pass",
                                 "Candidate holds eligible work authorization."))
        elif "sponsor" in cand_auth or "visa" in cand_auth:
            checks.append(_check(
                "work_authorization", "fail",
                "Posting requires the candidate to already hold work authorization; "
                "the candidate's status may depend on sponsorship.",
                job_value="work authorization required", candidate_value=candidate.get("work_authorization"),
            ))
        else:
            checks.append(_check(
                "work_authorization", "conflict",
                "Posting requires work authorization; the candidate's stated status is not clearly eligible.",
                penalty=FILTER_PENALTIES["sponsorship_mismatch"],
                job_value="work authorization required", candidate_value=candidate.get("work_authorization"),
            ))
    elif job_requires is True:
        checks.append(_check("work_authorization", "unknown",
                             "Posting requires work authorization; the candidate has not stated a status."))
    else:
        checks.append(_check("work_authorization", "pass", "No work-authorization requirement stated."))

    # ---- sponsorship ----
    offers = signals.get("offers_sponsorship")
    needs = candidate.get("sponsorship_required")
    if needs and offers is False:
        checks.append(_check(
            "sponsorship", "fail",
            "Candidate requires visa sponsorship; the posting states it is not offered.",
            job_value="no sponsorship", candidate_value="sponsorship required",
        ))
    elif needs and offers is None:
        checks.append(_check(
            "sponsorship", "conflict",
            "Candidate requires sponsorship; the posting does not state whether it is offered.",
            penalty=FILTER_PENALTIES["sponsorship_mismatch"],
            job_value="not stated", candidate_value="sponsorship required",
        ))
    elif needs:
        checks.append(_check("sponsorship", "pass", "Posting offers sponsorship."))
    else:
        checks.append(_check("sponsorship", "pass", "Candidate does not require sponsorship."))

    # ---- employment type ----
    # Without a stated candidate preference we only flag mismatches the track
    # itself makes obvious (an internship for a senior candidate; an explicitly
    # stated part-time/contract role on a full-time track).
    job_type = signals.get("employment_type")
    if job_type in ("internship",) and (candidate.get("seniority") in ("senior", "lead", "principal")):
        checks.append(_check(
            "employment_type_mismatch", "fail",
            "Internship posting for a senior candidate.",
            job_value=job_type, candidate_value=candidate.get("seniority"),
        ))
    elif job_type in ("part_time", "contract", "temporary", "freelance"):
        if signals.get("employment_type_explicit"):
            checks.append(_check(
                "employment_type_mismatch", "conflict",
                f"Posting is {job_type.replace('_', ' ')}; the candidate's track is full-time.",
                penalty=FILTER_PENALTIES["employment_type_mismatch"],
                job_value=job_type,
            ))
        else:
            checks.append(_check("employment_type_mismatch", "unknown",
                                 f"Posting suggests {job_type.replace('_', ' ')} — not confirmed by a source."))
    else:
        checks.append(_check("employment_type_mismatch", "pass",
                             "Employment type is full-time or matches the track."))

    # ---- compensation ----
    salary = signals.get("salary") or {}
    cand_min = candidate.get("salary_minimum")
    if salary.get("stated") and cand_min:
        job_max = salary.get("max") or salary.get("min")
        if job_max is not None and job_max < cand_min:
            checks.append(_check(
                "compensation_below_minimum", "fail",
                f"Posted maximum ({job_max:,}) is below the candidate's stated minimum ({cand_min:,}).",
                job_value=job_max, candidate_value=cand_min,
            ))
        elif salary.get("min") and salary["min"] < cand_min <= (salary.get("max") or 0):
            checks.append(_check(
                "compensation_below_minimum", "conflict",
                "Posted range starts below the candidate's stated minimum.",
                penalty=FILTER_PENALTIES["compensation_below_minimum"],
                job_value=f"{salary.get('min'):,}-{salary.get('max'):,}", candidate_value=cand_min,
            ))
        else:
            checks.append(_check("compensation_below_minimum", "pass", "Posted range meets the stated minimum."))
    else:
        checks.append(_check("compensation_below_minimum", "unknown",
                             "Salary not stated in the posting or no minimum set by the candidate."))

    # ---- seniority ----
    jd_level = _SENIORITY_LEVELS.get(signals.get("seniority") or "unknown")
    cand_level = _SENIORITY_LEVELS.get(candidate.get("seniority") or "unknown")
    if jd_level is None or cand_level is None:
        checks.append(_check("seniority_mismatch", "unknown",
                             f"Seniority not determinable (job: {signals.get('seniority')}, "
                             f"candidate: {candidate.get('seniority')})."))
    else:
        diff = jd_level - cand_level
        if abs(diff) >= 2:
            direction = "below" if diff < 0 else "above"
            checks.append(_check(
                "seniority_mismatch", "conflict",
                f"Role is {abs(diff)} levels {direction} the candidate's seniority "
                f"({signals.get('seniority')} vs {candidate.get('seniority')}).",
                penalty=FILTER_PENALTIES["seniority_mismatch"],
                job_value=signals.get("seniority"), candidate_value=candidate.get("seniority"),
            ))
        elif diff == -1:
            checks.append(_check(
                "seniority_mismatch", "conflict",
                f"Slightly below the candidate's seniority ({signals.get('seniority')} vs "
                f"{candidate.get('seniority')}) — possible lateral-down.",
                penalty=FILTER_PENALTIES["seniority_mismatch"] // 2,
                job_value=signals.get("seniority"), candidate_value=candidate.get("seniority"),
            ))
        else:
            checks.append(_check("seniority_mismatch", "pass",
                                 f"Seniority aligns ({signals.get('seniority')} vs {candidate.get('seniority')})."))

    # ---- required skills ----
    required = signals.get("required_skills") or []
    missing_required: List[str] = []
    if required:
        cand_tokens = _skill_token_set(candidate.get("grounding_text") or "")
        for skill in required:
            if not _has_skill(cand_tokens, skill):
                missing_required.append(skill)
        if required and not any(_has_skill(cand_tokens, s) for s in required):
            checks.append(_check(
                "required_skills_missing", "fail",
                f"None of the {len(required)} required skills "
                f"({', '.join(required[:6])}) are evidenced in the candidate profile.",
                job_value=required[:12], candidate_value="no required skill found",
            ))
        elif missing_required:
            penalty = min(REQUIRED_SKILLS_MAX_PENALTY, FILTER_PENALTIES["required_skills_missing"] * len(missing_required))
            checks.append(_check(
                "required_skills_missing", "conflict",
                f"{len(missing_required)} of {len(required)} required skills are missing: "
                f"{', '.join(missing_required[:6])}.",
                penalty=penalty,
                job_value=required[:12], candidate_value=missing_required[:12],
            ))
        else:
            checks.append(_check("required_skills_missing", "pass",
                                 f"All {len(required)} required skills are evidenced."))
    else:
        checks.append(_check("required_skills_missing", "unknown",
                             "The posting does not state an explicit required-skill list."))

    fails = [c for c in checks if c["status"] == "fail"]
    conflicts = [c for c in checks if c["status"] == "conflict"]
    total_penalty = sum(c["penalty"] for c in checks)
    status = "filtered" if fails else ("penalized" if conflicts else "eligible")
    return {
        "status": status,
        "checks": checks,
        "total_penalty": total_penalty,
        "missing_required_skills": missing_required,
        "reasons": [c["detail"] for c in fails + conflicts],
    }


def find_duplicate_job(db: Session, user_id: int, job: Job) -> Optional[Job]:
    """
    Another row on the same board for the same role — the cross-source
    duplicate the discovery merge can miss (different ``dedupe_key`` per
    source). The *earliest* row is canonical; later rows are duplicates.
    """
    title_norm = (job.title_normalized or str(job.title or "").strip().lower()[:300]).strip()
    company_norm = (job.company_name_normalized or str(job.company or "").strip().lower()[:200]).strip()
    if not title_norm or not company_norm:
        return None
    row = (
        db.query(Job)
        .filter(
            Job.user_id == user_id,
            Job.id != job.id,
            Job.title_normalized == title_norm,
            Job.company_name_normalized == company_norm,
            Job.location == (job.location or ""),
        )
        .order_by(Job.id.asc())
        .first()
    )
    return row


def is_already_applied(db: Session, user_id: int, job: Job) -> bool:
    """This row is applied, or another row on the board for the same role is."""
    if job.status == "applied":
        return True
    title_norm = (job.title_normalized or str(job.title or "").strip().lower()[:300]).strip()
    company_norm = (job.company_name_normalized or str(job.company or "").strip().lower()[:200]).strip()
    if not title_norm or not company_norm:
        return False
    applied = (
        db.query(Job.id)
        .filter(
            Job.user_id == user_id,
            Job.id != job.id,
            Job.status == "applied",
            Job.title_normalized == title_norm,
            Job.company_name_normalized == company_norm,
        )
        .first()
    )
    return applied is not None


# --------------------------------------------------------------------------- #
# Stage 2 — deterministic score with feature contributions
# --------------------------------------------------------------------------- #

def _feature(key: str, label: str, *, value: Optional[float] = None, scored: bool = True,
             reason: str = "", evidence: Optional[List[Dict[str, Any]]] = None,
             missing: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    One rubric criterion. The contribution is derived from the *rounded*
    stored score, so the row is self-consistent: anyone who re-computes
    ``round(score * weight, 4)`` from the stored values gets the stored
    contribution back. That self-consistency is the reproducibility contract.
    """
    weight = FEATURE_WEIGHTS[key]
    score = None if value is None else round(max(0.0, min(100.0, float(value))), 1)
    return {
        "key": key,
        "label": label,
        "weight": weight,
        "score": score if scored else None,
        "contribution": round((score or 0.0) * weight, 4) if scored else 0.0,
        "scored": scored,
        "reason": reason,
        "evidence": evidence or [],
        "missing": missing or [],
    }


def _years_score(candidate: Dict[str, Any], signals: Dict[str, Any], jd: str) -> Tuple[Optional[float], str, List[Dict[str, Any]]]:
    cand_years = candidate.get("years")
    jd_years = signals.get("years_min")
    relevance = cosine_sim(tf(" ".join(
        " ".join([e["title"], e["description"]]) for e in candidate.get("experience", [])
    )), tf(jd))
    parts: List[float] = []
    reasons: List[str] = []
    evidence: List[Dict[str, Any]] = []
    if cand_years is not None and jd_years is not None:
        if cand_years >= jd_years:
            years_score = 90 + min(10, (cand_years - jd_years) * 2)
            reasons.append(f"{cand_years} recorded years vs {jd_years} required")
            evidence.append({"kind": "profile", "quote": f"~{cand_years} years of recorded experience",
                             "detail": "derived from experience durations"})
        else:
            years_score = max(20.0, 90.0 - (jd_years - cand_years) * 15.0)
            reasons.append(f"{cand_years} recorded years vs {jd_years} required — {jd_years - cand_years} short")
        parts.append(years_score)
    if cand_years is None and jd_years is not None:
        parts.append(50.0)
        reasons.append(f"posting asks for {jd_years}+ years; candidate years not determinable (neutral)")
    parts.append(relevance * 100.0)
    reasons.append(f"role-text relevance {relevance:.2f}")
    if evidence or cand_years is not None:
        evidence.append({"kind": "jd", "quote": (jd[:200] or "").strip(),
                         "detail": "job description excerpt"})
    value = sum(parts) / len(parts)
    return value, "; ".join(reasons), evidence


def deterministic_score(
    job: Job,
    signals: Dict[str, Any],
    candidate: Dict[str, Any],
    filters: Dict[str, Any],
    *,
    open_boards_same_company: int = 0,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Stage 2 — the reproducible score.

    Pure function of (job row, candidate view, stage-1 result, ``now``) under a
    fixed ``MATCHER_VERSION``: rerunning it on the same stored inputs returns
    the identical number and the identical feature contributions.
    """
    now = now or datetime.utcnow()
    jd = job.description or ""
    features: List[Dict[str, Any]] = []

    # ---- required skills ----
    required = signals.get("required_skills") or []
    cand_tokens = _skill_token_set(candidate.get("grounding_text") or "")
    matched_required: List[Dict[str, Any]] = []
    missing_required: List[Dict[str, Any]] = []
    if required:
        for skill in required:
            found = _has_skill(cand_tokens, skill)
            if found:
                matched_required.append({"name": skill, "required": True,
                                         "evidence": [{"kind": "profile", "quote": skill,
                                                        "detail": "present in candidate profile"}]})
            else:
                missing_required.append({"name": skill, "required": True, "severity": "blocking",
                                         "remediable": False})
        value = 100.0 * len(matched_required) / len(required)
        features.append(_feature(
            "required_skills", "Required skills", value=value,
            reason=f"{len(matched_required)} of {len(required)} required skills are evidenced in the profile.",
            evidence=[e for m in matched_required for e in m["evidence"]][:6],
            missing=[m["name"] for m in missing_required],
        ))
    else:
        features.append(_feature("required_skills", "Required skills", scored=False,
                                 reason="The posting does not state an explicit required-skill list."))

    # ---- preferred skills ----
    preferred = signals.get("preferred_skills") or []
    if preferred:
        matched_pref = [s for s in preferred if _has_skill(cand_tokens, s)]
        value = 100.0 * len(matched_pref) / len(preferred)
        features.append(_feature(
            "preferred_skills", "Preferred skills", value=value,
            reason=f"{len(matched_pref)} of {len(preferred)} preferred skills present (nice-to-have, not required).",
            evidence=[{"kind": "profile", "quote": s, "detail": "present in candidate profile"} for s in matched_pref[:5]],
            missing=[s for s in preferred if not _has_skill(cand_tokens, s)][:6],
        ))
    else:
        features.append(_feature("preferred_skills", "Preferred skills", scored=False,
                                 reason="No preferred ('nice to have') skills stated."))

    # ---- relevant experience ----
    exp_value, exp_reason, exp_evidence = _years_score(candidate, signals, jd)
    features.append(_feature("relevant_experience", "Relevant experience", value=exp_value,
                             reason=exp_reason, evidence=exp_evidence))

    # ---- seniority ----
    jd_level = _SENIORITY_LEVELS.get(signals.get("seniority") or "unknown")
    cand_level = _SENIORITY_LEVELS.get(candidate.get("seniority") or "unknown")
    if jd_level is None or cand_level is None:
        features.append(_feature("seniority", "Seniority", scored=False,
                                 reason="Seniority not determinable from the posting or the profile."))
    else:
        diff = abs(jd_level - cand_level)
        value = {0: 100.0, 1: 70.0, 2: 40.0}.get(diff, 15.0)
        features.append(_feature(
            "seniority", "Seniority", value=value,
            reason=f"Job {signals.get('seniority')} vs candidate {candidate.get('seniority')} "
                   f"(level gap {diff}).",
            evidence=[{"kind": "jd", "quote": job.title, "detail": "job title"}],
        ))

    # ---- industry ----
    job_industry = signals.get("industry")
    cand_industries = candidate.get("industries") or []
    if not job_industry:
        features.append(_feature("industry", "Industry", scored=False,
                                 reason="Industry not stated in the posting."))
    elif not cand_industries:
        features.append(_feature("industry", "Industry", scored=False,
                                 reason="No industries recorded for the candidate."))
    else:
        value = 100.0 if job_industry in cand_industries else 40.0
        features.append(_feature(
            "industry", "Industry", value=value,
            reason=(f"Industry {job_industry} matches the candidate's recorded industries."
                    if value == 100.0 else
                    f"Industry {job_industry} is outside the candidate's recorded "
                    f"industries ({', '.join(cand_industries)})."),
            evidence=[{"kind": "profile", "quote": ", ".join(cand_industries), "detail": "recorded industries"}],
        ))

    # ---- location (carries the stage-1 verdict) ----
    loc_check = next((c for c in filters["checks"] if c["key"] == "location_mismatch"), None)
    if loc_check and loc_check["status"] == "pass":
        features.append(_feature("location", "Location", value=100.0, reason=loc_check["detail"]))
    elif loc_check and loc_check["status"] == "conflict":
        features.append(_feature("location", "Location", value=40.0, reason=loc_check["detail"],
                                 missing=[str(loc_check.get("job_value") or "")]))
    else:
        features.append(_feature("location", "Location", scored=False,
                                 reason=loc_check["detail"] if loc_check else "Location not stated."))

    # ---- compensation ----
    salary = signals.get("salary") or {}
    cand_min = candidate.get("salary_minimum")
    if not salary.get("stated"):
        features.append(_feature("compensation", "Compensation", scored=False,
                                 reason="Compensation not stated in the posting."))
    elif cand_min:
        job_max = salary.get("max") or salary.get("min")
        if job_max is not None and job_max < cand_min:
            value = 0.0
            reason = f"Posted maximum {job_max:,} is below the candidate's stated minimum {cand_min:,}."
        else:
            value = 75.0
            reason = (f"Posted range {salary.get('min'):,}-{salary.get('max'):,} meets the stated minimum "
                      f"{cand_min:,}." if salary.get("min") else
                      f"Posted amount meets the stated minimum {cand_min:,}.")
        features.append(_feature("compensation", "Compensation", value=value, reason=reason,
                                 evidence=[{"kind": "jd", "quote": f"{salary.get('min'):,}-{salary.get('max'):,}",
                                            "detail": "stated compensation"}]))
    else:
        features.append(_feature("compensation", "Compensation", value=70.0,
                                 reason="Posting discloses compensation; the candidate has set no minimum (neutral)."))

    # ---- remote preference ----
    workplace = signals.get("workplace")
    remote_pref = (candidate.get("remote_preference") or "").lower()
    if workplace is None or workplace == "unknown":
        features.append(_feature("remote", "Remote preference", scored=False,
                                 reason="Workplace policy not stated."))
    elif not remote_pref:
        features.append(_feature("remote", "Remote preference", scored=False,
                                 reason="Candidate has not stated a remote preference."))
    else:
        if workplace == "remote":
            value, reason = (100.0, "Job is remote; the candidate's preference is met.") \
                if remote_pref in ("remote", "hybrid", "any", "remote-first") else (
                40.0, "Job is fully remote but the candidate prefers onsite.")
        elif workplace == "hybrid":
            value, reason = (80.0, "Job is hybrid — close to the stated preference.") \
                if remote_pref in ("hybrid", "any") else (60.0, "Job is hybrid; the candidate prefers " + remote_pref + ".")
        else:
            value, reason = (40.0, "Job is onsite; the candidate prefers remote work.") \
                if remote_pref in ("remote",) else (80.0, "Job is onsite, matching the candidate's onsite preference.")
        features.append(_feature("remote", "Remote preference", value=value, reason=reason))

    # ---- career trajectory ----
    targets = candidate.get("target_roles") or ([candidate.get("current_title")] if candidate.get("current_title") else [])
    jd_title_tokens = set(tokenize(job.title or ""))
    best_overlap = 0.0
    best_target = ""
    for target in targets:
        tokens = set(tokenize(target))
        if not tokens:
            continue
        overlap = len(tokens & jd_title_tokens) / len(tokens)
        if overlap > best_overlap:
            best_overlap, best_target = overlap, target
    if best_target:
        value = 90.0 if best_overlap >= 0.8 else (70.0 if best_overlap >= 0.5 else 45.0)
        jd_level = _SENIORITY_LEVELS.get(signals.get("seniority") or "unknown")
        cand_level = _SENIORITY_LEVELS.get(candidate.get("seniority") or "unknown")
        if jd_level is not None and cand_level is not None and jd_level <= cand_level - 2:
            value = min(value, 60.0)
            reason = (f"Title aligns with target '{best_target}', but the role is two levels below the "
                      f"candidate — a lateral-down move.")
        else:
            reason = (f"Role title aligns with the tracked target '{best_target}' "
                      f"(token overlap {best_overlap:.0%}).")
        features.append(_feature("career_trajectory", "Career trajectory", value=value, reason=reason,
                                 evidence=[{"kind": "profile", "quote": best_target,
                                            "detail": "tracked target role"}]))
    else:
        features.append(_feature("career_trajectory", "Career trajectory", scored=False,
                                 reason="No tracked target role to compare against."))

    # ---- freshness ----
    age_hours = signals.get("age_hours")
    if age_hours is None:
        features.append(_feature("freshness", "Posting freshness", scored=False,
                                 reason="Posting date not available."))
    else:
        days = age_hours / 24.0
        value = 100.0 if days <= 1 else 90.0 if days <= 3 else 75.0 if days <= 7 else \
            60.0 if days <= 14 else 40.0 if days <= 30 else 25.0
        features.append(_feature("freshness", "Posting freshness", value=value,
                                 reason=f"Posted ~{days:.0f} day(s) ago.",
                                 evidence=[{"kind": "job", "quote": str(job.posted_at),
                                            "detail": "posted_at"}]))

    # ---- hiring signal ----
    signals_list = list(signals.get("hiring_signals") or [])
    if open_boards_same_company >= 3:
        signals_list.append(f"{open_boards_same_company} open roles at {job.company}")
    value = 40.0 + 15.0 * len(signals_list)
    reason = ("Signals: " + "; ".join(signals_list) + ".") if signals_list else \
        "No urgency or hiring signals detected — neutral."
    features.append(_feature("hiring_signal", "Hiring signal", value=min(100.0, value), reason=reason))

    # ---- application friction ----
    friction = signals.get("friction") or {}
    if not friction.get("known"):
        value, reason = 50.0, "No application-form data — neutral assumption."
    else:
        value = 90.0
        bits = []
        if friction.get("requires_login"):
            value -= 25.0
            bits.append("requires login")
        if friction.get("required_fields", 0) >= 8:
            value -= 15.0
            bits.append(f"{friction['required_fields']} required fields")
        if friction.get("referral"):
            value -= 30.0
            bits.append("asks for a referral")
        reason = ("Known friction: " + ", ".join(bits) + ".") if bits else \
            "Low-friction application (no login, few required fields)."
    features.append(_feature("application_friction", "Application friction", value=max(0.0, value), reason=reason))

    # ---- aggregate ----
    scored = [f for f in features if f["scored"]]
    weight_sum = sum(f["weight"] for f in scored)
    raw = (sum(f["contribution"] for f in scored) / weight_sum) if weight_sum > 0 else 0.0
    penalty = int(filters.get("total_penalty") or 0)
    adjusted = max(0.0, min(100.0, raw - penalty))
    band = band_for(adjusted)

    return {
        "weights": dict(FEATURE_WEIGHTS),
        "criteria": features,
        "raw": round(raw, 1),
        "penalty": penalty,
        "overall": round(adjusted, 1),
        "band": band,
        "matched_skills": matched_required + [
            {"name": s, "required": False,
             "evidence": [{"kind": "profile", "quote": s, "detail": "present in candidate profile"}]}
            for s in (signals.get("preferred_skills") or []) if _has_skill(cand_tokens, s)
        ],
        "missing_skills": (missing_required + [
            {"name": s, "required": False, "severity": "minor", "remediable": True}
            for s in (signals.get("preferred_skills") or []) if not _has_skill(cand_tokens, s)
        ][:12]),
    }


def fingerprint_match(profile_sha256: str, job_description_sha256: str, scorer_version: str) -> str:
    """Stable id of the *inputs* of a score — same fingerprint ⇒ same number."""
    return _sha256(f"{profile_sha256}:{job_description_sha256}:{scorer_version}")


# --------------------------------------------------------------------------- #
# Stage 3 — AI evidence review (guardrailed)
# --------------------------------------------------------------------------- #

REVIEW_SCHEMA = SchemaSpec([
    FieldSpec("recommendation", "str", choices=RECOMMENDATIONS),
    FieldSpec("recommendation_reason", "str", min_length=10, max_length=400),
    FieldSpec("strengths", "list"),
    FieldSpec("requirements_review", "list"),
    FieldSpec("risks", "list"),
    FieldSpec("resume_emphasis", "list"),
    FieldSpec("application_action", "str", min_length=10, max_length=300),
])

REVIEW_OUTPUT_TOKENS = 1400


def _review_checks(candidate: Dict[str, Any], jd: str, band: str):
    """Guardrail checks for the stage-3 answer."""
    profile_norm = _norm_text(candidate.get("grounding_text") or "")
    jd_norm = _norm_text(jd)

    def check(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []

        def text_of(item: Any) -> str:
            if isinstance(item, str):
                return item
            if isinstance(item, dict):
                return str(item.get("claim") or item.get("requirement") or item.get("text") or "")
            return str(item)

        # Every claimed strength must be present in the candidate's own data.
        for strength in data.get("strengths") or []:
            claim = text_of(strength)
            token = _norm_text(claim)
            if token and token not in profile_norm:
                # Short claims: allow a multi-word claim whose every significant
                # word is present (the model paraphrases).
                words = [w for w in token.split(".") if len(w) > 3]
                if not (words and all(w in profile_norm for w in words)):
                    issues.append({"code": "unsupported_strength", "severity": "error",
                                   "field": "strengths", "value": claim[:80],
                                   "message": f"'{claim[:80]}' is not present in the candidate's profile."})

        # Explicit requirements must be in the posting; inferred ones must say so.
        for req in data.get("requirements_review") or []:
            req_text = text_of(req)
            basis = str(req.get("basis") or "").lower() if isinstance(req, dict) else ""
            if not basis or basis not in ("explicit", "inferred"):
                issues.append({"code": "missing_basis", "severity": "error",
                               "field": "requirements_review", "value": req_text[:80],
                               "message": f"'{req_text[:80]}' must be marked explicit or inferred."})
                continue
            if basis == "explicit":
                token = _norm_text(req_text)
                if token and token not in jd_norm:
                    issues.append({"code": "unsupported_requirement", "severity": "error",
                                   "field": "requirements_review", "value": req_text[:80],
                                   "message": f"'{req_text[:80]}' is not stated in the job description — "
                                              "mark it inferred or remove it."})

        # The recommendation must follow the deterministic band.
        recommendation = str(data.get("recommendation") or "")
        if recommendation not in RECOMMENDATION_BY_BAND.get(band, RECOMMENDATIONS):
            issues.append({"code": "recommendation_mismatch", "severity": "error",
                           "field": "recommendation", "value": recommendation,
                           "message": f"band '{band}' does not support recommendation '{recommendation}' "
                                      f"(allowed: {', '.join(RECOMMENDATION_BY_BAND.get(band, []))})."})

        # High fit without candidate evidence is not allowed.
        if band in ("strong", "good"):
            strengths = data.get("strengths") or []
            with_evidence = [s for s in strengths if _norm_text(text_of(s))]
            if len(with_evidence) < 3:
                issues.append({"code": "high_fit_missing_evidence", "severity": "error",
                               "field": "strengths",
                               "value": str(len(with_evidence)),
                               "message": f"a {band} match needs at least 3 strengths grounded in the "
                                          "candidate's own data."})

        # No probability-of-interview language, ever (at this stage).
        blob = json.dumps(data, default=str)
        for m in _PROBABILITY_RE.finditer(blob):
            issues.append({"code": "probability_claim", "severity": "error",
                           "field": "$", "value": m.group(0)[:80],
                           "message": "The review must not claim an interview probability or guarantee — "
                                      "this is an estimated fit, not a calibrated likelihood."})
        return issues

    return check


def _prompt_candidate_block(candidate: Dict[str, Any]) -> str:
    """The minimized candidate slice sent to the model — no contact details."""
    block = {
        "skills": candidate.get("skills", [])[:30],
        "current_title": candidate.get("current_title"),
        "seniority": candidate.get("seniority"),
        "years": candidate.get("years"),
        "experience": [
            {"title": e.get("title"), "duration": e.get("duration"),
             "description": (e.get("description") or "")[:200]}
            for e in (candidate.get("experience") or [])[:8]
        ],
        "industries": candidate.get("industries"),
        "target_roles": candidate.get("target_roles"),
        "remote_preference": candidate.get("remote_preference"),
        "summary_skills_text": " ".join(candidate.get("skills", []))[:600],
    }
    return json.dumps(block, default=str)


async def review_match_ai(
    job: Job,
    candidate: Dict[str, Any],
    deterministic: Dict[str, Any],
    filters: Dict[str, Any],
    *,
    db=None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Stage 3 — the model reviews the deterministic result under guardrails.

    Returns ``{status: ok|unavailable|rejected, review, guardrail, error}``.
    The deterministic score is *never replaced* by the model: the AI adds the
    evidence-backed verdict on top of the reproducible number.
    """
    from app.services.ai_client import fit_prompt_part, input_budget_chars

    jd = job.description or ""
    band = deterministic.get("band") or "unknown"
    criteria_brief = "; ".join(
        f"{f['label']}: {f['reason']}" for f in deterministic.get("criteria", []) if f.get("scored")
    )
    filters_brief = "; ".join(
        f"{c['key']}={c['status']} {c['detail']}" for c in filters.get("checks", [])
        if c["status"] in ("conflict", "fail", "unknown")
    )
    budget = input_budget_chars(db=db, user_id=user_id)
    safe_jd, _ = fit_prompt_part(strip_ai_artifacts(jd), budget, label="matching.jd")
    cand_json, _ = fit_prompt_part(_prompt_candidate_block(candidate), budget, label="matching.candidate")
    det_json, _ = fit_prompt_part(json.dumps({
        "score": deterministic.get("overall"), "band": band,
        "criteria": criteria_brief, "missing_skills": [m.get("name") for m in deterministic.get("missing_skills", [])][:12],
    }, default=str), budget, label="matching.deterministic")

    prompt = f"""You are the evidence reviewer for a job match. A deterministic scorer already produced a reproducible score; your job is to review it, not replace it.

Deterministic pre-assessment (authoritative number — do not re-derive a score):
{det_json}

Hard-filter notes: {filters_brief or "no conflicts detected"}
Candidate (minimized — no contact details):
{cand_json}

Job posting:
\"\"\"{safe_jd}\"\"\"

Hard rules — an automated checker rejects violations:
- "strengths" must each be present in the candidate data above (claim, 5-15 words).
- "requirements_review" entries must say basis "explicit" (worded in the posting) or "inferred" (you deduced it); explicit ones must be phrased as the posting phrases them.
- "recommendation" must follow the band: band strong/good ⇒ apply or apply_with_tailoring; weak ⇒ hold or skip.
- High bands (strong/good) need at least 3 grounded strengths.
- NEVER state or imply a probability, percentage or guarantee of an interview/offer. You are recommending, not predicting.
- "risks" names what could still go wrong (e.g. unstated salary, unclear work-authorization policy, seniority stretch).
- "resume_emphasis" lists 2-4 concrete resume emphases for THIS posting.
- "application_action" is the next step the user should take, one sentence.

Return JSON:
{{"recommendation": "apply|apply_with_tailoring|hold|skip", "recommendation_reason": "why", "strengths": ["..."], "requirements_review": [{{"requirement": "...", "basis": "explicit|inferred", "met": true, "evidence": "..."}}], "risks": ["..."], "resume_emphasis": ["..."], "application_action": "..."}}"""

    try:
        data, report = await run_guarded_task(
            "match_review",
            system=("You are a strict, evidence-bound technical recruiter. You state only what the "
                    "candidate's recorded data proves and the posting says. You never predict "
                    "interview odds and never inflate the match to be encouraging."),
            prompt=prompt,
            schema=REVIEW_SCHEMA,
            checks=[_review_checks(candidate, jd, band)],
            db=db,
            user_id=user_id,
            temperature=0.1,
            max_tokens=REVIEW_OUTPUT_TOKENS,
        )
    except AIUnavailableError as exc:
        return {"status": "unavailable", "review": None, "guardrail": None, "error": exc.payload()}
    except GuardrailError as exc:
        return {"status": "rejected", "review": None,
                "guardrail": {"passed": False, "issues": exc.issues}, "error": exc.payload()}

    strengths = [
        (s if isinstance(s, dict) else {"claim": str(s), "evidence": ""})
        for s in (data.get("strengths") or [])[:8]
    ]
    requirements = []
    for req in (data.get("requirements_review") or [])[:12]:
        if not isinstance(req, dict):
            req = {"requirement": str(req), "basis": "inferred", "met": False, "evidence": ""}
        requirements.append({
            "requirement": strip_ai_artifacts(str(req.get("requirement") or ""))[:160],
            "basis": "explicit" if str(req.get("basis") or "").lower() == "explicit" else "inferred",
            "met": bool(req.get("met", False)),
            "evidence": strip_ai_artifacts(str(req.get("evidence") or ""))[:240],
        })
    review = {
        "recommendation": str(data.get("recommendation") or ""),
        "recommendation_reason": strip_ai_artifacts(str(data.get("recommendation_reason") or ""))[:400],
        "strengths": [{"claim": strip_ai_artifacts(str(s.get("claim") or ""))[:160],
                       "evidence": strip_ai_artifacts(str(s.get("evidence") or ""))[:240]} for s in strengths],
        "requirements_review": requirements,
        "risks": [strip_ai_artifacts(str(r))[:200] for r in (data.get("risks") or [])[:8]],
        "resume_emphasis": [strip_ai_artifacts(str(r))[:200] for r in (data.get("resume_emphasis") or [])[:4]],
        "application_action": strip_ai_artifacts(str(data.get("application_action") or ""))[:300],
        "prompt_version": REVIEW_PROMPT_VERSION,
        "model": str(report.model or ""),
        "attempts": report.attempts,
    }
    return {"status": "ok", "review": review, "guardrail": report.to_dict(), "error": None}


# --------------------------------------------------------------------------- #
# Writer — stage orchestration + durable match row
# --------------------------------------------------------------------------- #

def _resolve_candidate(db: Session, user_id: int, persona: Optional[Persona] = None
                       ) -> Tuple[Dict[str, Any], Optional[int], str]:
    """
    The scoring inputs' identity: (minimal view, profile row id, profile sha256).

    Prefers the versioned ``candidate_profiles`` document (its
    ``document_sha256`` is the version stamp); falls back to the legacy
    ``profiles`` row, hashed canonically. No profile at all → empty view,
    empty sha (the writer then records ``insufficient_data``).
    """
    from app.services.candidate_profile import get_current_profile

    cp = get_current_profile(db, user_id, persona.id if persona else None)
    if cp is not None and cp.document:
        return minimal_candidate_view(cp.document, persona), cp.id, (cp.document_sha256 or _sha256(json.dumps(cp.document, sort_keys=True, default=str)))
    legacy = db.query(Profile).filter(Profile.user_id == user_id).order_by(Profile.created_at.desc()).first()
    if legacy is not None and legacy.data:
        sha = _sha256(json.dumps(legacy.data, sort_keys=True, default=str))
        return minimal_candidate_view(legacy.data, persona), legacy.id, sha
    return {}, None, ""


def _staleness_for(row: MatchResult, db: Session) -> str:
    """Lazily-computed staleness on read (contract 06 §5)."""
    from app.services.candidate_profile import get_current_profile

    if row.staleness != "fresh":
        return row.staleness
    if row.profile_id:
        current = get_current_profile(db, row.user_id, row.persona_id)
        if current is not None and current.id != row.profile_id:
            return "profile_changed"
    if row.job_description_sha256:
        job = db.query(Job).filter(Job.id == row.job_id, Job.user_id == row.user_id).first()
        if job is not None and _sha256(job.description or "") != row.job_description_sha256:
            return "job_changed"
    return "fresh"


def _match_to_dict(row: MatchResult) -> Dict[str, Any]:
    payload = {
        "match_id": row.id,
        "score": float(row.score),
        "band": row.band,
        "score_source": row.score_source,
        "score_source_label": SCORE_SOURCE_LABELS.get(row.score_source, row.score_source),
        "confidence": row.confidence,
        "reason": row.reason,
        "scorer": row.scorer,
        "scorer_version": row.scorer_version,
        "model": row.model,
        "prompt_version": row.prompt_version,
        "profile_id": row.profile_id,
        "profile_sha256": row.profile_sha256,
        "job_description_sha256": row.job_description_sha256,
        "fingerprint": fingerprint_match(row.profile_sha256, row.job_description_sha256, row.scorer_version),
        "hard_filters": row.hard_filters or {},
        "rubric": row.rubric or {},
        "matched_skills": row.matched_skills or [],
        "missing_skills": row.missing_skills or [],
        "ai_review": row.ai_review,
        "guardrail_report": row.guardrail_report or {},
        "flags": row.flags or {},
        "staleness": row.staleness,
        "computed_at": row.computed_at,
        "expires_at": row.expires_at,
        "disclaimer": DISCLAIMER,
        "calibrated_probability": False,
    }
    return payload


SCORE_SOURCE_LABELS: Dict[str, str] = {
    "ai": "AI-verified match",
    "preliminary": "Deterministic estimate (no AI review)",
    "pending": "Not scored yet",
    "rejected": "AI review rejected by the accuracy guard",
    "insufficient_data": "Not enough candidate/job text to score",
    "funding_context": "Funding-radar posting (no job description)",
    "unscored": "Not scored",
}


async def compute_match(
    db: Session,
    user_id: int,
    job: Job,
    *,
    ai: bool = True,
    persona: Optional[Persona] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Run all three stages for one job and persist the result.

    * Idempotent (contract 06 invariant 7): identical inputs (profile sha, JD
      sha, scorer version, persona) return the stored row and charge nothing.
    * Re-scores insert a new row and supersede the previous current one.
    * AI is plan-gated by the caller (``ai=False`` on free tier) and, when it
      fails, the deterministic result is still stored — provenance-labelled,
      never papered over.
    """
    now = datetime.utcnow()
    profile_view, profile_id, profile_sha = _resolve_candidate(db, user_id, persona)
    jd_sha = _sha256(job.description or "")
    jd_text = (job.description or "").strip()

    has_candidate_text = bool((profile_view.get("grounding_text") or "").strip())
    if not has_candidate_text or not jd_text:
        existing = _find_existing(db, user_id, job.id, persona.id if persona else None, profile_sha, jd_sha)
        if existing is not None and not force:
            return _stored_result(existing)
        reason = ("No candidate profile text to match against — upload/review a resume first."
                  if not has_candidate_text else "The posting has no description text to score.")
        row = _insert_match(
            db, user_id, job, persona, profile_id, profile_sha, jd_sha,
            scorer="deterministic", score_source="insufficient_data", score=0.0, band="unknown",
            confidence=None, reason=reason,
            hard_filters={"status": "unknown", "checks": [], "total_penalty": 0, "reasons": []},
            rubric={"weights": dict(FEATURE_WEIGHTS), "criteria": [], "raw": 0.0, "penalty": 0,
                    "overall": 0.0, "band": "unknown", "matched_skills": [], "missing_skills": []},
            matched_skills=[], missing_skills=[], ai_review=None, guardrail={},
            flags={"insufficient_text": True},
            now=now,
        )
        return _stored_result(row)

    signals = parse_job_signals(job, now=now)
    duplicate_of = find_duplicate_job(db, user_id, job)
    applied = is_already_applied(db, user_id, job)
    filters = run_hard_filters(
        job, signals, profile_view,
        is_duplicate=duplicate_of is not None,
        duplicate_of=duplicate_of.id if duplicate_of else None,
        already_applied=applied,
        now=now,
    )

    # How many open roles the company has on this board (hiring signal input).
    open_same_company = 0
    if job.company_name_normalized:
        open_same_company = (
            db.query(Job)
            .filter(Job.user_id == user_id,
                    Job.company_name_normalized == job.company_name_normalized,
                    Job.status.in_(("discovered", "queued", "preparing", "needs_input", "ready_to_apply")),
                    )
            .count()
        )

    det = deterministic_score(job, signals, profile_view, filters,
                              open_boards_same_company=open_same_company, now=now)

    existing = _find_existing(db, user_id, job.id, persona.id if persona else None, profile_sha, jd_sha)
    if existing is not None and not force:
        return _stored_result(existing)

    # ---------------- stage 3 ----------------
    ai_result: Dict[str, Any] = {"status": "skipped", "review": None, "guardrail": None, "error": None}
    if ai:
        ai_result = await review_match_ai(job, profile_view, det, filters, db=db, user_id=user_id)
        if ai_result["status"] == "ok":
            if db is not None and user_id is not None:
                from app.core.entitlements import increment_usage
                try:
                    increment_usage(db, user_id, "job_analysis_per_month", 1)
                except Exception:  # quota surfaced elsewhere; the match still stands
                    pass

    if filters["status"] == "filtered":
        # A filtered job is never a high-fit recommendation: the score is
        # capped at the top of the weak band and the band is derived from the
        # stored number, so the row stays self-consistent.
        score = float(min(det["overall"], 49.0))
        band = band_for(score)
        reason = "Filtered out by hard filters: " + " ".join(filters["reasons"][:3])
    else:
        band = det["band"]
        score = float(det["overall"])
        parts = [f"Estimated fit {det['raw']:.0f}/100 from {sum(1 for f in det['criteria'] if f['scored'])} scored features"]
        if det["penalty"]:
            parts.append(f"minus {det['penalty']} penalty for: " + "; ".join(filters["reasons"][:2]))
        if ai_result["status"] == "ok":
            parts.append(f"evidence review recommends: {ai_result['review']['recommendation']}")
        reason = " ".join(parts)

    # ---------------- evidence invariant ----------------
    # A high-fit match must have supporting candidate evidence (matched
    # required/preferred skills with profile evidence, or a grounded AI
    # strength). Without it, the band is demoted and the gap is flagged.
    flags: Dict[str, Any] = {}
    if filters["status"] == "filtered":
        flags["hard_filtered"] = True
    if duplicate_of is not None:
        flags["duplicate"] = True
        flags["duplicate_of"] = duplicate_of.id
    if applied and job.status != "applied":
        flags["already_applied"] = True
    has_candidate_evidence = any(
        m.get("evidence") for m in det["matched_skills"]
    ) or (
        ai_result["status"] == "ok"
        and any(s.get("claim") for s in (ai_result["review"] or {}).get("strengths", []))
    )
    if band in ("strong", "good") and not has_candidate_evidence:
        band = "possible"
        flags["evidence_gap"] = True
        reason += " (band demoted: no supporting candidate evidence on file)"
    if not jd_text:
        flags["insufficient_text"] = True
    if ai_result["status"] == "unavailable":
        flags["ai_unavailable"] = True
        flags["ai_error"] = (ai_result.get("error") or {}).get("reason")
    elif ai_result["status"] == "rejected":
        flags["ai_rejected"] = True

    # ---------------- confidence ----------------
    unscored = [f for f in det["criteria"] if not f["scored"]]
    confidence = max(0.2, 0.5 - 0.05 * len(unscored))
    if ai_result["status"] == "ok":
        confidence = min(0.9, confidence + 0.25)
    elif ai_result["status"] == "rejected":
        confidence = max(0.2, confidence - 0.1)
    confidence = round(confidence, 2)

    scorer = "hybrid" if ai_result["status"] == "ok" else "deterministic"
    score_source = "ai" if ai_result["status"] == "ok" else \
        ("rejected" if ai_result["status"] == "rejected" else "preliminary")
    if ai_result["status"] == "rejected":
        # The number is still the deterministic one — the label says the AI
        # verdict did not clear the guard, and the guard report is stored.
        score_source = "preliminary"
        flags["ai_rejected_detail"] = ai_result.get("error")

    guardrail = ai_result.get("guardrail") or {}
    if ai_result["status"] != "ok":
        guardrail = guardrail or {"passed": True, "issues": [], "checks": ["deterministic_only"],
                                  "note": "AI review did not run; the score is the deterministic estimate."}

    rubric = {
        "weights": det["weights"],
        "criteria": det["criteria"],
        "raw": det["raw"],
        "penalty": det["penalty"],
        "overall": det["overall"],
        "band": band,
        "recommendation": (ai_result["review"]["recommendation"]
                           if ai_result["status"] == "ok" else _deterministic_recommendation(band)),
        "recommendation_basis": "ai_review" if ai_result["status"] == "ok" else "deterministic_band",
    }

    row = _insert_match(
        db, user_id, job, persona, profile_id, profile_sha, jd_sha,
        scorer=scorer, score_source=score_source, score=score, band=band,
        confidence=confidence, reason=reason,
        hard_filters=filters, rubric=rubric,
        matched_skills=det["matched_skills"], missing_skills=det["missing_skills"],
        ai_review=ai_result["review"], guardrail=guardrail, flags=flags,
        now=now,
    )
    return _stored_result(row)


def _deterministic_recommendation(band: str) -> str:
    return {"strong": "apply", "good": "apply_with_tailoring",
            "possible": "hold", "weak": "skip"}.get(band, "hold")


def _find_existing(db: Session, user_id: int, job_id: int, persona_id: Optional[int],
                   profile_sha: str, jd_sha: str) -> Optional[MatchResult]:
    q = db.query(MatchResult).filter(
        MatchResult.user_id == user_id,
        MatchResult.job_id == job_id,
        MatchResult.profile_sha256 == profile_sha,
        MatchResult.job_description_sha256 == jd_sha,
        MatchResult.scorer_version == MATCHER_VERSION,
    )
    if persona_id is None:
        q = q.filter(MatchResult.persona_id.is_(None))
    else:
        q = q.filter(MatchResult.persona_id == persona_id)
    return q.order_by(MatchResult.id.desc()).first()


def _insert_match(
    db: Session,
    user_id: int,
    job: Job,
    persona: Optional[Persona],
    profile_id: Optional[int],
    profile_sha: str,
    jd_sha: str,
    *,
    scorer: str,
    score_source: str,
    score: float,
    band: str,
    confidence: Optional[float],
    reason: str,
    hard_filters: Dict[str, Any],
    rubric: Dict[str, Any],
    matched_skills: List[Dict[str, Any]],
    missing_skills: List[Dict[str, Any]],
    ai_review: Optional[Dict[str, Any]],
    guardrail: Dict[str, Any],
    flags: Dict[str, Any],
    now: datetime,
) -> MatchResult:
    persona_id = persona.id if persona else None
    previous = (
        db.query(MatchResult)
        .filter(MatchResult.user_id == user_id, MatchResult.job_id == job.id,
                MatchResult.is_current.is_(True),
                MatchResult.persona_id.is_(persona_id) if persona_id is None
                else MatchResult.persona_id == persona_id)
        .order_by(MatchResult.id.desc())
        .first()
    )
    row = MatchResult(
        user_id=user_id,
        job_id=job.id,
        persona_id=persona_id,
        profile_id=profile_id,
        profile_sha256=profile_sha,
        job_description_sha256=jd_sha,
        scorer=scorer,
        scorer_version=MATCHER_VERSION,
        model=ai_review.get("model") if ai_review else None,
        prompt_version=ai_review.get("prompt_version") if ai_review else None,
        score=score,
        band=band,
        confidence=confidence,
        score_source=score_source,
        reason=reason[:2000],
        hard_filters=hard_filters,
        rubric=rubric,
        matched_skills=matched_skills,
        missing_skills=missing_skills,
        ai_review=ai_review,
        guardrail_report=guardrail,
        flags=flags,
        is_current=True,
        staleness="fresh",
        computed_at=now,
        expires_at=now + timedelta(hours=72),
        created_at=now,
    )
    db.add(row)
    db.flush()
    if previous is not None:
        previous.is_current = False
        previous.superseded_by_id = row.id
    try:
        from app.services.events import record_job_event
        record_job_event(
            db, user_id=user_id, job_id=job.id, stage="matched", status="info",
            message=f"Match computed: estimated fit {score:.0f}/100 ({band}, {score_source})",
            meta={"match_id": row.id, "band": band, "score_source": score_source,
                  "scorer_version": MATCHER_VERSION,
                  "hard_filtered": bool(flags.get("hard_filtered"))},
            commit=False,
        )
    except Exception:
        pass
    db.commit()
    db.refresh(row)
    return row


def _stored_result(row: MatchResult) -> Dict[str, Any]:
    payload = _match_to_dict(row)
    payload["reused"] = True
    return payload


def get_current_match(db: Session, user_id: int, job_id: int,
                      persona_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    q = db.query(MatchResult).filter(
        MatchResult.user_id == user_id, MatchResult.job_id == job_id,
        MatchResult.is_current.is_(True),
    )
    if persona_id is None:
        q = q.filter(MatchResult.persona_id.is_(None))
    else:
        q = q.filter(MatchResult.persona_id == persona_id)
    row = q.order_by(MatchResult.id.desc()).first()
    if row is None:
        return None
    payload = _match_to_dict(row)
    payload["staleness"] = _staleness_for(row, db)
    return payload


def list_match_history(db: Session, user_id: int, job_id: int,
                       persona_id: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
    q = db.query(MatchResult).filter(
        MatchResult.user_id == user_id, MatchResult.job_id == job_id,
    )
    if persona_id is None:
        q = q.filter(MatchResult.persona_id.is_(None))
    else:
        q = q.filter(MatchResult.persona_id == persona_id)
    rows = q.order_by(MatchResult.id.desc()).limit(limit).all()
    out = []
    for r in rows:
        d = _match_to_dict(r)
        d["is_current"] = r.is_current
        d.pop("rubric", None)
        d.pop("hard_filters", None)
        d.pop("ai_review", None)
        d.pop("guardrail_report", None)
        out.append(d)
    return out


def list_best_matches(db: Session, user_id: int, *, limit: int = 50,
                      min_score: Optional[float] = None,
                      band: Optional[str] = None,
                      include_filtered: bool = False) -> List[Dict[str, Any]]:
    """
    The cross-job "best matches" read.

    Reads current match rows (one per job) and demotes — without hiding —
    anything the user has corrected (``not_relevant`` feedback) or that is
    hard-filtered, so the user's corrections change the ranking visibly.
    """
    q = db.query(MatchResult).filter(
        MatchResult.user_id == user_id, MatchResult.is_current.is_(True),
    )
    if min_score is not None:
        q = q.filter(MatchResult.score >= min_score)
    if band:
        q = q.filter(MatchResult.band == band)
    rows = q.order_by(MatchResult.score.desc(), MatchResult.id.desc()).limit(limit * 2).all()

    corrected = {
        r[0] for r in db.query(MatchFeedback.job_id).filter(
            MatchFeedback.user_id == user_id, MatchFeedback.kind == "not_relevant",
        ).all()
    }
    jobs = {
        r.id: r for r in db.query(Job).filter(
            Job.user_id == user_id, Job.id.in_([m.job_id for m in rows] or [0]),
        ).all()
    } if rows else {}

    out: List[Dict[str, Any]] = []
    for m in rows:
        job = jobs.get(m.job_id)
        if job is None:
            continue
        flags = m.flags or {}
        if not include_filtered and (flags.get("hard_filtered") or job.status == "skipped"):
            continue
        entry = {
            "job_id": m.job_id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "status": job.status,
            "score": float(m.score),
            "band": m.band,
            "score_source": m.score_source,
            "score_source_label": SCORE_SOURCE_LABELS.get(m.score_source, m.score_source),
            "confidence": m.confidence,
            "recommendation": (m.rubric or {}).get("recommendation"),
            "missing_required_skills": [
                s["name"] for s in (m.missing_skills or []) if s.get("required")
            ][:8],
            "flags": {k: v for k, v in flags.items() if k in
                      ("hard_filtered", "duplicate", "evidence_gap", "ai_unavailable", "already_applied")},
            "user_corrected": m.job_id in corrected,
            "staleness": m.staleness,
            "computed_at": m.computed_at,
            "match_id": m.id,
            "disclaimer": DISCLAIMER,
        }
        out.append(entry)
        if len(out) >= limit:
            break
    # User corrections sink to the bottom of the list, below equal scores.
    out.sort(key=lambda e: (e["user_corrected"], -e["score"]))
    return out[:limit]


# --------------------------------------------------------------------------- #
# Feedback — user corrections and the calibration dataset
# --------------------------------------------------------------------------- #

def record_feedback(
    db: Session,
    user_id: int,
    job: Job,
    kind: str,
    *,
    reason: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if kind not in MATCH_FEEDBACK_KINDS:
        raise ValueError(f"kind must be one of {list(MATCH_FEEDBACK_KINDS)}")
    current = (
        db.query(MatchResult)
        .filter(MatchResult.user_id == user_id, MatchResult.job_id == job.id,
                MatchResult.is_current.is_(True))
        .order_by(MatchResult.id.desc())
        .first()
    )
    row = MatchFeedback(
        user_id=user_id,
        job_id=job.id,
        match_id=current.id if current else None,
        kind=kind,
        reason=(reason or "")[:1000] or None,
        meta=meta or {},
        scorer_version=current.scorer_version if current else MATCHER_VERSION,
        created_at=datetime.utcnow(),
    )
    db.add(row)
    try:
        from app.services.events import record_job_event
        record_job_event(
            db, user_id=user_id, job_id=job.id, stage="match_feedback", status="info",
            message=f"Match feedback recorded: {kind}" + (f" — {reason}" if reason else ""),
            meta={"kind": kind, "match_id": row.match_id},
            commit=False,
        )
    except Exception:
        pass
    db.commit()
    db.refresh(row)
    return {
        "ok": True,
        "feedback_id": row.id,
        "kind": kind,
        "is_outcome": kind in MATCH_FEEDBACK_OUTCOME_KINDS,
        "calibration": calibration_status(db, user_id),
    }


def latest_feedback(db: Session, user_id: int, job_id: int) -> Optional[Dict[str, Any]]:
    row = (
        db.query(MatchFeedback)
        .filter(MatchFeedback.user_id == user_id, MatchFeedback.job_id == job_id)
        .order_by(MatchFeedback.id.desc())
        .first()
    )
    if row is None:
        return None
    return {"id": row.id, "kind": row.kind, "reason": row.reason, "meta": row.meta or {},
            "match_id": row.match_id, "created_at": row.created_at}


def calibration_status(db: Session, user_id: int,
                       scorer_version: Optional[str] = None) -> Dict[str, Any]:
    """
    Whether a calibrated interview probability *could* be offered.

    It cannot, until at least ``MIN_OUTCOMES_FOR_CALIBRATION`` recorded
    application outcomes (applied → rejected/interview) exist for the scorer
    version — and the response says exactly that, with the counts. This is the
    gate that keeps the product from ever claiming a calibrated probability
    it cannot support.
    """
    version = scorer_version or MATCHER_VERSION
    rows = (
        db.query(MatchFeedback.kind)
        .filter(MatchFeedback.user_id == user_id, MatchFeedback.scorer_version == version)
        .all()
    )
    kinds = [r[0] for r in rows]
    applied = kinds.count("applied")
    rejected = kinds.count("rejected")
    interviews = kinds.count("interview")
    outcomes = rejected + interviews
    sufficient = outcomes >= MIN_OUTCOMES_FOR_CALIBRATION
    return {
        "calibrated": False,  # never true in this release — the gate is the point
        "calibrated_probability": False,
        "sufficient_outcome_data": sufficient,
        "minimum_outcomes_required": MIN_OUTCOMES_FOR_CALIBRATION,
        "recorded_outcomes": {"applied": applied, "rejected": rejected, "interview": interviews},
        "scorer_version": version,
        "reason": (
            f"{outcomes}/{MIN_OUTCOMES_FOR_CALIBRATION} application outcomes recorded for scorer "
            f"{version} — too few to calibrate. Scores remain an estimated fit, "
            "not an interview probability."
            if not sufficient else
            f"{outcomes} outcomes recorded for scorer {version} — calibration analysis "
            "is possible but not yet enabled in this release."
        ),
    }
