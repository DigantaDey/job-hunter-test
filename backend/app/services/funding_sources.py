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

Provider robustness (v2.1.2)
----------------------------
* Every provider runs under ``FUNDING_PROVIDER_TIMEOUT_SECONDS`` (default 20)
  and is retried **once** on a transport error — never on a 4xx, because a
  rejected request does not fix itself by being repeated.
* A provider that fails is recorded in ``report["errors"][provider_id]``; it is
  no longer swallowed into an empty list. A provider that was *requested* but
  is not configured (no key, no import URL, synthetic data off) is reported the
  same way as ``not_configured`` — choosing "crunchbase" without a licence used
  to look exactly like "nobody raised money this month".
* When **every** requested provider fails, the report says so:
  ``report["scan_status"] = "scan_failed"`` with the per-provider errors. An
  all-providers-down scan must never reach the user as a quiet "no companies"
  radar. Partial success stays ``"ok"`` — but ``report["errors"]`` is part of
  the persisted report the UI renders.

The AI layer never invents companies: :func:`ai_rank_events` may only judge the
events a provider actually returned, and a single name that is not in that set
rejects the whole answer (``guardrail_failed`` → blocked/dead, per v2.1).

Open positions are provider facts, never guesses. A dataset that really reports
hiring may put ``open_positions`` (``[{title, url, location, summary}]``, a JSON
list, or ``"title::url|title::url"``) and/or ``careers_url`` on a row; that
lands in ``FundingCompany.meta`` and is the *only* thing that can start the
apply flow.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc
from app.services import http as http_client
from app.services.net_guard import OutboundURLBlocked
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

#: One retry on a transport failure (the request itself is idempotent).
PROVIDER_RETRIES = 1
#: Pause between the two attempts — long enough for a hiccup, short enough that
#: a scan of five providers never stalls the request that triggered it.
PROVIDER_RETRY_DELAY_SECONDS = 0.5
#: Hard cap on how many events one AI pass may be asked about.
MAX_AI_CANDIDATES = 60
#: Marker the prompt carries so tests (and logs) can pin the exact contract.
RANK_CONTRACT = "funding-scan-v2"


def normalize_stage(value: str) -> str:
    key = (value or "").strip().lower()
    return STAGE_ALIASES.get(key, "Undisclosed")


def normalize_company_name(value: Any) -> str:
    """The dedupe identity for a company name: strip, collapse spaces, casefold.

    Providers disagree about casing ("Stripe" vs "stripe") and about trailing
    spaces; both used to become separate radar rows. Display keeps the
    provider's own casing — only *lookup* is normalised.
    """
    return " ".join(str(value or "").split()).casefold()[:200]


#: Corporate suffixes a model may add or drop when echoing a name back. Used
#: only for a *second*, unambiguous lookup pass — never to merge two different
#: companies (an ambiguous alternative key is discarded).
_LEGAL_SUFFIXES = frozenset({
    "inc", "inc.", "llc", "ltd", "ltd.", "corp", "corp.", "corporation", "company",
    "co", "co.", "gmbh", "plc", "sa", "nv", "bv", "ab", "oy", "asa", "pty", "pvt",
    "limited", "holdings", "holding", "group",
})


def _alternative_key(value: Any) -> str:
    tokens = [t for t in normalize_company_name(value).split() if t not in _LEGAL_SUFFIXES]
    return " ".join(tokens)


class FundingProviderError(RuntimeError):
    """A provider could not produce events (transport failure or bad status).

    ``status`` is the provider's own HTTP status when there was one; ``None``
    means a transport-level failure (DNS/TLS/timeout), which is worth a retry.
    """

    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return not (self.status is not None and 400 <= self.status < 500)


def _is_retryable_provider_error(exc: BaseException) -> bool:
    """Transport/5xx → retry once. 4xx or a policy decision → do not retry."""
    if isinstance(exc, FundingProviderError):
        return exc.retryable
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and 400 <= status < 500:
        return False
    if isinstance(exc, (OutboundURLBlocked, PermissionError)):
        return False  # SSRF guard / robots.txt: a policy verdict, not a hiccup
    return True


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
        return normalize_company_name(self.name)

    def open_positions(self) -> List[Dict[str, Any]]:
        """Open positions the *provider* reported (never inferred)."""
        rows = (self.meta or {}).get("open_positions") or []
        return [row for row in rows if isinstance(row, dict) and str(row.get("title") or "").strip()]

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


