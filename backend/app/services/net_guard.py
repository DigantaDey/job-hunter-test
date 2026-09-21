"""
Outbound URL policy (SSRF guard).

Every outbound request in JobHunter AI is decided here: the HTTP client
(:mod:`app.services.http`) installs this policy on its transport, and the
browser autofill worker (:mod:`app.services.autofill`) runs the same policy as
a *pre-flight* before it points Playwright at a posting URL. It exists because
the set of URLs we act on is **not** fully operator-controlled: job posting URLs
come from third-party feeds, portals are detected from those URLs, and a funding
import URL is configuration.

Policy (deny first, allow narrowly):

* scheme must be ``http`` or ``https``;
* URLs carrying userinfo (``https://user:pass@host/``) are refused — we never
  forward credentials in a URL;
* the host must not be localhost, ``*.local``, or a cloud metadata name;
* every DNS answer must be a globally routable address: loopback, private,
  link-local (including ``169.254.169.254``), carrier-grade NAT, unique-local
  IPv6, multicast and documentation ranges are refused;
* common internal service ports (SSH, SMTP, Postgres, MySQL, Redis, mongo,
  Elasticsearch, memcached) are refused unless the host is allow-listed.

Two shapes of the same policy:

* **HTTP** (``check_url`` / ``preflight``) — the default above;
* **browser** (``check_navigation_url`` / ``preflight(..., browser=True)``) —
  stricter, because a navigation is a confused-deputy risk the HTTP client does
  not have: the browser will render, follow redirects and *type vault
  credentials* into whatever answers. For navigation, loopback, link-local
  (``169.254.0.0/16`` = the cloud metadata range) and metadata hostnames are
  refused **even when the host is on the operator allow-list**, and
  ``OUTBOUND_ALLOW_PRIVATE`` is ignored entirely — a private range is reachable
  by a browser only when that exact host is allow-listed.

Reusable pre-flight: :func:`preflight` answers with a :class:`URLVerdict`
instead of raising, so a caller can decline *before* doing expensive work
(launching a browser) and report why. The raising wrappers are one-liners over
it and share every rule.

Two operator escape hatches exist for genuinely internal targets:

* ``OUTBOUND_ALLOWED_HOSTS`` — comma-separated exact host names or suffixes
  (``.internal.corp`` style is not used; a bare suffix such as ``corp`` matches
  ``api.corp``);
* ``OUTBOUND_ALLOW_PRIVATE=true`` — disables the private-address check for the
  *HTTP* client only (documented as a risk; useful for an air-gapped
  deployment). It has no effect on browser navigation.

Residual risk: DNS is resolved here and again by the HTTP client, so a hostile
resolver could answer differently the second time (DNS rebinding). The window is
narrow and the mitigation is to keep ``OUTBOUND_ALLOW_PRIVATE`` off, which is
the default. This is recorded in ``docs/SECURITY.md``.

The DNS verdict cache is a bounded LRU (:class:`app.core.lru.BoundedTTLMap`): a
sweep over thousands of distinct hosts cannot grow it past
``DNS_CACHE_MAX_ENTRIES``.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.logging import get_logger

if TYPE_CHECKING:  # only the transport-guard signature needs it
    import httpx
from app.core.lru import BoundedTTLMap

log = get_logger("app.net_guard")

ALLOWED_SCHEMES = ("http", "https")

#: Ports that almost always mean "an internal service", not "a job board".
BLOCKED_PORTS = frozenset({22, 23, 25, 53, 111, 445, 1433, 1521, 2049, 2375, 3306, 3389,
                           5432, 5601, 5672, 6379, 9042, 9200, 9300, 11211, 27017})

BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "metadata", "metadata.google.internal",
                               "instance-data", "kubernetes.default", "kubernetes.default.svc"})

#: Names a *browser* is never pointed at — allow-list or not. These are the
#: cloud metadata services; nothing legitimate is ever served from them.
METADATA_HOSTNAMES = frozenset({"metadata", "metadata.google.internal", "instance-data"})

#: The AWS/GCP/Azure instance-metadata range, spelled out because it is the
#: single most valuable target on a cloud host.
LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")

#: RFC 6052 well-known NAT64 prefix. An IPv6-only deployment's DNS64 resolver
#: answers every IPv4-only hostname with an AAAA of exactly this shape — the
#: synthesised record is ``64:ff9b::`` followed by the four bytes of the public
#: IPv4 address. The prefix is IANA-reserved, but refusing it refuses the whole
#: IPv4 internet (which is every job board), so we classify the *embedded*
#: address instead. Network-specific NAT64 prefixes (RFC 6052 §3.3) are
#: indistinguishable from ordinary global addresses and cannot be detected here.
NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")

_DNS_TTL = 60.0

#: Cached DNS verdicts: host -> (allowed, detail, addresses). Bounded LRU + TTL,
#: so a worker sweeping thousands of distinct hosts cannot grow it without limit.
_DNS_CACHE: BoundedTTLMap = BoundedTTLMap(
    name="net_guard.dns",
    max_entries=settings.dns_cache_max_entries,
    default_ttl=_DNS_TTL,
)


class OutboundURLBlocked(PermissionError):
    """Raised when a URL fails the outbound policy."""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"outbound request blocked ({reason}): {url}")


@dataclass(frozen=True)
class URLVerdict:
    """
    The answer to "may this process act on *url*?".

    ``code`` is a coarse, metric-safe category (``scheme``, ``unsafe_address``,
    …); ``reason`` is the human explanation. ``addresses`` records what DNS
    actually answered, which is what makes a blocked posting URL diagnosable.
    """

    url: str
    allowed: bool
    code: str = ""
    reason: str = ""
    scheme: str = ""
    host: str = ""
    port: int = 0
    addresses: Tuple[str, ...] = field(default_factory=tuple)
    allowlisted: bool = False
    dns_checked: bool = False
    #: True when the stricter browser-navigation policy produced this verdict.
    navigation: bool = False

    def raise_if_blocked(self) -> "URLVerdict":
        if not self.allowed:
            raise OutboundURLBlocked(self.url, self.reason)
        return self


def host_matches(host: str, domain: str) -> bool:
    """True when *host* is *domain* or a subdomain of it (case-insensitive)."""
    host = (host or "").strip().lower().rstrip(".")
    candidate = (domain or "").strip().lower().lstrip(".").rstrip(".")
    if not host or not candidate:
        return False
    return host == candidate or host.endswith("." + candidate)


def _match_allowlist(host: str, allowed: Sequence[str]) -> bool:
    for entry in allowed or ():
        if host_matches(host, entry):
            return True
    return False


def _unsafe_ip_reason(address: str) -> Optional[str]:
    """Return a human reason when *address* must not be dialled, else None."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return f"unparsable address {address}"
    if isinstance(ip, ipaddress.IPv6Address):
        # Translation envelopes (::ffff:0:0/96 IPv4-mapped, 64:ff9b::/96
        # NAT64/DNS64) are IANA-reserved prefixes that merely *carry* an IPv4
        # destination. Classify the destination, not the envelope: on an
        # IPv6-only deployment the DNS64 resolver answers every IPv4-only
        # job-board host with a 64:ff9b::… AAAA record, and refusing the
        # prefix refuses the entire IPv4 internet. A wrapped loopback/private
        # address is still refused, because the embedded IPv4 is what gets
        # classified.
        embedded: Optional[ipaddress.IPv4Address] = ip.ipv4_mapped
        if embedded is None and ip in NAT64_WELL_KNOWN_PREFIX:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            reason = _unsafe_ip_reason(str(embedded))
            if reason is None:
                return None
            return f"{reason} embedded in translated address {ip}"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address (cloud metadata range)"
    if ip.is_private:
        return "private address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_reserved or ip.is_unspecified:
        return "reserved address"
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return "carrier-grade NAT address"
    if isinstance(ip, ipaddress.IPv6Address) and ip in ipaddress.ip_network("fc00::/7"):
        return "unique-local IPv6 address"
    return None


