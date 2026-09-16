"""
Bounded, TTL-aware LRU map.

The long-lived worker process keeps a few in-process caches — outbound GET
responses, per-host politeness state, DNS verdicts. Plain dicts are a slow leak
there: an entry is only replaced when the *same* key is seen again, and a
discovery/funding/company-intel sweep touches thousands of distinct keys per
day, so the dict grows for the whole process lifetime.

:class:`BoundedTTLMap` is the single implementation those caches use:

* a **hard entry cap** — the least-recently-used entry is evicted first, so the
  map cannot exceed ``max_entries`` no matter how many distinct keys arrive;
* an optional **per-entry TTL**, honoured on read and used to prefer evicting
  already-expired entries;
* an optional ``evictable`` predicate so an entry that is *in use* (a
  politeness lock a coroutine currently holds) is never pulled out from under
  its owner — the map may transiently exceed its cap rather than break
  correctness, and that overflow is itself bounded by the number of
  concurrently-held entries;
* hit/miss/eviction counters via :meth:`stats`, surfaced on ``/api/ops/status``
  so a growing cache is visible before it becomes an incident.

Not thread-safe by design: every user runs it inside a single asyncio loop.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

#: Sentinel for "absent": typed ``Any`` so ``dict.get(key, _MISSING)`` results
#: stay unpackable after the identity check (mypy does not narrow ``is`` on a
#: module-level ``object()`` singleton).
_MISSING: Any = object()


class BoundedTTLMap:
    """LRU map with a hard size cap and optional per-entry expiry."""

    def __init__(
        self,
        *,
        name: str,
        max_entries: int = 128,
        default_ttl: Optional[float] = None,
        evictable: Optional[Callable[[Any], bool]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.name = name
        self.max_entries = max(1, int(max_entries))
        self.default_ttl = default_ttl
        self._evictable = evictable
        self._clock = clock
        #: key -> (expiry timestamp or None, value); ordered oldest → newest.
        self._entries: "OrderedDict[Any, Tuple[Optional[float], Any]]" = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.expirations = 0
        self.writes = 0

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def get(self, key: Any, default: Any = None) -> Any:
        """Return the live value for *key*, marking it most-recently-used."""
        item = self._entries.get(key, _MISSING)
        if item is _MISSING:
            self.misses += 1
            return default
        expires, value = item
        if expires is not None and expires <= self._clock():
            self._entries.pop(key, None)
            self.expirations += 1
            self.misses += 1
            return default
        self._entries.move_to_end(key)
        self.hits += 1
        return value

    def peek(self, key: Any, default: Any = None) -> Any:
        """Value for *key* without touching recency or the hit counter."""
        item = self._entries.get(key, _MISSING)
        if item is _MISSING:
            return default
        expires, value = item
        if expires is not None and expires <= self._clock():
            return default
        return value

    def __contains__(self, key: Any) -> bool:
        return self.peek(key, _MISSING) is not _MISSING

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def put(self, key: Any, value: Any, ttl: Optional[float] = None) -> None:
        """Insert *value*, evicting least-recently-used entries to stay in bounds."""
        effective_ttl = self.default_ttl if ttl is None else ttl
        expires = None if not effective_ttl else self._clock() + float(effective_ttl)
        self._make_room()
        self._entries[key] = (expires, value)
        self._entries.move_to_end(key)
        self.writes += 1

    def pop(self, key: Any, default: Any = None) -> Any:
        item = self._entries.pop(key, _MISSING)
        return default if item is _MISSING else item[1]

    def clear(self) -> None:
        self._entries.clear()

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self._entries)

    def keys(self) -> List[Any]:
        return list(self._entries.keys())

    def values(self) -> List[Any]:
        return [value for _expires, value in self._entries.values()]

    def items(self) -> Iterable[Tuple[Any, Any]]:
        return [(key, value) for key, (_expires, value) in self._entries.items()]

    def stats(self) -> Dict[str, Any]:
        """Counters for ops/monitoring (never the cached values themselves)."""
        return {
            "name": self.name,
            "entries": len(self._entries),
            "max_entries": self.max_entries,
            "default_ttl": self.default_ttl,
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "evictions": self.evictions,
            "expirations": self.expirations,
            # ``over_capacity`` is >0 only while every candidate entry is
            # pinned by its owner (see ``evictable``) — always transient.
            "over_capacity": max(0, len(self._entries) - self.max_entries),
        }

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _can_evict(self, key: Any) -> bool:
        if self._evictable is None:
            return True
        try:
            return bool(self._evictable(self._entries[key][1]))
        except Exception:  # noqa: BLE001 - a broken predicate must not lose data
            return True

    def _make_room(self) -> None:
        if len(self._entries) < self.max_entries:
            return
        now = self._clock()
        # Expired entries first: dropping them is free and always correct.
        for key in list(self._entries):
            if len(self._entries) < self.max_entries:
                break
            expires, _value = self._entries[key]
            if expires is not None and expires <= now:
                self._entries.pop(key, None)
                self.expirations += 1
        # Then least-recently-used, skipping anything its owner still holds.
        for key in list(self._entries):
            if len(self._entries) < self.max_entries:
                break
            if not self._can_evict(key):
                continue
            self._entries.pop(key, None)
            self.evictions += 1


__all__ = ["BoundedTTLMap"]