@dataclass
class RankedEvent:
    """A provider event plus the model's grounded verdict on it.

    ``event`` is the only source of facts (name, stage, amount, source, url):
    the model contributes ``matched``/``rank``/``why`` and nothing else.
    """

    event: FundingEvent
    matched: bool = False
    rank: int = 0
    why: str = ""

    @property
    def name(self) -> str:
        return self.event.name

    @property
    def stage(self) -> str:
        return self.event.stage


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
async def _sec_edgar(context: Dict[str, Any], window_days: int, limit: int) -> List[FundingEvent]:
    """
    EDGAR full-text search over Form D filings.

    Form D discloses a private placement, not the round size/stage, so events are
    labelled ``stage="Undisclosed"`` and ``verified=True`` (the *filing* is real
    and linked). Stage filtering requires a licensed provider (Crunchbase/Tracxn).

    A transport failure or a non-200 is raised (not swallowed): the caller turns
    it into a per-provider entry in the scan report, which is how "EDGAR is down"
    stays distinguishable from "no company matches your focus".
    """
    keywords = (context.get("funding_focus") or context.get("industries") or context.get("keywords") or [])[:3]
    query = " ".join(str(k) for k in keywords).strip()
    today = datetime.utcnow().date()
    start = today - timedelta(days=max(1, window_days))
    headers = {"User-Agent": settings.sec_edgar_user_agent or settings.http_user_agent,
               "Accept": "application/json"}
    response = await http_client.request(
        "GET", "https://efts.sec.gov/LATEST/search-index",
        params={
            "q": query or "", "forms": "D", "hits": min(50, max(10, limit * 2)),
            "dateRange": "custom", "startdt": start.isoformat(), "enddt": today.isoformat(),
        },
        headers=headers, cache_seconds=settings.discovery_cache_seconds,
        timeout=settings.funding_provider_timeout_seconds,
    )
    if response.status_code != 200:
        raise FundingProviderError(f"EDGAR full-text search returned HTTP {response.status_code}",
                                   status=response.status_code)
    try:
        payload = response.json()
    except Exception as exc:
        raise FundingProviderError(f"EDGAR returned a body that is not JSON: {exc}") from exc

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
                # ``raised_at_estimated`` is set by the radar when a filing had no
                # parseable date: the row is real, the *day* it happened is not.
                meta={"form_type": source.get("form_type", "D"), "accession": accession, "cik": cik,
                      "raised_at_estimated": filed is None},
            ))
    inc("jobhunter_funding_events_total", provider="sec_edgar", value=len(events))
    return events


async def _crunchbase(context: Dict[str, Any], window_days: int, limit: int) -> List[FundingEvent]:
    if not settings.crunchbase_api_key:
        return []
    keywords = (context.get("keywords") or [])[:3]
    data = await http_client.get_json(
        "https://api.crunchbase.com/api/v4/searches/organizations",
        params={"user_key": settings.crunchbase_api_key},
        # Crunchbase v4 search is a POST in practice; the operator-specific
        # query lives in meta so teams can adapt without code changes.
        headers={"Content-Type": "application/json"},
        cache_seconds=settings.discovery_cache_seconds,
        timeout=settings.funding_provider_timeout_seconds,
    )
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
            meta={"keywords": keywords, "open_positions": _parse_open_positions(props.get("open_positions"))},
        ))
    inc("jobhunter_funding_events_total", provider="crunchbase", value=len(events))
    return [e for e in events if e.name]


async def _tracxn(context: Dict[str, Any], window_days: int, limit: int) -> List[FundingEvent]:
    if not settings.tracxn_api_key:
        return []
    response = await http_client.request(
        "POST", "https://api.tracxn.io/api/v2/companies/search",
        json_body={"query": " ".join(map(str, (context.get("keywords") or [])[:3])), "limit": limit},
        headers={"Authorization": f"Bearer {settings.tracxn_api_key}", "Content-Type": "application/json"},
        cache_seconds=settings.discovery_cache_seconds,
        timeout=settings.funding_provider_timeout_seconds,
    )
    if response.status_code != 200:
        raise FundingProviderError(f"tracxn returned HTTP {response.status_code}", status=response.status_code)
    try:
        rows = (response.json() or {}).get("companies") or []
    except Exception as exc:
        raise FundingProviderError(f"tracxn returned a body that is not JSON: {exc}") from exc
    events = []
    for row in rows[:limit]:
        last_round = row.get("lastRound") if isinstance(row.get("lastRound"), dict) else {}
        events.append(FundingEvent(
            name=row.get("name", ""),
            stage=normalize_stage(last_round.get("type", "")),
            raised_at=parse_datetime(last_round.get("date")),
            website=row.get("website", "") or "",
            industry=row.get("industry", "") or "",
            summary=row.get("description", "") or "",
            source="tracxn",
            verified=True,
            meta={"open_positions": _parse_open_positions(row.get("openPositions") or row.get("open_positions"))},
        ))
    inc("jobhunter_funding_events_total", provider="tracxn", value=len(events))
    return [e for e in events if e.name]


