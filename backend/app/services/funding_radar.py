"""
Funding Radar — recently funded companies (Seed → Series D where the data
supports it) matched to the candidate's extracted context.

Data provenance is explicit on every row (``source`` + ``verified``): SEC EDGAR
Form D filings are real and linked; Crunchbase/Tracxn/imported datasets are
licensed; the synthetic demo set is opt-in and labelled.

AI is a hard dependency (v2.1)
-----------------------------
:func:`scan_funded_companies` never returns events the model did not judge.
One grounded pass (:func:`funding_sources.ai_rank_events`) decides relevance,
order and the one-sentence ``why`` — the old keyword-string pre-filter is gone,
including its inverted form (zero keyword matches used to disable the filter and
return *everything*). Outcomes:

* no key / invalid key / undecryptable key → ``AIClientError`` with a blocked
  reason ⇒ ``state="blocked_needs_action"`` (sync) or dead-letter (queued);
* timeout / unreachable / 429 / 5xx / breaker → transient ⇒ pausable 503 (sync)
  or ``paused`` (queued, drained when the provider is back);
* a company name the providers never returned → ``guardrail_failed``, blocked;
* nothing relevant → ``scan_status="ok"`` + ``reason="no_matching_events"``.

An empty radar always carries a ``reason`` so "nothing matched your focus" is
never confused with "the scan could not run" (``provider_errors``).

Persistence
-----------
:func:`sync_funding_db` upserts on the **normalised** company name
(:func:`funding_sources.normalize_company_name` — strip + casefold), so
"Stripe" and "stripe" are one row; the stored ``name`` keeps the provider's
canonical casing from first sight (a stable identity in the UI).
``discovered_at`` is first-seen and is never rewritten; ``last_seen_at`` is
stamped by every scan that still returns the row. Provider ``meta`` is *merged*
(not replaced) so facts reported earlier — filing URL, CIK — survive a re-scan
whose payload omits them; the keys the new event does carry always win, and
hiring data (``open_positions``) is deliberately never carried over from an
older scan.

:func:`prune_funding_db` deletes a user's rows whose ``last_seen_at`` (falling
back to ``discovered_at`` for rows written before v2.1.2) is older than the
freshness window, as one bulk ``DELETE`` served by ``ix_funding_user_seen``.
So a company leaves the radar ``window_days`` after the providers stopped
reporting it, while a row that keeps being re-confirmed is never
deleted-and-recreated. The old code rewrote ``discovered_at`` on every sync,
which made "discovered 2 minutes ago" meaningless and made prune a per-row
``DELETE`` loop. Prune only runs as part of a sync, and a sync only happens
after a scan that actually succeeded — an outage must not erase the radar.

The most recent scan report (provider counts and errors, AI diagnostics,
timestamps, the failure diagnosis when there was one) is persisted per user as
the settings row ``funding.last_report`` — a settings key rather than
``FundingCompany.meta`` because the report matters *most* when the scan produced
no rows to hang it on. It is server-written: it is deliberately not in
``user_settings.WRITABLE_KEYS``, so a client cannot forge a green scan.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc
from app.models.models import FundingCompany, FundingScan, FundingScanCompany, Job
from app.services import funding_sources
from app.services.company_normalize import normalize_company_name as normalize_link_name
from app.services.funding_sources import (
    STAGES,
    FundingEvent,
    normalize_company_name,
    normalize_stage,
)
from app.services.sources.base import parse_datetime
from app.services.user_settings import get_setting, set_setting

log = get_logger("app.funding_radar")

#: ``scan_status`` values shared by the scan report, the persisted
#: ``funding.last_report`` and ``GET /api/funding/companies``.
SCAN_OK = "ok"
SCAN_FAILED = "scan_failed"      # every requested provider failed / is unusable
SCAN_PAUSED = "paused"           # transient AI outage — resumes on its own
SCAN_BLOCKED = "blocked"         # AI needs action (no key, bad key, guardrail)
SCAN_STATES = (SCAN_OK, SCAN_FAILED, SCAN_PAUSED, SCAN_BLOCKED)

#: ``reason`` (lowercase snake_case) for an empty or failed radar.
REASON_NO_MATCHING_EVENTS = "no_matching_events"
REASON_NO_EVENTS = "no_events_fetched"
REASON_PROVIDER_ERRORS = "provider_errors"

#: Settings key holding the most recent scan report for a user.
REPORT_KEY = "last_report"
REPORT_CATEGORY = "funding"


def require_ai_for_scan(db=None, user_id: Optional[int] = None) -> None:
    """Fail fast when the funding radar cannot be ranked.

    The radar's product *is* the AI relevance verdict, so an unconfigured model
    is a blocked outcome (``state="blocked_needs_action"``, fix pointing at
    Settings → AI API) — not a licence to hand back the raw provider list. The
    check runs before any provider call: a blocked scan must not burn outbound
    requests, the monthly quota, or the user's patience.
    """
    from app.services.ai_client import AIClientError, resolve_config, resolve_config_for_user

    cfg: Dict[str, Any] = {}
    if db is not None and user_id is not None:
        try:
            cfg = resolve_config_for_user(db, int(user_id), "funding_scan")
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("funding_scan AI config resolution failed: %s", exc)
    if not cfg:
        cfg = resolve_config("funding_scan")
    if str(cfg.get("api_key") or "").strip():
        return
    # A key that exists but cannot be decrypted is a different fix than no key.
    reason = "stored_key_unreadable" if cfg.get("key_error") else "no_api_key"
    raise AIClientError(
        f"{reason}: the funding radar ranks every event with the AI model, so it cannot run "
        "without a working API key",
        reason=reason, retryable=False, meta={"workflow": "funding_scan"},
    )


def _candidate_limit(limit: int) -> int:
    """How many events to fetch for the AI to judge, given the radar size.

    Relevance is now the model's job, so it gets a wider candidate pool than the
    radar shows: the freshest ``limit`` events alone could all be irrelevant
    while a slightly older event is a perfect match.
    """
    return min(funding_sources.MAX_AI_CANDIDATES, max(int(limit or 1), int(limit or 1) * 2))


async def scan_funded_companies(
    context: Dict[str, Any],
    *,
    stages: Optional[List[str]] = None,
    window_days: int = 45,
    limit: int = 18,
    provider: Optional[str] = None,
    db=None,
    user_id: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch real funding events, then let the AI decide relevance, order and why.

    Returns ``(companies, report)``. ``report["scan_status"]`` is ``"ok"`` or
    ``"scan_failed"``; an empty ``companies`` list always carries
    ``report["reason"]``. AI failures raise (see the module docstring) — this
    function has no unranked fallback path.
    """
    require_ai_for_scan(db=db, user_id=user_id)

    events, report = await funding_sources.fetch_funding_events(
        context, window_days=window_days, limit=_candidate_limit(limit), provider=provider
    )
    report["provider_status"] = funding_sources.provider_status()
    report["limit"] = int(limit)
    report["returned"] = 0

    if report.get("scan_status") == SCAN_FAILED:
        # Every requested provider failed: an outage, not an empty radar.
        report["reason"] = REASON_PROVIDER_ERRORS
        report["scanned"] = 0
        return [], report

    if stages:
        wanted = {normalize_stage(s) for s in stages}
        before = len(events)
        events = [e for e in events if e.stage in wanted or e.stage == "Undisclosed"]
        report["stage_filter"] = {"wanted": sorted(wanted), "kept": len(events),
                                  "dropped": before - len(events)}

    if not events:
        report.update({"scan_status": SCAN_OK, "reason": REASON_NO_EVENTS, "scanned": 0})
        return [], report

    ranked, ai_report = await funding_sources.ai_rank_events(
        context, events, db=db, user_id=user_id
    )
    report["ai"] = ai_report
    report["scanned"] = len(events)

    companies: List[Dict[str, Any]] = []
    for position, item in enumerate((r for r in ranked if r.matched), start=1):
        if position > limit:
            break
        row = item.event.to_dict()
        row["rank"] = position
        row["why"] = item.why
        # Provenance label only — keywords never decide relevance any more.
        row["keywords_matched"] = _matched_keywords(item.event, context)
        row["open_positions"] = item.event.open_positions()[:5]
        companies.append(row)

    report["scan_status"] = SCAN_OK
    report["reason"] = None if companies else REASON_NO_MATCHING_EVENTS
    report["returned"] = len(companies)
    return companies, report


