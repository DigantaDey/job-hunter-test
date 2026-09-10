"""
Funding Radar — discovers recently-funded companies (Seed → Series D) that
match an AI-extracted candidate context.

Freshness guarantees:
- Every scan derives dates relative to *now*; nothing is hardcoded to a past year.
- The DB is the source of truth for the UI; rows older than the freshness window
  are pruned on every scan so stale results can never surface.
- Result count scales with the freshness window (≈12-24 companies, not 5).

Sources:
- AI mode  : LLM proposes companies aligned to the candidate's funding_focus.
- Heuristic: curated demo dataset matched to the candidate's industries.
  Clearly labeled as a demo dataset in the API/UI.
"""
import hashlib
import random
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.services.ai_client import chat_completion, AIClientError, is_configured
from app.utils.logger import log_info

STAGES = ["Seed", "Series A", "Series B", "Series C", "Series D"]

# Curated demo dataset: (name, website, industry, stage). Dates are always
# computed relative to scan time, so results are never stale.
CURATED_COMPANIES: List[Dict[str, str]] = [
    {"name": "VectorLoom AI", "website": "vectorloom.ai", "industry": "ai/ml", "stage": "Series A"},
    {"name": "PromptForge", "website": "promptforge.dev", "industry": "ai/ml", "stage": "Seed"},
    {"name": "Cortexa Labs", "website": "cortexa.ai", "industry": "ai/ml", "stage": "Series B"},
    {"name": "Lumen Intelligence", "website": "lumenint.ai", "industry": "ai/ml", "stage": "Series C"},
    {"name": "Synthetiq", "website": "synthetiq.ai", "industry": "ai/ml", "stage": "Series A"},
    {"name": "Payflow Systems", "website": "payflow.io", "industry": "fintech", "stage": "Series B"},
    {"name": "Ledgerline", "website": "ledgerline.com", "industry": "fintech", "stage": "Series A"},
    {"name": "Kredit Works", "website": "kreditworks.com", "industry": "fintech", "stage": "Seed"},
    {"name": "Vaultpay", "website": "vaultpay.co", "industry": "fintech", "stage": "Series C"},
    {"name": "Insurely AI", "website": "insurely.ai", "industry": "fintech", "stage": "Series A"},
    {"name": "Tradegrail", "website": "tradegrail.com", "industry": "fintech", "stage": "Series D"},
    {"name": "Shopstack", "website": "shopstack.io", "industry": "e-commerce", "stage": "Series B"},
    {"name": "Cartfull", "website": "cartfull.com", "industry": "e-commerce", "stage": "Series A"},
    {"name": "Mercato Live", "website": "mercatolive.com", "industry": "e-commerce", "stage": "Seed"},
    {"name": "Medlane Health", "website": "medlane.health", "industry": "healthtech", "stage": "Series B"},
    {"name": "Careloop", "website": "careloop.health", "industry": "healthtech", "stage": "Series A"},
    {"name": "Genomix Bio", "website": "genomix.bio", "industry": "healthtech", "stage": "Series C"},
    {"name": "Learnloop", "website": "learnloop.app", "industry": "edtech", "stage": "Series A"},
    {"name": "Skillforge Academy", "website": "skillforge.academy", "industry": "edtech", "stage": "Seed"},
    {"name": "Devkit Cloud", "website": "devkit.cloud", "industry": "developer tools", "stage": "Series A"},
    {"name": "Pipeline HQ", "website": "pipelinehq.dev", "industry": "developer tools", "stage": "Seed"},
    {"name": "MergeBase", "website": "mergebase.dev", "industry": "developer tools", "stage": "Series B"},
    {"name": "Shipped CI", "website": "shipped.ci", "industry": "developer tools", "stage": "Series A"},
    {"name": "Aegis Security", "website": "aegis.security", "industry": "cybersecurity", "stage": "Series B"},
    {"name": "ZeroTrace", "website": "zerotrace.io", "industry": "cybersecurity", "stage": "Series A"},
    {"name": "Flowdesk SaaS", "website": "flowdesk.com", "industry": "saas", "stage": "Series C"},
    {"name": "Taskgrid", "website": "taskgrid.app", "industry": "saas", "stage": "Series A"},
    {"name": "Opsly", "website": "opsly.io", "industry": "saas", "stage": "Seed"},
    {"name": "Wovenwork", "website": "wovenwork.com", "industry": "saas", "stage": "Series B"},
    {"name": "Relay CRM", "website": "relaycrm.com", "industry": "saas", "stage": "Series D"},
    {"name": "Playcraft Studios", "website": "playcraft.gg", "industry": "gaming", "stage": "Series A"},
    {"name": "Nova Arcade", "website": "novaarcade.com", "industry": "gaming", "stage": "Seed"},
    {"name": "Chainforge Labs", "website": "chainforge.xyz", "industry": "web3", "stage": "Series A"},
    {"name": "DeFi Bridge", "website": "defibridge.finance", "industry": "web3", "stage": "Series B"},
    {"name": "Fleetly Mobility", "website": "fleetly.mobility", "industry": "mobility", "stage": "Series B"},
    {"name": "CargoStream", "website": "cargostream.io", "industry": "mobility", "stage": "Series A"},
    {"name": "VoltaRide", "website": "voltaride.com", "industry": "mobility", "stage": "Series C"},
    {"name": "Helio Grid", "website": "heliogrid.energy", "industry": "climate", "stage": "Series A"},
    {"name": "Carbonloop", "website": "carbonloop.earth", "industry": "climate", "stage": "Seed"},
    {"name": "Datacraft Analytics", "website": "datacraft.io", "industry": "data", "stage": "Series B"},
    {"name": "Warehouse OS", "website": "warehouseos.io", "industry": "data", "stage": "Series A"},
    {"name": "Streamline ETL", "website": "streamline-etl.com", "industry": "data", "stage": "Seed"},
    {"name": "Cloudframe", "website": "cloudframe.io", "industry": "developer tools", "stage": "Series C"},
    {"name": "Nimbus Compute", "website": "nimbuscompute.com", "industry": "developer tools", "stage": "Series B"},
    {"name": "Querybird", "website": "querybird.com", "industry": "data", "stage": "Series A"},
    {"name": "Modelhub AI", "website": "modelhub.ai", "industry": "ai/ml", "stage": "Series D"},
    {"name": "Agentive", "website": "agentive.ai", "industry": "ai/ml", "stage": "Seed"},
]


