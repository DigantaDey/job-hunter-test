"""Funding radar endpoints — monetized.

Contract (v2.1.2): the AI relevance pass is a hard dependency, so every response
says honestly what happened. ``GET /funding/companies`` always carries
``scan_status`` (``ok`` | ``scan_failed`` | ``paused`` | ``blocked``) and
``last_report`` (the most recent provider/AI report, persisted per user), and an
explicit ``?refresh=true`` that cannot be ranked fails with the standard v2.1
outage shape (pausable 503 ``status="ai_paused"`` + ``retry_after_hint``, or
``state="blocked_needs_action"`` pointing at Settings → AI API) instead of
returning a list the model never saw.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from app.api.deps import CurrentUser, DbSession, search_context
from app.core import audit
from app.core.config import settings
from app.core.entitlements import enforce, increment_usage
from app.core.logging import get_logger
from app.models.models import FundingCompany, Job, Profile
from app.services import funding_radar
from app.services import persona as persona_service
from app.services.ai_client import is_ai_error
from app.services.ai_guardrails import describe_ai_error
from app.services.funding_radar import SCAN_BLOCKED, SCAN_OK, SCAN_PAUSED
from app.services.funding_sources import normalize_company_name, provider_status
from app.services.job_queue import enqueue
from app.services.outreach import draft_email
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
    return {"providers": provider_status(), "active": settings.funding_provider,
            "synthetic_allowed": settings.allow_synthetic_funding_data}


def _days_ago(moment) -> Optional[int]:
    if not moment:
        return None
    return max(0, (datetime.utcnow() - moment).days)


def _company_payload(row: FundingCompany) -> Dict[str, Any]:
    """One radar row, with its provenance and the AI's own explanation."""
    meta: Dict[str, Any] = dict(row.meta or {})
    positions = [p for p in (meta.get("open_positions") or []) if isinstance(p, dict)]
    return {
        "id": row.id,
        "name": row.name,
        "stage": row.stage,
        "raised_at": row.raised_at,
        "days_ago": _days_ago(row.raised_at),
        # The provider reported the event but not a usable date — the UI must not
        # turn that into "raised today".
        "raised_at_estimated": bool(meta.get("raised_at_estimated")),
        "website": row.website,
        "industry": row.industry,
        "summary": row.summary,
        "keywords_matched": row.keywords_matched,
        "why": str(meta.get("why") or ""),
        "rank": meta.get("rank"),
        "source": row.source,
        "verified": row.verified,
        # Real, provider-reported openings only — never inferred (the dead
        # ``has_open_positions`` flag was removed in v2.1.2).
        "open_positions": [{"title": str(p.get("title") or "")[:200],
                            "url": str(p.get("url") or "")[:500]} for p in positions[:5]],
        "discovered_at": row.discovered_at,
        "last_seen_at": row.last_seen_at,
        "url": meta.get("url", ""),
    }


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

    It also carries ``scan_status`` + ``last_report``. An explicit
    ``refresh=true`` that cannot be ranked raises the standard v2.1 outage shape
    (the caller gets a pausable/blocked 503, never an unranked list); an
    *implicit* refresh (page open, radar stale) records the failure in
    ``last_report`` and still returns the rows already on the radar, so a user
    with a broken key sees their last good scan plus the reason it is stale —
    not a blank page.
    """
    window = int(get_setting(db, user.id, "funding", "freshness_days", settings.funding_freshness_days))
    persona = persona_service.get_persona(db, user.id, persona_id) or persona_service.ensure_default_persona(db, user.id)
    extra_context = _user_funding_context(db, user.id)
    context = await search_context(db, user.id, extra_context=extra_context)
    context = persona_service.context_for_persona(context, persona, extra_context=extra_context)
    if not include_unverified:
        include_unverified = bool(get_setting(db, user.id, "funding", "include_unverified", False))

    report = funding_radar.latest_scan_report(db, int(user.id))
    if refresh or funding_radar.funding_needs_refresh(db, user.id):
        enforce(db, user.id, "funding_companies_per_month")
        provider = get_setting(db, user.id, "funding", "provider", settings.funding_provider)
        try:
            companies, report = await funding_radar.scan_funded_companies(
                context, window_days=window, limit=settings.funding_limit, provider=provider,
                db=db, user_id=int(user.id))
        except Exception as exc:
            if not is_ai_error(exc):
                raise
            outage = describe_ai_error(exc, workflow="funding_scan")
            status = SCAN_PAUSED if outage.pausable else SCAN_BLOCKED
            report = funding_radar.store_scan_report(db, int(user.id), funding_radar.outage_report(outage),
                                                     scan_status=status, reason=outage.reason)
            audit.audit(db, "funding.scan_failed", user=user, target=provider,
                        detail={"scan_status": status, "reason": outage.reason,
                                "detail": outage.detail[:300], "explicit": bool(refresh)})
            if refresh:
                # An explicit re-scan is the user asking for AI-ranked results:
                # answer with the outage, not with the stale rows. Wrapping keeps
                # the workflow (and therefore the diagnosis) in the 503 body.
                raise describe_ai_error(exc, workflow="funding_scan") from exc
            log.info("funding auto-refresh for user %s unavailable (%s): %s",
                     user.id, outage.reason, outage.detail)
        else:
            if report.get("scan_status") == SCAN_OK:
                funding_radar.sync_funding_db(db, user.id, companies, window)
            else:
                # Nothing was fetched — do not prune a radar during an outage.
                companies = []
            report = funding_radar.store_scan_report(db, int(user.id), report)
            audit.audit(db, "funding.scan" if report.get("scan_status") == SCAN_OK else "funding.scan_failed",
                        user=user, target=provider,
                        detail={"found": len(companies), "scan_status": report.get("scan_status"),
                                "reason": report.get("reason"), "errors": report.get("errors") or {},
                                "provider_report": report})
            increment_usage(db, user.id, "funding_companies_per_month", len(companies))

    rows = funding_radar.list_funding_companies(db, user.id, stage=stage, include_unverified=include_unverified)
    profile = db.query(Profile).filter(Profile.user_id == user.id).order_by(Profile.created_at.desc()).first()
    refreshed_at = report.get("scanned_at") or None
    if not refreshed_at:
        latest = max((row.last_seen_at or row.discovered_at for row in rows
                      if row.last_seen_at or row.discovered_at), default=None)
        refreshed_at = latest.isoformat() if latest else None
    return {
        "companies": [_company_payload(row) for row in rows],
        "providers": provider_status(),
        "window_days": window,
        "scan_status": str(report.get("scan_status") or SCAN_OK),
        "reason": report.get("reason"),
        "scanned": report.get("scanned"),
        "last_report": report or None,
        "context": context,
        "refreshed_at": refreshed_at,
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
    audit.audit(db, "funding.refresh_queued", user=user, target=provider,
                detail={"window_days": window, "focus": focus[:200], "queued": item is not None})
    return {"queued": item is not None, "pipeline_job_id": item.id if item else None,
            "duplicate": item is None, "provider": provider, "window_days": window,
            "context_source": context.get("source"), "focus_applied": focus,
            "context": context}


def _find_company(db, user_id: int, company_name: str) -> Optional[FundingCompany]:
    """Look a radar row up by normalised name.

    ``ilike(company_name)`` used to treat ``%``/``_`` in the path as wildcards
    (any row of that user could match) and missed the casing normalisation the
    sync now applies — so the lookup uses the same key the dedupe does.
    """
    key = normalize_company_name(company_name)
    if not key:
        return None
    exact = (
        db.query(FundingCompany)
        .filter(FundingCompany.user_id == user_id, FundingCompany.name == company_name)
        .first()
    )
    if exact is not None:
        return exact
    for row in db.query(FundingCompany).filter(FundingCompany.user_id == user_id).all():
        if normalize_company_name(row.name) == key:
            return row
    return None


def _provider_positions(row: FundingCompany) -> List[Dict[str, Any]]:
    """Open positions the *provider* reported for this company (may be empty)."""
    meta: Dict[str, Any] = dict(row.meta or {})
    return [p for p in (meta.get("open_positions") or [])
            if isinstance(p, dict) and str(p.get("title") or "").strip()]


def _funding_job_description(row: FundingCompany, position: Dict[str, Any], url: str) -> str:
    """Facts only — the funding event and the provider's own posting text.

    v2.1.2 removed the fabricated posting ("Open Role at {name}" described as
    "{name} recently raised {stage}…") that the AI was then asked to score as
    though a recruiter had written it.
    """
    meta: Dict[str, Any] = dict(row.meta or {})
    facts: List[str] = []
    stage = row.stage or "Undisclosed"
    facts.append(f"Funding event: {stage} round reported by {row.source or 'unknown'}"
                 f"{' (verified source)' if row.verified else ' (unverified source)'}.")
    if row.raised_at and not meta.get("raised_at_estimated"):
        facts.append(f"Announced: {row.raised_at.date().isoformat()}.")
    if meta.get("raised_usd"):
        facts.append(f"Amount reported: USD {float(meta['raised_usd']):,.0f}.")
    if row.industry:
        facts.append(f"Industry: {row.industry}.")
    if row.summary:
        facts.append(f"Provider summary: {row.summary}")
    if row.website:
        facts.append(f"Website: {row.website}.")
    facts.append(f"Open position reported by the provider: {str(position.get('title')).strip()}")
    if str(position.get("location") or "").strip():
        facts.append(f"Location reported by the provider: {str(position['location']).strip()}")
    if str(position.get("summary") or "").strip():
        facts.append(f"Provider posting text: {str(position['summary']).strip()}")
    if url:
        facts.append(f"Source: {url}")
    facts.append("No job description was available — this record contains only the funding "
                 "provider's own fields, and is scored from funding context.")
    return "\n".join(facts)


def _outreach_context(row: FundingCompany) -> str:
    """Factual funding angle for the founder draft (never an invented role)."""
    meta: Dict[str, Any] = dict(row.meta or {})
    stage = (row.stage or "Undisclosed").strip()
    when = ""
    if row.raised_at and not meta.get("raised_at_estimated"):
        when = f" on {row.raised_at.date().isoformat()}"
    if stage == "Undisclosed":
        head = f"Reported a private placement (SEC Form D){when}"
    else:
        head = f"Reported a {stage} round{when}"
    parts = [head + "."]
    if row.industry:
        parts.append(f"Sector on record: {row.industry}.")
    if row.summary:
        parts.append(row.summary.strip()[:400])
    return " ".join(part for part in parts if part)


@router.post("/{company_name}/process")
async def process_company(company_name: str, request: Request, user: CurrentUser, db: DbSession):
    """
    Act on a funded company.

    Two honest outcomes, decided by what the *provider* actually reported:

    * ``meta.open_positions`` carries a real posting → track it as a Job, scored
      from funding context (``score_source="funding_context"``) because there is
      no job description to score, and labelled as such;
    * otherwise → draft a founder cold email for approval.

    Nothing is applied to and nothing is sent: drafts stay in the approval
    bucket.
    """
    row = _find_company(db, int(user.id), company_name)
    if not row:
        raise HTTPException(404, "Company not in the radar — refresh the scan first")

    profile = db.query(Profile).filter(Profile.user_id == user.id).order_by(Profile.created_at.desc()).first()
    if not profile:
        raise HTTPException(400, "Upload a master resume first")
    profile_persona = persona_service.get_persona(db, user.id, None)

    positions = _provider_positions(row)
    if positions:
        position = positions[0]
        existing = db.query(Job).filter(Job.user_id == user.id, Job.company == row.name).first()
        if existing:
            return {"action": "apply_flow", "job_id": existing.id, "company": row.name,
                    "positions_source": "provider",
                    "message": "A job for this company is already tracked"}
        meta: Dict[str, Any] = dict(row.meta or {})
        job_title = str(position.get("title") or "").strip()[:300]
        url = str(position.get("url") or "").strip() or str(meta.get("careers_url") or "").strip()
        url_source = "provider_position" if position.get("url") else (
            "provider_careers_url" if meta.get("careers_url") else "")
        if not url and row.website:
            url, url_source = f"https://{row.website}", "company_website"
        job = Job(
            user_id=user.id,
            title=job_title,
            company=row.name,
            location=str(position.get("location") or "").strip()[:200],
            description=_funding_job_description(row, position, url),
            url=url[:500],
            source="funding_radar",
            # Without a key two funding jobs for one user would collide on the
            # UNIQUE(user_id, dedupe_key) default "" — and a re-run must not
            # create the same tracked job twice.
            dedupe_key=f"funding_radar:{hashlib.sha256(f'{row.name}|{job_title}'.encode()).hexdigest()[:24]}",
            status="discovered",
            # Honest provenance: there is no JD, so there is no AI verdict to
            # show. The old code scored a sentence it had just invented.
            score=0.0,
            score_reason="Scored from funding context — no job description available",
            score_source="funding_context",
            score_detail={"stage": row.stage, "industry": row.industry, "source": row.source,
                          "verified": bool(row.verified), "open_positions": len(positions),
                          "scored_by": "funding_radar",
                          "note": "No job description was available; funding context only."},
            persona_id=profile_persona.id if profile_persona else None,
            # ``company_size`` stays "unknown": the provider reported a round,
            # not a headcount, and "startup" would be our guess.
            company_info={"stage": row.stage, "industry": row.industry, "source": row.source,
                          "funding_url": meta.get("url", ""), "positions_source": "provider",
                          "url_source": url_source},
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        audit.audit(db, "funding.processed", user=user, target=row.name,
                    detail={"job_id": job.id, "action": "job_created", "positions_source": "provider",
                            "score_source": job.score_source}, request=request)
        return {"action": "apply_flow", "job_id": job.id, "company": row.name,
                "positions_source": "provider", "score_source": job.score_source,
                "url_source": url_source,
                "message": (f"{job.title} at {row.name} tracked from the provider's own posting — "
                            "apply from the Jobs page")}

    result = await draft_email(db, user, company=row.name, founder=True,
                               extra_context=_outreach_context(row))
    email_row = result["email"]
    audit.audit(db, "funding.processed", user=user, target=row.name,
                detail={"email_id": email_row.id, "action": "founder_email"}, request=request)
    return {
        "action": "cold_email_founder",
        "email_id": email_row.id,
        "company": row.name,
        "decision": result["decision"],
        "positions_source": "none_reported",
        "message": f"No open role reported by the provider — founder outreach drafted for "
                   f"{row.name} (approval required)",
    }
