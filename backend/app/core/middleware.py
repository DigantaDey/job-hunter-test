"""
HTTP middleware: request ids, access logs, security headers and rate limiting.

Ordering matters. In ``main.py`` the middlewares are added so that requests pass
through ``RequestContext`` first (so every later log line has the id) and the
``BodySizeLimit`` fast-rejects oversized payloads before any router code runs.
"""
from __future__ import annotations

import time
import uuid
from collections import deque
from typing import Deque, Dict, Tuple

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core import metrics
from app.core.logging import get_logger, request_id_var

logger = get_logger("http")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach an id to every request, log it, and count it."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming[:64] if incoming else uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        request.state.request_id = request_id

        route = request.scope.get("route")
        path_label = getattr(route, "path", None) or _fallback_path(request.url.path)
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
            metrics.gauge("jobhunter_http_requests_in_progress", 0)
            metrics.counter("jobhunter_http_requests_total", {
                "method": request.method, "path": path_label, "status": str(status_code),
            })
            metrics.observe("jobhunter_http_request_duration_seconds", duration,
                            {"method": request.method, "path": path_label})
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


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized bodies before they hit memory-hungry parsers."""

    def __init__(self, app, *, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        header = request.headers.get("content-length")
        if header and header.isdigit() and int(header) > self.max_bytes:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Request body too large (limit {self.max_bytes // (1024 * 1024)} MB).",
                         "code": "payload_too_large"},
            )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window limiter keyed by API key / user / client IP.

    Deliberately in-process: a single-node deployment is the documented topology
    and a shared Redis limiter would be a new required dependency. The limiter is
    still per-credential, so one noisy user cannot exhaust another's budget.
    """

    def __init__(self, app, *, limit: int = 240, limit_per_minute: int | None = None,
                 window_seconds: int = 60, write_limit: int | None = None,
                 exempt_paths: Tuple[str, ...] | list | set = ("/api/health", "/api/health/live",
                                                               "/api/health/ready", "/api/metrics",
                                                               "/api/track/")) -> None:
        super().__init__(app)
        self.limit = int(limit_per_minute or limit)
        self.write_limit = int(write_limit or max(20, self.limit // 3))
        self.window = window_seconds
        self.exempt_paths = tuple(exempt_paths)
        self._hits: Dict[str, Deque[float]] = {}
        self._last_sweep = 0.0

    def _key(self, request: Request) -> str:
        auth = request.headers.get("authorization", "")
        if auth:
            return f"auth:{auth[-24:]}"
        api_key = request.headers.get("x-api-key", "")
        if api_key:
            return f"key:{api_key[-8:]}"
        client = request.client.host if request.client else "unknown"
        return f"ip:{client}"

    def _allow(self, key: str, budget: int) -> Tuple[bool, int]:
        now = time.monotonic()
        bucket = self._hits.setdefault(key, deque())
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= budget:
            return False, max(1, int(self.window - (now - bucket[0])))
        bucket.append(now)
        return True, 0

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path.startswith(self.exempt_paths) or request.method == "OPTIONS":
            return await call_next(request)

        budget = self.limit if request.method in ("GET", "HEAD") else self.write_limit
        allowed, retry_after = self._allow(self._key(request), budget)
        if not allowed:
            metrics.counter("jobhunter_rate_limited_total", {"path": _fallback_path(path)})
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                content={"detail": "Too many requests. Slow down and retry shortly.", "code": "rate_limited"},
            )

        self._sweep()
        return await call_next(request)

    def _sweep(self) -> None:
        now = time.monotonic()
        if now - self._last_sweep < 300:
            return
        self._last_sweep = now
        for key in [k for k, bucket in self._hits.items() if not bucket or now - bucket[-1] > self.window]:
            self._hits.pop(key, None)
