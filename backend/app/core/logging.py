"""
Structured logging for Job Hunter.

Production runs emit one JSON object per line (``LOG_JSON=true``) so logs can be
shipped to any aggregator without a parsing rule; development keeps the human
readable format. Every log record automatically carries the request id, the
authenticated user id and the deployment environment when they are available.

A log line is a trust boundary like any other output: the request id is a
caller-supplied header, and messages routinely embed request-derived values, so
a value containing a newline could otherwise append records nobody wrote. The
context filter scrubs those values on the way onto the record
(:func:`sanitize_log_value`) and the text formatter scrubs everything it
interpolates, so neither format can be used to forge a line.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Optional

request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
user_id_var: ContextVar[Optional[int]] = ContextVar("user_id", default=None)

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}

# --------------------------------------------------------------------------- #
# Control-character scrubbing (log injection)
# --------------------------------------------------------------------------- #
#: C0 controls, DEL, the C1 range, and the two Unicode line terminators that are
#: not ``\n`` but that log shippers and terminal emulators still treat as line
#: breaks. Any of these inside a value a caller controls (the ``x-request-id``
#: header is the obvious one) lets that caller mint extra log records: a
#: ``"x\\n2026-01-01T00:00:00 INFO  app.audit  audit [user_id=1]"`` header writes
#: a line nobody can tell from a real one. JSON mode is immune because
#: ``json.dumps`` escapes them; the text formatter interpolates raw, so every
#: value it touches goes through :func:`sanitize_log_value` first.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")

_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def sanitize_log_value(value: Any, *, limit: int = 512) -> Any:
    """
    Return *value* with control characters escaped, so it cannot break out of
    the log line it is written into.

    Non-strings pass through untouched (a ``user_id`` is an ``int``; there is
    nothing to inject and stringifying it would only make the record less
    useful). The three whitespace controls are escaped readably and the rest of
    the range as ``\\xNN``, so the *content* of an attack is still visible in the
    log rather than silently deleted. Strings longer than *limit* are truncated —
    a caller-controlled field has no business being a megabyte of log line.
    """
    if not isinstance(value, str):
        return value
    if _CONTROL_CHARS.search(value):
        value = _CONTROL_CHARS.sub(lambda m: _ESCAPES.get(m.group(0), f"\\x{ord(m.group(0)):02x}"), value)
    if len(value) > limit:
        # The ellipsis counts towards the limit, so the result never exceeds it.
        value = value[: max(0, limit - 1)] + "…"
    return value


class ContextFilter(logging.Filter):
    """Inject request/user context into every record."""

    def __init__(self, service: str = "api", environment: str = "development") -> None:
        super().__init__()
        self.service = service
        self.environment = environment

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        record.service = self.service
        record.environment = self.environment
        # Scrubbed here, at the point the caller-controlled header becomes a
        # record attribute, so *every* formatter and every consumer of
        # ``record.request_id`` sees the safe value — not just the text one.
        record.request_id = sanitize_log_value(request_id_var.get(), limit=64)
        if user_id_var.get() is not None and not hasattr(record, "user_id"):
            record.user_id = user_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": getattr(record, "service", "api"),
            "environment": getattr(record, "environment", "development"),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """
    One record per line, with every interpolated value control-char scrubbed.

    This format is the injection-prone one (JSON escapes on its own), and it
    interpolates data a caller controls — ``request_id`` comes straight from the
    ``x-request-id`` header, and ``getMessage()`` routinely carries
    request-derived values (a domain, a URL path, a provider error). Left raw, a
    single header could append arbitrary fake records to the log. Each field is
    therefore passed through :func:`sanitize_log_value`, and the traceback block
    is indented so that even a newline smuggled inside an exception message
    cannot start a line at column 0, where a real record begins.
    """

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{self.formatTime(record, '%Y-%m-%dT%H:%M:%S')} "
            f"{sanitize_log_value(record.levelname):<7} "
            f"{sanitize_log_value(record.name):<28} "
            f"{sanitize_log_value(record.getMessage())}"
        )
        rid = getattr(record, "request_id", None)
        if rid:
            base = f"{base} [request_id={sanitize_log_value(rid, limit=64)}]"
        uid = getattr(record, "user_id", None)
        if uid is not None:
            base = f"{base} [user_id={sanitize_log_value(uid)}]"
        if record.exc_info:
            indented = "\n".join(f"    {line}" for line in
                                 self.formatException(record.exc_info).splitlines())
            base = f"{base}\n{indented}"
        return base


def configure_logging(
    level: str = "INFO",
    *,
    json_output: bool = False,
    service: str = "api",
    environment: str = "development",
) -> None:
    """Install a single stdout handler on the root logger (idempotent)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    handler.addFilter(ContextFilter(service=service, environment=environment))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Uvicorn installs its own handlers; route them through ours so request logs
    # stay JSON and keep the same shape.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    # httpx logs the full URL for every request at INFO — too chatty in prod.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str = "app") -> logging.Logger:
    return logging.getLogger(name)


class LogContext:
    """
    Bind request/user context to log records inside a block (worker tasks,
    background jobs) — ``with LogContext(request_id=f"job-{id}", user_id=3):``.
    """

    def __init__(self, request_id: Optional[str] = None, user_id: Optional[int] = None) -> None:
        self.request_id = request_id
        self.user_id = user_id
        self._tokens: list = []

    def __enter__(self) -> "LogContext":
        if self.request_id is not None:
            self._tokens.append((request_id_var, request_id_var.set(str(self.request_id))))
        if self.user_id is not None:
            self._tokens.append((user_id_var, user_id_var.set(str(self.user_id))))
        return self

    def __exit__(self, *exc: object) -> bool:
        for var, token in reversed(self._tokens):
            var.reset(token)
        self._tokens.clear()
        return False


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # pragma: no cover - defensive
        return "-"


def log_event(logger: logging.Logger, level: str, message: str, **fields: Any) -> None:
    """Log a structured event without spamming keyword arguments at call sites."""
    logger.log(getattr(logging, level.upper(), logging.INFO), message, extra=fields)
