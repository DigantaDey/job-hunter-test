"""
Structured logging for Job Hunter.

Production runs emit one JSON object per line (``LOG_JSON=true``) so logs can be
shipped to any aggregator without a parsing rule; development keeps the human
readable format. Every log record automatically carries the request id, the
authenticated user id and the deployment environment when they are available.
"""
from __future__ import annotations

import json
import logging
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


class ContextFilter(logging.Filter):
    """Inject request/user context into every record."""

    def __init__(self, service: str = "api", environment: str = "development") -> None:
        super().__init__()
        self.service = service
        self.environment = environment

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        record.service = self.service
        record.environment = self.environment
        record.request_id = request_id_var.get()
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
    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%Y-%m-%dT%H:%M:%S')} {record.levelname:<7} {record.name:<28} {record.getMessage()}"
        rid = getattr(record, "request_id", None)
        if rid:
            base = f"{base} [request_id={rid}]"
        uid = getattr(record, "user_id", None)
        if uid is not None:
            base = f"{base} [user_id={uid}]"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
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
