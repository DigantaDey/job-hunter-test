import asyncio
import random
import re
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

import httpx
from bs4 import BeautifulSoup

from app.core.config import settings

# Adapter pattern for job boards
KNOWN_SOURCES = ["linkedin", "naukri", "indeed", "instahyre", "lever", "greenhouse", "workday", "custom"]

# Larger curated pool (industries align with the funding radar dataset)
MOCK_JOBS = [
    {"title": "Senior Backend Engineer", "company": "Stripe", "location": "Remote", "source": "lever", "industry": "fintech", "description": "Build scalable backend with Python, FastAPI, AWS. 5+ years experience. Design microservices and payment rails."},
    {"title": "Full Stack Developer", "company": "Notion", "location": "San Francisco, CA", "source": "greenhouse", "industry": "saas", "description": "React, TypeScript, Node.js. Build collaborative editor. Startup mindset, product engineering."},
    {"title": "Software Engineer - Platform", "company": "Datadog", "location": "New York, NY", "source": "workday", "industry": "developer tools", "description": "Go, Kubernetes, distributed systems. Observability platform at scale."},
    {"title": "Product Engineer", "company": "Linear", "location": "Remote", "source": "lever", "industry": "saas", "description": "Next.js, TypeScript, GraphQL. Early stage startup Series B. Craft-focused product development."},
    {"title": "ML Engineer", "company": "Anthropic", "location": "San Francisco, CA", "source": "greenhouse", "industry": "ai/ml", "description": "PyTorch, LLM, Python. Research and production ML systems."},
    {"title": "Backend Engineer", "company": "TCS", "location": "Bangalore, India", "source": "naukri", "industry": "saas", "description": "Java, Spring Boot, microservices. Enterprise applications."},
    {"title": "Frontend Engineer", "company": "Flipkart", "location": "Bangalore, India", "source": "naukri", "industry": "e-commerce", "description": "React, Redux, JavaScript performance optimization for e-commerce at scale."},
    {"title": "Senior Software Engineer", "company": "Google", "location": "Mountain View, CA", "source": "workday", "industry": "saas", "description": "Large scale systems, Go, Java, Python. Big company infrastructure."},
    {"title": "Founding Engineer", "company": "Stealth AI Startup", "location": "Remote", "source": "instahyre", "industry": "ai/ml", "description": "Seed stage, 3 people, Python, LLM, fastapi. Equity heavy founding role."},
    {"title": "Software Developer", "company": "Infosys", "location": "Hyderabad, India", "source": "indeed", "industry": "saas", "description": "Java, SQL, enterprise applications, cloud migration."},
    {"title": "Staff Engineer", "company": "LinkedIn", "location": "Sunnyvale, CA", "source": "linkedin", "industry": "saas", "description": "LinkedIn feed, Scala, Kafka, distributed data systems."},
    {"title": "DevOps Engineer", "company": "HashiCorp", "location": "Remote", "source": "lever", "industry": "developer tools", "description": "Terraform, Kubernetes, AWS, CI/CD pipelines, infrastructure as code."},
    {"title": "Data Engineer", "company": "Snowflake", "location": "Remote", "source": "greenhouse", "industry": "data", "description": "SQL, Python, Spark, Airflow, dbt. Analytics engineering at petabyte scale."},
    {"title": "Machine Learning Engineer", "company": "Scale AI", "location": "San Francisco, CA", "source": "greenhouse", "industry": "ai/ml", "description": "Python, PyTorch, data pipelines, LLM evaluation, MLops."},
    {"title": "Frontend Engineer", "company": "Vercel", "location": "Remote", "source": "lever", "industry": "developer tools", "description": "React, Next.js, TypeScript, DX tooling, edge rendering."},
    {"title": "Payments Engineer", "company": "Razorpay", "location": "Bangalore, India", "source": "instahyre", "industry": "fintech", "description": "Python, Java, payment systems, UPI, high-throughput transaction APIs."},
    {"title": "Mobile Engineer", "company": "CRED", "location": "Bangalore, India", "source": "instahyre", "industry": "fintech", "description": "Android, Kotlin, fintech app development, design-first culture."},
    {"title": "Backend Engineer", "company": "Zerodha", "location": "Bangalore, India", "source": "naukri", "industry": "fintech", "description": "Python, Go, trading systems, low latency, kubernetes."},
    {"title": "Full Stack Engineer", "company": "Zoho", "location": "Chennai, India", "source": "naukri", "industry": "saas", "description": "JavaScript, Java, SaaS product development, SQL."},
    {"title": "AI Engineer", "company": "Perplexity", "location": "Remote", "source": "greenhouse", "industry": "ai/ml", "description": "LLM, RAG, Python, search infrastructure, answer engines."},
    {"title": "Site Reliability Engineer", "company": "Cloudflare", "location": "Remote", "source": "greenhouse", "industry": "developer tools", "description": "Go, Kubernetes, SRE, edge network, incident response."},
    {"title": "Security Engineer", "company": "CrowdStrike", "location": "Remote", "source": "workday", "industry": "cybersecurity", "description": "Python, threat detection, siem, incident response, cloud security."},
    {"title": "Growth Engineer", "company": "Shopify", "location": "Remote", "source": "workday", "industry": "e-commerce", "description": "Ruby, JavaScript, experimentation, e-commerce storefront performance."},
    {"title": "Backend Engineer", "company": "Dukaan", "location": "Remote", "source": "instahyre", "industry": "e-commerce", "description": "Node.js, PostgreSQL, Redis, marketplace systems, docker."},
    {"title": "Product Manager - Platform", "company": "Freshworks", "location": "Chennai, India", "source": "naukri", "industry": "saas", "description": "B2B SaaS, roadmap, SQL, analytics, cross-functional leadership."},
    {"title": "QA Automation Engineer", "company": "BrowserStack", "location": "Mumbai, India", "source": "instahyre", "industry": "developer tools", "description": "Selenium, Python, CI/CD, test infrastructure."},
    {"title": "Engineering Manager", "company": "Rippling", "location": "Remote", "source": "greenhouse", "industry": "saas", "description": "React, TypeScript, team leadership, HR SaaS, system design."},
    {"title": "Blockchain Engineer", "company": "Alchemy", "location": "Remote", "source": "lever", "industry": "web3", "description": "Solidity, web3 infrastructure, node APIs, Go."},
    {"title": "Health Platform Engineer", "company": "Practo", "location": "Bangalore, India", "source": "naukri", "industry": "healthtech", "description": "Java, AWS, healthcare apis, telemedicine, postgresql."},
    {"title": "Edtech Backend Engineer", "company": "Unacademy", "location": "Bangalore, India", "source": "instahyre", "industry": "edtech", "description": "Python, Django, Redis, live classes infrastructure."},
    {"title": "Game Engineer", "company": "Unity", "location": "Remote", "source": "workday", "industry": "gaming", "description": "C#, Unity engine, multiplayer systems, performance."},
    {"title": "Climate Data Scientist", "company": "Watershed", "location": "Remote", "source": "lever", "industry": "climate", "description": "Python, carbon accounting, SQL, data modeling, climate analytics."},
    {"title": "Logistics Engineer", "company": "Delhivery", "location": "Gurugram, India", "source": "naukri", "industry": "mobility", "description": "Python, Go, supply chain optimization, kafka, spark."},
    {"title": "Junior Web Developer", "company": "Zomato", "location": "Delhi, India", "source": "indeed", "industry": "e-commerce", "description": "JavaScript, React, food delivery platform, rest apis."},
    {"title": "Associate Software Engineer", "company": "Accenture", "location": "Pune, India", "source": "indeed", "industry": "saas", "description": "Java, SQL, spring boot, enterprise projects, cloud."},
    {"title": "Data Analyst", "company": "Swiggy", "location": "Bangalore, India", "source": "instahyre", "industry": "e-commerce", "description": "SQL, Python, Tableau, analytics, experimentation."},
    {"title": "Site Reliability Engineer", "company": "Postman", "location": "Bangalore, India", "source": "instahyre", "industry": "developer tools", "description": "Kubernetes, AWS, observability, devtools platform, go."},
    {"title": "ML Platform Engineer", "company": "Databricks", "location": "Remote", "source": "workday", "industry": "data", "description": "Spark, Python, mlflow, distributed compute, machine learning."},
    {"title": "API Engineer", "company": "Twilio", "location": "Remote", "source": "lever", "industry": "developer tools", "description": "Java, Go, rest apis, communications platform, reliability."},
    {"title": "Frontend Engineer II", "company": "Atlassian", "location": "Remote", "source": "workday", "industry": "saas", "description": "React, TypeScript, design systems, collaboration tools."},
]