def _matched_keywords(event: FundingEvent, context: Dict[str, Any]) -> List[str]:
    """Which of the candidate's own keywords appear in this event's text.

    A provenance label for the UI ("↳ fintech"), never a filter: relevance is
    the model's verdict.
    """
    hay = f"{event.industry} {event.summary} {event.name}".lower()
    keys = [str(k) for k in (context.get("funding_focus") or []) + (context.get("industries") or [])
            + (context.get("keywords") or [])]
    seen: List[str] = []
    for key in keys:
        needle = key.strip().lower()
        if needle and needle in hay and needle not in seen:
            seen.append(needle)
    return seen[:6]


def sync_funding_db(db: Session, user_id: int, companies: List[Dict[str, Any]],
                    window_days: int) -> Dict[str, int]:
    """Upsert scanned companies, then prune what the freshness window dropped.

    Only call this with the result of a scan that succeeded: pruning during an
    outage would delete a radar the providers are merely failing to confirm.
    """
    now = datetime.utcnow()
    # One query for the user's radar instead of one per company (N+1), keyed by
    # the normalised name so casing differences collapse onto the same row.
    existing: Dict[str, FundingCompany] = {}
    for stored in db.query(FundingCompany).filter(FundingCompany.user_id == user_id).all():
        # ``name_normalized`` is the stored identity (and the UNIQUE key); fall
        # back to normalising the display name for rows written before the
        # column existed.
        existing.setdefault(stored.name_normalized or normalize_company_name(stored.name), stored)

    added = updated = 0
    for company in companies:
        name = str(company.get("name") or "").strip()
        key = normalize_company_name(name)
        if not key:
            continue
        row = existing.get(key)
        raised_at = company.get("raised_at") or None
        if row is None:
            row = FundingCompany(user_id=user_id, name=name[:200], name_normalized=key,
                                 discovered_at=now)
            db.add(row)
            existing[key] = row
            added += 1
        else:
            updated += 1  # display name keeps the first-seen provider casing
        meta: Dict[str, Any] = dict(row.meta or {})
        provider_meta: Dict[str, Any] = dict(company.get("meta") or {})
        meta.update(provider_meta)
        meta.update({
            "url": company.get("url", "") or meta.get("url", ""),
            "raised_usd": company.get("raised_usd") if company.get("raised_usd") is not None
            else meta.get("raised_usd"),
            # The row is real; the *day* it happened may not be (a Form D with no
            # parseable date). The UI must not claim "raised today".
            "raised_at_estimated": bool(provider_meta.get("raised_at_estimated")) or raised_at is None,
            "why": company.get("why", "") or "",
            "rank": company.get("rank"),
            # Hiring data is deliberately *not* carried over from an earlier
            # scan: a role no provider reports any more must not keep the apply
            # flow alive.
            "open_positions": provider_meta.get("open_positions") or [],
            "careers_url": provider_meta.get("careers_url", "") or "",
            "last_scan": now.isoformat(),
        })
        row.stage = company.get("stage") or "Undisclosed"
        # An event with no parseable date keeps the date we already had rather
        # than being re-stamped "today" (the flag below says it is an estimate).
        resolved_raised: Any = raised_at or row.raised_at or now
        row.raised_at = resolved_raised
        row.website = company.get("website") or ""
        row.industry = company.get("industry") or ""
        row.summary = company.get("summary") or ""
        row.keywords_matched = company.get("keywords_matched") or []
        row.source = company.get("source") or "unknown"
        row.verified = bool(company.get("verified"))
        row.last_seen_at = now
        row.meta = meta
    db.commit()
    # v2.2.5 linkage — keep has_open_positions fresh via the shared matcher.
    try:
        refresh_funding_has_open_positions(db, user_id)
    except Exception as exc:  # noqa: BLE001 - sync is best-effort
        log.warning("has_open_positions sync failed for user %s: %s", user_id, exc)
    pruned = prune_funding_db(db, user_id, window_days)
    inc("jobhunter_funding_sync_total", added=added, updated=updated, pruned=pruned)
    return {"added": added, "updated": updated, "pruned": pruned}


