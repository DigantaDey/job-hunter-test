"""
AI keyword/context extraction.

Derives a rich search context (keywords, roles, industries, tech stack,
locations, seniority, funding focus) from the user's profile, master resume
text and any free-form context the user provides — in addition to (not
instead of) explicit user input.

Works in two modes:
- AI mode  : OpenAI-compatible chat completion returning strict JSON.
- Heuristic: TF-based keyword mining + role/industry inference (offline).
"""
import json
import re
from collections import Counter
from typing import Any, Dict, List, Optional

from app.services.ai_client import chat_completion, AIClientError

STOPWORDS = {
    "the", "and", "for", "with", "a", "an", "in", "on", "of", "to", "is", "are",
    "as", "at", "by", "from", "or", "i", "my", "we", "our", "have", "has", "had",
    "was", "were", "been", "will", "would", "can", "could", "should", "their",
    "this", "that", "these", "those", "it", "its", "his", "her", "they", "them",
    "you", "your", "he", "she", "not", "but", "also", "more", "most", "very",
    "years", "year", "months", "month", "experience", "work", "working", "worked",
    "built", "build", "building", "using", "used", "strong", "good", "excellent",
    "candidate", "resume", "curriculum", "vitae", "summary", "skills", "project",
    "projects", "company", "companies", "team", "teams", "role", "roles", "job",
    "jobs", "via", "across", "into", "about", "over", "under", "all", "any",
}

# Canonical skill synonyms so "js" and "javascript" land on the same concept
SKILL_SYNONYMS = {
    "js": "javascript", "nodejs": "node.js", "node": "node.js",
    "k8s": "kubernetes", "ml": "machine learning", "ai": "artificial intelligence",
    "llm": "llm", "golang": "go", "py": "python", "ts": "typescript",
    "postgres": "postgresql", "psql": "postgresql", "reactjs": "react",
    "react.js": "react", "nextjs": "next.js", "vuejs": "vue", "tf": "tensorflow",
}

TECH_HINTS = {
    "python", "java", "javascript", "typescript", "react", "node.js", "next.js",
    "vue", "angular", "svelte", "go", "rust", "golang", "ruby", "rails", "php",
    "laravel", "swift", "kotlin", "flutter", "dart", "c++", "c#", ".net",
    "fastapi", "django", "flask", "spring", "spring boot", "express", "nestjs",
    "graphql", "rest", "grpc", "aws", "gcp", "azure", "docker", "kubernetes",
    "terraform", "ci/cd", "jenkins", "github actions", "postgresql", "mysql",
    "mongodb", "redis", "elasticsearch", "kafka", "rabbitmq", "spark", "hadoop",
    "airflow", "snowflake", "dbt", "tableau", "power bi", "machine learning",
    "deep learning", "pytorch", "tensorflow", "scikit-learn", "nlp", "llm",
    "computer vision", "mlops", "data science", "data engineering", "etl",
    "microservices", "system design", "devops", "sre", "android", "ios",
    "react native", "blockchain", "solidity", "web3", "fintech", "payments",
    "cybersecurity", "salesforce", "sap", "product management", "ui/ux", "figma",
}

INDUSTRY_HINTS = {
    "fintech": ["fintech", "payment", "payments", "banking", "lending", "trading", "insurance", "upi", "stripe"],
    "ai/ml": ["ai", "artificial intelligence", "machine learning", "deep learning", "llm", "nlp", "computer vision", "ml", "pytorch", "tensorflow", "genai", "generative"],
    "e-commerce": ["e-commerce", "ecommerce", "retail", "marketplace", "shopify", "storefront", "cart"],
    "healthtech": ["health", "healthcare", "medical", "clinical", "biotech", "pharma", "patient"],
    "edtech": ["education", "edtech", "learning", "course", "student", "tutoring"],
    "developer tools": ["developer tools", "devtools", "api", "sdk", "infrastructure", "open source", "compiler", "ide"],
    "cybersecurity": ["security", "cybersecurity", "auth", "encryption", "siem", "vulnerability"],
    "saas": ["saas", "b2b", "enterprise software", "crm", "erp", "workflow", "productivity", "collaboration"],
    "gaming": ["game", "gaming", "unity", "unreal", "multiplayer"],
    "web3": ["web3", "blockchain", "crypto", "solidity", "defi", "nft"],
    "mobility": ["mobility", "automotive", "ev", "ride", "logistics", "supply chain", "delivery"],
    "climate": ["climate", "clean energy", "solar", "sustainability", "carbon"],
}