def _slug(text: str) -> str:
    return re.sub(r"\W+", "-", text.lower()).strip("-")[:60] or "job"


def _stable_id(job: Dict[str, Any]) -> str:
    """Deterministic id so dedupe works across discovery runs."""
    return f"{_slug(job['company'])}-{_slug(job['title'])}"


def _match(job: Dict[str, Any], keywords: List[str]) -> bool:
    hay = (job["title"] + " " + job["description"] + " " + job.get("industry", "") + " " + job["company"]).lower()
    if not keywords:
        return True
    for kw in keywords:
        k = kw.lower().strip()
        if k and (k in hay or all(w in hay for w in k.split())):
            return True
    return False


def _synthetic_job(kw: str, i: int) -> Dict[str, Any]:
    """Generate additional keyword-matched roles so result count scales with the search context."""
    kw_clean = kw.strip().title()
    templates = [
        ("{kw} Engineer", "{c}", ["Build and scale {kw} systems.", "Python, AWS, microservices, CI/CD."]),
        ("{kw} Specialist", "{c}", ["Own {kw} initiatives end-to-end.", "Collaborate with product and data teams."]),
        ("Senior {kw} Developer", "{c}", ["Lead {kw} delivery for enterprise clients.", "Mentor junior engineers, drive architecture."]),
    ]
    companies = [
        ("TechNova", "greenhouse", "saas"), ("Quantive Labs", "lever", "data"),
        ("BrightStack", "workday", "developer tools"), ("NimbusWorks", "lever", "saas"),
        ("OrbitSoft", "greenhouse", "saas"), ("Helios Systems", "workday", "developer tools"),
        ("Corely", "instahyre", "saas"), ("Aralyx", "lever", "ai/ml"),
    ]
    t = templates[i % len(templates)]
    c = companies[i % len(companies)]
    title = t[0].format(kw=kw_clean)
    desc = ". ".join(t[2]).format(kw=kw_clean) + f". Role matching your profile focus: {kw_clean}."
    return {"title": title, "company": c[0], "location": "Remote", "source": c[1], "industry": c[2], "description": desc}


