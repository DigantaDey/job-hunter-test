import json
import math
import re
from typing import Any, Dict, List, Tuple

from app.services.ai_client import AIClientError, chat_completion

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
    skill_candidates = [t for t in jd_tokens if len(t) > 2 and t not in STOPWORDS]
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
            "score": 50.0,
            "reason": "Insufficient data, default 50",
        }
    detail = _detailed_breakdown(profile, jd)
    detail["score"] = float(detail["overall"])
    detail["reason"] = f"skill overlap {detail['coverage']}% of JD terms, similarity {detail['similarity']:.2f} (calibrated heuristic)"
    return detail

async def ai_score(profile: Dict[str,Any], jd: str, ai_config: Dict[str,Any]=None) -> Tuple[float, str]:
    # Sanitize external JD as untrusted — strip potential prompt injection
    safe_jd = jd[:4000].replace("```", "").replace("SYSTEM:", "").replace("Ignore previous", "")
    prompt = f"""
You are a career matching engine. Score how well this candidate profile matches the job description from 0 to 100.
Be strict, no hallucination. Consider skills, experience relevance, seniority, domain.
Return JSON: {{"score": number, "reason": "short explanation", "missing_skills": [], "strengths": [], "breakdown": {{"skills": number, "experience": number, "seniority": number, "location": number, "salary": number, "education": number}}, "recommendation": "HIGH PRIORITY|GOOD FIT|MODERATE|LOW", "recommendation_reason": "why"}}

Profile JSON:
{json.dumps(profile, indent=2)[:4000]}

JD:
\"\"\"{safe_jd}\"\"\"
"""
    try:
        data = await chat_completion("scoring", prompt, temperature=0.2, ai_config=ai_config)
        score = float(data.get("score", 50))
        reason = data.get("reason", "AI scored")
        # If AI provides breakdown, use it
        if "breakdown" in data:
            # Validate breakdown
            bd = data["breakdown"]
            if isinstance(bd, dict):
                # Store extra for later use via detailed function
                pass
        return max(0.0, min(100.0, score)), f"AI: {reason}"
    except (AIClientError, TypeError, ValueError, KeyError):
        return heuristic_score(profile, jd)

async def ai_score_detailed(profile: Dict[str, Any], jd: str, ai_config: Dict[str, Any] = None) -> Dict[str, Any]:
    """Detailed scoring with AI fallback to heuristic."""
    safe_jd = jd[:4000].replace("```", "").replace("SYSTEM:", "").replace("Ignore previous", "")
    prompt = f"""
You are a career matching engine. Score how well this candidate profile matches the job description from 0 to 100.
Be strict, no hallucination. Consider skills, experience relevance, seniority, domain.
Return JSON: {{"score": number, "reason": "short explanation", "missing_skills": [], "strengths": [], "breakdown": {{"skills": number, "experience": number, "seniority": number, "location": number, "salary": number, "education": number}}, "recommendation": "HIGH PRIORITY|GOOD FIT|MODERATE|LOW", "recommendation_reason": "why"}}

Profile JSON:
{json.dumps(profile, indent=2)[:4000]}

JD:
\"\"\"{safe_jd}\"\"\"
"""
    try:
        data = await chat_completion("scoring", prompt, temperature=0.2, ai_config=ai_config)
        score = float(data.get("score", 50))
        breakdown = data.get("breakdown") or {}
        # Ensure all keys
        default_bd = {"skills": 70, "experience": 70, "seniority": 70, "location": 80, "salary": 80, "education": 75}
        for k in default_bd:
            if k not in breakdown or not isinstance(breakdown[k], (int, float)):
                breakdown[k] = default_bd[k]
        return {
            "overall": max(0, min(100, int(score))),
            "score": max(0.0, min(100.0, score)),
            "reason": data.get("reason", "AI scored"),
            "breakdown": breakdown,
            "strong_matches": data.get("strengths", [])[:15],
            "missing_weak": data.get("missing_skills", [])[:10],
            "recommendation": data.get("recommendation", "MODERATE"),
            "recommendation_reason": data.get("recommendation_reason", data.get("reason", "")),
            "source": "ai",
        }
    except Exception:
        # Fallback to heuristic detailed
        detail = heuristic_score_detailed(profile, jd)
        detail["source"] = "heuristic"
        return detail


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