def _normalize_skill(s: str) -> str:
    t = s.strip().lower()
    return SKILL_SYNONYMS.get(t, t)


def _significant_tokens(text: str, top_n: int = 18) -> List[str]:
    tokens = re.findall(r"[a-zA-Z][a-zA-Z\+\.]{1,30}", (text or "").lower())
    words = [t.strip(".") for t in tokens]
    words = [w for w in words if len(w) > 2 and w not in STOPWORDS and not w.isdigit()]
    counts = Counter(words)
    return [w for w, _ in counts.most_common(top_n)]


def heuristic_context(profile: Dict[str, Any], extra_context: str = "") -> Dict[str, Any]:
    """Offline fallback: mine keywords from profile fields + resume text."""
    skills = [_normalize_skill(s) for s in (profile.get("skills") or []) if isinstance(s, str)]
    tech_stack: List[str] = []
    for s in skills:
        if s in TECH_HINTS and s not in tech_stack:
            tech_stack.append(s)
    for t in _significant_tokens(json.dumps(profile), 60):
        if t in TECH_HINTS and t not in tech_stack:
            tech_stack.append(t)

    # Roles from experience titles (skip placeholder titles from the offline parser)
    roles: List[str] = []
    junk_markers = ("extracted", "experience", "unknown", "n/a")
    if profile.get("current_title") and not any(m in str(profile["current_title"]).lower() for m in junk_markers):
        roles.append(str(profile["current_title"]).strip())
    for e in profile.get("experience", []) or []:
        if isinstance(e, dict) and e.get("title"):
            title = str(e["title"]).strip()
            if (title and title.lower() not in [r.lower() for r in roles]
                    and not any(m in title.lower() for m in junk_markers)):
                roles.append(title)
    if not roles:
        summary = profile.get("summary", "") or ""
        m = re.search(r"([\w\s/]{3,40}?(engineer|developer|designer|scientist|manager|analyst|architect|marketer))", summary, re.IGNORECASE)
        if m:
            roles.append(m.group(1).strip().title())

    # Industries from hints across all text
    all_text = " ".join([
        profile.get("summary", "") or "",
        json.dumps(profile.get("projects", [])),
        json.dumps(profile.get("experience", [])),
        extra_context,
    ]).lower()
    industries = [ind for ind, hints in INDUSTRY_HINTS.items() if any(h in all_text for h in hints)]

    raw_text = profile.get("raw_text", "") or ""
    mined = [t for t in _significant_tokens(raw_text + " " + extra_context, 24) if t not in [s.lower() for s in skills]]

    keywords: List[str] = []
    for kw in (skills[:8] + tech_stack[:6] + roles[:3] + industries[:3]):
        k = kw.strip()
        if k and k.lower() not in [x.lower() for x in keywords]:
            keywords.append(k)

    locations = [profile.get("location")] if profile.get("location") else []
    return {
        "keywords": keywords[:14] or ["software engineer", "developer"],
        "roles": roles[:6] or ["Software Engineer"],
        "industries": industries[:6] or ["saas"],
        "tech_stack": tech_stack[:12] or skills[:8],
        "locations": locations,
        "seniority": _infer_seniority(profile),
        "funding_focus": industries[:4] or ["saas"],
        "source": "heuristic",
    }