def _mock_generate_jobs(keywords: List[str], freshness_hours: int, limit: int = 14) -> List[Dict[str, Any]]:
    matched = [j for j in MOCK_JOBS if _match(j, keywords)]
    if len(matched) < limit:
        extra_needed = min(limit - len(matched), max(0, len(keywords) * 2))
        for i, kw in enumerate([k for k in keywords if len(k.strip()) > 2][:extra_needed]):
            matched.append(_synthetic_job(kw, i))
    if not matched:
        matched = list(MOCK_JOBS)
    result = []
    for j in matched[:limit]:
        slug = _slug(f"{j['company']} {j['title']}")
        result.append({
            **j,
            "url": f"https://jobs.example/{j['source']}/{slug}",
            "discovered_at": datetime.utcnow() - timedelta(hours=random.randint(0, max(1, freshness_hours))),
            "freshness_hours": freshness_hours,
        })
    random.shuffle(result)
    return result


# ---------------------------------------------------------------------------
# Live adapters — public, keyless JSON job APIs (ToS-respecting). When the
# network is unavailable (offline/dev sandbox), they return [] and the curated
# pool below keeps discovery working. In a real deployment with internet they
# contribute genuine listings on top of the curated/synthetic pool.
# ---------------------------------------------------------------------------
LIVE_SOURCES = ["arbeitnow", "remotive"]