async def _imported(context: Dict[str, Any], window_days: int, limit: int) -> List[FundingEvent]:
    """Operator-hosted JSON/CSV export (licence-compliant Crunchbase/Tracxn data).

    Documented row schema (all optional except ``name``): ``name``/``company``,
    ``stage``/``round``, ``date``/``announced_at``/``raised_at``, ``website``/
    ``url``, ``industry``/``sector``, ``summary``/``description``,
    ``raised_usd``/``amount``, ``careers_url``, ``open_positions``.
    """
    if not settings.funding_import_url:
        return []
    response = await http_client.request("GET", settings.funding_import_url,
                                         cache_seconds=settings.discovery_cache_seconds,
                                         timeout=settings.funding_provider_timeout_seconds)
    if response.status_code != 200:
        raise FundingProviderError(f"funding import returned HTTP {response.status_code}",
                                   status=response.status_code)
    body = response.text.strip()
    rows: List[Dict[str, Any]] = []
    try:
        if body.startswith("[") or body.startswith("{"):
            parsed = json.loads(body)
            rows = parsed if isinstance(parsed, list) else parsed.get("companies", [])
        else:
            rows = list(csv.DictReader(io.StringIO(body)))
    except Exception as exc:
        # A broken export is an operator problem, not "no companies this month".
        raise FundingProviderError(f"funding import is not valid JSON/CSV: {exc}") from exc
    events = []
    for row in rows[:limit * 2]:
        if not isinstance(row, dict):
            continue
        name = row.get("name") or row.get("company") or row.get("Company")
        if not name:
            continue
        announced = parse_datetime(row.get("date") or row.get("announced_at") or row.get("raised_at"))
        positions = _parse_open_positions(row.get("open_positions") or row.get("openPositions"))
        careers_url = str(row.get("careers_url") or row.get("careers") or "")[:500]
        events.append(FundingEvent(
            name=str(name),
            stage=normalize_stage(str(row.get("stage") or row.get("round") or "")),
            raised_at=announced,
            website=str(row.get("website") or row.get("url") or ""),
            industry=str(row.get("industry") or row.get("sector") or ""),
            summary=str(row.get("summary") or row.get("description") or ""),
            raised_usd=_safe_float(row.get("raised_usd") or row.get("amount")),
            source="imported",
            verified=True,
            meta={"open_positions": positions, "careers_url": careers_url,
                  "raised_at_estimated": announced is None},
        ))
    inc("jobhunter_funding_events_total", provider="imported", value=len(events))
    return events


