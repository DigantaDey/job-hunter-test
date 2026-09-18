"""
Back-compat shim.

The v1.2 scraper (curated pool + two live APIs + mocked form detection) has been
split into purpose-built modules:

* ``app.services.sources``        — real, ToS-compliant source adapters
* ``app.services.discovery``      — the discovery pipeline (scoring, dedupe, persistence)
* ``app.services.form_detector``  — real HTML form parsing
* ``app.services.demo_pool``      — clearly-labelled offline seed data

This module keeps the old import surface working for existing callers/tests.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services.demo_pool import demo_jobs
from app.services.form_detector import detect_form_structure as _detect_form_structure
from app.services.sources import fetch_all, list_sources

# Re-exported for backwards compatibility
KNOWN_SOURCES = ["greenhouse", "lever", "ashby", "workable", "recruitee", "smartrecruiters",
                 "personio", "workday",
                 "remotive", "arbeitnow", "jobicy", "remoteok", "himalayas", "themuse",
                 "weworkremotely", "adzuna", "jooble", "usajobs",
                 "linkedin", "indeed", "naukri", "instahyre"]


async def detect_form_structure(url: str, source: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    return await _detect_form_structure(url, source, **kwargs)


async def discover_jobs(keywords: List[str], freshness_hours: int, sources: Optional[List[str]] = None,
                        limit: int = 14, live_enabled: bool = True) -> List[Dict[str, Any]]:
    """Legacy helper: returns normalised postings *without* persisting them."""
    postings: List[Dict[str, Any]] = []
    if live_enabled:
        fetched, _report = await fetch_all(keywords, limit=limit, since_hours=max(24 * 7, freshness_hours),
                                           sources=sources)
        postings.extend(p.to_dict() for p in fetched)
    postings.extend(demo_jobs(keywords, freshness_hours, limit=limit))
    return postings[:limit]


async def fetch_jd(url: str) -> str:  # pragma: no cover - thin wrapper
    from app.services.http import get_text

    try:
        html = await get_text(url)
    except Exception:
        return ""
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)[:5000]


__all__ = ["KNOWN_SOURCES", "discover_jobs", "detect_form_structure", "fetch_jd", "list_sources", "demo_jobs"]
