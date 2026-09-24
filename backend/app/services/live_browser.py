"""
The session's *live* browser window — one per session, visible, and kept open
while a human still has work to do in it.

Why this exists
---------------
Before this module every assisted pass opened a browser, did its seconds of
work and closed the window in a ``finally`` — including the moment the pass
paused for a human step. The user was told "take over in that same window",
but the window was already gone, and a handoff could only offer a link into a
*different* browser that shares nothing with the automation.

This registry fixes the lifetime: a **headed** window a pass opened stays open
while the session is live (paused, awaiting the user, ready to submit), so the
human can sign in, solve a bot check or press Submit in the very context the
automation will continue in. The cookies a login produced are exported back to
the session when the human closes the action, so the next pass resumes logged
in instead of hitting the wall again.

Rules
-----
* Only **headed** windows are registered. A headless run has nothing a human
  can watch or take over, so it keeps the old close-at-once behaviour and
  burns no idle resources.
* The registry is **process-local**, and so is the window: in the default
  single-process topology (``RUN_WORKER_IN_API=true``, or ``./run.sh``) the
  queue passes and the API routes share one event loop, so everything works.
  In a split deploy the worker owns its windows; the API reports
  ``{"open": false}`` honestly rather than pretending.
* Every entry carries the loop it was created on, so a close requested from
  another thread (a sync route, a sweep) is dispatched to that loop instead of
  being awaited across loops.
* Idle windows are swept after :data:`IDLE_TTL_SECONDS`; a window nobody
  claims is a window that leaks.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.core.logging import get_logger

log = get_logger("app.live_browser")

#: How long a window may sit untouched before the sweeper closes it. A human
#: step (sign-in, MFA, bot check) is minutes, not hours; the sweep runs on
#: every registry touch, so an abandoned window never outlives the next API
#: call by more than its own idle time.
IDLE_TTL_SECONDS = 20 * 60


@dataclass
class _Entry:
    driver: Any
    loop: asyncio.AbstractEventLoop
    opened_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)


#: session_id → the window that session's human step happens in.
_ENTRIES: Dict[int, _Entry] = {}


def remember(session_id: int, driver: Any) -> None:
    """
    Register ``driver`` as this session's live window.

    Must be called from the loop the driver runs on (the pass loop is). An
    unhealthy driver (user closed the window) replaces nothing and is not
    stored.
    """
    _sweep()
    if not _healthy(driver):
        _ENTRIES.pop(session_id, None)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - callers are async code
        return
    _ENTRIES[session_id] = _Entry(driver=driver, loop=loop)
    log.info("session %s: browser window kept open for the human step", session_id)


def adopt(session_id: int) -> Optional[Any]:
    """
    The session's live window, if there is a usable one.

    A window whose page or browser has gone away (the user closed it) is
    dropped here, so the next pass opens a fresh one instead of driving a
    corpse. ``last_used`` refreshes, pushing the idle sweep out.
    """
    _sweep()
    entry = _ENTRIES.get(session_id)
    if entry is None:
        return None
    if not _healthy(entry.driver):
        forget(session_id)
        log.info("session %s: browser window was closed by the user", session_id)
        return None
    entry.last_used = time.monotonic()
    return entry.driver


def forget(session_id: int) -> None:
    """Drop the registry row without closing the window (caller closes it)."""
    _ENTRIES.pop(session_id, None)


def holds(session_id: int, driver: Any) -> bool:
    """Whether *this exact driver* is the session's registered window."""
    entry = _ENTRIES.get(session_id)
    return entry is not None and entry.driver is driver


def status(session_id: int) -> Dict[str, Any]:
    """
    The window's state for a session payload — a plain dict, safe to read
    from a sync route: it never exposes the driver, the loop or a value.
    """
    entry = _ENTRIES.get(session_id)
    if entry is None:
        return {"open": False}
    if not _healthy(entry.driver):
        forget(session_id)
        return {"open": False}
    return {"open": True,
            "idle_seconds": int(time.monotonic() - entry.last_used),
            "age_seconds": int(time.monotonic() - entry.opened_at)}


def any_open() -> Dict[str, Any]:
    """Counts for ops/debug views — never a driver or a URL."""
    open_ids = [sid for sid, entry in _ENTRIES.items() if _healthy(entry.driver)]
    return {"open": len(open_ids), "session_ids": sorted(open_ids)}


async def close(session_id: int) -> None:
    """Close this session's window now (cancel, expiry, re-authentication)."""
    entry = _ENTRIES.pop(session_id, None)
    if entry is None:
        return
    loop = entry.loop
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - callers are async code
        running = None
    if running is loop:
        await _close_entry(entry)
        return
    if loop.is_closed():
        # The owning loop is gone (a test's ``asyncio.run``, a crashed
        # worker): best effort on *this* loop — a real driver raises, a fake
        # one closes, and either way nothing propagates to the caller.
        await _close_entry(entry)
        return
    try:
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_close_entry(entry), loop))
    except RuntimeError:  # pragma: no cover - race with loop shutdown
        pass


async def close_all() -> None:
    """Process shutdown: every window, everywhere it lives."""
    entries = list(_ENTRIES.items())
    _ENTRIES.clear()
    loops: Dict[asyncio.AbstractEventLoop, list] = {}
    for _sid, entry in entries:
        loops.setdefault(entry.loop, []).append(entry)
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    for loop, group in loops.items():
        if loop is running:
            for entry in group:
                await _close_entry(entry)
        else:
            for entry in group:
                try:
                    asyncio.run_coroutine_threadsafe(_close_entry(entry), loop)
                except RuntimeError:  # pragma: no cover - loop already gone
                    log.warning("live browser on a closed loop — leaving to the OS")


def sweep() -> None:
    """Close windows idle past the TTL. Safe from any thread."""
    _sweep()


def _sweep() -> None:
    now = time.monotonic()
    expired = [sid for sid, entry in _ENTRIES.items()
               if now - entry.last_used > IDLE_TTL_SECONDS]
    for sid in expired:
        entry = _ENTRIES.pop(sid, None)
        if entry is None:
            continue
        log.info("session %s: idle browser window swept after %ss",
                 sid, int(now - entry.last_used))
        _schedule_close(entry)


def _schedule_close(entry: _Entry) -> None:
    coro = _close_entry(entry)
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is entry.loop:
        entry.loop.create_task(coro)
        return
    if entry.loop.is_closed():
        # Owner loop gone: close on whatever loop we have, else here and now.
        if running is not None:
            running.create_task(coro)
            return
        try:
            asyncio.run(coro)
        except RuntimeError:  # pragma: no cover - nested loop somewhere
            pass
        return
    try:
        asyncio.run_coroutine_threadsafe(coro, entry.loop)
    except RuntimeError:  # pragma: no cover - loop already closed
        log.warning("could not schedule browser close — loop is gone")


async def _close_entry(entry: _Entry) -> None:
    try:
        await entry.driver.close()
    except Exception as exc:  # pragma: no cover - best effort by design
        log.warning("closing a live browser failed (%s)", type(exc).__name__)


def _healthy(driver: Any) -> bool:
    healthy = getattr(driver, "healthy", None)
    if callable(healthy):
        try:
            return bool(healthy())
        except Exception:  # pragma: no cover - defensive
            return False
    return True


__all__ = [
    "IDLE_TTL_SECONDS",
    "adopt",
    "any_open",
    "close",
    "close_all",
    "forget",
    "holds",
    "remember",
    "status",
    "sweep",
]
