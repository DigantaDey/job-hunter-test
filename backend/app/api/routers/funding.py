"""Funding radar endpoints — monetized."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Request

from app.api.deps import CurrentUser, DbSession, search_context
from app.core import audit
from app.core.config import settings
from app.core.entitlements import enforce, increment_usage
from app.core.logging import get_logger
from app.models.models import FundingCompany, Job, Profile
from app.services import funding_radar
from app.services import persona as persona_service
from app.services.funding_sources import provider_status
from app.services.job_queue import enqueue
from app.services.outreach import draft_email
from app.services.scoring import score_job
from app.services.user_settings import get_setting, set_setting

router = APIRouter(prefix="/funding", tags=["funding"])
log = get_logger("app.funding_api")


def _user_funding_context(db, user_id: int) -> str:
    rows = get_setting(db, user_id, "funding", "context_notes", "") or ""
    industries = get_setting(db, user_id, "funding", "industries", "") or ""
    return ", ".join(str(part) for part in (rows, industries) if part)


@router.get("/context")
async def funding_context(user: CurrentUser, db: DbSession, persona_id: Optional[int] = None):
    extra_context = _user_funding_context(db, user.id)
    context = await search_context(db, user.id, extra_context=extra_context)
    persona = persona_service.get_persona(db, user.id, persona_id)
    return persona_service.context_for_persona(context, persona, extra_context=extra_context)


@router.get("/providers")
def providers():
    return {"providers": provider_status(), "active": settings.funding_provider, "synthetic_allowed": settings.allow_synthetic_funding_data}


def _days_ago(moment) -> Optional[int]:
    if not moment:
        return None
    return max(0, (datetime.utcnow() - moment).days)


@router.get("/companies")
async def list_companies(
    user: CurrentUser,
    db: DbSession,
    stage: Optional[str] = None,
    refresh: bool = False,
    include_unverified: bool = False,
    persona_id: Optional[int] = None,
):
    """
    Return the radar rows for this user, refreshing when stale or requested.

    The response carries the *search context that produced the rows* (with its
    ``profile_id``) — without it the UI cannot tell "no resume uploaded" from
    "resume uploaded, nothing matched", which is what made the page claim no
    resume existed after a successful upload.
    """
    window = int(get_setting(db, user.id, "funding", "freshness_days", settings.funding_freshness_days))
    persona = persona_service.get_persona(db, user.id, persona_id) or persona_service.ensure_default_persona(db, user.id)
    extra_context = _user_funding_context(db, user.id)

    if refresh or funding_radar.funding_needs_refresh(db, user.id, window):
        enforce(db, user.id, "funding_companies_per_month")
        context = await search_context(db, user.id, extra_context=extra_context)
        context = persona_service.context_for_persona(context, persona, extra_context=extra_context)
        provider = get_setting(db, user.id, "funding", "provider", settings.funding_provider)
        companies, report = await funding_radar.scan_funded_companies(context, window_days=window, limit=settings.funding_limit, provider=provider,
                                                                      db=db, user_id=int(user.id))
        funding_radar.sync_funding_db(db, user.id, companies, window)
        audit.audit(db, "funding.scan", user_id=user.id, target=provider, detail={"found": len(companies), "provider_report": report})
        increment_usage(db, user.id, "funding_companies_per_month", len(companies))
    else:
        context = await search_context(db, user.id, extra_context=extra_context)
        context = persona_service.context_for_persona(context, persona, extra_context=extra_context)
        if not include_unverified:
            include_unverified = bool(get_setting(db, user.id, "funding", "include_unverified", False))

    rows = funding_radar.list_funding_companies(db, user.id, stage=stage, include_unverified=include_unverified)
    profile = db.query(Profile).filter(Profile.user_id == user.id).order_by(Profile.created_at.desc()).first()
    latest = max((row.discovered_at for row in rows if row.discovered_at), default=None)
    return {
        "companies": [
            {
                "id": row.id,
                "name": row.name,
                "stage": row.stage,
                "raised_at": row.raised_at,
                "days_ago": _days_ago(row.raised_at),
                "website": row.website,
                "industry": row.industry,
                "summary": row.summary,
                "keywords_matched": row.keywords_matched,
                "source": row.source,
                "verified": row.verified,
                "has_open_positions": row.has_open_positions,
                "discovered_at": row.discovered_at,
                "url": (row.meta or {}).get("url", ""),
            }
            for row in rows
        ],
        "providers": provider_status(),
        "window_days": window,
        "context": context,
        "refreshed_at": latest.isoformat() if latest else None,
        "resume": ({
            "id": profile.master_resume_id,
            "profile_id": profile.id,
            "name": (profile.data or {}).get("name", ""),
            "current_title": (profile.data or {}).get("current_title", ""),
            "extraction_source": profile.extraction_source or "ai",
        } if profile else None),
        "persona": persona_service.to_dict(persona, include_memory=False) if persona else None,
    }


@router.post("/refresh")
async def refresh(user: CurrentUser, db: DbSession, payload: dict = Body(default={})):
    """
    Queue a funding re-scan.

    Any "focus" text the user typed is persisted to their funding settings
    first — previously it was accepted and then dropped, so re-scanning with a
    new focus changed nothing.
    """
    enforce(db, user.id, "funding_companies_per_month")

    focus = str((payload or {}).get("context") or "").strip()
    if focus:
        set_setting(db, user.id, "funding", "context_notes", focus[:600])
        db.commit()
    industries = str((payload or {}).get("industries") or "").strip()
    if industries:
        set_setting(db, user.id, "funding", "industries", industries[:400])
        db.commit()

    extra_context = _user_funding_context(db, user.id)
    persona = persona_service.get_persona(db, user.id, (payload or {}).get("persona_id")) or \
        persona_service.ensure_default_persona(db, user.id)
    context = await search_context(db, user.id, extra_context=extra_context, force_refresh=True)
    context = persona_service.context_for_persona(context, persona, extra_context=extra_context)
    window = int((payload or {}).get("window_days") or get_setting(db, user.id, "funding", "freshness_days", settings.funding_freshness_days))
    provider = (payload or {}).get("provider") or get_setting(db, user.id, "funding", "provider", settings.funding_provider)
    # The dedupe key includes the focus/persona: re-scanning with different
    # input is different work, and the queue constraint is per (user, key).
    item = enqueue(
        db,
        user_id=user.id,
        pipeline="funding",
        payload={"window_days": window, "provider": provider, "stages": (payload or {}).get("stages"),
                 "user_context": extra_context, "persona_id": persona.id if persona else None,
                 "context": context},
        priority=5,
        dedupe_key=f"funding:{provider}:{window}:{persona.id if persona else 0}:{hashlib.sha256(extra_context.encode()).hexdigest()[:10]}",
    )
    audit.audit(db, "funding.refresh_queued", user_id=user.id, target=provider,
                detail={"window_days": window, "focus": focus[:200], "queued": item is not None})
    return {"queued": item is not None, "pipeline_job_id": item.id if item else None,
            "duplicate": item is None, "provider": provider, "window_days": window,
            "context_source": context.get("source"), "focus_applied": focus,
            "context": context}


@router.post("/{company_name}/process")
async def process_company(company_name: str, request: Request, user: CurrentUser, db: DbSession):
    """
    Act on a funded company: create a job when roles are open, otherwise draft a
    founder cold email for approval. Never applies or sends by itself.
    """
    row = db.query(FundingCompany).filter(FundingCompany.user_id == user.id, FundingCompany.name.ilike(company_name)).first()
    if not row:
        raise HTTPException(404, "Company not in the radar — refresh the scan first")

    profile = db.query(Profile).filter(Profile.user_id == user.id).order_by(Profile.created_at.desc()).first()
    if not profile:
        raise HTTPException(400, "Upload a master resume first")
    profile_persona = persona_service.get_persona(db, user.id, None)

    if row.has_open_positions:
        existing = db.query(Job).filter(Job.user_id == user.id, Job.company == row.name).first()
        if existing:
            return {"action": "apply_flow", "job_id": existing.id, "company": row.name, "message": "A job for this company is already tracked"}
        result = await score_job(profile.data or {}, f"Engineering role at {row.name} ({row.stage}, {row.industry}). {row.summary}", db=db, user_id=user.id)
        score, reason = result["score"], result["reason"]
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
            score_source=result["score_source"],
            score_detail=result.get("detail") or {},
            persona_id=profile_persona.id if profile_persona else None,
            company_size="startup",
            company_info={"stage": row.stage, "industry": row.industry, "source": row.source},
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        audit.audit(db, "funding.processed", user=user, target=row.name, detail={"job_id": job.id, "action": "job_created"}, request=request)
        return {"action": "apply_flow", "job_id": job.id, "company": row.name, "message": f"Job created for {row.name} — apply from the Jobs page"}

    result = await draft_email(db, user, company=row.name, founder=True, extra_context=f"Recently raised {row.stage}. {row.summary}")
    email_row = result["email"]
    audit.audit(db, "funding.processed", user=user, target=row.name, detail={"email_id": email_row.id, "action": "founder_email"}, request=request)
    return {
        "action": "cold_email_founder",
        "email_id": email_row.id,
        "company": row.name,
        "decision": result["decision"],
        "message": f"No open role — founder outreach drafted for {row.name} (approval required)",
    }