def prune_funding_db(db: Session, user_id: int, window_days: int) -> int:
    """Delete rows the providers have not reported within ``window_days``.

    One bulk ``DELETE`` on ``(user_id, COALESCE(last_seen_at, discovered_at))``;
    rows with no usable timestamp are kept (never delete what we cannot date).
    """
    cutoff = datetime.utcnow() - timedelta(days=max(1, window_days))
    last_seen = func.coalesce(FundingCompany.last_seen_at, FundingCompany.discovered_at)
    deleted = (
        db.query(FundingCompany)
        .filter(FundingCompany.user_id == user_id, last_seen < cutoff)
        .delete(synchronize_session=False)
    )
    if deleted:
        db.commit()
        inc("jobhunter_funding_pruned_total", value=int(deleted))
    return int(deleted or 0)


#: The meter a funding scan is billed to. One scan = one unit, wherever the
#: scan ran.
FUNDING_SCAN_METER = "funding_companies_per_month"


def funding_scan_charged(report: Optional[Dict[str, Any]]) -> bool:
    """Is this scan outcome billable? Only a scan that actually succeeded is.

    ``paused``/``blocked`` (AI outage) and ``scan_failed`` (every provider
    failed) are outages, not work the user asked for and got — they cost
    nothing, which is what makes the sync and the queued path cost the same.
    """
    return str((report or {}).get("scan_status") or "") == SCAN_OK


