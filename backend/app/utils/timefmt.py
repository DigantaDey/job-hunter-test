"""Timestamp rendering — the one helper new endpoints serialise through.

``docs/contracts/01-conventions.md`` §2: timestamps are **naive UTC at rest** and
**ISO-8601 with an explicit UTC offset on the wire**. The offset is not
decoration. A naive ``2026-09-18T14:03:22`` handed to ``new Date(...)`` in a
browser is read in the *visitor's* zone, so the same row renders up to ±14 h off
depending on where the user is — which is why
``frontend/src/lib/automation.ts`` carries a comment about it and why every
timestamp this layer returns goes through :func:`iso_utc`.

Two older modules (``services.auto_scheduler``, ``services.application_packet``)
have their own local copy from before this helper existed; they are left alone
(their output is already offset-bearing / already consumed), and new code imports
from here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

__all__ = ["iso_utc", "parse_iso_utc", "coerce_datetime"]


def iso_utc(moment: Any) -> Optional[str]:
    """Render a timestamp as ``2026-09-18T14:03:22Z`` (``None`` stays ``None``).

    ``None`` means "has not happened" and is preserved as ``None`` — never ``""``,
    never the epoch (contracts/01 §2 rule 2). An aware datetime is converted to
    UTC; a naive one is *assumed* to be UTC, which is what every writer in this
    codebase stores.
    """
    if not isinstance(moment, datetime):
        return None
    if moment.tzinfo is None:
        aware = moment.replace(tzinfo=timezone.utc)
    else:
        aware = moment.astimezone(timezone.utc)
    return aware.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso_utc(value: Any) -> Optional[datetime]:
    """Parse a wire timestamp into the naive-UTC form the DB stores.

    Tolerant on purpose: clients send ``…Z``, ``…+00:00``, ``…+02:00`` and (from
    ``<input type="datetime-local">``) a naive local-looking string. A naive
    value is read as UTC — the same assumption every writer makes — and an
    unparseable value is ``None`` rather than an exception, so a bad date in one
    field cannot 500 a write the rest of which is valid.
    """
    if isinstance(value, datetime):
        return coerce_datetime(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return coerce_datetime(parsed)


def coerce_datetime(moment: Any) -> Optional[datetime]:
    """Normalise to naive UTC (what the columns hold), or ``None``."""
    if not isinstance(moment, datetime):
        return None
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)
