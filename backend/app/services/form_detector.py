"""
Real ATS/application-form detection.

Replaces the previous hard-coded "0.92 confidence" mock with an actual fetch +
parse of the posting page:

* portal/ATS fingerprinting from the URL + DOM markers;
* form field extraction (name/id/label/type/required/options);
* deterministic mapping of known fields to profile attributes;
* an honest ``confidence`` derived from what was actually observed, and a
  ``source`` of ``html`` / ``ats_api`` / ``unavailable`` so callers (and the UI)
  can tell verified structure from a best-effort guess;
* optional AI pass for unmapped fields — suggestions are validated, never
  trusted blindly.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc
from app.services import http as http_client

log = get_logger("app.forms")

PORTAL_PATTERNS = {
    "greenhouse": (r"greenhouse\.io", r"boards\.greenhouse"),
    "lever": (r"lever\.co",),
    "workday": (r"myworkdayjobs\.com", r"workday\.com"),
    "ashby": (r"ashbyhq\.com",),
    "workable": (r"workable\.com",),
    "smartrecruiters": (r"smartrecruiters\.com",),
    "icims": (r"icims\.com",),
    "successfactors": (r"successfactors\.com", r"sapsf\.com"),
    "taleo": (r"taleo\.net",),
    "bamboohr": (r"bamboohr\.com",),
    "jobvite": (r"jobvite\.com",),
    "linkedin": (r"linkedin\.com",),
    "indeed": (r"indeed\.com",),
    "naukri": (r"naukri\.com",),
    "instahyre": (r"instahyre\.com",),
}

VAULT_DOMAINS = {
    "lever": "jobs.lever.co",
    "greenhouse": "boards.greenhouse.io",
    "workday": "myworkdayjobs.com",
    "ashby": "jobs.ashbyhq.com",
    "workable": "apply.workable.com",
    "smartrecruiters": "jobs.smartrecruiters.com",
}

KNOWN_FIELDS: Dict[str, List[str]] = {
    "firstName": ["first name", "firstname", "given name", "first_name", "fname"],
    "lastName": ["last name", "lastname", "surname", "family name", "last_name", "lname"],
    "fullName": ["full name", "name", "your name", "applicant name"],
    "email": ["email", "e-mail", "email address"],
    "phone": ["phone", "mobile", "telephone", "contact number"],
    "location": ["location", "city", "current location", "where are you based"],
    "linkedin": ["linkedin", "linkedin profile", "linkedin url"],
    "github": ["github", "portfolio", "website", "personal site"],
    "resume": ["resume", "cv", "attach resume", "resume/cv"],
    "coverLetter": ["cover letter", "coverletter", "letter of interest", "additional information"],
    "workAuthorization": ["work authorization", "authorized to work", "right to work", "visa", "sponsorship"],
    "salaryExpectation": ["salary", "compensation", "expected compensation", "pay expectation"],
    "noticePeriod": ["notice period", "availability", "start date", "earliest start"],
    "gender": ["gender"],
    "veteranStatus": ["veteran"],
    "disabilityStatus": ["disability"],
    "race": ["race", "ethnicity"],
}

# Volatile/irrelevant inputs we never try to fill.
IGNORED_INPUT_TYPES = {"hidden", "submit", "button", "image", "reset", "search"}
IGNORED_NAME_HINTS = ("csrf", "token", "_utf8", "honeypot", "utm_", "g-recaptcha", "recaptcha", "search")


def detect_portal_type(url: str, source: Optional[str] = None) -> str:
    host = urlparse(url or "").netloc.lower()
    if source and source in PORTAL_PATTERNS:
        return source
    for portal, patterns in PORTAL_PATTERNS.items():
        if any(re.search(p, host) for p in patterns):
            return portal
    return "custom"


def _label_for(element, soup: BeautifulSoup) -> str:
    element_id = element.get("id")
    if element_id:
        label = soup.find("label", attrs={"for": element_id})
        if label:
            return label.get_text(" ", strip=True)
    parent_label = element.find_parent("label")
    if parent_label:
        return parent_label.get_text(" ", strip=True)
    described = element.get("aria-label") or element.get("placeholder") or ""
    if described:
        return described
    # Workday/Ashby style: nearest preceding text node
    parent = element.find_parent(["div", "fieldset", "li"])
    if parent:
        text = parent.get_text(" ", strip=True)
        if 0 < len(text) < 90:
            return text
    return ""


def map_field_name(label: str, name: str, field_type: str) -> Optional[str]:
    """Map a raw field to a canonical profile key (or None when unknown)."""
    hay = f"{label} {name}".lower().replace("_", " ").replace("-", " ")
    for canonical, hints in KNOWN_FIELDS.items():
        for hint in hints:
            if hint in hay:
                return canonical
    if field_type == "file":
        return "resume"
    if field_type == "email":
        return "email"
    if field_type == "tel":
        return "phone"
    if field_type == "url" and "linkedin" in hay:
        return "linkedin"
    return None


def _extract_fields(soup: BeautifulSoup, portal: str) -> List[Dict[str, Any]]:
    fields: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for form in soup.find_all("form") or [soup]:
        for element in form.find_all(["input", "select", "textarea"]):
            name = (element.get("name") or element.get("id") or "").strip()
            field_type = (element.get("type") or element.name or "text").lower()
            if field_type in IGNORED_INPUT_TYPES:
                continue
            if not name or any(hint in name.lower() for hint in IGNORED_NAME_HINTS):
                continue
            if name in seen:
                continue
            seen.add(name)
            label = _label_for(element, soup)[:200]
            options: List[str] = []
            if element.name == "select":
                options = [opt.get_text(" ", strip=True) for opt in element.find_all("option")][:12]
            required = element.has_attr("required") or element.get("aria-required") == "true"
            if element.get("required") in ("false", "0"):
                required = False
            fields.append({
                "name": name,
                "label": label or name,
                "type": "file" if field_type == "file" else field_type,
                "required": bool(required),
                "options": options,
                "profile_key": map_field_name(label, name, field_type),
            })
    return fields


def _login_required(soup: BeautifulSoup, portal: str) -> bool:
    if soup.find("input", attrs={"type": "password"}):
        return True
    text = soup.get_text(" ", strip=True).lower()[:4000]
    markers = ("sign in to apply", "log in to apply", "create an account to apply", "already have an account")
    if any(marker in text for marker in markers):
        return True
    return portal in {"workday", "successfactors", "taleo", "icims"}


def heuristic_schema(url: str, portal: str, *, confidence: float = 0.35, source: str = "heuristic") -> Dict[str, Any]:
    """Portal-derived expectations used when the page cannot be fetched."""
    requires_login = portal in {"workday", "successfactors", "taleo", "icims", "linkedin", "indeed", "naukri"}
    fields = [
        {"name": "firstName", "label": "First name", "type": "text", "required": True, "options": [], "profile_key": "firstName"},
        {"name": "lastName", "label": "Last name", "type": "text", "required": True, "options": [], "profile_key": "lastName"},
        {"name": "email", "label": "Email", "type": "email", "required": True, "options": [], "profile_key": "email"},
        {"name": "phone", "label": "Phone", "type": "tel", "required": False, "options": [], "profile_key": "phone"},
        {"name": "resume", "label": "Resume", "type": "file", "required": True, "options": [], "profile_key": "resume"},
    ]
    if requires_login:
        fields.append({"name": "password", "label": "Password", "type": "password", "required": True,
                       "options": [], "profile_key": None})
    return {
        "portal_type": portal,
        "requires_login": requires_login,
        "vault_domain": VAULT_DOMAINS.get(portal, urlparse(url or "").netloc),
        "fields": fields,
        "ai_confidence": confidence,
        "detection_source": source,
        "url": url,
    }


async def detect_form_structure(
    url: str,
    source: Optional[str] = None,
    *,
    allow_fetch: bool = True,
    use_ai: bool = False,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Return the application-form schema for a posting URL.

    ``detection_source`` is one of ``html`` (parsed live), ``heuristic``
    (portal-known defaults), ``unavailable`` (fetch blocked/failed).
    """
    portal = detect_portal_type(url, source)

    if not url or not url.startswith("http"):
        return heuristic_schema(url, portal, confidence=0.3, source="heuristic")

    schema = heuristic_schema(url, portal, confidence=0.4, source="heuristic")

    if not allow_fetch or not settings.live_scraping_enabled:
        schema["detection_source"] = "heuristic"
        return schema

    try:
        response = await http_client.request(
            "GET", url, cache_seconds=settings.discovery_cache_seconds,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
    except PermissionError as exc:
        schema["detection_source"] = "unavailable"
        schema["error"] = f"robots.txt disallows fetching this page: {exc}"
        return schema
    except Exception as exc:
        schema["detection_source"] = "unavailable"
        schema["error"] = f"{type(exc).__name__}: {exc}"
        return schema

    if response.status_code != 200:
        schema["detection_source"] = "unavailable"
        schema["error"] = f"HTTP {response.status_code}"
        return schema

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.lower():
        schema["detection_source"] = "unavailable"
        schema["error"] = f"unsupported content type: {content_type}"
        return schema

    soup = BeautifulSoup(response.text, "lxml")
    fields = _extract_fields(soup, portal)
    requires_login = _login_required(soup, portal)

    if not fields:
        # JS-rendered ATS shells (Workday/Lever SPAs) have no static form.
        schema["requires_login"] = requires_login or schema["requires_login"]
        schema["detection_source"] = "heuristic"
        schema["notes"] = "no static form fields found — page is likely JS-rendered (Playwright would be required)"
        return schema

    mapped = [f for f in fields if f.get("profile_key")]
    unmapped = [f for f in fields if not f.get("profile_key")]

    if use_ai and unmapped:
        try:
            from app.services.ai_client import AIClientError, chat_completion

            prompt = (
                "Map these application-form fields to canonical profile keys. Allowed keys: "
                f"{sorted(KNOWN_FIELDS)}. Return JSON {{\"mapping\": {{\"<field_name>\": \"<key or null>\"}}}}.\n"
                f"Fields: {json.dumps([{ 'name': f['name'], 'label': f['label']} for f in unmapped])[:1500]}"
            )
            # No per-call timeout — inherit the global wait so slow reasoning models
            # are not cut off (ai.timeout / AI_TIMEOUT is the single knob).
            data = await chat_completion("form_detect", prompt, temperature=0)
            mapping = data.get("mapping") if isinstance(data, dict) else {}
            for field in unmapped:
                suggested = (mapping or {}).get(field["name"])
                if suggested in KNOWN_FIELDS:
                    field["profile_key"] = suggested
                    field["profile_key_source"] = "ai"
        except (AIClientError, Exception) as exc:  # AI is optional here
            log.debug("AI field mapping skipped: %s", exc)

    mapped = [f for f in fields if f.get("profile_key")]
    confidence = min(0.97, 0.45 + 0.5 * (len(mapped) / max(1, len(fields))))

    schema.update({
        "requires_login": requires_login,
        "fields": fields,
        "ai_confidence": round(confidence, 2),
        "detection_source": "html",
        "mapped_fields": [f["name"] for f in mapped],
        "unmapped_fields": [f["name"] for f in fields if not f.get("profile_key")],
        "has_file_upload": any(f["type"] == "file" for f in fields),
        "title": (soup.title.get_text(strip=True)[:200] if soup.title else ""),
    })
    inc("jobhunter_form_detections_total", result="html", portal=portal)
    return schema
