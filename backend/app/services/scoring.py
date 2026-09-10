import re
import math
from typing import Dict, Any, Tuple, List
import httpx
import json
from app.core.config import settings

STOPWORDS = set(["the","and","for","with","a","an","in","on","of","to","is","are","as","at","by","from","or"])

def tokenize(text: str) -> List[str]:
    tokens = re.findall(r"[a-zA-Z0-9\+#\.]+", text.lower())
    return [t for t in tokens if t not in STOPWORDS and len(t)>1]

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
    # Experience years boost if mentioned
    exp_bonus = 0
    if "year" in jd.lower() and profile_text.lower().count("year"):
        exp_bonus = 5
    score = (sim * 70) + (coverage * 25) + exp_bonus
    score = min(100, max(0, round(score,1)))
    reason = f"cosine={sim:.2f}, coverage={coverage:.2f}, heuristic"
    return score, reason

async def ai_score(profile: Dict[str,Any], jd: str, ai_config: Dict[str,Any]=None) -> Tuple[float, str]:
    if not settings.ai_api_key:
        return heuristic_score(profile, jd)
    base_url = (ai_config or {}).get("base_url") or settings.ai_base_url
    api_key = (ai_config or {}).get("api_key") or settings.ai_api_key
    model = (ai_config or {}).get("model") or settings.ai_model
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
        async with httpx.AsyncClient(timeout=settings.ai_timeout) as client:
            resp = await client.post(f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type":"application/json"},
                json={
                    "model": model,
                    "messages":[{"role":"user","content": prompt}],
                    "temperature": 0.2,
                    "response_format": {"type":"json_object"}
                })
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"]
                data = json.loads(content)
                score = float(data.get("score", 50))
                reason = data.get("reason","AI scored")
                return max(0, min(100, score)), f"AI: {reason}"
            else:
                return heuristic_score(profile, jd)
    except:
        return heuristic_score(profile, jd)

def should_generate_new_resume(score: float, best_existing_sim: float) -> bool:
    """
    Decide when to use already available resume or create new one.
    If score low (<60) or existing resumes not similar to JD (<0.75), generate new.
    """
    if score < 60:
        return False  # don't waste generation if poor match? actually we may skip applying
    if best_existing_sim < 0.75:
        return True
    return False
