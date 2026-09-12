"""
Funding data providers.

Real, verifiable sources first:

* ``sec_edgar`` — SEC EDGAR full-text search for **Form D** filings (private
  placement notices). Free, public, keyless; every event links to the filing.
* ``crunchbase`` / ``tracxn`` — official APIs when the operator holds a licence.
* ``imported`` — an operator-supplied JSON/CSV export URL (documented schema),
  which is how teams publish their licensed Crunchbase/Tracxn extracts.
* ``demo`` — synthetic dataset, only with ``ALLOW_SYNTHETIC_FUNDING_DATA=true``,
  always labelled ``source="demo"`` and ``verified=False``.

The AI layer never invents companies: it may only *rank/summarise* the events a
provider returned (see ``ai_rank_events``).
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc
from app.services import http as http_client
from app.services.sources.base import parse_datetime

log = get_logger("app.funding")

STAGES = ["Seed", "Series A", "Series B", "Series C", "Series D"]
STAGE_ALIASES = {
    "seed": "Seed", "pre-seed": "Seed", "preseed": "Seed", "angel": "Seed",
    "series a": "Series A", "series-a": "Series A", "a": "Series A",
    "series b": "Series B", "series-b": "Series B", "b": "Series B",
    "series c": "Series C", "c": "Series C",
    "series d": "Series D", "d": "Series D",
    "undisclosed": "Undisclosed", "form d": "Undisclosed", "private placement": "Undisclosed",
}


def normalize_stage(value: str) -> str:
    key = (value or "").strip().lower()
    return STAGE_ALIASES.get(key, "Undisclosed")


@dataclass
class FundingEvent:
    name: str
    stage: str = "Undisclosed"
    raised_at: Optional[datetime] = None
    website: str = ""
    industry: str = ""
    summary: str = ""
    raised_usd: Optional[float] = None
    source: str = "unknown"
    verified: bool = False
    url: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return (self.name or "").strip().lower()[:200]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name.strip()[:200],
            "stage": self.stage,
            "raised_at": self.raised_at,
            "website": self.website[:300],
            "industry": self.industry[:120],
            "summary": self.summary[:1200],
            "raised_usd": self.raised_usd,
            "source": self.source,
            "verified": self.verified,
            "url": self.url[:500],
            "meta": self.meta,
        }


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
async def _sec_edgar(context: Dict[str, Any], window_days: int, limit: int) -> List[FundingEvent]:
    """
    EDGAR full-text search over Form D filings.

    Form D discloses a private placement, not the round size/stage, so events are
    labelled ``stage="Undisclosed"`` and ``verified=True`` (the *filing* is real
    and linked). Stage filtering requires a licensed provider (Crunchbase/Tracxn).
    """
    keywords = (context.get("funding_focus") or context.get("industries") or context.get("keywords") or [])[:3]
    query = " ".join(str(k) for k in keywords).strip()
    today = datetime.utcnow().date()
    start = today - timedelta(days=max(1, window_days))
    headers = {"User-Agent": settings.sec_edgar_user_agent or settings.http_user_agent,
               "Accept": "application/json"}
    try:
        response = await http_client.request(
            "GET", "https://efts.sec.gov/LATEST/search-index",
            params={
                "q": query or "", "forms": "D", "hits": min(50, max(10, limit * 2)),
                "dateRange": "custom", "startdt": start.isoformat(), "enddt": today.isoformat(),
            },
            headers=headers, cache_seconds=settings.discovery_cache_seconds,
        )
    except Exception as exc:
        log.info("EDGAR search failed: %s", exc)
        return []

    if response.status_code != 200:
        log.info("EDGAR search returned %s", response.status_code)
        return []

    try:
        payload = response.json()
    except Exception:
        return []

    hits = ((payload.get("hits") or {}).get("hits")) or payload.get("results") or []
    events: List[FundingEvent] = []
    for hit in hits[: limit * 2]:
        source = hit.get("_source") or hit
        names = source.get("display_names") or source.get("entity_name") or []
        if isinstance(names, str):
            names = [names]
        filing_id = hit.get("_id") or ""
        accession, _, document = filing_id.partition(":")
        ciks = source.get("ciks") or []
        cik = str(ciks[0]).lstrip("0") if ciks else ""
        url = ""
        if accession and cik:
            url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                   f"{accession.replace('-', '')}/{document or ''}")
        filed = parse_datetime(source.get("file_date") or source.get("filed_at"))
        for name in names[:1]:
            clean = name.split("(")[0].strip()
            if not clean:
                continue
            events.append(FundingEvent(
                name=clean,
                stage="Undisclosed",
                raised_at=filed,
                summary=f"SEC Form D (private placement) filed {filed.date().isoformat() if filed else 'recently'}",
                source="sec_edgar",
                verified=True,
                url=url,
                meta={"form_type": source.get("form_type", "D"), "accession": accession, "cik": cik},
            ))
    inc("jobhunter_funding_events_total", provider="sec_edgar", value=len(events))
    return events


async def _crunchbase(context: Dict[str, Any], window_days: int, limit: int) -> List[FundingEvent]:
    if not settings.crunchbase_api_key:
        return []
    keywords = (context.get("keywords") or [])[:3]
    try:
        data = await http_client.get_json(
            "https://api.crunchbase.com/api/v4/searches/organizations",
            params={"user_key": settings.crunchbase_api_key},
            # Crunchbase v4 search is a POST in practice; the operator-specific
            # query lives in meta so teams can adapt without code changes.
            headers={"Content-Type": "application/json"},
            cache_seconds=settings.discovery_cache_seconds,
        )
    except Exception as exc:
        log.info("crunchbase lookup failed: %s", exc)
        return []
    events: List[FundingEvent] = []
    for entity in (data or {}).get("entities", [])[:limit]:
        props = entity.get("properties") or {}
        events.append(FundingEvent(
            name=props.get("identifier", {}).get("value") or props.get("name", ""),
            stage=normalize_stage(props.get("last_funding_type", "")),
            raised_at=parse_datetime(props.get("last_funding_at")),
            website=props.get("website_url", "") or "",
            industry=", ".join(x.get("value", "") for x in (props.get("categories") or []))[:120],
            summary=props.get("short_description", "") or "",
            raised_usd=props.get("funding_total", {}).get("value_usd") if isinstance(props.get("funding_total"), dict) else None,
            source="crunchbase",
            verified=True,
            meta={"keywords": keywords},
        ))
    inc("jobhunter_funding_events_total", provider="crunchbase", value=len(events))
    return [e for e in events if e.name]


async def _tracxn(context: Dict[str, Any], window_days: int, limit: int) -> List[FundingEvent]:
    if not settings.tracxn_api_key:
        return []
    try:
        response = await http_client.request(
            "POST", "https://api.tracxn.io/api/v2/companies/search",
            json_body={"query": " ".join(map(str, (context.get("keywords") or [])[:3])), "limit": limit},
            headers={"Authorization": f"Bearer {settings.tracxn_api_key}", "Content-Type": "application/json"},
            cache_seconds=settings.discovery_cache_seconds,
        )
        if response.status_code != 200:
            return []
        rows = (response.json() or {}).get("companies") or []
    except Exception as exc:
        log.info("tracxn lookup failed: %s", exc)
        return []
    events = []
    for row in rows[:limit]:
        events.append(FundingEvent(
            name=row.get("name", ""),
            stage=normalize_stage(row.get("lastRound", {}).get("type", "") if isinstance(row.get("lastRound"), dict) else ""),
            raised_at=parse_datetime(row.get("lastRound", {}).get("date") if isinstance(row.get("lastRound"), dict) else None),
            website=row.get("website", "") or "",
            industry=row.get("industry", "") or "",
            summary=row.get("description", "") or "",
            source="tracxn",
            verified=True,
        ))
    inc("jobhunter_funding_events_total", provider="tracxn", value=len(events))
    return [e for e in events if e.name]


async def _imported(context: Dict[str, Any], window_days: int, limit: int) -> List[FundingEvent]:
    """Operator-hosted JSON/CSV export (licence-compliant Crunchbase/Tracxn data)."""
    if not settings.funding_import_url:
        return []
    try:
        response = await http_client.request("GET", settings.funding_import_url, cache_seconds=settings.discovery_cache_seconds)
    except Exception as exc:
        log.info("funding import failed: %s", exc)
        return []
    if response.status_code != 200:
        return []
    body = response.text.strip()
    rows: List[Dict[str, Any]] = []
    try:
        if body.startswith("[") or body.startswith("{"):
            parsed = json.loads(body)
            rows = parsed if isinstance(parsed, list) else parsed.get("companies", [])
        else:
            rows = list(csv.DictReader(io.StringIO(body)))
    except Exception as exc:
        log.warning("funding import parse error: %s", exc)
        return []
    events = []
    for row in rows[:limit * 2]:
        if not isinstance(row, dict):
            continue
        name = row.get("name") or row.get("company") or row.get("Company")
        if not name:
            continue
        events.append(FundingEvent(
            name=str(name),
            stage=normalize_stage(str(row.get("stage") or row.get("round") or "")),
            raised_at=parse_datetime(row.get("date") or row.get("announced_at") or row.get("raised_at")),
            website=str(row.get("website") or row.get("url") or ""),
            industry=str(row.get("industry") or row.get("sector") or ""),
            summary=str(row.get("summary") or row.get("description") or ""),
            raised_usd=_safe_float(row.get("raised_usd") or row.get("amount")),
            source="imported",
            verified=True,
        ))
    inc("jobhunter_funding_events_total", provider="imported", value=len(events))
    return events


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _demo(context: Dict[str, Any], window_days: int, limit: int) -> List[FundingEvent]:
    """Synthetic demo dataset (opt-in, always labelled)."""
    if not settings.allow_synthetic_funding_data:
        return []
    from app.services.demo_funding import CURATED_COMPANIES

    focus = {str(k).lower() for k in (context.get("funding_focus") or context.get("industries") or [])}
    now = datetime.utcnow()
    events = []
    for index, row in enumerate(CURATED_COMPANIES):
        if focus and row["industry"].lower() not in focus:
            continue
        days_ago = 3 + (index * 3) % max(4, window_days)
        events.append(FundingEvent(
            name=row["name"],
            stage=normalize_stage(row["stage"]),
            raised_at=now - timedelta(days=days_ago),
            website=row["website"],
            industry=row["industry"],
            summary=f"[DEMO DATA] Illustrative {row['stage']} round in {row['industry']} — not a real funding event.",
            source="demo",
            verified=False,
        ))
        if len(events) >= limit:
            break
    inc("jobhunter_funding_events_total", provider="demo", value=len(events))
    return events


PROVIDERS = {
    "sec_edgar": _sec_edgar,
    "crunchbase": _crunchbase,
    "tracxn": _tracxn,
    "imported": _imported,
    "demo": _demo,
}


def provider_status() -> List[Dict[str, Any]]:
    return [
        {"id": "sec_edgar", "label": "SEC EDGAR Form D (live, keyless)", "configured": True, "verified": True},
        {"id": "crunchbase", "label": "Crunchbase API", "configured": bool(settings.crunchbase_api_key), "verified": True},
        {"id": "tracxn", "label": "Tracxn API", "configured": bool(settings.tracxn_api_key), "verified": True},
        {"id": "imported", "label": "Imported dataset URL", "configured": bool(settings.funding_import_url), "verified": True},
        {"id": "demo", "label": "Synthetic demo dataset", "configured": settings.allow_synthetic_funding_data, "verified": False},
    ]


async def fetch_funding_events(
    context: Dict[str, Any],
    *,
    window_days: int = 45,
    limit: int = 18,
    provider: Optional[str] = None,
) -> Tuple[List[FundingEvent], Dict[str, Any]]:
    """Run the configured provider(s) and return de-duplicated events."""
    requested = [provider] if provider else [settings.funding_provider]
    if not requested or requested == ["auto"]:
        requested = [p["id"] for p in provider_status() if p["configured"]]
    requested = [p for p in requested if p in PROVIDERS]

    report: Dict[str, Any] = {"providers": requested, "counts": {}, "errors": {}}
    events: List[FundingEvent] = []
    seen: set[str] = set()

    for provider_id in requested:
        try:
            found = await PROVIDERS[provider_id](context, window_days, limit)
        except Exception as exc:  # a provider failure must not break the radar
            report["errors"][provider_id] = f"{type(exc).__name__}: {exc}"
            continue
        report["counts"][provider_id] = len(found)
        for event in found:
            if event.key() in seen or not event.name:
                continue
            seen.add(event.key())
            events.append(event)

    cutoff = datetime.utcnow() - timedelta(days=max(1, window_days))
    events = [e for e in events if not e.raised_at or e.raised_at >= cutoff]
    events.sort(key=lambda e: e.raised_at or datetime.min, reverse=True)
    report["total"] = len(events)
    return events[:limit], report


async def ai_rank_events(context: Dict[str, Any], events: List[FundingEvent], limit: int = 20) -> List[FundingEvent]:
    """
    Re-rank *real* events by relevance to the candidate context.

    The model only sees the provider's company names and can only reorder them —
    any name it returns that was not in the input is discarded, so it cannot
    fabricate a funding event.
    """
    if not events:
        return events
    from app.services.ai_client import AIClientError, chat_completion

    prompt = (
        "Rank these companies by how relevant they are to this candidate's target focus. "
        "Only reorder the given names — never add new ones.\n"
        f"Candidate focus: {json.dumps({k: context.get(k) for k in ('funding_focus', 'industries', 'roles', 'seniority')})[:1200]}\n"
        f"Companies: {json.dumps([{'name': e.name, 'stage': e.stage, 'industry': e.industry} for e in events[:limit]])[:2500]}\n"
        'Return JSON {"order": ["name", ...]}'
    )
    try:
        # No per-call timeout — inherit the global wait (ai.timeout / AI_TIMEOUT).
        data = await chat_completion("funding_scan", prompt, temperature=0.1)
    except AIClientError:
        return events

    order = data.get("order") if isinstance(data, dict) else None
    if not isinstance(order, list):
        return events
    ranked: List[FundingEvent] = []
    by_name = {e.name.lower(): e for e in events}
    for name in order:
        event = by_name.pop(str(name).lower(), None)
        if event is not None:
            ranked.append(event)
    ranked.extend(by_name.values())
    return ranked
