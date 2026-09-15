"""
HTTP edge middleware: request ids, access logs, security headers, client-IP
resolution, the request-body cap and the rate limiter.

Ordering matters. In ``main.py`` the middlewares are added so that requests pass
through ``RequestContext`` first (so every later log line has the id) and the
``BodySizeLimit`` fast-rejects oversized payloads before any router code runs.

This module *is* the edge: it runs before authentication, before routing and
before any body parser, so every value it touches is attacker-controlled. Three
rules follow from that, and each has its own section below.

1. **Nothing keyed on request data may be unbounded.** The rate limiter's hit log
   is an LRU with a hard cap (``RATE_LIMIT_MAX_KEYS``) and its keys are HMAC
   fingerprints — of a *verified* user id, of an API key, or of the resolved
   client IP — never a slice of a raw header (``auth:<last 24 characters>`` was
   a memory-DoS primitive: unique random values minted a dict entry each, and
   pruning only happened when the *same* value came back). Metric label values
   are route templates, with a small admission list for unmatched paths.
2. **The body cap is enforced on the wire, not on the header.** ``Content-Length``
   is checked when present (a cheap fast-reject) *and* the bytes are counted as
   they arrive, so ``Transfer-Encoding: chunked`` — which carries no
   ``Content-Length`` at all — cannot stream an unbounded body past the limit.
3. **A forwarded address is believed only from a configured proxy.** The client IP
   is the rightmost ``X-Forwarded-For`` hop that is not itself a trusted proxy,
   and the walk only happens when the peer that handed us the request *is* one;
   otherwise the peer address is used and the header is ignored (and counted, so
   the misconfiguration is visible rather than silent). See ``TRUSTED_PROXIES``
   / ``FORWARDED_ALLOW_IPS`` in ``docs/DEPLOYMENT.md`` §3.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import time
import uuid
from collections import OrderedDict, deque
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import metrics
from app.core.config import settings
from app.core.logging import get_logger, request_id_var
from app.core.security import decode_token

logger = get_logger("http")


# --------------------------------------------------------------------------- #
# Client IP resolution — the trust boundary
# --------------------------------------------------------------------------- #
def _peer_host(scope: Mapping[str, Any]) -> str:
    """The address of the socket that handed us this request (``""`` if none)."""
    client = scope.get("client")
    return (client[0] if client else "") or ""


def _header_values(scope: Mapping[str, Any], name: str) -> List[str]:
    """Every value of a (possibly repeated) header, in the order it arrived."""
    wanted = name.lower().encode("latin-1")
    return [value.decode("latin-1") for key, value in (scope.get("headers") or []) if key == wanted]


def _parse_forwarded_host(value: str) -> str:
    """Strip the optional port from one ``X-Forwarded-For`` entry.

    Proxies disagree about the format: bare ``1.2.3.4``, IPv4 ``1.2.3.4:5678``
    and bracketed IPv6 ``[2001:db8::1]:443`` all show up in the wild. Anything
    unrecognisable is returned as-is so a trust check never silently normalises
    attacker input into something that matches.
    """
    text = (value or "").strip()
    if not text:
        return ""
    if text.startswith("["):
        end = text.find("]")
        return text[1:end] if end != -1 else text.strip("[]")
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        return host if port.isdigit() else text
    return text


def _forwarded_hops(scope: Mapping[str, Any]) -> List[str]:
    """The ``X-Forwarded-For`` chain as a list of addresses, left → right."""
    hops: List[str] = []
    for header in _header_values(scope, "x-forwarded-for"):
        hops.extend(_parse_forwarded_host(part) for part in header.split(","))
    return [hop for hop in hops if hop]


#: Parsed ``TRUSTED_PROXIES`` per configured value (settings are re-read, and
#: tests monkeypatch them, so this is keyed on the tuple rather than cached once).
_network_cache: Dict[Tuple[str, ...], Tuple[Any, ...]] = {}


def _trusted_networks() -> Tuple[Any, ...]:
    entries = tuple(str(entry).strip() for entry in (settings.trusted_proxies or ()) if str(entry).strip())
    cached = _network_cache.get(entries)
    if cached is None:
        parsed: List[Any] = []
        for entry in entries:
            try:
                # ``ip_network`` accepts a bare address too (as a /32 or /128),
                # so membership testing below has one shape to deal with.
                parsed.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                logger.warning("TRUSTED_PROXIES entry %r is not an IP address or CIDR — ignored", entry)
        cached = tuple(parsed)
        if len(_network_cache) > 8:  # never let a settings-reload loop grow this
            _network_cache.clear()
        _network_cache[entries] = cached
    return cached


def is_trusted_proxy(candidate: str) -> bool:
    """True when *candidate* is an address we accept ``X-Forwarded-For`` from.

    With nothing configured the answer is always False: the peer address is then
    the only attribution we have, which is the correct fail-closed behaviour for
    a deployment with no reverse proxy in front of it.
    """
    if not candidate:
        return False
    networks = _trusted_networks()
    if not networks:
        return False
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False  # a unix-socket literal or junk can never be a configured proxy
    for network in networks:
        try:
            if address in network:
                return True
        except TypeError:
            continue  # IPv4 peer vs IPv6 entry (or the reverse)
    return False


_last_forwarding_warning = 0.0


def _note_forwarding(reason: str, peer: str, hops: Sequence[str]) -> None:
    """Count every forwarded-for header we saw, and warn about the ones we refused."""
    global _last_forwarding_warning
    metrics.counter("jobhunter_edge_forwarded_for_total", {"reason": reason})
    if reason != "untrusted_peer":
        return
    now = time.monotonic()
    if now - _last_forwarding_warning < 60.0:
        return
    _last_forwarding_warning = now
    logger.warning(
        "ignoring X-Forwarded-For (%s) from untrusted peer %s — if that peer is your reverse proxy, list it "
        "in TRUSTED_PROXIES (and/or FORWARDED_ALLOW_IPS for uvicorn's own resolver), see "
        "docs/DEPLOYMENT.md §3; if it is not, this is someone trying to spoof their client IP",
        ",".join(hops[:4])[:120], peer or "unknown",
    )


#: Resolved once per request and memoised on the scope: the limiter (outer),
#: ``deps.client_ip()`` and the audit trail all ask for the same answer, and
#: asking twice would both re-parse the header and double-count the metric.
_SCOPE_CLIENT_IP_KEY = "jobhunter_client_ip"


def resolve_client_ip(scope: Mapping[str, Any]) -> str:
    """
    The address this request is attributed to (rate limits, lockouts, audit rows).

    ``X-Forwarded-For`` is a *claim* by whoever handed us the request, and each
    proxy appends the address it saw — so the only entry that a client cannot
    write is the **rightmost hop that is not itself a trusted proxy**. Everything
    to its left came from that hop or from the client and may say anything.

    The walk happens only when the peer is a configured proxy
    (:data:`settings.trusted_proxies`). Otherwise the header is attacker input
    and the peer address wins. Two supported shapes:

    * **uvicorn resolves it** (``--proxy-headers --forwarded-allow-ips=<proxy>``):
      ``scope["client"]`` already holds the resolved client, which then appears in
      the forwarded chain — recognised as ``resolved_upstream`` and returned
      unchanged;
    * **the app resolves it** (``TRUSTED_PROXIES`` set): the rightmost untrusted
      hop is returned.

    With no proxy configured at all there is no header to consider and the peer
    (``request.client.host``) is used, which is the sane answer for a bare
    ``uvicorn``/``docker run -p`` deployment.
    """
    memoised = scope.get(_SCOPE_CLIENT_IP_KEY)
    if isinstance(memoised, str) and memoised:
        return memoised

    peer = _peer_host(scope)
    hops = _forwarded_hops(scope)
    if not hops:
        resolved = peer
    elif not is_trusted_proxy(peer):
        # A peer that is already the client (uvicorn resolved the chain for us)
        # is normal and needs no warning; anything else is a spoofing attempt or
        # a proxy nobody told us about.
        _note_forwarding("resolved_upstream" if peer in hops else "untrusted_peer", peer, hops)
        resolved = peer
    else:
        resolved = hops[0]  # every hop claims to be a proxy: the client is inside the trusted net
        for candidate in reversed(hops):
            if not is_trusted_proxy(candidate):
                _note_forwarding("rightmost_untrusted_hop", peer, hops)
                resolved = candidate
                break
        else:
            _note_forwarding("all_hops_trusted", peer, hops)

    try:  # memoise for the rest of the request (the scope is a plain mutable dict)
        scope[_SCOPE_CLIENT_IP_KEY] = resolved  # type: ignore[index]
    except TypeError:  # pragma: no cover - an immutable mapping still resolves
        pass
    return resolved


def client_ip(scope_or_request: Any) -> str:
    """Resolve the client IP from a Starlette ``Request`` or a raw ASGI scope."""
    scope = getattr(scope_or_request, "scope", scope_or_request)
    return resolve_client_ip(scope if isinstance(scope, Mapping) else dict(scope or {}))


# --------------------------------------------------------------------------- #
# Bounded metric labels
# --------------------------------------------------------------------------- #
#: A label value is a permanent series in the in-process registry, so the set of
#: values must be bounded by *us*, not by the caller. Matched requests are
#: labelled with their route template (bounded by the route table); unmatched
#: ones — a scanner walking random URLs, a rejected ``Host`` — have no template,
#: so the first ``_MAX_UNMATCHED_PATH_LABELS`` collapsed shapes keep their own
#: series and everything after that is counted as ``other``.
_MAX_UNMATCHED_PATH_LABELS = 64
_UNMATCHED_PATH_LABEL = "other"
_path_labels: "OrderedDict[str, str]" = OrderedDict()


def _fallback_path(path: str) -> str:
    """Collapse ids so metric cardinality stays bounded (``/api/jobs/12`` -> ``/api/jobs/{id}``)."""
    parts = path.split("/")
    collapsed = []
    for index, part in enumerate(parts):
        if index > 2 and (part.isdigit() or len(part) > 24):
            collapsed.append("{id}")
        else:
            collapsed.append(part)
    return "/".join(collapsed)


def _bounded_path_label(collapsed: str) -> str:
    """First-seen-wins admission list, so unmatched paths cannot grow the registry."""
    if collapsed in _path_labels:
        return collapsed
    if len(_path_labels) >= _MAX_UNMATCHED_PATH_LABELS:
        return _UNMATCHED_PATH_LABEL
    _path_labels[collapsed] = collapsed
    return collapsed


def _route_template(raw_path: str, template: str) -> str:
    """
    The route's template as a *full* path.

    FastAPI keeps a sub-router's ``route.path`` relative to the prefix it was
    included with (the ops router's health route is ``/health``, served at
    ``/api/health``), so the prefix the request actually carried is restored by
    locating the template's static head inside the matched path. Bounded either
    way — this is label cosmetics, not a security boundary.
    """
    head = template.split("{", 1)[0]
    if not head or head == "/":
        return template
    index = raw_path.find(head)
    return template if index <= 0 else raw_path[:index] + template


def path_label(scope: Mapping[str, Any], raw_path: str) -> str:
    """
    The ``path`` label for this request: its route template when it matched one.

    Called *after* the downstream app ran: ``scope["route"]`` is only populated
    once the router has matched, so reading it beforehand (as this middleware
    used to) silently labelled every request with a collapsed raw path instead —
    unbounded cardinality from any client that invents URLs. Unmatched requests
    go through the bounded admission list in :func:`_bounded_path_label`.
    """
    template = getattr(scope.get("route"), "path", None)
    if template:
        return _route_template(raw_path, str(template))
    return _bounded_path_label(_fallback_path(raw_path))


# --------------------------------------------------------------------------- #
# Request context / access log
# --------------------------------------------------------------------------- #
class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach an id to every request, log it, and count it."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming[:64] if incoming else uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        request.state.request_id = request_id

        metrics.gauge("jobhunter_http_requests_in_progress", 1)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            # Echo the id so clients (and support) can correlate a report with logs.
            response.headers.setdefault("X-Request-ID", request_id)
            return response
        finally:
            duration = time.perf_counter() - started
            # Resolved after the fact: only then does the scope know which route
            # matched, and the route template is the bounded label we want.
            label = path_label(request.scope, request.url.path)
            metrics.gauge("jobhunter_http_requests_in_progress", 0)
            metrics.counter("jobhunter_http_requests_total", {
                "method": request.method, "path": label, "status": str(status_code),
            })
            metrics.observe("jobhunter_http_request_duration_seconds", duration,
                            {"method": request.method, "path": label})
            extra = {"method": request.method, "path": request.url.path, "status": status_code,
                     "duration_ms": round(duration * 1000, 1), "request_id": request_id}
            user_id = getattr(request.state, "user_id", None)
            if user_id is not None:
                extra["user_id"] = user_id
            logger.log(30 if status_code < 400 else 40, "request", extra=extra)
            request_id_var.reset(token)

    @staticmethod
    def _set_response_header(request: Request, response: Response) -> Response:  # pragma: no cover
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Conservative defaults that don't break the SPA."""

    CSP = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )

    def __init__(self, app, *, enable_hsts: bool = False, csp: bool = True) -> None:
        super().__init__(app)
        self.enable_hsts = enable_hsts
        self.csp = csp

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if self.csp:
            response.headers.setdefault("Content-Security-Policy", self.CSP)
        if self.enable_hsts:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


