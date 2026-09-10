"""
Curated demo/offline job pool.

This is *seed data*, not scraping: it keeps the product usable offline and in
demo environments. It is disabled by default in production
(``INCLUDE_DEMO_POOL=false``) and every job it produces is labelled
``source="demo"`` in the UI so it can never be mistaken for a live posting.
"""
import hashlib
from datetime import datetime, timedelta
from typing import Any, Dict, List

DEMO_JOBS: List[Dict[str, Any]] = [
    {
        "title": "Senior Backend Engineer",
        "company": "Stripe",
        "location": "Remote",
        "source": "lever",
        "industry": "fintech",
        "description": "Build scalable backend with Python, FastAPI, AWS. 5+ years experience. Design microservices and payment rails."
    },
    {
        "title": "Full Stack Developer",
        "company": "Notion",
        "location": "San Francisco, CA",
        "source": "greenhouse",
        "industry": "saas",
        "description": "React, TypeScript, Node.js. Build collaborative editor. Startup mindset, product engineering."
    },
    {
        "title": "Software Engineer - Platform",
        "company": "Datadog",
        "location": "New York, NY",
        "source": "workday",
        "industry": "developer tools",
        "description": "Go, Kubernetes, distributed systems. Observability platform at scale."
    },
    {
        "title": "Product Engineer",
        "company": "Linear",
        "location": "Remote",
        "source": "lever",
        "industry": "saas",
        "description": "Next.js, TypeScript, GraphQL. Early stage startup Series B. Craft-focused product development."
    },
    {
        "title": "ML Engineer",
        "company": "Anthropic",
        "location": "San Francisco, CA",
        "source": "greenhouse",
        "industry": "ai/ml",
        "description": "PyTorch, LLM, Python. Research and production ML systems."
    },
    {
        "title": "Backend Engineer",
        "company": "TCS",
        "location": "Bangalore, India",
        "source": "naukri",
        "industry": "saas",
        "description": "Java, Spring Boot, microservices. Enterprise applications."
    },
    {
        "title": "Frontend Engineer",
        "company": "Flipkart",
        "location": "Bangalore, India",
        "source": "naukri",
        "industry": "e-commerce",
        "description": "React, Redux, JavaScript performance optimization for e-commerce at scale."
    },
    {
        "title": "Senior Software Engineer",
        "company": "Google",
        "location": "Mountain View, CA",
        "source": "workday",
        "industry": "saas",
        "description": "Large scale systems, Go, Java, Python. Big company infrastructure."
    },
    {
        "title": "Founding Engineer",
        "company": "Stealth AI Startup",
        "location": "Remote",
        "source": "instahyre",
        "industry": "ai/ml",
        "description": "Seed stage, 3 people, Python, LLM, fastapi. Equity heavy founding role."
    },
    {
        "title": "Software Developer",
        "company": "Infosys",
        "location": "Hyderabad, India",
        "source": "indeed",
        "industry": "saas",
        "description": "Java, SQL, enterprise applications, cloud migration."
    },
    {
        "title": "Staff Engineer",
        "company": "LinkedIn",
        "location": "Sunnyvale, CA",
        "source": "linkedin",
        "industry": "saas",
        "description": "LinkedIn feed, Scala, Kafka, distributed data systems."
    },
    {
        "title": "DevOps Engineer",
        "company": "HashiCorp",
        "location": "Remote",
        "source": "lever",
        "industry": "developer tools",
        "description": "Terraform, Kubernetes, AWS, CI/CD pipelines, infrastructure as code."
    },
    {
        "title": "Data Engineer",
        "company": "Snowflake",
        "location": "Remote",
        "source": "greenhouse",
        "industry": "data",
        "description": "SQL, Python, Spark, Airflow, dbt. Analytics engineering at petabyte scale."
    },
    {
        "title": "Machine Learning Engineer",
        "company": "Scale AI",
        "location": "San Francisco, CA",
        "source": "greenhouse",
        "industry": "ai/ml",
        "description": "Python, PyTorch, data pipelines, LLM evaluation, MLops."
    },
    {
        "title": "Frontend Engineer",
        "company": "Vercel",
        "location": "Remote",
        "source": "lever",
        "industry": "developer tools",
        "description": "React, Next.js, TypeScript, DX tooling, edge rendering."
    },
    {
        "title": "Payments Engineer",
        "company": "Razorpay",
        "location": "Bangalore, India",
        "source": "instahyre",
        "industry": "fintech",
        "description": "Python, Java, payment systems, UPI, high-throughput transaction APIs."
    },
    {
        "title": "Mobile Engineer",
        "company": "CRED",
        "location": "Bangalore, India",
        "source": "instahyre",
        "industry": "fintech",
        "description": "Android, Kotlin, fintech app development, design-first culture."
    },
    {
        "title": "Backend Engineer",
        "company": "Zerodha",
        "location": "Bangalore, India",
        "source": "naukri",
        "industry": "fintech",
        "description": "Python, Go, trading systems, low latency, kubernetes."
    },
    {
        "title": "Full Stack Engineer",
        "company": "Zoho",
        "location": "Chennai, India",
        "source": "naukri",
        "industry": "saas",
        "description": "JavaScript, Java, SaaS product development, SQL."
    },
    {
        "title": "AI Engineer",
        "company": "Perplexity",
        "location": "Remote",
        "source": "greenhouse",
        "industry": "ai/ml",
        "description": "LLM, RAG, Python, search infrastructure, answer engines."
    },
    {
        "title": "Site Reliability Engineer",
        "company": "Cloudflare",
        "location": "Remote",
        "source": "greenhouse",
        "industry": "developer tools",
        "description": "Go, Kubernetes, SRE, edge network, incident response."
    },
    {
        "title": "Security Engineer",
        "company": "CrowdStrike",
        "location": "Remote",
        "source": "workday",
        "industry": "cybersecurity",
        "description": "Python, threat detection, siem, incident response, cloud security."
    },
    {
        "title": "Growth Engineer",
        "company": "Shopify",
        "location": "Remote",
        "source": "workday",
        "industry": "e-commerce",
        "description": "Ruby, JavaScript, experimentation, e-commerce storefront performance."
    },
    {
        "title": "Backend Engineer",
        "company": "Dukaan",
        "location": "Remote",
        "source": "instahyre",
        "industry": "e-commerce",
        "description": "Node.js, PostgreSQL, Redis, marketplace systems, docker."
    },
    {
        "title": "Product Manager - Platform",
        "company": "Freshworks",
        "location": "Chennai, India",
        "source": "naukri",
        "industry": "saas",
        "description": "B2B SaaS, roadmap, SQL, analytics, cross-functional leadership."
    },
    {
        "title": "QA Automation Engineer",
        "company": "BrowserStack",
        "location": "Mumbai, India",
        "source": "instahyre",
        "industry": "developer tools",
        "description": "Selenium, Python, CI/CD, test infrastructure."
    },
    {
        "title": "Engineering Manager",
        "company": "Rippling",
        "location": "Remote",
        "source": "greenhouse",
        "industry": "saas",
        "description": "React, TypeScript, team leadership, HR SaaS, system design."
    },
    {
        "title": "Blockchain Engineer",
        "company": "Alchemy",
        "location": "Remote",
        "source": "lever",
        "industry": "web3",
        "description": "Solidity, web3 infrastructure, node APIs, Go."
    },
    {
        "title": "Health Platform Engineer",
        "company": "Practo",
        "location": "Bangalore, India",
        "source": "naukri",
        "industry": "healthtech",
        "description": "Java, AWS, healthcare apis, telemedicine, postgresql."
    },
    {
        "title": "Edtech Backend Engineer",
        "company": "Unacademy",
        "location": "Bangalore, India",
        "source": "instahyre",
        "industry": "edtech",
        "description": "Python, Django, Redis, live classes infrastructure."
    },
    {
        "title": "Game Engineer",
        "company": "Unity",
        "location": "Remote",
        "source": "workday",
        "industry": "gaming",
        "description": "C#, Unity engine, multiplayer systems, performance."
    },
    {
        "title": "Climate Data Scientist",
        "company": "Watershed",
        "location": "Remote",
        "source": "lever",
        "industry": "climate",
        "description": "Python, carbon accounting, SQL, data modeling, climate analytics."
    },
    {
        "title": "Logistics Engineer",
        "company": "Delhivery",
        "location": "Gurugram, India",
        "source": "naukri",
        "industry": "mobility",
        "description": "Python, Go, supply chain optimization, kafka, spark."
    },
    {
        "title": "Junior Web Developer",
        "company": "Zomato",
        "location": "Delhi, India",
        "source": "indeed",
        "industry": "e-commerce",
        "description": "JavaScript, React, food delivery platform, rest apis."
    },
    {
        "title": "Associate Software Engineer",
        "company": "Accenture",
        "location": "Pune, India",
        "source": "indeed",
        "industry": "saas",
        "description": "Java, SQL, spring boot, enterprise projects, cloud."
    },
    {
        "title": "Data Analyst",
        "company": "Swiggy",
        "location": "Bangalore, India",
        "source": "instahyre",
        "industry": "e-commerce",
        "description": "SQL, Python, Tableau, analytics, experimentation."
    },
    {
        "title": "Site Reliability Engineer",
        "company": "Postman",
        "location": "Bangalore, India",
        "source": "instahyre",
        "industry": "developer tools",
        "description": "Kubernetes, AWS, observability, devtools platform, go."
    },
    {
        "title": "ML Platform Engineer",
        "company": "Databricks",
        "location": "Remote",
        "source": "workday",
        "industry": "data",
        "description": "Spark, Python, mlflow, distributed compute, machine learning."
    },
    {
        "title": "API Engineer",
        "company": "Twilio",
        "location": "Remote",
        "source": "lever",
        "industry": "developer tools",
        "description": "Java, Go, rest apis, communications platform, reliability."
    },
    {
        "title": "Frontend Engineer II",
        "company": "Atlassian",
        "location": "Remote",
        "source": "workday",
        "industry": "saas",
        "description": "React, TypeScript, design systems, collaboration tools."
    }
]

