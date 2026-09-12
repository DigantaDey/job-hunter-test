"""
Funding Radar — recently funded companies (Seed → Series D where the data
supports it) matched to the candidate's extracted context.

Data provenance is explicit on every row (``source`` + ``verified``): SEC EDGAR
Form D filings are real and linked; Crunchbase/Tracxn/imported datasets are
licensed; the synthetic demo set is opt-in and labelled.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.metrics import inc
from app.models.models import FundingCompany
from app.services import funding_sources
from app.services.funding_sources import STAGES, FundingEvent, normalize_stage
from app.services.sources.base import keyword_score

log = get_logger("app.funding_radar")


async def scan_funded_companies(
    context: Dict[str, Any],
    *,
    stages: Optional[List[str]] = None,
    window_days: int = 45,
    limit: int = 18,
    provider: Optional[str] = None,
    db=None,
    user_id: Optional[int] = None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch + filter + AI-rank real funding events.

    The AI ranking is a hard dependency when AI is configured: on a transient
    outage this raises and the caller pauses (queue) or reports (sync) — the
    events are real either way, but an unranked list is not the product.
    """
    events, report = await funding_sources.fetch_funding_events(
        context, window_days=window_days, limit=limit, provider=provider
    )

    if stages:
        wanted = {normalize_stage(s) for s in stages}
        events = [e for e in events if e.stage in wanted or e.stage == "Undisclosed"]

    # Relevance filter: keep events matching the candidate's focus when we have one.
    focus = [str(k).lower() for k in (context.get("funding_focus") or context.get("industries") or []) if k]
    if focus:
        relevant = [e for e in events if not e.industry or keyword_score(f"{e.industry} {e.summary}", focus) > 0]
        if relevant:
            events = relevant

    if await _ai_enabled():
        events = await funding_sources.ai_rank_events(context, events, limit=limit, db=db, user_id=user_id)

    report["provider_status"] = funding_sources.provider_status()
    companies = []
    for event in events[:limit]:
        row = event.to_dict()
        row["keywords_matched"] = _matched_keywords(event, context)
        companies.append(row)
    report["returned"] = len(companies)
    return companies, report


async def _ai_enabled() -> bool:
    from app.services.ai_client import is_configured

    return is_configured("funding_scan")


def _matched_keywords(event: FundingEvent, context: Dict[str, Any]) -> List[str]:
    hay = f"{event.industry} {event.summary} {event.name}".lower()
    keys = [str(k) for k in (context.get("funding_focus") or []) + (context.get("industries") or []) + (context.get("keywords") or [])]
    return [k for k in keys if k.lower() in hay][:6]


def sync_funding_db(db: Session, user_id: int, companies: List[Dict[str, Any]], window_days: int) -> Dict[str, int]:
    """Upsert scanned companies and prune anything outside the freshness window."""
    added = updated = 0
    for company in companies:
        name = (company.get("name") or "").strip()
        if not name:
            continue
        row = (
            db.query(FundingCompany)
            .filter(FundingCompany.user_id == user_id, FundingCompany.name == name)
            .first()
        )
        if row is None:
            row = FundingCompany(user_id=user_id, name=name)
            db.add(row)
            added += 1
        else:
            updated += 1
        row.stage = company.get("stage") or "Undisclosed"
        row.raised_at = company.get("raised_at") or datetime.utcnow()
        row.website = company.get("website") or ""
        row.industry = company.get("industry") or ""
        row.summary = company.get("summary") or ""
        row.keywords_matched = company.get("keywords_matched") or []
        row.source = company.get("source") or "unknown"
        row.verified = bool(company.get("verified"))
        row.discovered_at = datetime.utcnow()
        row.meta = {"url": company.get("url", ""), "raised_usd": company.get("raised_usd")}
    db.commit()
    pruned = prune_funding_db(db, user_id, window_days)
    inc("jobhunter_funding_sync_total", added=added, updated=updated, pruned=pruned)
    return {"added": added, "updated": updated, "pruned": pruned}


def prune_funding_db(db: Session, user_id: int, window_days: int) -> int:
    cutoff = datetime.utcnow() - timedelta(days=max(1, window_days))
    stale = (
        db.query(FundingCompany)
        .filter(FundingCompany.user_id == user_id, FundingCompany.discovered_at < cutoff)
        .all()
    )
    for row in stale:
        db.delete(row)
    if stale:
        db.commit()
    return len(stale)


def funding_needs_refresh(db: Session, user_id: int, window_days: int) -> bool:
    latest = (
        db.query(FundingCompany)
        .filter(FundingCompany.user_id == user_id)
        .order_by(FundingCompany.discovered_at.desc())
        .first()
    )
    if not latest or not latest.discovered_at:
        return True
    return datetime.utcnow() - latest.discovered_at > timedelta(hours=max(1, window_days * 12))


def list_funding_companies(
    db: Session,
    user_id: int,
    *,
    stage: Optional[str] = None,
    include_unverified: bool = False,
    limit: int = 200,
) -> List[FundingCompany]:
    query = db.query(FundingCompany).filter(FundingCompany.user_id == user_id)
    if stage:
        query = query.filter(FundingCompany.stage == normalize_stage(stage))
    if not include_unverified:
        query = query.filter(FundingCompany.verified.is_(True))
    return query.order_by(FundingCompany.raised_at.desc()).limit(limit).all()


__all__ = [
    "STAGES",
    "scan_funded_companies",
    "sync_funding_db",
    "prune_funding_db",
    "funding_needs_refresh",
    "list_funding_companies",
    "normalize_stage",
]