def charge_funding_scan(db: Session, user_id: int, report: Optional[Dict[str, Any]]) -> int:
    """The single charge point for a funding scan (sync **and** queued).

    Brief 5's convention: charge where the work actually happened, exactly
    once, and never for a failure. Before this, the interactive radar billed
    ``len(companies)`` per scan while the queued path billed at *trigger* time
    (and never refunded a failed job), so the same scan cost anywhere between
    nothing and twenty units depending on which button produced it.

    Returns the number of units charged (0 or 1). Never raises: the ledger must
    not be able to lose a scan that already ran.
    """
    if not funding_scan_charged(report):
        return 0
    from app.core.entitlements import increment_usage

    try:
        increment_usage(db, int(user_id), FUNDING_SCAN_METER, 1)
    except Exception as exc:  # noqa: BLE001 - never lose the scan over the ledger
        log.warning("could not charge %s for user %s: %s", FUNDING_SCAN_METER, user_id, exc)
        return 0
    return 1


def find_funding_company(db: Session, user_id: int, company_name: str) -> Optional[FundingCompany]:
    """Look a radar row up by its stored normalised key (one indexed query).

    The key is the same one the sync dedupes on, so a name that differs only in
    casing, surrounding or repeated whitespace finds the row — and ``%``/``_``
    are plain characters, not wildcards (the old ``ilike`` made any row of the
    user matchable from the URL).
    """
    key = normalize_company_name(company_name)
    if not key:
        return None
    return (
        db.query(FundingCompany)
        .filter(FundingCompany.user_id == int(user_id), FundingCompany.name_normalized == key)
        .order_by(FundingCompany.id)
        .first()
    )