def _browser_hard_block(host: str, literal: str) -> Optional[Tuple[str, str]]:
    """
    Absolute refusals for browser navigation — the operator allow-list cannot
    re-permit them, because the browser is the one confused deputy that will
    happily render an internal service and be fed credentials.

    Returns ``(code, reason)`` or None.
    """
    if host in METADATA_HOSTNAMES or host.endswith(".local"):
        return "metadata_host", f"host '{host}' is a cloud-metadata or .local name"
    if ipaddress_ok(literal):
        ip = ipaddress.ip_address(literal)
        if ip.is_loopback:
            return "loopback", "loopback address (a browser must never visit this host)"
        if ip.is_link_local or (isinstance(ip, ipaddress.IPv4Address) and ip in LINK_LOCAL_V4):
            return "link_local", "link-local address (cloud metadata range 169.254.0.0/16)"
    return None


#: Second-level parts of the common multi-part public suffixes. Used only by
#: :func:`registrable_domain` — we ship no public-suffix list and do not want
#: the dependency for a "did this redirect stay inside one organisation?" check.
MULTI_PART_SUFFIXES = frozenset({"co", "com", "org", "net", "edu", "gov", "ac", "ne", "or",
                                 "go", "ltd", "plc", "ad", "asn", "id", "mil"})


