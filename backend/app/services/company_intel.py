"""
Company intelligence — deeper research per company.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.models import CompanyIntel
from app.services.ai_client import chat_completion, AIClientError

log = get_logger("app.company_intel")


async def fetch_company_intel(company: str, db: Session = None, user_id: Optional[int] = None, force_refresh: bool = False) -> Dict[str, Any]:
    """Fetch or generate company intelligence."""
    if not force_refresh and db and user_id:
        existing = db.query(CompanyIntel).filter(CompanyIntel.user_id == user_id, CompanyIntel.company == company).first()
        if existing:
            # Return cached if less than 30 days old
            if (datetime.utcnow() - existing.updated_at).days < 30:
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

    # Generate via AI
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

    try:
        data = await chat_completion(
            "company_intel",
            prompt,
            temperature=0.3,
            max_tokens=800,
            db=db,
            user_id=user_id,
        )
    except (AIClientError, Exception) as exc:
        log.warning("company intel failed for %s: %s", company, exc)
        data = {
            "website": "",
            "industry": "unknown",
            "size": "unknown",
            "funding_stage": "unknown",
            "tech_stack": [],
            "culture": "",
            "recent_news": [],
            "summary": f"{company} — company intelligence unavailable (AI not configured)",
            "sources": [],
        }

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