def refresh_funding_has_open_positions(db: Session, user_id: int) -> int:
    """Keep FundingCompany.has_open_positions fresh via the shared matcher.

    Exact match on the shared normalized name only — no fuzzy, no substring.
    Called from sync_funding_db and from discovery persistence (both directions).
    """
    rows = db.query(FundingCompany).filter(FundingCompany.user_id == int(user_id)).all()
    if not rows:
        return 0
    jobs = db.query(Job).filter(Job.user_id == int(user_id)).all()
    job_norms = {normalize_link_name(j.company) for j in jobs if str(j.company or "").strip()}
    job_norms.discard("")
    changed = 0
    for fc in rows:
        norm = normalize_link_name(fc.name)
        should = bool(norm and norm in job_norms)
        if bool(fc.has_open_positions) != should:
            fc.has_open_positions = should
            changed += 1
    if changed:
        db.commit()
    return changed


def _prune_funding_scans(db: Session, user_id: int, keep: int = 30) -> int:
    """Retention: keep 30 newest scans per user."""
    rows = (
        db.query(FundingScan.id)
        .filter(FundingScan.user_id == int(user_id))
        .order_by(FundingScan.scanned_at.desc(), FundingScan.id.desc())
        .all()
    )
    ids = [r[0] for r in rows]
    if len(ids) <= keep:
        return 0
    to_delete = ids[keep:]
    db.query(FundingScanCompany).filter(FundingScanCompany.scan_id.in_(to_delete)).delete(synchronize_session=False)
    deleted = db.query(FundingScan).filter(FundingScan.id.in_(to_delete)).delete(synchronize_session=False)
    db.commit()
    return int(deleted or 0)


def persist_funding_scan(
    db: Session, user_id: int, report: Dict[str, Any] | None, companies: List[Dict[str, Any]] | None
):
    """Persist one funding scan and its membership (v2.2.5 history).

    Called for every completed scan (ok or scan_failed) — the history records
    what *this* scan returned, not the union. Handles retention (30 newest).
    """
    report = dict(report or {})
    companies = list(companies or [])
    status = str(report.get("scan_status") or SCAN_OK)
    if status not in (SCAN_OK, SCAN_FAILED):
        if status in (SCAN_PAUSED, SCAN_BLOCKED):
            return None
        status = SCAN_FAILED if status else SCAN_OK
    events_seen = int(report.get("scanned") or report.get("fetched") or report.get("total") or 0)
    companies_found = int(report.get("returned") if report.get("returned") is not None else len(companies))
    provider_errors = report.get("errors") or report.get("provider_errors") or {}
    if not isinstance(provider_errors, dict):
        provider_errors = {"error": str(provider_errors)[:300]}
    now = datetime.utcnow()
    scan = FundingScan(
        user_id=int(user_id),
        scanned_at=now,
        status=status,
        provider_errors=dict(provider_errors),
        events_seen=events_seen,
        companies_found=companies_found,
        meta=dict(report),
    )
    db.add(scan)
    db.flush()
    if companies:
        existing: Dict[str, FundingCompany] = {}
        link_lookup: Dict[str, FundingCompany] = {}
        for row in db.query(FundingCompany).filter(FundingCompany.user_id == int(user_id)).all():
            fk = row.name_normalized or funding_sources.normalize_company_name(row.name)
            if fk and fk not in existing:
                existing[fk] = row
            lk = normalize_link_name(row.name)
            if lk and lk not in link_lookup:
                link_lookup[lk] = row
        for idx, comp in enumerate(companies):
            name = str(comp.get("name") or "").strip()
            if not name:
                continue
            fk = funding_sources.normalize_company_name(name)
            row = existing.get(fk)
            if row is None:
                lk = normalize_link_name(name)
                row = link_lookup.get(lk) if lk else None
            if row is None:
                continue
            rank = int(comp.get("rank") or (idx + 1))
            why = str(comp.get("why") or "")[:500]
            db.add(FundingScanCompany(scan_id=scan.id, company_id=row.id, rank=rank, why=why))
    db.commit()
    db.refresh(scan)
    _prune_funding_scans(db, int(user_id))
    return scan