def registrable_domain(host: str) -> str:
    """
    The organisation-level domain of *host* (``careers.jobs.lever.co`` →
    ``lever.co``, ``boards.greenhouse.co.uk`` → ``greenhouse.co.uk``).

    A heuristic (two labels, three when the second-to-last is a known
    second-level suffix): enough to tell "the ATS redirected inside its own
    domain" from "the posting sent the browser somewhere else", and it errs
    toward reporting *different* domains, never toward treating two
    organisations as one.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host or ipaddress_ok(host):
        return host
    labels = host.split(".")
    if len(labels) < 2:
        return host
    depth = 3 if labels[-2] in MULTI_PART_SUFFIXES and len(labels) >= 3 else 2
    return ".".join(labels[-depth:])


def same_registrable_domain(host_a: str, host_b: str) -> bool:
    """True when both hosts belong to the same organisation-level domain."""
    a, b = registrable_domain(host_a), registrable_domain(host_b)
    return bool(a) and a == b


async def _resolve(host: str, port: int) -> List[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


async def preflight(
    url: str,
    *,
    allow_private: Optional[bool] = None,
    allowlist: Optional[Sequence[str]] = None,
    browser: bool = False,
    resolve: bool = True,
) -> URLVerdict:
    """
    Decide whether *url* may be fetched (``browser=False``) or navigated to by
    the autofill browser (``browser=True``) **without raising**.

    Callers that want an exception use :func:`check_url` /
    :func:`check_navigation_url`, which are thin wrappers over this function and
    therefore cannot drift from it.

    DNS failures are *not* treated as a policy violation: the connection itself
    will fail, and refusing to guess keeps the guard usable in offline test
    environments (where a mocked transport never resolves anything).
    """
    if not url or not isinstance(url, str):
        return URLVerdict(url=str(url), allowed=False, code="empty_url", reason="empty url",
                          navigation=browser)

    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").strip().lower().rstrip(".")
    try:
        port = parts.port
    except ValueError:  # malformed port such as https://host:99999/
        return URLVerdict(url=url, allowed=False, code="invalid_port", reason="invalid port",
                          scheme=scheme, host=host, navigation=browser)
    effective_port = port or (443 if scheme == "https" else 80)

    def deny(code: str, reason: str, *, addresses: Tuple[str, ...] = (),
             allowlisted: bool = False, dns_checked: bool = False) -> URLVerdict:
        return URLVerdict(url=url, allowed=False, code=code, reason=reason, scheme=scheme,
                          host=host, port=effective_port, addresses=addresses,
                          allowlisted=allowlisted, dns_checked=dns_checked, navigation=browser)

    def allow(code: str = "ok", reason: str = "", *, addresses: Tuple[str, ...] = (),
              allowlisted: bool = False, dns_checked: bool = False) -> URLVerdict:
        return URLVerdict(url=url, allowed=True, code=code, reason=reason, scheme=scheme,
                          host=host, port=effective_port, addresses=addresses,
                          allowlisted=allowlisted, dns_checked=dns_checked, navigation=browser)

    if scheme not in ALLOWED_SCHEMES:
        return deny("scheme", f"scheme '{scheme or 'none'}' is not allowed")
    if parts.username or parts.password:
        return deny("credentials_in_url", "credentials in the URL are not allowed")
    if not host:
        return deny("missing_host", "missing host")

    literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    hosts = settings.outbound_allowed_hosts if allowlist is None else list(allowlist or [])

    if browser:
        # Absolute refusals first: an allow-listed metadata host is still a
        # metadata host, and ``OUTBOUND_ALLOW_PRIVATE`` does not apply to a
        # browser at all.
        hard = _browser_hard_block(host, literal)
        if hard:
            return deny(hard[0], hard[1])
        trusted = _match_allowlist(host, hosts)
        skip_private = False
    else:
        trusted = _match_allowlist(host, hosts)
        skip_private = settings.outbound_allow_private if allow_private is None else allow_private

    if trusted:
        return allow("allowlisted", "host is on OUTBOUND_ALLOWED_HOSTS", allowlisted=True)

    if host in BLOCKED_HOSTNAMES or host.endswith(".local") or host.endswith(".internal"):
        return deny("unroutable_host", f"host '{host}' is not routable on the public internet")
    if effective_port in BLOCKED_PORTS:
        return deny("internal_port", f"port {effective_port} is reserved for internal services")

    # Literal IPs need no DNS lookup: decide immediately.
    if ipaddress_ok(literal):
        reason = _unsafe_ip_reason(literal)
        if reason and not skip_private:
            return deny("unsafe_address", reason, addresses=(literal,))
        return allow("literal_ip", addresses=(literal,))

    cached = _DNS_CACHE.peek(host)
    if cached is not None:
        was_allowed, detail, addresses = cached
        if not was_allowed and not skip_private:
            return deny("unsafe_address", detail, addresses=addresses, dns_checked=True)
        return allow("dns_cached", addresses=addresses, dns_checked=True)

    if not resolve:
        return allow("dns_skipped")

    try:
        addresses_tuple = tuple(await _resolve(host, effective_port))
    except (socket.gaierror, UnicodeError, OSError, asyncio.TimeoutError) as exc:
        log.debug("dns lookup for %s failed (%s) — deferring to the HTTP client", host, exc)
        return allow("dns_unavailable", reason=str(exc))

    for address in addresses_tuple:
        reason = _unsafe_ip_reason(address)
        if reason:
            detail = f"{host} resolves to {address} ({reason})"
            _DNS_CACHE.put(host, (False, detail, addresses_tuple))
            if not skip_private:
                return deny("unsafe_address", detail, addresses=addresses_tuple, dns_checked=True)
    _DNS_CACHE.put(host, (True, "public", addresses_tuple))
    return allow("dns_public", addresses=addresses_tuple, dns_checked=True)


async def check_url(url: str, *, allow_private: Optional[bool] = None,
                    allowlist: Optional[Sequence[str]] = None) -> None:
    """Raise :class:`OutboundURLBlocked` when *url* must not be fetched."""
    verdict = await preflight(url, allow_private=allow_private, allowlist=allowlist)
    verdict.raise_if_blocked()


async def preflight_navigation(url: str, *, allowlist: Optional[Sequence[str]] = None,
                               resolve: bool = True) -> URLVerdict:
    """
    Browser-navigation pre-flight: the deny-first policy, with loopback,
    link-local/metadata and non-allow-listed private ranges refused absolutely.

    Run this *before* launching a browser or calling ``page.goto`` — it is the
    only thing standing between a feed-supplied posting URL and the worker's
    headless Chromium (which will otherwise fetch cloud credentials, probe
    ``127.0.0.1:<port>`` and be fed the user's vault password by
    ``_attempt_login``).
    """
    return await preflight(url, allowlist=allowlist, browser=True, resolve=resolve)


async def check_navigation_url(url: str, *, allowlist: Optional[Sequence[str]] = None) -> URLVerdict:
    """Raise :class:`OutboundURLBlocked` when a browser must not navigate to *url*."""
    verdict = await preflight_navigation(url, allowlist=allowlist)
    return verdict.raise_if_blocked()


def ipaddress_ok(value: str) -> bool:
    """True when *value* parses as an IP address (used to short-circuit DNS)."""
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def dns_cache_stats() -> dict:
    """Bounded-cache counters for ``/api/ops/status`` (never the verdicts)."""
    return {**_DNS_CACHE.stats(), "ttl_seconds": _DNS_TTL}


def clear_dns_cache() -> None:
    _DNS_CACHE.clear()


def install_transport_guard(transport: "httpx.AsyncBaseTransport") -> "httpx.AsyncBaseTransport":
    """
    Wrap an ``httpx`` async transport so that *every* request it carries —
    including each redirect hop httpx resolves for us — is policy-checked.
    """
    original = transport.handle_async_request

    async def guarded(request: "httpx.Request") -> "httpx.Response":
        await check_url(str(request.url))
        return await original(request)

    # Instance-level override of an httpx method: the guard must see every hop,
    # including redirects httpx resolves inside this transport.
    wrapper: Any = transport
    wrapper.handle_async_request = guarded
    return transport


__all__ = [
    "OutboundURLBlocked",
    "URLVerdict",
    "preflight",
    "preflight_navigation",
    "check_url",
    "check_navigation_url",
    "clear_dns_cache",
    "dns_cache_stats",
    "host_matches",
    "registrable_domain",
    "same_registrable_domain",
    "install_transport_guard",
    "BLOCKED_PORTS",
    "METADATA_HOSTNAMES",
]