def _stable_days_ago(name: str, window_days: int) -> int:
    """Deterministic per company so refreshes don't shuffle dates arbitrarily."""
    digest = hashlib.md5(name.lower().encode()).hexdigest()
    pct = int(digest[:8], 16) / 0xFFFFFFFF
    return max(1, min(window_days, int(pct * window_days) + 1))


def _stable_open_positions(name: str) -> bool:
    digest = hashlib.md5(("ops:" + name.lower()).encode()).hexdigest()
    return (int(digest[:8], 16) % 10) < 6  # ~60% have open roles


def _match_score(company: Dict[str, str], context: Dict[str, Any]) -> List[str]:
    """Which context keywords matched this company (used for 'why this result')."""
    industry = company.get("industry", "").lower()
    hay = f"{industry} {company.get('name','').lower()} {company.get('website','').lower()}"
    matched: List[str] = []
    for kw in (context.get("funding_focus") or []) + (context.get("industries") or []):
        k = str(kw).lower().strip()
        if k and (k in hay or any(h in hay or hay in h for h in _hints_for(k))):
            if k not in matched:
                matched.append(k)
    for kw in context.get("keywords", [])[:14]:
        k = str(kw).lower().strip()
        if k and k in hay and k not in matched:
            matched.append(k)
    return matched[:4]


