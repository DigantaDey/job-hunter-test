"""Funding radar endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Request

from app.api.deps import CurrentUser, DbSession, search_context
from app.core import audit
from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import FundingCompany, Job, Profile
from app.services import funding_radar
from app.services.funding_sources import provider_status
from app.services.job_queue import enqueue
from app.services.outreach import draft_email
from app.services.scoring import ai_score
from app.services.user_settings import get_setting

router = APIRouter(prefix="/funding", tags=["funding"])
log = get_logger("app.funding_api")


def _user_funding_context(db, user_id: int) -> str:
    rows = get_setting(db, user_id, "funding", "context_notes", "") or ""
    industries = get_setting(db, user_id, "funding", "industries", "") or ""
    return ", ".join(str(part) for part in (rows, industries) if part)


@router.get("/context")
async def funding_context(user: CurrentUser, db: DbSession):
    return await search_context(db, user.id, extra_context=_user_funding_context(db, user.id))


@router.get("/providers")
def providers():
    return {"providers": provider_status(), "active": settings.funding_provider,
            "synthetic_allowed": settings.allow_synthetic_funding_data}


@router.get("/companies")
async def list_companies(
    user: CurrentUser,
    db: DbSession,
    stage: Optional[str] = None,
    refresh: bool = False,
    include_unverified: bool = False,
):
    """Return the radar rows for this user, refreshing when stale or requested."""
    window = int(get_setting(db, user.id, "funding", "freshness_days", settings.funding_freshness_days))
    if refresh or funding_radar.funding_needs_refresh(db, user.id, window):
        context = await search_context(db, user.id, extra_context=_user_funding_context(db, user.id))
        provider = get_setting(db, user.id, "funding", "provider", settings.funding_provider)
        companies, report = await funding_radar.scan_funded_companies(context, window_days=window,
                                                                      limit=settings.funding_limit,
                                                                      provider=provider)
        funding_radar.sync_funding_db(db, user.id, companies, window)
        audit.audit(db, "funding.scan", user_id=user.id, target=provider,
                    detail={"found": len(companies), "provider_report": report})
    elif not include_unverified:
        include_unverified = bool(get_setting(db, user.id, "funding", "include_unverified", False))

    rows = funding_radar.list_funding_companies(db, user.id, stage=stage,
                                                include_unverified=include_unverified)
    return {
        "companies": [
            {
                "id": row.id, "name": row.name, "stage": row.stage, "raised_at": row.raised_at,
                "website": row.website, "industry": row.industry, "summary": row.summary,
                "keywords_matched": row.keywords_matched, "source": row.source, "verified": row.verified,
                "has_open_positions": row.has_open_positions, "discovered_at": row.discovered_at,
                "url": (row.meta or {}).get("url", ""),
            }
            for row in rows
        ],
        "providers": provider_status(),
        "window_days": window,
    }


@router.post("/refresh")
async def refresh(user: CurrentUser, db: DbSession, payload: dict = Body(default={})):
    context = await search_context(db, user.id, extra_context=_user_funding_context(db, user.id), force_refresh=True)
    window = int(payload.get("window_days") or get_setting(db, user.id, "funding", "freshness_days",
                                                           settings.funding_freshness_days))
    provider = payload.get("provider") or get_setting(db, user.id, "funding", "provider", settings.funding_provider)
    item = enqueue(db, user_id=user.id, pipeline="funding",
                   payload={"window_days": window, "provider": provider, "stages": payload.get("stages"),
                            "user_context": _user_funding_context(db, user.id)},
                   priority=5, dedupe_key=f"funding:{provider}:{window}")
    return {"queued": item is not None, "pipeline_job_id": item.id if item else None,
            "duplicate": item is None, "provider": provider, "window_days": window,
            "context_source": context.get("source")}


@router.post("/{company_name}/process")
async def process_company(company_name: str, request: Request, user: CurrentUser, db: DbSession):
    """
    Act on a funded company: create a job when roles are open, otherwise draft a
    founder cold email for approval. Never applies or sends by itself.
    """
    row = (
        db.query(FundingCompany)
        .filter(FundingCompany.user_id == user.id, FundingCompany.name.ilike(company_name))
        .first()
    )
    if not row:
        raise HTTPException(404, "Company not in the radar — refresh the scan first")

    profile = db.query(Profile).filter(Profile.user_id == user.id).order_by(Profile.created_at.desc()).first()
    if not profile:
        raise HTTPException(400, "Upload a master resume first")

    if row.has_open_positions:
        existing = db.query(Job).filter(Job.user_id == user.id, Job.company == row.name).first()
        if existing:
            return {"action": "apply_flow", "job_id": existing.id, "company": row.name,
                    "message": "A job for this company is already tracked"}
        score, reason = await ai_score(profile.data or {},
                                       f"Engineering role at {row.name} ({row.stage}, {row.industry}). {row.summary}")
        job = Job(
            user_id=user.id,
            title=f"Open Role at {row.name}",
            company=row.name,
            location="",
            description=f"{row.name} recently raised {row.stage} ({row.industry}). {row.summary}",
            url=f"https://{row.website}/careers" if row.website else "",
            source="funding_radar",
            status="discovered",
            score=score,
            score_reason=reason,
            company_size="startup",
            company_info={"stage": row.stage, "industry": row.industry, "source": row.source},
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        audit.audit(db, "funding.processed", user=user, target=row.name,
                    detail={"job_id": job.id, "action": "job_created"}, request=request)
        return {"action": "apply_flow", "job_id": job.id, "company": row.name,
                "message": f"Job created for {row.name} — apply from the Jobs page"}

    result = await draft_email(db, user, company=row.name, founder=True,
                               extra_context=f"Recently raised {row.stage}. {row.summary}")
    email_row = result["email"]
    audit.audit(db, "funding.processed", user=user, target=row.name,
                detail={"email_id": email_row.id, "action": "founder_email"}, request=request)
    return {"action": "cold_email_founder", "email_id": email_row.id, "company": row.name,
            "decision": result["decision"],
            "message": f"No open role — founder outreach drafted for {row.name} (approval required)"}