_INDUSTRY_HINTS = {
    "fintech": ["fintech", "payment", "banking", "lending", "trading", "insurance"],
    "ai/ml": ["ai", "machine learning", "deep learning", "llm", "nlp", "ml", "data scientist", "artificial intelligence"],
    "e-commerce": ["ecommerce", "e-commerce", "retail", "marketplace"],
    "healthtech": ["health", "healthcare", "medical", "clinical", "biotech"],
    "edtech": ["education", "edtech", "learning"],
    "developer tools": ["developer tools", "devtools", "api", "sdk", "infrastructure", "open source"],
    "cybersecurity": ["security", "cybersecurity", "auth", "encryption", "siem"],
    "saas": ["saas", "b2b", "crm", "erp", "workflow", "productivity"],
    "gaming": ["game", "gaming", "unity", "unreal"],
    "web3": ["web3", "blockchain", "crypto", "solidity", "defi"],
    "mobility": ["mobility", "automotive", "ev", "logistics", "supply chain"],
    "climate": ["climate", "clean energy", "solar", "sustainability", "carbon"],
    "data": ["data engineering", "analytics", "etl", "warehouse", "data platform"],
}


def _guess_industry(text: str) -> str:
    """Map free text to an industry bucket.

    Short tokens (e.g. "ai", "ml") are matched on whole words only, so they
    don't false-positive inside words like "chain".
    """
    t = (text or "").lower()
    tokens = set(re.findall(r"[a-z0-9+#.]+", t))
    for ind, hints in _INDUSTRY_HINTS.items():
        for h in hints:
            h = h.lower()
            if " " in h or len(h) > 3:
                if h in t:
                    return ind
            elif h in tokens:
                return ind
    return "saas"


