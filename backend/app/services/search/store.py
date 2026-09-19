"""Short independent transactions: distributed leases, quotas and usage.

No locks/transactions are held over network I/O. Conditional UPDATEs and unique
keys enforce limits on SQLite and Postgres alike. Cache hits cost zero requests.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import delete, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import SearchBudget, SearchCache, SearchUsage


class BudgetExceeded(Exception):
    pass


def _insert_if_missing(db: Session, model, key: str, **values) -> None:
    if db.get(model, key) is None:
        try:
            with db.begin_nested():
                db.add(model(key=key, **values))
                db.flush()
        except IntegrityError:
            pass  # another worker inserted the same key


class SearchStore:
    def __init__(self, bind):
        self.bind = bind

    def claim(self, key: str) -> tuple[str, dict]:
        """Return cache hit, busy, or an opaque lease owner token."""
        now = datetime.utcnow()
        with Session(self.bind) as db, db.begin():
            # Cache cardinality is bounded by daily budgets * TTL; reap old rows.
            db.execute(delete(SearchCache).where(SearchCache.expires_at < now - timedelta(days=1),
                                                SearchCache.lease_until < now))
            db.execute(delete(SearchBudget).where(SearchBudget.expires_at < now))
            _insert_if_missing(db, SearchCache, key, payload={}, expires_at=now,
                               lease_until=now, owner="")
            row = db.get(SearchCache, key)
            assert row is not None
            if row.expires_at > now:
                return "hit", dict(row.payload)
            owner = str(uuid.uuid4())
            claimed = db.execute(update(SearchCache).where(
                SearchCache.key == key, SearchCache.expires_at <= now, SearchCache.lease_until <= now,
            ).values(owner=owner, lease_until=now + timedelta(seconds=60)))
            return (owner, {}) if cast(CursorResult, claimed).rowcount else ("busy", {})

    def finish(self, key: str, owner: str, payload: dict, ttl: int) -> None:
        now = datetime.utcnow()
        with Session(self.bind) as db, db.begin():
            db.execute(update(SearchCache).where(SearchCache.key == key, SearchCache.owner == owner).values(
                payload=payload, expires_at=now + timedelta(seconds=ttl), lease_until=now,
            ))

    def reserve(self, user_id: int, provider: str, key: str, cost: int) -> int:
        """Charge all four quotas atomically BEFORE sending an attempt."""
        now = datetime.utcnow()
        minute = now.replace(second=0, microsecond=0)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        windows = (
            ("global:minute", None, minute, 60, settings.job_search_global_rpm),
            ("global:day", None, day, 86400, settings.job_search_global_daily),
            (f"user:{user_id}:minute", user_id, minute, 60, settings.job_search_user_rpm),
            (f"user:{user_id}:day", user_id, day, 86400, settings.job_search_user_daily),
        )
        with Session(self.bind) as db, db.begin():
            for scope, uid, start, seconds, limit in windows:
                bucket = f"{scope}:{start.isoformat()}"
                _insert_if_missing(db, SearchBudget, bucket, user_id=uid, used=0,
                                   expires_at=start + timedelta(seconds=seconds))
                changed = db.execute(update(SearchBudget).where(
                    SearchBudget.key == bucket, SearchBudget.used < limit,
                ).values(used=SearchBudget.used + 1))
                if not cast(CursorResult, changed).rowcount:
                    raise BudgetExceeded()  # rolls back ALL quota increments
            usage = SearchUsage(user_id=user_id, provider=provider, query_hash=key,
                                estimated_cost_microusd=max(0, cost))
            db.add(usage)
            db.flush()
            return usage.id

    def outcome(self, usage_id: int, outcome: str) -> None:
        with Session(self.bind) as db, db.begin():
            db.execute(update(SearchUsage).where(SearchUsage.id == usage_id).values(outcome=outcome))
