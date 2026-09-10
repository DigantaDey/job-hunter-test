"""
Internal Prometheus listener.

Why a separate port rather than ``GET /api/metrics`` next to the API:

* the public port is reachable by users, a second one can be kept off the
  internet entirely (``expose:`` in compose, a firewall rule, or a loopback
  bind) — so no token, rate limit or path filter has to be trusted;
* an unauthenticated endpoint on the public port is also a scraping target:
  every unknown URL adds a permanent label to the registry.

The listener runs **inside the API process** because the registry is in-process
state (``app.core.metrics``): a sidecar process would serve an empty registry.

    METRICS_PORT=9464            # 0 disables this listener (the route is used)
    METRICS_HOST=127.0.0.1       # loopback by default; 0.0.0.0 inside a container
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import metrics_text
from app.core.security import constant_time_equals

log = get_logger("app.metrics_server")

TEXT_CONTENT_TYPE = "text/plain; version=0.0.4"


def render(*, token: str = "") -> tuple[int, str]:
    """Return ``(status, body)`` for one metrics request."""
    if settings.metrics_token and not constant_time_equals(token, settings.metrics_token):
        return 401, "Metrics token required\n"
    if not settings.metrics_enabled:
        return 404, "Metrics are disabled\n"
    return 200, metrics_text(version=settings.version, environment=settings.environment)


# --------------------------------------------------------------------------- #
# Minimal ASGI application (no router: one path, one method)
# --------------------------------------------------------------------------- #
async def metrics_app(scope: Dict[str, Any], receive: Any, send: Any) -> None:
    if scope.get("type") != "http":  # pragma: no cover - lifespan/websocket unused
        return
    path = scope.get("path", "")
    if scope.get("method") not in ("GET", "HEAD") or path.rstrip("/") not in ("/metrics", ""):
        body = b"Not found. Metrics live at /metrics on this port.\n"
        await send({"type": "http.response.start", "status": 404,
                    "headers": [(b"content-type", b"text/plain"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
        return

    supplied = ""
    for name, value in scope.get("headers") or []:
        if name == b"authorization":
            supplied = value.decode("latin-1").removeprefix("Bearer ").strip()
            break
    query = (scope.get("query_string") or b"").decode("latin-1")
    if "metrics_token=" in query:
        for pair in query.split("&"):
            if pair.startswith("metrics_token="):
                supplied = pair.split("=", 1)[1]
                break

    status, body = render(token=supplied)
    payload = body.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", TEXT_CONTENT_TYPE.encode()),
                            (b"content-length", str(len(payload)).encode()),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": payload})


class MetricsServer:
    """A uvicorn server for :func:`metrics_app` running in a daemon thread."""

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None) -> None:
        self.host = host or settings.metrics_host
        self.port = int(port or settings.metrics_port)
        self._thread: Optional[threading.Thread] = None
        self._server: Any = None

    @property
    def running(self) -> bool:
        """True once the listener has actually bound its port."""
        if self._thread is None or not self._thread.is_alive():
            return False
        return bool(getattr(self._server, "started", False))

    def start(self) -> bool:
        """Start the listener. Returns False when it could not be bound."""
        if self.port <= 0 or not settings.metrics_enabled or self.running:
            return False

        import uvicorn

        config = uvicorn.Config(metrics_app, host=self.host, port=self.port,
                                log_level="warning", access_log=False, loop="asyncio")
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # the API process owns signals

        loop = asyncio.new_event_loop()

        def _serve() -> None:
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(server.serve())
            except BaseException as exc:  # noqa: BLE001 - includes uvicorn's SystemExit(3)
                # Another worker/process already owns the port (uvicorn --workers
                # N): that instance serves the registry of its own worker, so stay
                # quiet instead of taking the process down on a duplicate bind.
                log.warning("metrics listener on %s:%s not started (%s: %s)",
                            self.host, self.port, type(exc).__name__, exc)
            finally:
                try:
                    loop.close()
                except Exception:  # pragma: no cover - teardown best effort
                    pass

        self._server = server
        self._thread = threading.Thread(target=_serve, name="metrics-server", daemon=True)
        self._thread.start()
        log.info("metrics listener on http://%s:%s/metrics", self.host, self.port)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._server = None


_server: Optional[MetricsServer] = None


def start_metrics_server() -> Optional[MetricsServer]:
    """Start the process-wide listener (no-op unless METRICS_PORT is set)."""
    global _server
    if _server is None:
        candidate = MetricsServer()
        if candidate.start():
            _server = candidate
    return _server


def stop_metrics_server() -> None:
    global _server
    if _server is not None:
        _server.stop()
        _server = None


__all__ = ["MetricsServer", "metrics_app", "render", "start_metrics_server", "stop_metrics_server"]