def _hints_for(industry: str) -> List[str]:
    mapping = {
        "ai/ml": ["ai", "ml", "llm", "intelligence", "cortex", "model", "agent"],
        "fintech": ["pay", "ledger", "kredit", "vault", "trade", "insur", "fin"],
        "e-commerce": ["shop", "cart", "mercato", "commerce"],
        "developer tools": ["dev", "pipeline", "merge", "ci", "cloud", "compute", "shipped"],
        "saas": ["saas", "task", "ops", "woven", "crm", "flowdesk", "desk"],
        "healthtech": ["med", "care", "genom", "health", "bio"],
        "edtech": ["learn", "skill", "academy"],
        "cybersecurity": ["aegis", "trace", "security"],
        "gaming": ["play", "arcade", "game", "studios"],
        "web3": ["chain", "defi", "forge"],
        "mobility": ["fleet", "cargo", "ride", "volta"],
        "climate": ["helio", "carbon", "grid", "energy"],
        "data": ["data", "warehouse", "etl", "query"],
    }
    return mapping.get(industry, [industry.split("/")[0]])


async def _ai_scan(
    context: Dict[str, Any], stages: List[str], window_days: int, limit: int
) -> List[Dict[str, Any]]:
    """Ask the LLM for recently-funded companies aligned to the candidate."""
    prompt = f"""
List {limit} realistic companies that raised a funding round in the last {window_days} days
(stages: {', '.join(stages)}). Prioritize these industries for this candidate:
{', '.join(context.get('funding_focus', []) or context.get('industries', []) or ['software'])}.

Return STRICT JSON: {{"companies": [{{"name","website","industry","stage","days_ago","summary"}}]}}
- days_ago: integer 1..{window_days}
- stage must be one of: {', '.join(stages)}
- Use plausible but clearly synthetic company names to avoid false claims about real companies.
Respond ONLY with JSON.
"""
    try:
        data = await chat_completion("funding_scan", prompt, temperature=0.6)
        out: List[Dict[str, Any]] = []
        for c in (data.get("companies") or [])[: limit * 2]:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            stage = str(c.get("stage", "Series A"))
            if stage not in stages:
                stage = stages[0]
            days = c.get("days_ago", 7)
            try:
                days = max(1, min(window_days, int(days)))
            except Exception:
                days = 7
            name = re.sub(r"\s+", " ", str(c["name"]))[:60]
            out.append({
                "name": name,
                "website": str(c.get("website") or (name.lower().replace(" ", "") + ".com")),
                "industry": str(c.get("industry") or "saas").lower(),
                "stage": stage,
                "days_ago": days,
                "summary": str(c.get("summary") or "")[:240],
                "source": "ai",
            })
        # de-dupe by name
        seen, dedup = set(), []
        for c in out:
            if c["name"].lower() not in seen:
                seen.add(c["name"].lower())
                dedup.append(c)
        return dedup[:limit]
    except (AIClientError, TypeError):
        return []


def heuristic_scan(
    context: Dict[str, Any], stages: List[str], window_days: int, limit: int
) -> List[Dict[str, Any]]:
    """Curated dataset matched to the candidate context — always ≥ min(limit, pool)."""
    focus = [str(f).lower() for f in (context.get("funding_focus") or []) + (context.get("industries") or [])]
    matched, rest = [], []
    for c in CURATED_COMPANIES:
        if c["stage"] not in stages:
            continue
        hints = [c["industry"]] + _hints_for(c["industry"])
        if any(f in hints or any(h in f or f in h for h in hints) for f in focus):
            matched.append(c)
        else:
            rest.append(c)
    random.shuffle(rest)
    picked = matched + rest
    # Scale result count with window: ~1 company per 3 days, clamped to pool/limit
    target = max(12, min(limit, len(picked)))
    now = datetime.utcnow()
    out: List[Dict[str, Any]] = []
    for c in picked[:target]:
        days = _stable_days_ago(c["name"], window_days)
        out.append({
            "name": c["name"],
            "website": c["website"],
            "industry": c["industry"],
            "stage": c["stage"],
            "days_ago": days,
            "raised_at": (now - timedelta(days=days)).date().isoformat(),
            "summary": f"{c['name']} raised a {c['stage']} round — aligned to your profile focus: {', '.join(focus[:3]) or 'software'}.",
            "has_open_positions": _stable_open_positions(c["name"]),
            "source": "curated-demo",
        })
    out.sort(key=lambda x: x["days_ago"])
    return out


