import re
import json
import math
from typing import Dict, Any, Tuple, List
from app.services.ai_client import chat_completion, AIClientError

STOPWORDS = set(["the","and","for","with","a","an","in","on","of","to","is","are","as","at","by","from","or"])

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

def tokenize(text: str) -> List[str]:
    # NOTE: no '.' in the char class — previously "Python." tokenized as
    # "python." and never matched the skill "python", zeroing many scores.
    tokens = re.findall(r"[a-zA-Z0-9\+#/]+", text.lower())
    return [_normalize_token(t) for t in tokens if t not in STOPWORDS and len(t.strip("+#/"))>1]

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

def heuristic_score(profile: Dict[str,Any], jd: str) -> Tuple[float, str]:
    profile_text = " ".join([
        " ".join(profile.get("skills",[])),
        profile.get("summary",""),
        " ".join([e.get("description","") + " " + e.get("title","") for e in profile.get("experience",[])]),
        " ".join([p.get("description","") for p in profile.get("projects",[])]),
    ])
    if not profile_text.strip() or not jd.strip():
        return 50.0, "Insufficient data, default 50"
    a = tf(profile_text)
    b = tf(jd)
    sim = cosine_sim(a,b)  # 0-1
    # Keyword coverage boost
    jd_tokens = set(tokenize(jd))
    prof_tokens = set(tokenize(profile_text))
    coverage = len(jd_tokens & prof_tokens) / max(1, len(jd_tokens))
    # Calibrated scaling: raw cosine against a long profile reads low even for
    # strong matches, so normalize typical good matches (sim≈0.3, cov≈0.5) → ~85.
    relevance = 0.5 * min(1.0, sim / 0.32) + 0.5 * min(1.0, coverage / 0.55)
    # Experience years boost if mentioned
    exp_bonus = 0
    if "year" in jd.lower() and profile_text.lower().count("year"):
        exp_bonus = 5
    score = relevance * 88 + exp_bonus
    score = min(100, max(0, round(score,1)))
    reason = f"skill overlap {int(coverage*100)}% of JD terms, similarity {sim:.2f} (calibrated heuristic)"
    return score, reason

async def ai_score(profile: Dict[str,Any], jd: str, ai_config: Dict[str,Any]=None) -> Tuple[float, str]:
    prompt = f"""
You are a career matching engine. Score how well this candidate profile matches the job description from 0 to 100.
Be strict, no hallucination. Consider skills, experience relevance, seniority, domain.
Return JSON: {{"score": number, "reason": "short explanation", "missing_skills": [], "strengths": [] }}

Profile JSON:
{json.dumps(profile, indent=2)[:4000]}

JD:
\"\"\"{jd[:4000]}\"\"\"
"""
    try:
        data = await chat_completion("scoring", prompt, temperature=0.2, ai_config=ai_config)
        score = float(data.get("score", 50))
        reason = data.get("reason", "AI scored")
        return max(0.0, min(100.0, score)), f"AI: {reason}"
    except (AIClientError, TypeError, ValueError, KeyError):
        return heuristic_score(profile, jd)


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