def _parse_open_positions(value: Any) -> List[Dict[str, Any]]:
    """Normalise provider-reported open positions.

    Accepts a list of objects, a list of strings, a JSON string, or the
    ``"title::url|title::url"`` shape licensed CSV exports actually use. An
    empty/absent value stays empty — this function never invents a role.
    """
    if not value:
        return []
    rows: Any = value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                rows = json.loads(text)
            except ValueError:
                rows = [part for part in text.split("|") if part.strip()]
        else:
            rows = [part for part in text.split("|") if part.strip()]
    if not isinstance(rows, list):
        return []
    positions: List[Dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            title = str(row.get("title") or row.get("role") or "").strip()
            if not title:
                continue
            positions.append({"title": title[:200], "url": str(row.get("url") or "")[:500],
                              "location": str(row.get("location") or "")[:120],
                              "summary": str(row.get("summary") or row.get("description") or "")[:1200]})
        elif isinstance(row, str):
            title, _, url = row.partition("::")
            if not title.strip():
                continue
            positions.append({"title": title.strip()[:200], "url": url.strip()[:500],
                              "location": "", "summary": ""})
        if len(positions) >= 20:
            break
    return positions


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
            meta={"open_positions": _parse_open_positions(row.get("open_positions"))},
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


def _configured_providers() -> Dict[str, bool]:
    """Provider id → is it usable right now? (tests may monkeypatch PROVIDERS.)"""
    status = {row["id"]: bool(row["configured"]) for row in provider_status()}
    return {provider_id: status.get(provider_id, True) for provider_id in PROVIDERS}


async def _run_provider(provider_id: str, context: Dict[str, Any], window_days: int,
                        limit: int) -> List[FundingEvent]:
    """Run one provider under its timeout, with a single retry on transport errors."""
    timeout = max(1.0, float(settings.funding_provider_timeout_seconds or 20.0))
    last_error: Optional[BaseException] = None
    for attempt in range(1, PROVIDER_RETRIES + 2):
        started = datetime.utcnow()
        try:
            return await asyncio.wait_for(PROVIDERS[provider_id](context, window_days, limit), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:  # asyncio.wait_for's own deadline (3.11: == TimeoutError)
            last_error = FundingProviderError(f"timed out after {timeout:g}s")
        except Exception as exc:  # noqa: BLE001 - a provider failure is data, not a crash
            last_error = exc
            if not _is_retryable_provider_error(exc):
                break
        if attempt <= PROVIDER_RETRIES:
            inc("jobhunter_funding_provider_retries_total", provider=provider_id)
            log.info("funding provider %s failed (attempt %s), retrying: %s", provider_id, attempt, last_error)
            await asyncio.sleep(PROVIDER_RETRY_DELAY_SECONDS)
    elapsed = (datetime.utcnow() - started).total_seconds()
    inc("jobhunter_funding_provider_errors_total", provider=provider_id)
    log.warning("funding provider %s failed after %.1fs: %s", provider_id, elapsed, last_error)
    raise last_error or FundingProviderError("provider failed")


async def fetch_funding_events(
    context: Dict[str, Any],
    *,
    window_days: int = 45,
    limit: int = 18,
    provider: Optional[str] = None,
) -> Tuple[List[FundingEvent], Dict[str, Any]]:
    """Run the configured provider(s) and return de-duplicated events.

    The report is the honest record of the fetch: ``counts`` per provider,
    ``errors`` per provider (including ``not_configured``), and ``scan_status``
    — ``"scan_failed"`` when nothing could be fetched at all.
    """
    requested = [provider] if provider else [settings.funding_provider]
    requested = [str(p).strip() for p in (requested or []) if str(p or "").strip()]
    if not requested or requested == ["auto"]:
        requested = [p["id"] for p in provider_status() if p["configured"]]

    report: Dict[str, Any] = {
        "providers": requested, "counts": {}, "errors": {}, "scan_status": "ok",
        "window_days": max(1, window_days), "fetched_at": datetime.utcnow().isoformat(),
    }
    events: List[FundingEvent] = []
    seen: set[str] = set()
    configured = _configured_providers()

    if not requested:
        report["errors"]["providers"] = "no_providers_configured"

    for provider_id in requested:
        if provider_id not in PROVIDERS:
            report["errors"][provider_id] = "unknown_provider"
            continue
        if not configured.get(provider_id, True):
            # Requested but unusable (no key / no import URL / synthetic off).
            report["errors"][provider_id] = "not_configured"
            continue
        try:
            found = await _run_provider(provider_id, context, window_days, limit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported, never silent
            report["errors"][provider_id] = f"{type(exc).__name__}: {exc}"[:300]
            continue
        report["counts"][provider_id] = len(found)
        for event in found:
            key = event.key()
            if not key or key in seen:
                continue
            seen.add(key)
            events.append(event)

    cutoff = datetime.utcnow() - timedelta(days=max(1, window_days))
    events = [e for e in events if not e.raised_at or e.raised_at >= cutoff]
    events.sort(key=lambda e: e.raised_at or datetime.min, reverse=True)
    report["fetched"] = len(events)
    if not report["counts"] and report["errors"]:
        # Nothing could be fetched — every requested provider failed, is
        # unusable, or there is none to run. This is an outage, not an empty
        # radar, so the caller must not present it as "no companies matched".
        report["scan_status"] = "scan_failed"
    kept = events[:limit]
    report["total"] = len(kept)
    return kept, report


# --------------------------------------------------------------------------- #
# The AI pass — relevance + rank + explanation in ONE grounded call
# --------------------------------------------------------------------------- #
RANK_SYSTEM = (
    "You are the funding-radar relevance engine for a job seeker. You are given REAL funding "
    "events returned by data providers plus the candidate's focus, and you decide which events "
    "match that focus, in which order, and why. The event data is the only truth: you never "
    "invent companies, stages, amounts, dates or job openings, and you never add outside "
    "knowledge about a company. Your job is relevance, not research."
)


def _event_for_prompt(event: FundingEvent) -> Dict[str, Any]:
    """The compact, factual slice of an event the model is allowed to reason about."""
    return {
        "name": event.name,
        "stage": event.stage,
        "industry": event.industry[:120],
        "summary": event.summary[:400],
        "raised_usd": event.raised_usd,
        "raised_at": event.raised_at.date().isoformat() if event.raised_at else None,
        "source": event.source,
    }


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    return str(value or "").strip().lower() in {"true", "yes", "y", "match", "matched", "relevant"}


def _as_rank(value: Any, fallback: int) -> int:
    try:
        rank = int(value)
    except (TypeError, ValueError):
        return fallback
    return rank if rank > 0 else fallback


def _clean_why(value: Any) -> str:
    """One sentence, hygienic: the model's prose never reaches the UI raw."""
    from app.services.ai_guardrails import strip_ai_artifacts

    text = strip_ai_artifacts(str(value or ""))
    if not text:
        return ""
    sentence = text.split("\n", 1)[0].strip()
    return sentence[:300]


def _ground_index(events: List[FundingEvent]) -> Tuple[Dict[str, FundingEvent], Dict[str, FundingEvent]]:
    """Name → event, plus an *unambiguous* suffix-insensitive second index."""
    exact: Dict[str, FundingEvent] = {}
    alternatives: Dict[str, FundingEvent] = {}
    alternative_counts: Counter = Counter()
    for event in events:
        exact.setdefault(normalize_company_name(event.name), event)
        alt = _alternative_key(event.name)
        if alt:
            alternative_counts[alt] += 1
            alternatives.setdefault(alt, event)
    # A suffix-stripped key that maps to two different companies proves nothing —
    # drop it rather than guess which one the model meant.
    return exact, {key: event for key, event in alternatives.items() if alternative_counts[key] == 1}


def _find_event(name: Any, exact: Dict[str, FundingEvent],
                alternatives: Dict[str, FundingEvent]) -> Optional[FundingEvent]:
    key = normalize_company_name(name)
    if key in exact:
        return exact[key]
    alt = _alternative_key(name)
    return alternatives.get(alt) if alt else None


def _field_conflicts(row: Dict[str, Any], event: FundingEvent) -> List[str]:
    """Did the model restate an event field *differently*? (recorded, never used)"""
    conflicts: List[str] = []
    stage = str(row.get("stage") or "").strip()
    if stage and normalize_stage(stage) != event.stage:
        conflicts.append("stage")
    amount = _safe_float(row.get("raised_usd") or row.get("amount"))
    if amount is not None and event.raised_usd is not None and abs(amount - event.raised_usd) > 1:
        conflicts.append("raised_usd")
    return conflicts


async def ai_rank_events(
    context: Dict[str, Any],
    events: List[FundingEvent],
    limit: Optional[int] = None,
    *,
    db=None,
    user_id: Optional[int] = None,
) -> Tuple[List[RankedEvent], Dict[str, Any]]:
    """
    Relevance-filter, rank and explain *real* events in one grounded AI pass.

    Returns ``(ranked, diagnostics)``. ``ranked`` holds every candidate with the
    model's verdict; matched events come first, ordered by ``rank``. Events the
    model did not mention are returned ``matched=False`` — silence is never a
    match, and an event the model never saw is never shown as relevant.

    Grounding guardrails (v2.1):

    * every returned company name must exist in ``events``. A name that does not
      is *fabrication*: the whole answer is rejected with
      ``guardrail_failed``/``blocked_needs_action`` (queued scans dead-letter) —
      there is no fallback to an unranked list;
    * stage, amount, date, source and URL always come from the event, never from
      the model. A model that restates them differently is recorded in
      ``diagnostics["field_conflicts"]`` and ignored;
    * ``why`` is cleaned (markdown/HTML stripped) and capped — it is an
      explanation, not a data field.

    AI is a hard dependency: on failure this raises ``AIClientError`` /
    ``AIUnavailableError`` and the caller decides (sync endpoint → pausable 503
    or blocked 503, queued scan → paused or dead-lettered).
    """
    diagnostics: Dict[str, Any] = {"candidates": len(events), "sent": 0, "matched": 0,
                                   "verdicts": 0, "coverage": 0.0, "truncated": False,
                                   "field_conflicts": 0, "contract": RANK_CONTRACT}
    if not events:
        return [], diagnostics

    from app.services.ai_client import (
        AIClientError,
        chat_completion,
        fit_prompt_part,
        input_budget_chars,
    )
    from app.services.ai_guardrails import AIUnavailableError

    budget = input_budget_chars(db=db, user_id=user_id)
    focus = {key: context.get(key) for key in
             ("funding_focus", "industries", "roles", "seniority", "keywords", "locations")
             if context.get(key)}
    focus_json, _focus_truncated = fit_prompt_part(json.dumps(focus, default=str), budget,
                                                  label="funding_scan.focus")

    cap = min(MAX_AI_CANDIDATES, max(1, int(limit) if limit else len(events)))
    sent = list(events[:cap])
    # Never show the model a half-cut event: shrink the slice until it fits the
    # user's input budget, so the grounding set is exactly what it saw.
    events_json, truncated = "", False
    while sent:
        events_json, truncated = fit_prompt_part(
            json.dumps([_event_for_prompt(e) for e in sent], default=str), budget,
            label="funding_scan.events")
        if not truncated or len(sent) == 1:
            break
        sent = sent[: max(1, len(sent) // 2)]
    diagnostics.update({"sent": len(sent), "truncated": bool(truncated)})

    prompt = (
        f"Contract: {RANK_CONTRACT}\n"
        "For EVERY funding event below decide whether it matches this candidate's focus, rank the "
        "matches, and explain each verdict in one sentence that cites the event's own fields "
        "(industry, summary, stage, amount, date). Use only the companies listed — never add, "
        "rename or complete a name, and never claim a company is hiring unless the event says so.\n"
        f"Candidate focus: {focus_json}\n"
        f"Funding events: {events_json}\n"
        'Return JSON only: {"companies": [{"name": "<exactly as given>", "matched": true, '
        '"rank": 1, "why": "<one sentence>"}]}'
    )
    # No per-call timeout — inherit the global wait (ai.timeout / AI_TIMEOUT).
    data = await chat_completion("funding_scan", prompt, system=RANK_SYSTEM, temperature=0.1,
                                db=db, user_id=user_id)
    payload = data.get("content") if isinstance(data, dict) and "content" in data else data
    rows = payload.get("companies") if isinstance(payload, dict) else None
    if rows is None and isinstance(payload, list):
        rows = payload
    if not isinstance(rows, list):
        # The model answered but not with a usable verdict list — surface it.
        raise AIClientError(
            "invalid_json: funding_scan did not return a 'companies' list",
            reason="invalid_json", retryable=True,
        )
    if not rows:
        raise AIClientError(
            f"empty_response: funding_scan returned no verdicts for {len(sent)} funding event(s)",
            reason="empty_response", retryable=True,
        )

    exact, alternatives = _ground_index(sent)
    verdicts: Dict[int, RankedEvent] = {}
    unknown: List[str] = []
    conflicts = 0
    for position, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        event = _find_event(name, exact, alternatives)
        if event is None:
            unknown.append(name[:160])
            continue
        if id(event) in verdicts:
            continue  # the same company twice: the first verdict wins
        conflicts += len(_field_conflicts(row, event))
        verdicts[id(event)] = RankedEvent(
            event=event,
            matched=_as_bool(row.get("matched")),
            rank=_as_rank(row.get("rank"), position),
            why=_clean_why(row.get("why")) if _as_bool(row.get("matched")) else "",
        )

    if unknown:
        # Fabrication is not a ranking problem — it is a trust problem. Reject the
        # whole answer (blocked/dead per v2.1) instead of quietly dropping names.
        log.warning("funding_scan returned %s name(s) outside the provider event set: %s",
                    len(unknown), unknown[:5])
        raise AIUnavailableError(
            "guardrail_failed",
            workflow="funding_scan",
            state="blocked_needs_action",
            detail=(f"The model returned {len(unknown)} company name(s) that no provider reported: "
                    f"{', '.join(unknown[:5])}"),
        )

    matched = sorted((r for r in verdicts.values() if r.matched), key=lambda r: r.rank)
    rejected = sorted((r for r in verdicts.values() if not r.matched), key=lambda r: r.rank)
    unjudged = [RankedEvent(event=event, matched=False, rank=0, why="")
                for event in sent if id(event) not in verdicts]
    diagnostics.update({
        "matched": len(matched), "verdicts": len(verdicts),
        "coverage": round(len(verdicts) / max(1, len(sent)), 3),
        "field_conflicts": conflicts, "unjudged": len(unjudged),
    })
    inc("jobhunter_funding_ai_matched_total", value=len(matched))
    return matched + rejected + unjudged, diagnostics
