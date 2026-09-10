import asyncio
import random
import re
from datetime import datetime, timedelta
from typing import List, Dict, Any
import httpx
from bs4 import BeautifulSoup

# Adapter pattern for job boards
KNOWN_SOURCES = ["linkedin", "naukri", "indeed", "instahyre", "lever", "greenhouse", "workday", "custom"]

MOCK_JOBS = [
    {"title": "Senior Backend Engineer", "company": "Stripe", "location": "Remote", "source": "lever", "description": "Build scalable backend with Python, FastAPI, AWS. 5+ years experience. Design microservices."},
    {"title": "Full Stack Developer", "company": "Notion", "location": "San Francisco, CA", "source": "greenhouse", "description": "React, TypeScript, Node.js. Build collaborative editor. Startup mindset."},
    {"title": "Software Engineer - Platform", "company": "Datadog", "location": "New York, NY", "source": "workday", "description": "Go, Kubernetes, distributed systems. Observability platform."},
    {"title": "Product Engineer", "company": "Linear", "location": "Remote", "source": "lever", "description": "Next.js, TypeScript. Early stage startup, Series B."},
    {"title": "ML Engineer", "company": "Anthropic", "location": "San Francisco, CA", "source": "greenhouse", "description": "PyTorch, LLM, Python. Research and production."},
    {"title": "Backend Engineer", "company": "TCS", "location": "Bangalore, India", "source": "naukri", "description": "Java, Spring Boot, microservices. Enterprise."},
    {"title": "Frontend Engineer", "company": "Flipkart", "location": "Bangalore, India", "source": "naukri", "description": "React, Redux, performance optimization."},
    {"title": "Senior Software Engineer", "company": "Google", "location": "Mountain View, CA", "source": "workday", "description": "Large scale systems, Go, Java. Big company."},
    {"title": "Founding Engineer", "company": "Stealth AI Startup", "location": "Remote", "source": "instahyre", "description": "Seed stage, 3 people, Python, LLM. Equity heavy."},
    {"title": "Software Developer", "company": "Infosys", "location": "Hyderabad, India", "source": "indeed", "description": "Java, SQL, enterprise applications."},
    {"title": "Staff Engineer", "company": "LinkedIn", "location": "Sunnyvale, CA", "source": "linkedin", "description": "LinkedIn feed, Scala, Kafka."},
]

def _mock_generate_jobs(keywords: List[str], freshness_hours: int, limit: int = 8) -> List[Dict[str,Any]]:
    # filter by keywords fuzzily
    kws = [k.lower() for k in keywords]
    filtered = []
    for job in MOCK_JOBS:
        text = (job["title"] + " " + job["description"]).lower()
        if not kws or any(kw in text for kw in kws if kw.strip()):
            filtered.append(job)
    if not filtered:
        filtered = MOCK_JOBS
    random.shuffle(filtered)
    result = []
    for j in filtered[:limit]:
        # random url
        slug = re.sub(r'\W+','-', j["title"].lower()).strip('-')
        result.append({
            **j,
            "url": f"https://{j['source']}.com/jobs/{slug}-{random.randint(1000,9999)}",
            "discovered_at": datetime.utcnow() - timedelta(hours=random.randint(0, freshness_hours)),
            "freshness_hours": freshness_hours,
        })
    return result

async def discover_jobs(keywords: List[str], freshness_hours: int, sources: List[str] = None) -> List[Dict[str,Any]]:
    """
    Real implementation would scrape with Playwright/BS4 + AI to find structure.
    For now, mock + AI structure detection stub.
    """
    # Simulate network delay
    await asyncio.sleep(0.5 + random.random()*0.5)
    jobs = _mock_generate_jobs(keywords, freshness_hours)
    # AI to find structure would be here; we return with extra form structure
    for j in jobs:
        j["forms_detected"] = await detect_form_structure(j["url"])
    return jobs

async def detect_form_structure(url: str) -> Dict[str,Any]:
    """
    Uses AI to find job website structure, forms etc.
    Mocked: returns synthetic form fields.
    In production, would fetch HTML, use LLM to parse.
    """
    # Simulate AI call
    domain = url.split("/")[2] if "://" in url else "unknown"
    if "workday" in domain or "greenhouse" in domain or "lever" in domain:
        return {
            "requires_login": True,
            "fields": [
                {"name":"firstName","type":"text","required":True},
                {"name":"lastName","type":"text","required":True},
                {"name":"email","type":"email","required":True},
                {"name":"phone","type":"tel","required":True},
                {"name":"resume","type":"file","required":True},
                {"name":"coverLetter","type":"file","required":False},
                {"name":"linkedin","type":"url","required":False},
                {"name":"workAuthorization","type":"select","required":True,"options":["Yes","No"]},
            ],
            "portal_type": domain.split(".")[0],
            "ai_confidence": 0.92
        }
    else:
        return {
            "requires_login": False,
            "fields": [
                {"name":"email","type":"email","required":True},
                {"name":"resume","type":"file","required":True},
            ],
            "portal_type": "simple",
            "ai_confidence": 0.88
        }

async def fetch_jd(url: str) -> str:
    # In production, fetch real JD; here mock
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, follow_redirects=True)
            if resp.status_code==200:
                soup = BeautifulSoup(resp.text, "lxml")
                return soup.get_text()[:5000]
    except:
        pass
    return ""
