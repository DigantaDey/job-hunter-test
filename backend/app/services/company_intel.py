"""
Company intelligence — deeper research per company.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.models import CompanyIntel

log = get_logger("app.company_intel")

#: Starting output budget (the user's configured output ceiling still caps it).
INTEL_OUTPUT_TOKENS = 800

#: A cached row is "fresh" — and therefore free to serve — for this long.
CACHE_TTL_DAYS = 30


def get_cached_intel(db: Optional[Session], user_id: Optional[int], company: str) -> Optional[Dict[str, Any]]:
    """A fresh (less than :data:`CACHE_TTL_DAYS` old) cached row as its
    response dict — or ``None``.

    The *single* definition of "fresh enough to serve without AI", shared by
    the read endpoint (which must decide whether to charge) and
    :func:`fetch_company_intel` (which must decide whether to call the model),
    so the two can never disagree about what counts as a cache hit.
    """
    if not db or not user_id:
        return None
    existing = db.query(CompanyIntel).filter(
        CompanyIntel.user_id == user_id, CompanyIntel.company == company
    ).first()
    if existing and (datetime.utcnow() - existing.updated_at).days < CACHE_TTL_DAYS:
        return {
            "company": existing.company,
            "website": existing.website,
            "industry": existing.industry,
            "size": existing.size,
            "funding_stage": existing.funding_stage,
            "tech_stack": existing.tech_stack,
            "culture": existing.culture,
            "recent_news": existing.recent_news,
            "summary": existing.summary,
            "sources": existing.sources,
            "verified": existing.verified,
            "cached": True,
        }
    return None


async def fetch_company_intel(company: str, db: Optional[Session] = None, user_id: Optional[int] = None, force_refresh: bool = False) -> Dict[str, Any]:
    """Fetch or generate company intelligence."""
    if not force_refresh:
        cached = get_cached_intel(db, user_id, company)
        if cached is not None:
            return cached

    # Generate via AI. AI is a hard dependency: on failure this raises and the
    # endpoint reports the outage (pausable 503). A failed research call is
    # NOT cached — the previous behaviour (persisting an "unavailable" stub as
    # if it were intel for 30 days) is exactly the silent degradation this
    # product removed in v2.1.
    prompt = f"""
Research company: {company}

Return JSON: {{
  "website": "https://...",
  "industry": "e.g. SaaS, Fintech",
  "size": "startup|small|medium|big|unknown",
  "funding_stage": "Seed|Series A|Series B|etc",
  "tech_stack": ["Python", "React"],
  "culture": "short culture summary",
  "recent_news": [{{"title": "...", "date": "2024-01-01"}}],
  "summary": "2-3 sentence overview",
  "sources": ["source1"]
}}

Be concise, no hallucination beyond reasonable public knowledge. If unknown, use "unknown".
"""

    from app.services.ai_client import chat_completion

    data = await chat_completion(
        "company_intel",
        prompt,
        temperature=0.3,
        max_tokens=INTEL_OUTPUT_TOKENS,
        db=db,
        user_id=user_id,
    )

    if db and user_id:
        try:
            existing = db.query(CompanyIntel).filter(CompanyIntel.user_id == user_id, CompanyIntel.company == company).first()
            if existing:
                existing.website = data.get("website", "")
                existing.industry = data.get("industry", "")
                existing.size = data.get("size", "unknown")
                existing.funding_stage = data.get("funding_stage", "")
                existing.tech_stack = data.get("tech_stack", [])
                existing.culture = data.get("culture", "")
                existing.recent_news = data.get("recent_news", [])
                existing.summary = data.get("summary", "")
                existing.sources = data.get("sources", [])
                existing.updated_at = datetime.utcnow()
            else:
                intel = CompanyIntel(
                    user_id=user_id,
                    company=company,
                    website=data.get("website", ""),
                    industry=data.get("industry", ""),
                    size=data.get("size", "unknown"),
                    funding_stage=data.get("funding_stage", ""),
                    tech_stack=data.get("tech_stack", []),
                    culture=data.get("culture", ""),
                    recent_news=data.get("recent_news", []),
                    summary=data.get("summary", ""),
                    sources=data.get("sources", []),
                    verified=False,
                )
                db.add(intel)
            db.commit()
        except Exception as exc:
            log.warning("failed to cache company intel: %s", exc)

    return {**data, "company": company, "cached": False}