def _normalize_live(raw: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    """Map a raw API row from Arbeitnow / Remotive to the internal job shape."""
    try:
        if source == "arbeitnow":
            title = (raw.get("title") or "").strip()
            company = (raw.get("company_name") or "").strip()
            location = (raw.get("location") or "").strip() or "Remote"
            url = (raw.get("url") or "").strip()
            desc = " ".join((raw.get("tags") or [])[:10]).strip()
        elif source == "remotive":
            title = (raw.get("title") or "").strip()
            company = (raw.get("company_name") or "").strip()
            location = (raw.get("candidate_required_location") or "").strip() or "Remote"
            url = (raw.get("url") or "").strip()
            desc = (raw.get("description") or "").strip()[:800]
        else:
            return None
        if not title or not company:
            return None
        if not url:
            url = f"https://{source}.com/jobs?search={_slug(title)}"
        if not desc:
            desc = title
        return {
            "title": title,
            "company": company,
            "location": location,
            "source": source,
            "industry": _guess_industry(f"{title} {desc}"),
            "description": desc,
            "url": url,
        }
    except Exception:
        return None


async def _fetch_arbeitnow(keywords: List[str], limit: int = 10) -> List[Dict[str, Any]]:
    query = " ".join((keywords or [])[:4])
    url = "https://www.arbeitnow.com/api/job-board-api"
    try:
        async with httpx.AsyncClient(timeout=settings.live_scrape_timeout, headers={"User-Agent": "JobHunter/1.2"}) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return []
        rows = (resp.json() or {}).get("data") or []
        jobs: List[Dict[str, Any]] = []
        for raw in rows:
            norm = _normalize_live(raw, "arbeitnow")
            if norm and (not query or any(k.lower() in f"{norm['title']} {norm['description']}".lower() for k in keywords or [])):
                jobs.append(norm)
            if len(jobs) >= limit:
                break
        return jobs
    except Exception:
        return []


async def _fetch_remotive(keywords: List[str], limit: int = 10) -> List[Dict[str, Any]]:
    query = " ".join((keywords or [])[:4])
    url = "https://remotive.com/api/remote-jobs"
    try:
        params = {"search": query} if query else {}
        async with httpx.AsyncClient(timeout=settings.live_scrape_timeout, headers={"User-Agent": "JobHunter/1.2"}) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return []
        rows = (resp.json() or {}).get("jobs") or []
        jobs: List[Dict[str, Any]] = []
        for raw in rows:
            norm = _normalize_live(raw, "remotive")
            if norm:
                jobs.append(norm)
            if len(jobs) >= limit:
                break
        return jobs
    except Exception:
        return []


async def fetch_live_jobs(keywords: List[str], sources: Optional[List[str]] = None, limit: int = 12, enabled: bool = True) -> List[Dict[str, Any]]:
    """Fetch live listings from keyless public APIs; never raises.

    Disabled by env (`LIVE_SCRAPING_ENABLED=false`) or per-run by the user's
    Settings toggle (`scraping.live_enabled`).
    """
    if not settings.live_scraping_enabled or not enabled:
        return []
    sources = [s for s in (sources or LIVE_SOURCES) if s in LIVE_SOURCES]
    tasks = []
    if "arbeitnow" in sources:
        tasks.append(_fetch_arbeitnow(keywords, limit))
    if "remotive" in sources:
        tasks.append(_fetch_remotive(keywords, limit))
    if not tasks:
        return []
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: List[Dict[str, Any]] = []
    for r in results:
        if isinstance(r, list):
            out.extend(r)
    return out[:limit]


async def discover_jobs(keywords: List[str], freshness_hours: int, sources: Optional[List[str]] = None, limit: int = 14, live_enabled: bool = True) -> List[Dict[str, Any]]:
    """
    Discovery across adapters (linkedin/naukri/indeed/instahyre/lever/greenhouse/workday
    curated pool + live keyless APIs). Results are deduplicated by a stable
    (company,title) key. In production each adapter would use Playwright/BS4 +
    AI form detection; the curated/synthetic pool keeps the demo deterministic.
    """
    await asyncio.sleep(0.3 + random.random() * 0.3)

    live: List[Dict[str, Any]] = await fetch_live_jobs(keywords, limit=max(4, limit // 2), enabled=live_enabled)
    jobs = _mock_generate_jobs(keywords, freshness_hours, limit=limit)

    # Merge live first, then curated; dedupe by (company, title).
    seen = set()
    merged: List[Dict[str, Any]] = []
    for j in live + jobs:
        key = (j["company"].strip().lower(), j["title"].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append(j)
    if len(merged) > limit:
        merged = merged[:limit]

    for j in merged:
        j["stable_id"] = _stable_id(j)
        j["forms_detected"] = await detect_form_structure(j["url"], source=j["source"])
    return merged


async def detect_form_structure(url: str, source: Optional[str] = None) -> Dict[str, Any]:
    """
    Uses AI to find job website structure, forms etc.
    Mocked: returns synthetic form fields.
    In production, would fetch HTML, use LLM to parse.
    """
    domain = url.split("/")[2] if "://" in url else "unknown"
    portal = (source or "").lower() or domain.split(".")[0]
    if portal in ("workday", "greenhouse", "lever") or any(p in domain for p in ("workday", "greenhouse", "lever")):
        vault_domains = {
            "lever": "jobs.lever.co",
            "greenhouse": "boards.greenhouse.io",
            "workday": "myworkdayjobs.com",
        }
        return {
            "vault_domain": vault_domains.get(portal, domain),
            "requires_login": True,
            "fields": [
                {"name": "firstName", "type": "text", "required": True},
                {"name": "lastName", "type": "text", "required": True},
                {"name": "email", "type": "email", "required": True},
                {"name": "phone", "type": "tel", "required": True},
                {"name": "resume", "type": "file", "required": True},
                {"name": "coverLetter", "type": "file", "required": False},
                {"name": "linkedin", "type": "url", "required": False},
                {"name": "workAuthorization", "type": "select", "required": True, "options": ["Yes", "No"]},
            ],
            "portal_type": portal,
            "ai_confidence": 0.92,
        }
    return {
        "requires_login": False,
        "fields": [
            {"name": "email", "type": "email", "required": True},
            {"name": "resume", "type": "file", "required": True},
        ],
        "portal_type": "simple",
        "ai_confidence": 0.88,
    }


async def fetch_jd(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, follow_redirects=True)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                return soup.get_text()[:5000]
    except Exception:
        pass
    return ""