# Freshness spread so the freshness filter behaves realistically offline.
_FRESHNESS_OFFSETS = [2, 5, 9, 14, 20, 30, 44, 60, 90, 120]


def demo_jobs(keywords: List[str], freshness_hours: int, limit: int = 14) -> List[Dict[str, Any]]:
    """Return demo postings matching *keywords*, stamped with recent timestamps."""
    now = datetime.utcnow()
    keywords = [k.lower() for k in (keywords or []) if k]

    def matches(job: Dict[str, Any]) -> bool:
        if not keywords:
            return True
        hay = f"{job['title']} {job['description']} {job.get('industry', '')} {job['company']}".lower()
        return any(k in hay for k in keywords)

    selected = [j for j in DEMO_JOBS if matches(j)] or DEMO_JOBS
    out: List[Dict[str, Any]] = []
    for index, job in enumerate(selected[:limit]):
        hours = _FRESHNESS_OFFSETS[index % len(_FRESHNESS_OFFSETS)]
        entry = dict(job)
        entry["source"] = "demo"
        # A stable digest, not the builtin ``hash()``: PYTHONHASHSEED is
        # randomised per process, which would give every demo job a new id (and
        # so a new dedupe key) on each restart and pile up duplicates.
        digest = hashlib.sha1(f"{job['company']}|{job['title']}".encode("utf-8")).hexdigest()[:10]
        entry["external_id"] = f"demo-{index}-{digest}"
        entry["posted_at"] = now - timedelta(hours=hours)
        entry["url"] = f"https://demo.jobhunter.local/{job['company'].lower().replace(' ', '-')}/{index}"
        entry["remote"] = "remote" in (job.get("location", "") + job.get("description", "")).lower()
        entry["salary"] = ""
        out.append(entry)
    return out
