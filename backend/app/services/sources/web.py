"""Conservative URL-to-Posting normalization for search discovery.

A fetched page must contain a structured JobPosting with employer, title and
body. Search text, page titles, and ordinary career-page prose are NOT jobs.
Career pages can contribute bounded same-site/known-ATS links for validation.
"""
from __future__ import annotations

import ipaddress
import json
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.core.config import settings
from app.services import http
from app.services.net_guard import check_url, registrable_domain
from app.services.sources.base import Posting, Source, parse_datetime, strip_html

ATS_HOSTS = ("jobs.lever.co", "boards.greenhouse.io", "job-boards.greenhouse.io",
             "jobs.ashbyhq.com", "apply.workable.com", "jobs.smartrecruiters.com",
             "myworkdayjobs.com", "recruitee.com", "jobs.personio.com", "jobs.personio.de")


def candidate_job_url(value: str) -> str | None:
    """Canonical candidate URL, not a trust decision; DNS checked at fetch time."""
    if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 33 for c in value):
        return None
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme not in {"https", "http"} or not host or parts.username or parts.password:
            return None
        if parts.port not in (None, 80, 443) or "\\" in value:
            return None
        if "." not in host or host.endswith((".local", ".internal", ".localhost")):
            return None
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                return None
        except ValueError:
            pass
        # Search must not become a back door around explicitly gated adapters.
        if any(host == h or host.endswith("." + h) for h in
               ("linkedin.com", "indeed.com", "naukri.com", "instahyre.com")):
            return None
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid", "msclkid"}]
        return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", urlencode(sorted(query)), ""))
    except ValueError:
        return None


async def fetch_page(url: str) -> tuple[str, str]:
    # No implicit redirects: every hop must pass public-only SSRF AND robots
    # checks, even if an operator enabled private HTTP access elsewhere.
    for _ in range(4):
        await check_url(url, allow_private=False, allowlist=[])
        response = await http.request("GET", url, retries=0, cache_seconds=0, allow_redirects=False,
                                      respect_robots=True, timeout=settings.job_search_timeout_seconds)
        if response.status_code in (301, 302, 303, 307, 308):
            target = candidate_job_url(urljoin(url, response.headers.get("location", "")))
            if not target:
                raise ValueError("Unsafe redirect")
            url = target
            continue
        if response.status_code != 200:
            raise ValueError("Page unavailable")
        if "html" not in response.headers.get("content-type", "").lower():
            raise ValueError("Not an HTML job page")
        if len(response.text) > 2_000_000:
            raise ValueError("Page too large")
        return url, response.text
    raise ValueError("Too many redirects")


def _objects(value, depth=0):
    if depth > 8:
        return
    if isinstance(value, list):
        for item in value[:100]:
            yield from _objects(item, depth + 1)
    elif isinstance(value, dict):
        yield value
        for key in ("@graph", "mainEntity", "itemListElement", "item"):
            if key in value:
                yield from _objects(value[key], depth + 1)


class WebPageSource(Source):
    id = "employer_web"
    kind = "board"
    label = "Validated career page"

    def normalize_page(self, url: str, html: str) -> tuple[list[Posting], list[str]]:
        soup = BeautifulSoup(html, "lxml")
        postings = []
        for script in soup.select('script[type="application/ld+json"]')[:30]:
            try:
                data = json.loads(script.string or script.get_text())
            except (ValueError, TypeError, RecursionError):
                continue
            for item in _objects(data):
                types = item.get("@type", [])
                if "JobPosting" not in (types if isinstance(types, list) else [types]):
                    continue
                org = item.get("hiringOrganization")
                if not isinstance(org, dict):
                    continue
                title, employer, body = item.get("title"), org.get("name"), item.get("description")
                if not isinstance(title, str) or not isinstance(employer, str) or not isinstance(body, str):
                    continue
                if not all(strip_html(s) for s in (title, employer, body)):
                    continue
                # A career-listing may embed many jobs. Follow their individual
                # URLs instead of assigning the listing URL to every posting.
                declared = candidate_job_url(urljoin(url, str(item.get("url") or url)))
                if declared != candidate_job_url(url):
                    continue
                expires = parse_datetime(item.get("validThrough"))
                if item.get("validThrough") and expires is None:
                    continue  # malformed expiry isn't evidence of an open job
                location = item.get("jobLocation") or {}
                if isinstance(location, list):
                    location = next(iter(location), {})
                address = location.get("address", {}) if isinstance(location, dict) else {}
                place = ", ".join(str(address.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry")
                                  if address.get(k)) if isinstance(address, dict) else ""
                remote = item.get("jobLocationType") == "TELECOMMUTE"
                posting = Posting(title=strip_html(title, 300), company=strip_html(employer, 200),
                                  description=strip_html(body, 6000), url=url, source=self.id,
                                  location=place or ("Remote" if remote else ""), remote=remote,
                                  posted_at=parse_datetime(item.get("datePosted")), expires_at=expires,
                                  fetched_at=datetime.utcnow(), source_kind=self.kind,
                                  raw={"validation": "schema.org/JobPosting", "source_url": url})
                if not posting.is_expired():
                    postings.append(self.canonicalize(posting))
        links = []
        host = urlsplit(url).hostname or ""
        candidates = [a.get("href", "") for a in soup.select("a[href]")[:500]]
        # Include URLs from structured career listings as well as anchors.
        for script in soup.select('script[type="application/ld+json"]')[:30]:
            try:
                candidates.extend(str(o.get("url") or "") for o in _objects(json.loads(script.get_text()))
                                  if o.get("@type") == "JobPosting")
            except (ValueError, TypeError, RecursionError):
                pass
        for href in candidates:
            if not isinstance(href, str):
                continue
            target = candidate_job_url(urljoin(url, href))
            if not target or target == url:
                continue
            target_host = urlsplit(target).hostname or ""
            ats = any(target_host == h or target_host.endswith("." + h) for h in ATS_HOSTS)
            same_site = registrable_domain(target_host) == registrable_domain(host)
            job_path = any(word in urlsplit(target).path.lower() for word in ("job", "career", "position", "opening"))
            if ats or (same_site and job_path):
                links.append(target)
        return postings, list(dict.fromkeys(links))[:20]
