import json
import math
import re
from typing import Any, Dict, List, Tuple  # noqa: F401

from typing import Optional

from app.services.ai_client import AIClientError, chat_completion
from app.services.ai_guardrails import (
    AIUnavailableError,
    FieldSpec,
    GuardrailError,
    SchemaSpec,
    run_guarded_task,
    strip_ai_artifacts,
)

STOPWORDS = {"the", "and", "for", "with", "a", "an", "in", "on", "of", "to", "is", "are", "as", "at", "by", "from", "or"}

SKILL_SYNONYMS = {
    "js": "javascript", "nodejs": "nodejs", "node": "nodejs",
    "k8s": "kubernetes", "golang": "go", "py": "python", "ts": "typescript",
    "postgres": "postgresql", "psql": "postgresql", "reactjs": "react",
    "nextjs": "nextjs",
}

def _normalize_token(t: str) -> str:
    """Lowercase, strip trailing punctuation, unify separators like c++/ci/cd/node.js."""
    t = t.strip(".,;:!?()[]{}'\"").lower()
    t = SKILL_SYNONYMS.get(t, t)
    return t

# Tokens that are legitimately short but cannot be dropped (they are skills).
SHORT_TECH_TOKENS = {"c++", "c#", "go", "r", "js", "ts", "ai", "ml", "qa", "ui", "ux"}


def tokenize(text: str) -> List[str]:
    # NOTE: no '.' in the char class — previously "Python." tokenized as
    # "python." and never matched the skill "python", zeroing many scores.
    tokens = re.findall(r"[a-zA-Z0-9\+#/]+", text.lower())
    return [
        _normalize_token(t)
        for t in tokens
        if t not in STOPWORDS and (len(t.strip("+#/")) > 1 or t in SHORT_TECH_TOKENS)
    ]

def tf(text: str) -> Dict[str, float]:
    toks = tokenize(text)
    total = len(toks) or 1
    freq = {}
    for t in toks:
        freq[t] = freq.get(t, 0) + 1
    return {k: v/total for k,v in freq.items()}

def cosine_sim(a: Dict[str,float], b: Dict[str,float]) -> float:
    keys = set(a) | set(b)
    dot = sum(a.get(k,0)*b.get(k,0) for k in keys)
    na = math.sqrt(sum(v*v for v in a.values())) or 1
    nb = math.sqrt(sum(v*v for v in b.values())) or 1
    return dot/(na*nb)

def _extract_profile_text(profile: Dict[str, Any]) -> str:
    return " ".join([
        " ".join(profile.get("skills", [])),
        profile.get("summary", ""),
        " ".join([e.get("description", "") + " " + e.get("title", "") for e in profile.get("experience", [])]),
        " ".join([p.get("description", "") for p in profile.get("projects", [])]),
    ])