def _infer_seniority(profile: Dict[str, Any]) -> str:
    text = (json.dumps(profile) or "").lower()
    if any(w in text for w in ["cto", "vp ", "vice president", "principal", "staff engineer", "director"]):
        return "staff+"
    if re.search(r"1[0-9]\+?\s*years", text):
        return "senior+"
    if re.search(r"[5-9]\+?\s*years", text):
        return "senior"
    if re.search(r"[2-4]\+?\s*years", text):
        return "mid"
    return "junior"


async def ai_extract_context(
    profile: Dict[str, Any],
    resume_text: str = "",
    extra_context: str = "",
    ai_config: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """
    AI mode: extract a structured search context from the profile + resume +
    free-form context. Falls back to heuristic mining on any failure, and
    always merges heuristic keywords so the result is never empty.
    """
    fallback = heuristic_context(profile, extra_context)
    if not profile and not resume_text.strip() and not extra_context.strip():
        return fallback

    prompt = f"""
You are a career-intelligence extractor. From the candidate profile, resume text and
extra context below, derive the BEST search context an autonomous job-hunting system
should use to discover relevant jobs AND companies that recently raised funding
(Seed to Series D only).

Return STRICT JSON with exactly these keys:
{{
  "keywords": ["8-14 concise job-board search phrases/skills, most relevant first"],
  "roles": ["3-6 specific job titles that fit this candidate"],
  "industries": ["2-6 industries/domains to prioritize, lowercase"],
  "tech_stack": ["up to 12 concrete technologies"],
  "locations": ["locations to search, empty array if unknown"],
  "seniority": "junior|mid|senior|staff+",
  "funding_focus": ["2-5 industries most likely to be hiring after raising funding for this candidate"]
}}
Rules:
- Use ONLY information present in the profile/resume/context. Never invent employers or skills.
- Keywords must be useful as search queries (e.g. "backend engineer python", "react frontend", "ml engineer llm").

Profile JSON:
{json.dumps(profile, default=str)[:3500]}

Resume text:
\"\"\"{(resume_text or '')[:3000]}\"\"\"

Extra context from user:
\"\"\"{(extra_context or 'none')[:800]}\"\"\"

Respond ONLY with JSON.
"""
    try:
        data = await chat_completion("keyword_extract", prompt, temperature=0.2, ai_config=ai_config)
        if isinstance(data, dict):
            merged = _merge_context(fallback, data)
            merged["source"] = "ai"
            return merged
    except (AIClientError, TypeError):
        pass
    return fallback


def _merge_context(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """AI result wins per-field, but union with heuristic so nothing is lost."""
    merged = dict(base)
    for key in ("keywords", "roles", "industries", "tech_stack", "locations", "funding_focus"):
        ov = override.get(key) or []
        if isinstance(ov, list):
            bv = merged.get(key) or []
            seen, out = set(), []
            for item in ov + bv:
                k = str(item).strip().lower()
                if k and k not in seen:
                    seen.add(k)
                    out.append(str(item).strip())
            merged[key] = out
    if isinstance(override.get("seniority"), str) and override["seniority"]:
        merged["seniority"] = override["seniority"]
    return merged


def merge_user_keywords(context: Dict[str, Any], user_keywords: Optional[str]) -> Dict[str, Any]:
    """User-provided input is added on top of AI/heuristic extraction, never replaces it."""
    if not user_keywords:
        return context
    if isinstance(user_keywords, list):
        extras = [str(k).strip() for k in user_keywords if str(k).strip()]
    else:
        extras = [k.strip() for k in str(user_keywords).split(",") if k.strip()]
    existing_lower = [str(x).lower() for x in context.get("keywords", [])]
    new_ones = [e for e in extras if e.lower() not in existing_lower]
    # user intent first, preserving the user's own order
    context["keywords"] = (new_ones + [k for k in context.get("keywords", [])])[:18]
    context["user_keywords"] = extras
    return context


# In-memory cache for the derived search context (invalidated on profile change)
_context_cache: Dict[str, Any] = {"context": None, "profile_id": None, "extra_context": ""}