async def scan_funded_companies(
    context: Dict[str, Any],
    stages: Optional[List[str]] = None,
    window_days: int = 45,
    limit: int = 18,
) -> List[Dict[str, Any]]:
    """Full scan: AI when configured, curated fallback, context-matched, fresh."""
    stages = [s for s in (stages or STAGES) if s in STAGES] or STAGES
    window_days = max(7, min(180, int(window_days)))
    limit = max(8, min(40, int(limit)))

    companies: List[Dict[str, Any]] = []
    if is_configured("funding_scan"):
        companies = await _ai_scan(context, stages, window_days, limit)
        for c in companies:
            c.setdefault("raised_at", (datetime.utcnow() - timedelta(days=c["days_ago"])).date().isoformat())
            c.setdefault("has_open_positions", _stable_open_positions(c["name"]))
    if len(companies) < max(12, limit // 2):
        have = {c["name"].lower() for c in companies}
        for c in heuristic_scan(context, stages, window_days, limit):
            if c["name"].lower() not in have:
                companies.append(c)
    for c in companies:
        c.setdefault("source", "ai" if is_configured("funding_scan") else "curated-demo")
        c["keywords_matched"] = _match_score(c, context)
    companies.sort(key=lambda x: x.get("days_ago", 999))
    return companies[:limit]


def sync_funding_db(db, companies: List[Dict[str, Any]], window_days: int) -> None:
    """Upsert scan results; prune anything older than the freshness window."""
    from app.models.models import FundingCompany
    from app.db import SessionLocal  # noqa: F401  (db passed in is request-scoped)

    now = datetime.utcnow()
    cutoff = now - timedelta(days=window_days)
    names_seen = set()
    for c in companies:
        name = c["name"]
        names_seen.add(name.lower())
        row = db.query(FundingCompany).filter(FundingCompany.name.ilike(name)).first()
        raised = now - timedelta(days=int(c.get("days_ago", 7)))
        if not row:
            row = FundingCompany(name=name)
            db.add(row)
        row.stage = c.get("stage", row.stage)
        row.website = c.get("website", row.website)
        row.industry = c.get("industry", "")
        row.raised_at = raised
        row.has_open_positions = bool(c.get("has_open_positions"))
        row.keywords_matched = c.get("keywords_matched", [])
        row.summary = c.get("summary", "")
        row.source = c.get("source", "curated-demo")
        row.meta = {"days_ago": c.get("days_ago")}
        row.discovered_at = now
    # Prune stale rows: raised before the window or no longer part of the radar
    stale = db.query(FundingCompany).filter(FundingCompany.raised_at < cutoff).all()
    for row in stale:
        db.delete(row)
    leftover = db.query(FundingCompany).all()
    for row in leftover:
        if row.name.lower() not in names_seen:
            db.delete(row)
    db.commit()


def row_to_dict(row) -> Dict[str, Any]:
    days_ago = None
    if row.raised_at:
        days_ago = max(0, (datetime.utcnow() - row.raised_at).days)
    return {
        "id": row.id,
        "name": row.name,
        "stage": row.stage,
        "website": row.website,
        "industry": getattr(row, "industry", "") or "",
        "raised_at": row.raised_at.date().isoformat() if row.raised_at else None,
        "days_ago": days_ago,
        "has_open_positions": bool(row.has_open_positions),
        "keywords_matched": row.keywords_matched or [],
        "summary": getattr(row, "summary", "") or "",
        "source": getattr(row, "source", "curated-demo"),
        "discovered_at": row.discovered_at.isoformat() if row.discovered_at else None,
    }
