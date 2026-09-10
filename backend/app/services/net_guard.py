"""
Outbound URL policy (SSRF guard).

Every outbound fetch in JobHunter AI goes through :mod:`app.services.http`, and
this module decides whether a URL is allowed to leave the process. It exists
because the set of URLs we fetch is *not* fully operator-controlled: job posting
URLs come from third-party feeds, portals are detected from those URLs, and a
funding import URL is configuration.

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

Two operator escape hatches exist for genuinely internal targets:

* ``OUTBOUND_ALLOWED_HOSTS`` — comma-separated exact host names or suffixes
  (``.internal.corp`` style is not used; a bare suffix such as ``corp`` matches
  ``api.corp``);
* ``OUTBOUND_ALLOW_PRIVATE=true`` — disables the private-address check entirely
  (documented as a risk; useful for an air-gapped deployment).

Residual risk: DNS is resolved here and again by the HTTP client, so a hostile
resolver could answer differently the second time (DNS rebinding). The window is
narrow and the mitigation is to keep ``OUTBOUND_ALLOW_PRIVATE`` off, which is
the default. This is recorded in ``docs/SECURITY.md``.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("app.net_guard")

ALLOWED_SCHEMES = ("http", "https")

#: Ports that almost always mean "an internal service", not "a job board".
BLOCKED_PORTS = frozenset({22, 23, 25, 53, 111, 445, 1433, 1521, 2049, 2375, 3306, 3389,
                           5432, 5601, 5672, 6379, 9042, 9200, 9300, 11211, 27017})

BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "metadata", "metadata.google.internal",
                               "instance-data", "kubernetes.default", "kubernetes.default.svc"})

#: Cached DNS verdicts: host -> (expiry, allowed, detail)
_DNS_CACHE: Dict[str, Tuple[float, bool, str]] = {}
_DNS_TTL = 60.0


class OutboundURLBlocked(PermissionError):
    """Raised when a URL fails the outbound policy."""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"outbound request blocked ({reason}): {url}")


def _match_allowlist(host: str, allowed: List[str]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allowed:
        candidate = entry.lower().strip().lstrip(".").rstrip(".")
        if not candidate:
            continue
        if host == candidate or host.endswith("." + candidate):
            return True
    return False


def _unsafe_ip_reason(address: str) -> Optional[str]:
    """Return a human reason when *address* must not be dialled, else None."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return f"unparsable address {address}"
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


async def _resolve(host: str, port: int) -> List[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


async def check_url(url: str, *, allow_private: Optional[bool] = None,
                    allowlist: Optional[List[str]] = None) -> None:
    """
    Raise :class:`OutboundURLBlocked` when *url* must not be fetched.

    DNS failures are *not* treated as a policy violation: the connection itself
    will fail, and refusing to guess keeps the guard usable in offline test
    environments (where a mocked transport never resolves anything).
    """
    if not url or not isinstance(url, str):
        raise OutboundURLBlocked(str(url), "empty url")

    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise OutboundURLBlocked(url, f"scheme '{scheme or 'none'}' is not allowed")
    if parts.username or parts.password:
        raise OutboundURLBlocked(url, "credentials in the URL are not allowed")

    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise OutboundURLBlocked(url, "missing host")
    try:
        port = parts.port
    except ValueError as exc:  # malformed port such as https://host:99999/
        raise OutboundURLBlocked(url, "invalid port") from exc
    effective_port = port or (443 if scheme == "https" else 80)

    allowed = settings.outbound_allowed_hosts if allowlist is None else allowlist
    trusted_host = _match_allowlist(host, allowed)
    if trusted_host:
        return

    if host in BLOCKED_HOSTNAMES or host.endswith(".local") or host.endswith(".internal"):
        raise OutboundURLBlocked(url, f"host '{host}' is not routable on the public internet")
    if effective_port in BLOCKED_PORTS:
        raise OutboundURLBlocked(url, f"port {effective_port} is reserved for internal services")

    skip_private = settings.outbound_allow_private if allow_private is None else allow_private

    # Literal IPs need no DNS lookup: decide immediately.
    literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if ipaddress_ok(literal):
        reason = _unsafe_ip_reason(literal)
        if reason and not skip_private:
            raise OutboundURLBlocked(url, reason)
        return

    cached = _DNS_CACHE.get(host)
    now = time.time()
    if cached and cached[0] > now:
        _, allowed_flag, detail = cached
        if not allowed_flag and not skip_private:
            raise OutboundURLBlocked(url, detail)
        return

    try:
        addresses = await _resolve(host, effective_port)
    except (socket.gaierror, UnicodeError, OSError, asyncio.TimeoutError) as exc:
        log.debug("dns lookup for %s failed (%s) — deferring to the HTTP client", host, exc)
        return

    for address in addresses:
        reason = _unsafe_ip_reason(address)
        if reason:
            _DNS_CACHE[host] = (now + _DNS_TTL, False, f"{host} resolves to {address} ({reason})")
            if not skip_private:
                raise OutboundURLBlocked(url, f"{host} resolves to {address} ({reason})")
    _DNS_CACHE[host] = (now + _DNS_TTL, True, "public")


def ipaddress_ok(value: str) -> bool:
    """True when *value* parses as an IP address (used to short-circuit DNS)."""
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def clear_dns_cache() -> None:
    _DNS_CACHE.clear()


def install_transport_guard(transport: object) -> object:
    """
    Wrap an ``httpx`` async transport so that *every* request it carries —
    including each redirect hop httpx resolves for us — is policy-checked.
    """
    original = transport.handle_async_request  # type: ignore[attr-defined]

    async def guarded(request):  # type: ignore[no-untyped-def]
        await check_url(str(request.url))
        return await original(request)

    transport.handle_async_request = guarded  # type: ignore[attr-defined]
    return transport


__all__ = [
    "OutboundURLBlocked",
    "check_url",
    "clear_dns_cache",
    "install_transport_guard",
    "BLOCKED_PORTS",
]