# --------------------------------------------------------------------------- #
# Body size cap
# --------------------------------------------------------------------------- #
class _BodyTooLarge(Exception):
    """Internal unwind signal — never escapes :class:`BodySizeLimitMiddleware`."""


def _payload_too_large_body(max_bytes: int) -> bytes:
    return json.dumps({
        "detail": f"Request body too large (limit {max_bytes // (1024 * 1024)} MB).",
        "code": "payload_too_large",
        "request_id": request_id_var.get() or "",
    }).encode("utf-8")


def _declared_content_length(scope: Mapping[str, Any]) -> Optional[int]:
    """Largest declared ``Content-Length``, or None when the request has no length.

    A repeated header is a smuggling smell; h11 rejects the ambiguous cases
    before we see them, and taking the maximum of what is left fails closed.
    """
    declared: List[int] = []
    for value in _header_values(scope, "content-length"):
        for candidate in value.split(","):
            candidate = candidate.strip()
            if candidate.isdigit():
                declared.append(int(candidate))
    return max(declared) if declared else None


class BodySizeLimitMiddleware:
    """
    Reject oversized bodies before they reach a memory-hungry parser.

    Pure ASGI rather than ``BaseHTTPMiddleware``: the cap has to be enforced on
    the *receive channel*, and this is the one place that can wrap it without
    another task hop. Checking ``Content-Length`` alone is not a limit — a
    ``Transfer-Encoding: chunked`` request has no ``Content-Length``, so it used
    to stream straight past this middleware and be buffered whole by whatever
    read it. Both checks are now in force:

    * the declared length (when there is one) fast-rejects before the app runs;
    * the bytes are counted as they arrive, and the first chunk that crosses the
      cap answers ``413`` immediately and unwinds the request.

    The 413 is written on the *outer* send channel and every later response from
    the downstream app is dropped, so a handler that swallows the unwind (or one
    that had already started answering) cannot produce a second response.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)
        self.rejected_by_header = 0
        self.rejected_by_stream = 0
        _body_limiters.append(self)
        if len(_body_limiters) > 8:
            del _body_limiters[:-8]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _declared_content_length(scope)
        if declared is not None and declared > self.max_bytes:
            self.rejected_by_header += 1
            metrics.counter("jobhunter_http_payload_too_large_total", {"reason": "content_length"})
            await self._reject(send)
            return

        seen = 0
        rejected = False

        async def counted_receive() -> Message:
            nonlocal seen, rejected
            message = await receive()
            if message["type"] != "http.request":
                return message
            seen += len(message.get("body") or b"")
            if seen > self.max_bytes:
                if not rejected:
                    rejected = True
                    self.rejected_by_stream += 1
                    metrics.counter("jobhunter_http_payload_too_large_total", {"reason": "streamed"})
                    logger.warning("request body exceeded %s bytes after %s streamed bytes (%s %s)",
                                   self.max_bytes, seen, scope.get("method"), scope.get("path"))
                    await self._reject(send)
                # Raised on *every* later receive too: a handler that swallows the
                # first abort must not be able to keep buffering the rest.
                raise _BodyTooLarge()
            return message

        async def guarded_send(message: Message) -> None:
            # After our 413 nothing else may be written for this request: the
            # downstream app is still unwinding and will try to answer.
            if rejected and message["type"] in ("http.response.start", "http.response.body"):
                return
            await send(message)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except _BodyTooLarge:
            pass  # answered above
        except Exception as exc:  # noqa: BLE001 - the 413 is the answer either way
            if not rejected:
                raise
            logger.debug("body-limit abort unwound as %s after the 413 was sent", type(exc).__name__)

    async def _reject(self, send: Send) -> None:
        payload = _payload_too_large_body(self.max_bytes)
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("latin-1")),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": payload, "more_body": False})


_body_limiters: List["BodySizeLimitMiddleware"] = []


def body_limit_state() -> Dict[str, Any]:
    """Live body-cap counters for ``GET /api/ops/status``."""
    state: Dict[str, Any] = {"instances": len(_body_limiters), "max_bytes": 0,
                             "rejected_by_header": 0, "rejected_by_stream": 0}
    for limiter in _body_limiters:
        state["max_bytes"] = max(int(state["max_bytes"]), limiter.max_bytes)
        state["rejected_by_header"] += limiter.rejected_by_header
        state["rejected_by_stream"] += limiter.rejected_by_stream
    return state


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
def _fingerprint(value: str) -> str:
    """
    Keyed hash of a limiter identity.

    The limiter's keys are where a user id or a credential becomes a dict key,
    and that dict is exactly what a heap dump or a debug log would expose. HMAC
    with the app secret (truncated) means the stored key is not a credential and
    not an enumerable user id — while two requests from the same identity still
    land in the same bucket.
    """
    secret = (settings.secret_key or "").encode("utf-8")
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _verified_user_id(credential: str) -> Optional[int]:
    """The user id of an access token, verified by signature only (no DB hit).

    Anything that does not verify — forged, expired, wrong ``typ``, or simply
    random bytes — is ``None``, which makes the request *anonymous*: it shares
    the client-IP bucket instead of minting a bucket of its own.
    """
    try:
        payload = decode_token(credential)
    except Exception:  # noqa: BLE001 - garbage in, anonymous out
        return None
    if not isinstance(payload, dict) or payload.get("typ") != "access":
        return None
    try:
        user_id = int(payload.get("sub") or 0)
    except (TypeError, ValueError):
        return None
    return user_id or None


class SlidingWindowLog:
    """
    Bounded sliding-window hit log: ``key -> timestamps``, LRU-capped.

    Deliberately not :class:`app.core.lru.BoundedTTLMap`: this runs on the hot
    path of every request and its eviction candidate is found by scanning for
    expired entries, which under precisely the attack this class survives (a
    flood of distinct keys, nothing expired yet) is per-request work
    proportional to the cap. ``OrderedDict.popitem(last=False)`` is O(1), and the
    periodic sweep can stop at the first live bucket because recency order puts
    the oldest keys first.

    Bounded twice over: a hard key cap (so a flood cannot grow the dict) and a
    sweep (so idle keys are reclaimed instead of squatting until they are
    evicted).
    """

    def __init__(self, *, max_keys: int, window_seconds: float,
                 sweep_interval_seconds: float = 60.0, clock: Any = time.monotonic) -> None:
        self.max_keys = max(16, int(max_keys))
        self.window = max(1.0, float(window_seconds))
        self.sweep_interval = max(1.0, float(sweep_interval_seconds))
        self._clock = clock
        self._hits: "OrderedDict[str, Deque[float]]" = OrderedDict()
        self._last_sweep = float(clock())
        self.decisions = 0
        self.denials = 0
        self.evictions = 0
        self.expired_keys = 0
        self.sweeps = 0

    def allow(self, key: str, budget: int, *, now: Optional[float] = None) -> Tuple[bool, int]:
        """Record a hit for *key*; return ``(allowed, retry_after_seconds)``."""
        moment = self._clock() if now is None else float(now)
        self.decisions += 1
        self._sweep(moment)
        bucket = self._hits.get(key)
        if bucket is None:
            bucket = deque()
            self._hits[key] = bucket
            self._evict_over_cap()
        else:
            self._hits.move_to_end(key)
        while bucket and moment - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= max(1, int(budget)):
            self.denials += 1
            # The oldest hit in the window decides when a slot frees up. A
            # denied request is not recorded, so hammering the limit does not
            # keep extending its own window.
            return False, max(1, int(math.ceil(self.window - (moment - bucket[0]))))
        bucket.append(moment)
        return True, 0

    def _evict_over_cap(self) -> None:
        while len(self._hits) > self.max_keys:
            self._hits.popitem(last=False)
            self.evictions += 1

    def _sweep(self, now: float) -> None:
        """Drop keys whose window has fully elapsed (oldest first, bounded work)."""
        if now - self._last_sweep < self.sweep_interval:
            return
        self._last_sweep = now
        self.sweeps += 1
        for key in list(self._hits)[:1024]:
            bucket = self._hits.get(key)
            if bucket is None:
                continue
            if bucket and now - bucket[-1] <= self.window:
                break  # recency order: everything behind this key is live too
            self._hits.pop(key, None)
            self.expired_keys += 1

    def clear(self) -> None:
        self._hits.clear()

    def keys(self) -> List[str]:
        """The tracked keys, least → most recently used (diagnostics/tests only)."""
        return list(self._hits)

    def __len__(self) -> int:
        return len(self._hits)

    def stats(self) -> Dict[str, Any]:
        """Counters for ops/monitoring — never the keys themselves."""
        return {
            "keys": len(self._hits),
            "max_keys": self.max_keys,
            "window_seconds": self.window,
            "decisions": self.decisions,
            "denials": self.denials,
            "evictions": self.evictions,
            "expired_keys": self.expired_keys,
            "sweeps": self.sweeps,
        }


#: Every limiter this process has built (normally exactly one — the app's). Kept
#: so ``GET /api/ops/status`` can report the live instance and tests can reset it
#: without reaching into the middleware stack.
_limiters: List[SlidingWindowLog] = []


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window limiter keyed by *verified* identity, else by client IP.

    The key set is the attack surface here, so it is chosen to stay small no
    matter what the request claims:

    * ``Authorization: Bearer <jwt>`` that **verifies** → ``u:<hmac(user id)>`` —
      one budget per user, wherever they connect from;
    * a ``jh_``-shaped API key (``Authorization`` or ``X-API-Key``) →
      ``k:<hmac(key)>`` **and** the client-IP bucket. The middleware will not
      spend a database query per request to verify a key, so the credential is
      unproven here and the IP budget is what stops a flood of random ``jh_…``
      values (a legitimate machine client is one host per key, so the second
      bucket costs it nothing);
    * anything else — no credential, a forged or expired token, junk in the
      header → ``ip:<resolved client IP>``.

    That last rule is the point: 10 000 requests with 10 000 unique
    ``Authorization`` values are one client IP's budget, not 10 000 dict
    entries. Keys are HMAC fingerprints (never a slice of a raw header) and the
    log is LRU-capped at ``RATE_LIMIT_MAX_KEYS``, so neither unique credentials
    nor unique IPs can grow the process without bound.

    Deliberately in-process: a single-node, **single-worker** deployment is the
    documented topology (``WEB_CONCURRENCY=1``, see ``docs/DEPLOYMENT.md`` §6) and
    a shared Redis limiter would be a new required dependency. Raising the worker
    count multiplies every in-process budget by that count — the app says so
    loudly at startup rather than pretending the limit still holds.
    """

    def __init__(self, app, *, limit: int = 240, limit_per_minute: Optional[int] = None,
                 window_seconds: int = 60, write_limit: Optional[int] = None,
                 max_keys: Optional[int] = None,
                 exempt_paths: Tuple[str, ...] | list | set = ("/api/health", "/api/health/live",
                                                               "/api/health/ready", "/api/metrics",
                                                               "/api/track/")) -> None:
        super().__init__(app)
        self.limit = int(limit_per_minute or limit)
        self.write_limit = int(write_limit or max(20, self.limit // 3))
        self.window = window_seconds
        self.exempt_paths = tuple(exempt_paths)
        self._hits = SlidingWindowLog(
            max_keys=int(max_keys or settings.rate_limit_max_keys),
            window_seconds=window_seconds,
            sweep_interval_seconds=min(60.0, max(1.0, window_seconds / 2.0)),
        )
        _limiters.append(self._hits)
        if len(_limiters) > 8:
            del _limiters[:-8]

    # -- keys -------------------------------------------------------------- #
    def _identity(self, request: Request) -> Optional[str]:
        """HMAC key for the credential on this request, or None when anonymous."""
        authorization = request.headers.get("authorization", "") or ""
        credential = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if credential.startswith("jh_"):
            return f"k:{_fingerprint(credential)}"
        api_key = (request.headers.get("x-api-key", "") or "").strip()
        if api_key.startswith("jh_"):
            return f"k:{_fingerprint(api_key)}"
        if credential:
            user_id = _verified_user_id(credential)
            if user_id is not None:
                return f"u:{_fingerprint(f'user:{user_id}')}"
        return None

    def _keys(self, request: Request) -> Tuple[str, ...]:
        """Every bucket this request is charged to — all of them must allow it."""
        identity = self._identity(request)
        if identity is not None and identity.startswith("u:"):
            return (identity,)
        ip_key = f"ip:{resolve_client_ip(request.scope) or 'unknown'}"
        return (identity, ip_key) if identity else (ip_key,)

    # -- decision ---------------------------------------------------------- #
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path.startswith(self.exempt_paths) or request.method == "OPTIONS":
            return await call_next(request)

        budget = self.limit if request.method in ("GET", "HEAD") else self.write_limit
        for key in self._keys(request):
            allowed, retry_after = self._hits.allow(key, budget)
            if not allowed:
                metrics.counter("jobhunter_rate_limited_total",
                                {"path": _bounded_path_label(_fallback_path(path))})
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                    content={"detail": "Too many requests. Slow down and retry shortly.", "code": "rate_limited"},
                )
        return await call_next(request)

    def stats(self) -> Dict[str, Any]:
        return self._hits.stats()


def rate_limit_state() -> Dict[str, Any]:
    """Live limiter counters for ``GET /api/ops/status`` (never the keys)."""
    state: Dict[str, Any] = {"instances": len(_limiters), "keys": 0, "max_keys": 0, "window_seconds": 0,
                             "decisions": 0, "denials": 0, "evictions": 0, "expired_keys": 0, "sweeps": 0}
    for limiter in _limiters:
        snapshot = limiter.stats()
        state["max_keys"] = max(int(state["max_keys"]), int(snapshot["max_keys"]))
        state["window_seconds"] = snapshot["window_seconds"]
        for field in ("keys", "decisions", "denials", "evictions", "expired_keys", "sweeps"):
            state[field] += snapshot[field]
    return state


def reset_rate_limits() -> None:
    """Drop every tracked bucket.

    Tests use it to start hermetic; an operator can use it as "let everyone back
    in now" without a restart.
    """
    for limiter in _limiters:
        limiter.clear()


__all__ = [
    "BodySizeLimitMiddleware",
    "RateLimitMiddleware",
    "RequestContextMiddleware",
    "SecurityHeadersMiddleware",
    "SlidingWindowLog",
    "body_limit_state",
    "client_ip",
    "is_trusted_proxy",
    "path_label",
    "rate_limit_state",
    "reset_rate_limits",
    "resolve_client_ip",
]
