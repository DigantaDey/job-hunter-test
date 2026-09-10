"""
Decision-maker discovery & verification.

Provider order (first configured wins, results are merged and re-scored):

1. Hunter.io ``domain-search`` / ``email-finder`` (key required)
2. Apollo ``mixed_people/search`` (key required)
3. Clearbit company enrichment for firmographics (key required)
4. Role-alias heuristic (``hiring@``, ``jobs@``, ``careers@`` …)

Everything carries a ``source`` and a ``confidence``; the heuristic path is
explicitly labelled ``verified=False`` so a guessed address can never be
mistaken for a discovered one. Guessed addresses are also recorded so the UI can
warn that deliverability has not been proven.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc
from app.services import http as http_client

log = get_logger("app.contacts")

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
ROLE_PREFIXES = ["hiring", "jobs", "careers", "recruiting", "talent", "people", "hr", "founders", "hello"]
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "yopmail.com",
    "trashmail.com", "sharklasers.com", "getnada.com", "dispostable.com",
}
FREEMAIL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "protonmail.com", "icloud.com"}


def company_domain(company: str, website: str = "") -> str:
    if website:
        cleaned = website.split("//")[-1].split("/")[0].strip().lower()
        if cleaned and "." in cleaned:
            return cleaned[4:] if cleaned.startswith("www.") else cleaned
    slug = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    return f"{slug}.com" if slug else ""


def verify_email(email: str, *, check_mx: Optional[bool] = None) -> Dict[str, Any]:
    """Syntax + MX + policy checks. No SMTP probing (that gets IPs blacklisted)."""
    result: Dict[str, Any] = {
        "email": email,
        "syntax_ok": False,
        "mx_ok": None,
        "domain": "",
        "disposable": False,
        "freemail": False,
        "role_account": False,
        "reason": "",
        "score": 0.0,
    }
    if not email or not EMAIL_RE.match(email):
        result["reason"] = "invalid syntax"
        return result
    result["syntax_ok"] = True
    domain = email.split("@", 1)[1].lower()
    result["domain"] = domain
    result["disposable"] = domain in DISPOSABLE_DOMAINS
    result["freemail"] = domain in FREEMAIL_DOMAINS
    result["role_account"] = email.split("@", 1)[0].lower() in ROLE_PREFIXES

    if check_mx is None:
        check_mx = settings.contact_verify_mx
    if check_mx and not result["disposable"]:
        try:
            import dns.resolver

            answers = dns.resolver.resolve(domain, "MX", lifetime=5)
            result["mx_ok"] = len(answers) > 0
        except Exception as exc:
            log.debug("MX lookup failed for %s: %s", domain, exc)
            result["mx_ok"] = None  # unknown ≠ invalid

    score = 0.4
    if result["syntax_ok"]:
        score += 0.2
    if result["mx_ok"]:
        score += 0.3
    if result["role_account"]:
        score -= 0.05
    if result["disposable"]:
        score = 0.0
        result["reason"] = "disposable domain"
    result["score"] = round(max(0.0, min(1.0, score)), 2)
    return result


async def _hunter(domain: str, department: str) -> List[Dict[str, Any]]:
    if not settings.hunter_api_key or not domain:
        return []
    try:
        data = await http_client.get_json(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": settings.hunter_api_key, "limit": 10,
                    "department": department or "engineering"},
            cache_seconds=3600,
        )
    except Exception as exc:
        log.info("hunter lookup failed for %s: %s", domain, exc)
        return []
    contacts = []
    for entry in ((data or {}).get("data") or {}).get("emails", []) or []:
        if not entry.get("value"):
            continue
        contacts.append({
            "name": " ".join(filter(None, [entry.get("first_name"), entry.get("last_name")])) or "Hiring contact",
            "email": entry["value"],
            "title": entry.get("position") or "",
            "confidence": round(float(entry.get("confidence") or 0) / 100.0, 2),
            "source": "hunter",
            "verified": True,
        })
    return contacts


async def _apollo(domain: str, department: str) -> List[Dict[str, Any]]:
    if not settings.apollo_api_key or not domain:
        return []
    try:
        response = await http_client.request(
            "POST", "https://api.apollo.io/v1/mixed_people/search",
            json_body={
                "api_key": settings.apollo_api_key,
                "q_organization_domains": domain,
                "person_titles": ["head", "vp", "director", "manager", "founder", "cto", "engineering"],
                "page": 1,
                "per_page": 10,
            },
            headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
            cache_seconds=3600,
        )
        if response.status_code != 200:
            return []
        people = (response.json() or {}).get("people") or []
    except Exception as exc:
        log.info("apollo lookup failed for %s: %s", domain, exc)
        return []

    contacts = []
    for person in people:
        email = person.get("email")
        if not email:
            continue
        contacts.append({
            "name": person.get("name") or "Hiring contact",
            "email": email,
            "title": person.get("title") or "",
            "confidence": 0.7 if person.get("email_status") == "verified" else 0.55,
            "source": "apollo",
            "verified": person.get("email_status") == "verified",
        })
    return contacts


async def firmographics(domain: str) -> Dict[str, Any]:
    """Company size/industry enrichment (Clearbit when configured)."""
    if not settings.clearbit_api_key or not domain:
        return {}
    try:
        data = await http_client.get_json(
            "https://company.clearbit.com/v2/companies/find",
            params={"domain": domain},
            headers={"Authorization": f"Bearer {settings.clearbit_api_key}"},
            cache_seconds=86400,
        )
    except Exception as exc:
        log.debug("clearbit lookup failed for %s: %s", domain, exc)
        return {}
    metrics = (data or {}).get("metrics") or {}
    return {
        "name": data.get("name"),
        "employees": metrics.get("employees") or data.get("employees"),
        "industry": (data.get("category") or {}).get("industry") or data.get("industry"),
        "stage": (data.get("metrics") or {}).get("raised") and "funded" or None,
        "source": "clearbit",
    }


def heuristic_contacts(company: str, domain: str, department: str = "engineering") -> List[Dict[str, Any]]:
    """Role-alias guesses — explicitly unverified."""
    if not domain:
        return []
    prefix = (department or "hiring").split()[0].lower()
    prefixes = [prefix] if prefix in ROLE_PREFIXES else [prefix] + ROLE_PREFIXES[:3]
    contacts = []
    for index, alias in enumerate(dict.fromkeys(prefixes)):
        contacts.append({
            "name": f"{alias.title()} team" if alias not in ("founders",) else "Founders",
            "email": f"{alias}@{domain}",
            "title": "Role mailbox (guess)",
            "confidence": round(max(0.15, 0.3 - index * 0.05), 2),
            "source": "heuristic",
            "verified": False,
        })
    return contacts


async def discover_decision_makers(
    company: str,
    *,
    domain: str = "",
    department: str = "engineering",
    limit: int = 5,
) -> Dict[str, Any]:
    """Merge provider + heuristic contacts, de-duplicated and ranked."""
    resolved_domain = domain or company_domain(company)
    providers_used: List[str] = []
    contacts: List[Dict[str, Any]] = []

    for name, loader in (("hunter", _hunter), ("apollo", _apollo)):
        try:
            found = await loader(resolved_domain, department)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("%s failed: %s", name, exc)
            found = []
        if found:
            providers_used.append(name)
            contacts.extend(found)

    if not contacts:
        contacts.extend(heuristic_contacts(company, resolved_domain, department))
        providers_used.append("heuristic")

    deduped: Dict[str, Dict[str, Any]] = {}
    for contact in contacts:
        email = (contact.get("email") or "").strip().lower()
        if not email or email in deduped:
            continue
        contact["verification"] = verify_email(email)
        deduped[email] = contact

    ranked = sorted(
        deduped.values(),
        key=lambda c: (c.get("verified", False), c.get("confidence", 0), c["verification"]["score"]),
        reverse=True,
    )
    inc("jobhunter_contact_lookups_total", source=",".join(providers_used))
    return {
        "company": company,
        "domain": resolved_domain,
        "providers": providers_used,
        "contacts": ranked[:limit],
        "best": (ranked[0] if ranked else None),
        "firmographics": await firmographics(resolved_domain),
    }


async def find_decision_maker(company: str, department: str = "engineering", ai_config=None) -> Dict[str, Any]:
    """Back-compat single-contact helper (kept for the email pipeline)."""
    result = await discover_decision_makers(company, department=department, limit=1)
    best = result.get("best")
    if best:
        return {
            "name": best.get("name", "Hiring Manager"),
            "email": best["email"],
            "title": best.get("title", ""),
            "confidence": best.get("confidence", 0.0),
            "source": best.get("source", "heuristic"),
            "verified": bool(best.get("verified")),
            "verification": best.get("verification", {}),
        }
    domain = result.get("domain") or company_domain(company)
    return {"name": "Hiring Manager", "email": f"{department}@{domain}" if domain else "",
            "title": "Hiring Manager", "confidence": 0.0, "source": "none", "verified": False,
            "verification": {"score": 0.0, "reason": "no contact could be resolved"}}