def get_funding_history(db: Session, user_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    """Recent funding scans with diff new/lost (v2.2.5)."""
    lim = min(30, max(1, int(limit or 5)))
    scans: List[FundingScan] = (
        db.query(FundingScan)
        .filter(FundingScan.user_id == int(user_id))
        .order_by(FundingScan.scanned_at.desc(), FundingScan.id.desc())
        .limit(lim)
        .all()
    )
    if not scans:
        return []
    scan_ids = [s.id for s in scans]
    memberships: List[FundingScanCompany] = (
        db.query(FundingScanCompany).filter(FundingScanCompany.scan_id.in_(scan_ids)).all()
    )
    from collections import defaultdict
    mem_by_scan: Dict[int, List[FundingScanCompany]] = defaultdict(list)
    company_ids: set[int] = set()
    for m in memberships:
        mem_by_scan[m.scan_id].append(m)
        company_ids.add(int(m.company_id))
    comps: Dict[int, FundingCompany] = {}
    if company_ids:
        for c in db.query(FundingCompany).filter(FundingCompany.id.in_(company_ids)).all():
            comps[int(c.id)] = c
    sets: Dict[int, set[int]] = {sid: {int(m.company_id) for m in mem_by_scan.get(sid, [])} for sid in scan_ids}
    result: List[Dict[str, Any]] = []
    for idx, scan in enumerate(scans):
        cur_set = sets.get(scan.id, set())
        prev_set = sets.get(scans[idx + 1].id, set()) if idx + 1 < len(scans) else set()
        new_ids = sorted(cur_set - prev_set)
        lost_ids = sorted(prev_set - cur_set)
        members = sorted(mem_by_scan.get(scan.id, []), key=lambda m: int(m.rank))
        company_list: List[Dict[str, Any]] = []
        for m in members:
            c = comps.get(int(m.company_id))
            if not c:
                continue
            company_list.append(
                {
                    "id": c.id,
                    "name": c.name,
                    "stage": c.stage,
                    "rank": int(m.rank),
                    "why": m.why or "",
                    "verified": bool(c.verified),
                    "source": c.source,
                }
            )
        result.append(
            {
                "id": scan.id,
                "scanned_at": scan.scanned_at.isoformat() if scan.scanned_at else None,
                "status": scan.status,
                "provider_errors": scan.provider_errors or {},
                "events_seen": int(scan.events_seen or 0),
                "companies_found": int(scan.companies_found or 0),
                "companies": company_list,
                "new_count": len(new_ids),
                "lost_count": len(lost_ids),
                "new_company_ids": new_ids,
                "lost_company_ids": lost_ids,
                "new_ids": new_ids,
                "lost_ids": lost_ids,
                "diff": {"new": new_ids, "lost": lost_ids, "new_count": len(new_ids), "lost_count": len(lost_ids)},
                "meta": scan.meta or {},
            }
        )
    return result


def funding_needs_refresh(db: Session, user_id: int,
                          refresh_hours: Optional[int] = None) -> bool:
    """May an implicit refresh (opening the Funding page) re-scan now?

    The interval is ``FUNDING_REFRESH_HOURS`` (default 12) measured from the
    last scan *attempt*. It used to be derived from the freshness window as
    ``window_days * 12 hours`` — a 45-day window meant the radar re-scanned
    roughly every 22.5 days, coupling two unrelated knobs. An explicit refresh
    (``?refresh=true`` / ``POST /funding/refresh``) is never throttled here.
    """
    hours = max(1, int(refresh_hours or settings.funding_refresh_hours or 12))
    interval = timedelta(hours=hours)
    report = latest_scan_report(db, user_id)
    attempted = parse_datetime(report.get("attempted_at") or report.get("scanned_at"))
    if attempted is None:
        # No report (pre-2.1.2 rows, or a scan never ran): fall back to the
        # newest row we hold.
        last_seen = func.coalesce(FundingCompany.last_seen_at, FundingCompany.discovered_at)
        attempted = (
            db.query(func.max(last_seen)).filter(FundingCompany.user_id == user_id).scalar()
        )
    if attempted is None:
        return True
    return datetime.utcnow() - attempted > interval


def outage_report(outage: Any) -> Dict[str, Any]:
    """Shape an AI outage diagnosis like a scan report.

    ``funding.last_report`` stays uniform whether the scan failed at a provider
    or at the model, so the UI has one place to look for "why is my radar
    stale?".
    """
    return {
        "providers": [], "counts": {}, "errors": {},
        "reason": getattr(outage, "reason", None),
        "ai": {
            "state": getattr(outage, "state", None),
            "status": "ai_paused" if getattr(outage, "pausable", False) else "ai_blocked",
            "message": getattr(outage, "message", "") or "",
            "fix": getattr(outage, "fix", "") or "",
            "detail": getattr(outage, "detail", "") or "",
            "workflow": getattr(outage, "workflow", "") or "funding_scan",
            "retry_after_hint": getattr(outage, "retry_after_hint", None),
        },
    }


def store_scan_report(db: Session, user_id: int, report: Optional[Dict[str, Any]], *,
                      scan_status: Optional[str] = None, reason: Optional[str] = None,
                      commit: bool = True) -> Dict[str, Any]:
    """Persist the most recent scan report for this user (``funding.last_report``).

    ``attempted_at`` is stamped on every outcome (that is the refresh clock);
    ``scanned_at`` only on a successful scan (that is what "updated 10:42" in the
    UI means).
    """
    now = datetime.utcnow()
    stored: Dict[str, Any] = dict(report or {})
    status = str(scan_status or stored.get("scan_status") or SCAN_OK)
    stored["scan_status"] = status if status in SCAN_STATES else SCAN_OK
    if reason is not None:
        stored["reason"] = reason
    stored.setdefault("reason", None)
    stored["attempted_at"] = now.isoformat()
    if stored["scan_status"] == SCAN_OK:
        stored["scanned_at"] = now.isoformat()
    set_setting(db, user_id, REPORT_CATEGORY, REPORT_KEY, stored)
    if commit:
        db.commit()
    return stored


def latest_scan_report(db: Session, user_id: int) -> Dict[str, Any]:
    """The persisted report of the most recent scan attempt (``{}`` when none)."""
    report = get_setting(db, user_id, REPORT_CATEGORY, REPORT_KEY, None)
    return report if isinstance(report, dict) else {}


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
    "SCAN_OK",
    "SCAN_FAILED",
    "SCAN_PAUSED",
    "SCAN_BLOCKED",
    "persist_funding_scan",
    "get_funding_history",
    "refresh_funding_has_open_positions",
    "SCAN_STATES",
    "REASON_NO_MATCHING_EVENTS",
    "REASON_NO_EVENTS",
    "REASON_PROVIDER_ERRORS",
    "REPORT_KEY",
    "require_ai_for_scan",
    "outage_report",
    "scan_funded_companies",
    "sync_funding_db",
    "prune_funding_db",
    "funding_needs_refresh",
    "store_scan_report",
    "latest_scan_report",
    "list_funding_companies",
    "normalize_company_name",
    "normalize_stage",
    "FUNDING_SCAN_METER",
    "charge_funding_scan",
    "funding_scan_charged",
    "find_funding_company",
]