def _extract_years(text: str) -> int:
    m = re.search(r"(\d+)\+?\s*years", text, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return 0
    return 0

def _detect_seniority(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["principal", "staff", "architect", "director"]):
        return "principal"
    if "senior" in t or "sr." in t:
        return "senior"
    if "lead" in t:
        return "lead"
    if "mid" in t or "intermediate" in t:
        return "mid"
    if "junior" in t or "entry" in t:
        return "junior"
    return "unknown"

def _detailed_breakdown(profile: Dict[str, Any], jd: str) -> Dict[str, Any]:
    """
    Transparent job intelligence breakdown.
    Returns dict with skills, experience, seniority, location, salary, education, etc.
    """
    profile_text = _extract_profile_text(profile)
    jd_lower = jd.lower()
    profile_lower = profile_text.lower()

    # Skills
    profile_skills = [s.lower() for s in profile.get("skills", [])]
    jd_tokens = set(tokenize(jd))
    prof_tokens = set(tokenize(profile_text))
    common = jd_tokens & prof_tokens
    # Try to extract skill-like tokens from JD (simple heuristic)
    # Strong matches: profile skills present in JD
    strong_matches = [s for s in profile_skills if s.lower() in jd_lower]
    # Missing: JD tokens that look like tech but not in profile
    # For simplicity, use top JD tokens not in profile
    missing = [t for t in list(jd_tokens)[:50] if t not in prof_tokens and len(t) > 3][:10]

    # Coverage for skills score
    coverage = len(common) / max(1, len(jd_tokens))
    skills_score = min(100, max(0, int(coverage * 100 * 1.2)))  # boost

    # Experience
    jd_years = _extract_years(jd)
    profile_years = _extract_years(profile_text)
    # Also count experience entries
    exp_entries = len(profile.get("experience", []))
    if jd_years and profile_years:
        if profile_years >= jd_years:
            exp_score = 90 + min(10, (profile_years - jd_years) * 2)
        else:
            exp_score = max(30, 90 - (jd_years - profile_years) * 15)
    elif exp_entries:
        exp_score = min(100, 60 + exp_entries * 10)
    else:
        exp_score = 50

    # Seniority
    jd_seniority = _detect_seniority(jd)
    profile_seniority = _detect_seniority(profile_text + " " + " ".join(profile_skills))
    seniority_map = {"junior": 1, "mid": 2, "senior": 3, "lead": 4, "principal": 5, "unknown": 3}
    jd_level = seniority_map.get(jd_seniority, 3)
    prof_level = seniority_map.get(profile_seniority, 3)
    diff = abs(jd_level - prof_level)
    seniority_score = max(0, 100 - diff * 20)

    # Location (simple: if remote or same city)
    location_score = 100
    if "remote" in jd_lower:
        location_score = 100
    else:
        # If profile has location, check overlap
        prof_loc = (profile.get("location") or "").lower()
        if prof_loc and any(city in jd_lower for city in prof_loc.split()):
            location_score = 100
        else:
            location_score = 80  # neutral if unknown

    # Salary (placeholder: if JD mentions salary, assume match)
    salary_score = 88
    if "$" in jd or "₹" in jd or "salary" in jd_lower or "compensation" in jd_lower:
        salary_score = 85

    # Education
    edu_score = 80
    edu_keywords = ["bachelor", "master", "phd", "degree", "b.tech", "m.tech", "b.e", "m.s"]
    if any(k in jd_lower for k in edu_keywords):
        if any(k in profile_lower for k in edu_keywords):
            edu_score = 90
        else:
            edu_score = 60

    # Overall weighted
    overall = int(
        skills_score * 0.35 +
        exp_score * 0.25 +
        seniority_score * 0.15 +
        location_score * 0.10 +
        salary_score * 0.08 +
        edu_score * 0.07
    )
    overall = max(0, min(100, overall))

    # Recommendation
    if overall >= 85:
        rec = "HIGH PRIORITY"
        rec_reason = f"Strong skill alignment ({len(strong_matches)} matched) and experience fit."
    elif overall >= 70:
        rec = "GOOD FIT"
        rec_reason = f"Good match with some gaps: missing {', '.join(missing[:3]) if missing else 'minor areas'}."
    elif overall >= 50:
        rec = "MODERATE"
        rec_reason = f"Moderate fit. Consider tailoring resume to address {', '.join(missing[:3]) if missing else 'gaps'}."
    else:
        rec = "LOW"
        rec_reason = "Low alignment — significant skill/experience gaps."

    return {
        "overall": overall,
        "breakdown": {
            "skills": skills_score,
            "experience": exp_score,
            "seniority": seniority_score,
            "location": location_score,
            "salary": salary_score,
            "education": edu_score,
        },
        "strong_matches": strong_matches[:15],
        "missing_weak": missing[:10],
        "recommendation": rec,
        "recommendation_reason": rec_reason,
        "coverage": int(coverage * 100),
        "similarity": round(cosine_sim(tf(profile_text), tf(jd)), 3),
    }

def heuristic_score(profile: Dict[str,Any], jd: str) -> Tuple[float, str]:
    if not _extract_profile_text(profile).strip() or not jd.strip():
        return 50.0, "Insufficient data, default 50"
    detail = _detailed_breakdown(profile, jd)
    score = float(detail["overall"])
    reason = f"skill overlap {detail['coverage']}% of JD terms, similarity {detail['similarity']:.2f} (calibrated heuristic) | {detail['recommendation']}: {detail['recommendation_reason']}"
    return score, reason

def heuristic_score_detailed(profile: Dict[str, Any], jd: str) -> Dict[str, Any]:
    """Returns full detailed breakdown plus score/reason."""
    if not _extract_profile_text(profile).strip() or not jd.strip():
        return {
            "overall": 50,
            "breakdown": {"skills": 50, "experience": 50, "seniority": 50, "location": 50, "salary": 50, "education": 50},
            "strong_matches": [],
            "missing_weak": [],
            "recommendation": "UNKNOWN",
            "recommendation_reason": "Insufficient profile or JD data",
            "source": "insufficient_data",
            "score": 50.0,
            "reason": "Insufficient data, default 50",
        }
    detail = _detailed_breakdown(profile, jd)
    detail["score"] = float(detail["overall"])
    detail["source"] = "preliminary"
    detail["reason"] = (f"Preliminary keyword estimate (not an AI verdict): skill overlap "
                        f"{detail['coverage']}% of JD terms, similarity {detail['similarity']:.2f}")
    return detail

# --------------------------------------------------------------------------- #
# AI scoring — rubric, evidence and guardrails
# --------------------------------------------------------------------------- #
#: Rubric weights. The final number is a weighted mix, so a single flattering
#: dimension can never carry a weak candidate to "HIGH PRIORITY".
RUBRIC_WEIGHTS = {
    "skills": 0.34,
    "experience": 0.24,
    "seniority": 0.14,
    "domain": 0.12,
    "location": 0.08,
    "education": 0.08,
}

SCORE_SCHEMA = SchemaSpec([
    FieldSpec("score", "float", minimum=0, maximum=100),
    FieldSpec("reason", "str", min_length=20, max_length=600),
    FieldSpec("breakdown", "dict"),
    FieldSpec("strengths", "list"),
    FieldSpec("missing_skills", "list"),
    FieldSpec("evidence", "list"),
    FieldSpec("recommendation", "str", choices=["HIGH PRIORITY", "GOOD FIT", "MODERATE", "LOW"]),
    FieldSpec("recommendation_reason", "str", min_length=10, max_length=400),
])

#: How far the model may move the score away from the deterministic anchor
#: before the guardrail treats it as unjustified.
ANCHOR_TOLERANCE = 32
#: Score → recommendation bands (kept in code so the model cannot re-grade itself).
BANDS = [(80, "HIGH PRIORITY"), (65, "GOOD FIT"), (45, "MODERATE"), (0, "LOW")]


def _band(score: float) -> str:
    for threshold, label in BANDS:
        if score >= threshold:
            return label
    return "LOW"


def _score_checks(profile: Dict[str, Any], jd: str, anchor: Dict[str, Any]):
    """Guardrails that stop the model from flattering a weak match."""
    profile_text = _extract_profile_text(profile).lower()
    jd_lower = (jd or "").lower()

    def check(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []

        # Every claimed strength must be something the candidate actually has.
        for strength in data.get("strengths") or []:
            token = str(strength).strip().lower()
            if not token:
                continue
            normalised = re.sub(r"[^a-z0-9+#.]+", "", token)
            if normalised and normalised not in re.sub(r"[^a-z0-9+#.]+", "", profile_text):
                issues.append({"code": "unsupported_strength", "severity": "error", "field": "strengths",
                               "value": token,
                               "message": f"'{token}' is not present in the candidate's profile."})

        # Every claimed gap must be something the JD actually asks for.
        for missing in data.get("missing_skills") or []:
            token = str(missing).strip().lower()
            if not token:
                continue
            normalised = re.sub(r"[^a-z0-9+#.]+", "", token)
            if normalised and normalised not in re.sub(r"[^a-z0-9+#.]+", "", jd_lower):
                issues.append({"code": "unsupported_gap", "severity": "error", "field": "missing_skills",
                               "value": token,
                               "message": f"'{token}' is not mentioned in the job description."})

        # The breakdown must be numeric, in range, and consistent with the total.
        breakdown = data.get("breakdown") if isinstance(data.get("breakdown"), dict) else {}
        numeric = {k: v for k, v in breakdown.items() if isinstance(v, (int, float))}
        for key, value in numeric.items():
            if not 0 <= float(value) <= 100:
                issues.append({"code": "breakdown_out_of_range", "severity": "error",
                               "field": f"breakdown.{key}", "value": value,
                               "message": f"breakdown.{key} must be between 0 and 100."})
        weighted = sum(float(numeric.get(k, 0)) * w for k, w in RUBRIC_WEIGHTS.items() if k in numeric)
        weight_sum = sum(w for k, w in RUBRIC_WEIGHTS.items() if k in numeric)
        if weight_sum >= 0.9:
            expected = weighted / weight_sum
            if abs(float(data.get("score", 0)) - expected) > 18:
                issues.append({"code": "score_breakdown_mismatch", "severity": "error", "field": "score",
                               "value": data.get("score"),
                               "message": f"score {data.get('score')} does not match the weighted breakdown ({expected:.0f})."})

        # The verdict must follow the score, not the model's mood.
        expected_band = _band(float(data.get("score", 0)))
        if str(data.get("recommendation", "")).upper() != expected_band:
            issues.append({"code": "recommendation_mismatch", "severity": "error", "field": "recommendation",
                           "value": data.get("recommendation"),
                           "message": f"a score of {data.get('score')} must be labelled '{expected_band}'."})
        return issues

    return check


def _anchor_breakdown(profile: Dict[str, Any], jd: str) -> Dict[str, Any]:
    """Deterministic reference breakdown — used to bound and cross-check the AI."""
    detail = _detailed_breakdown(profile, jd)
    breakdown = dict(detail["breakdown"])
    breakdown["domain"] = int(detail["breakdown"].get("skills", 0) * 0.5
                              + detail["breakdown"].get("experience", 0) * 0.5)
    breakdown.pop("salary", None)
    return {"breakdown": breakdown, "overall": int(detail["overall"]),
            "strong_matches": detail["strong_matches"], "missing_weak": detail["missing_weak"],
            "coverage": detail["coverage"], "similarity": detail["similarity"]}


def _merge_breakdown(ai_breakdown: Any, anchor: Dict[str, Any]) -> Tuple[Dict[str, int], str]:
    """Use the model's numbers where given; derive the rest deterministically."""
    provided = {k: int(float(v)) for k, v in (ai_breakdown or {}).items()
                if isinstance(v, (int, float)) and 0 <= float(v) <= 100}
    if len(provided) >= len(RUBRIC_WEIGHTS):
        return provided, "ai"
    merged = dict(anchor["breakdown"])
    merged.update(provided)
    return {k: int(merged.get(k, 0)) for k in RUBRIC_WEIGHTS}, ("ai" if provided else "derived")


async def ai_score_detailed(
    profile: Dict[str, Any],
    jd: str,
    ai_config: Optional[Dict[str, Any]] = None,
    *,
    db=None,
    user_id: Optional[int] = None,
    persona: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Score one profile ↔ JD pair with the model, under guardrails.

    Raises ``AIUnavailableError`` (HTTP 503 with a diagnosis) when the model
    cannot be reached — scoring silently degrading to a keyword-overlap estimate
    is exactly what made the funnel untrustworthy. Callers that must keep moving
    (bulk discovery) catch it and mark the job ``score_source="pending"``.
    """
    if not (jd or "").strip() or not _extract_profile_text(profile or {}).strip():
        detail = heuristic_score_detailed(profile or {}, jd or "")
        detail["source"] = "insufficient_data"
        detail["reason"] = "Not enough profile or job text to score"
        return detail

    anchor = _anchor_breakdown(profile, jd)
    safe_jd = strip_ai_artifacts(jd)[:5000]
    persona_block = ""
    if persona:
        persona_block = (
            "\nCandidate track (persona) — weight the match towards this target:\n"
            f"{json.dumps(persona, default=str)[:1200]}\n"
        )

    prompt = f"""Score how well this candidate matches this job description, 0-100.

Rubric (the final score must be the weighted mix of these dimensions):
- skills {RUBRIC_WEIGHTS['skills']:.0%} — required technologies/competencies actually evidenced in the resume
- experience {RUBRIC_WEIGHTS['experience']:.0%} — years and relevance of comparable roles
- seniority {RUBRIC_WEIGHTS['seniority']:.0%} — scope/level alignment
- domain {RUBRIC_WEIGHTS['domain']:.0%} — industry/product domain alignment
- location {RUBRIC_WEIGHTS['location']:.0%} — location/remote feasibility
- education {RUBRIC_WEIGHTS['education']:.0%} — degree requirements

Hard rules — an automated checker rejects violations:
- Every "strength" must be present in the candidate profile. Never credit a skill they do not have.
- Every "missing_skills" entry must be explicitly requested by the job description.
- "evidence" quotes the exact resume/JD phrases you relied on (2-6 items).
- The recommendation must follow the score: >=80 HIGH PRIORITY, >=65 GOOD FIT, >=45 MODERATE, else LOW.
- Be strict. A candidate missing core requirements must score below 60 however well written the resume is.
- Do not invent requirements, employers or metrics.
{persona_block}
Candidate profile:
{json.dumps(profile, indent=2, default=str)[:5000]}

Job description:
\"\"\"{safe_jd}\"\"\"

Return JSON:
{{"score": number, "reason": "2-3 sentences", "breakdown": {{"skills": n, "experience": n, "seniority": n, "domain": n, "location": n, "education": n}}, "strengths": [], "missing_skills": [], "evidence": [], "recommendation": "HIGH PRIORITY|GOOD FIT|MODERATE|LOW", "recommendation_reason": "why"}}"""

    data, report = await run_guarded_task(
        "scoring",
        system=("You are a strict, evidence-bound technical recruiter. You score only what the resume "
                "proves and you never inflate a score to be encouraging."),
        prompt=prompt,
        schema=SCORE_SCHEMA,
        checks=[_score_checks(profile, jd, anchor)],
        db=db,
        user_id=user_id,
        temperature=0.1,
        max_tokens=1200,
    )

    score = max(0.0, min(100.0, float(data.get("score", 0))))
    breakdown, breakdown_source = _merge_breakdown(data.get("breakdown"), anchor)
    delta = round(score - anchor["overall"], 1)
    warnings: List[Dict[str, Any]] = list(report.issues)
    if abs(delta) > ANCHOR_TOLERANCE:
        warnings.append({"code": "anchor_divergence", "severity": "warning", "field": "score",
                         "value": delta,
                         "message": f"AI score is {delta:+.0f} from the deterministic anchor ({anchor['overall']})."})
    return {
        "overall": int(round(score)),
        "score": score,
        "reason": strip_ai_artifacts(str(data.get("reason") or ""))[:600],
        "breakdown": breakdown,
        "breakdown_source": breakdown_source,
        "weights": RUBRIC_WEIGHTS,
        "strong_matches": [strip_ai_artifacts(str(v))[:80] for v in (data.get("strengths") or [])][:15],
        "missing_weak": [strip_ai_artifacts(str(v))[:80] for v in (data.get("missing_skills") or [])][:12],
        "evidence": [strip_ai_artifacts(str(v))[:300] for v in (data.get("evidence") or [])][:8],
        "recommendation": _band(score),
        "recommendation_reason": strip_ai_artifacts(str(data.get("recommendation_reason") or ""))[:400],
        "anchor": {"overall": anchor["overall"], "coverage": anchor["coverage"],
                   "similarity": anchor["similarity"], "delta": delta},
        "guardrail": {**report.to_dict(), "issues": warnings},
        "source": "ai",
    }


async def ai_score(
    profile: Dict[str, Any],
    jd: str,
    ai_config: Optional[Dict[str, Any]] = None,
    *,
    db=None,
    user_id: Optional[int] = None,
    persona: Optional[Dict[str, Any]] = None,
) -> Tuple[float, str]:
    """Back-compatible ``(score, reason)`` wrapper over :func:`ai_score_detailed`."""
    detail = await ai_score_detailed(profile, jd, ai_config, db=db, user_id=user_id, persona=persona)
    reason = (f"AI {detail['score']:.0f}/100 ({detail['recommendation']}): {detail['reason']} "
              f"| anchor {detail['anchor']['overall']} (coverage {detail['anchor']['coverage']}%)")
    return float(detail["score"]), reason[:900]


async def score_job(
    profile: Dict[str, Any],
    jd: str,
    *,
    db=None,
    user_id: Optional[int] = None,
    persona: Optional[Dict[str, Any]] = None,
    allow_preliminary: bool = True,
) -> Dict[str, Any]:
    """
    The one scoring entry point the product should use.

    Returns the score plus its provenance so nothing in the UI can present a
    keyword-overlap estimate as an AI verdict:

    * ``ai``          — guardrail-verified model score (the only "real" score);
    * ``pending``     — AI unreachable; carries the full diagnosis to show the user;
    * ``preliminary`` — deterministic pre-rank, only when the caller opts in
      (bulk discovery, where scoring 200 postings with the model is not viable);
    * ``insufficient_data`` — nothing to score.
    """
    try:
        detail = await ai_score_detailed(profile, jd, db=db, user_id=user_id, persona=persona)
        return {"score": float(detail["score"]), "reason": f"AI: {detail['reason']}",
                "score_source": "ai", "detail": detail, "error": None}
    except AIUnavailableError as exc:
        if not allow_preliminary:
            raise
        preliminary = heuristic_score_detailed(profile or {}, jd or "")
        return {"score": 0.0, "reason": "Not scored — AI is unavailable",
                "score_source": "pending", "detail": preliminary,
                "error": exc.payload(),
                "preliminary_score": float(preliminary["overall"])}
    except GuardrailError as exc:
        return {"score": 0.0, "reason": "Score rejected by the accuracy guardrail",
                "score_source": "rejected", "detail": {}, "error": exc.payload()}


def jd_similarity(jd_a: str, jd_b: str) -> float:
    """Cosine similarity between two job descriptions (0–1). Used to decide
    whether an already-generated, approved resume can be reused for a new JD."""
    if not jd_a or not jd_b:
        return 0.0
    return round(cosine_sim(tf(jd_a), tf(jd_b)), 4)


def should_generate_new_resume(score: float, best_existing_sim: float) -> bool:
    """
    Decide whether to reuse an existing tagged resume or create a new one.

    - Reuse when the candidate is a strong match AND an already-generated resume
      was built for a very similar JD (similarity ≥ 0.85).
    - Generate a new tailored resume for a solid match (score ≥ 65) when no
      existing resume is close enough.
    - Otherwise fall back to the master resume (weak match — don't burn AI calls
      tailoring for roles the candidate barely fits).
    """
    if score >= 65 and best_existing_sim < 0.85:
        return True
    return False
