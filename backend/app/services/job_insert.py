"""One builder for a ``jobs`` row out of a scored candidate.

Why this is its own module: two code paths now create job rows from a candidate
dict — a live discovery run (``services/discovery.py``) and the shared pool's
instant match (``services/job_pool.py``). They must agree *column for column*,
because the row they write is also the dedupe identity the next run merges
against: a pool-sourced row whose ``title_normalized`` or ``dedupe_key`` was
derived differently would defeat the merge and show the same posting twice.

Nothing here scores, fetches or decides — it only maps the candidate shape
(``Posting.to_dict()`` plus the scoring fields discovery adds) onto
:class:`app.models.models.Job`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from app.models.models import Job
from app.services.company_normalize import normalize_company_name


def build_job_row(
    candidate: Dict[str, Any],
    *,
    user_id: int,
    freshness_hours: int,
    persona_id: Optional[int] = None,
    now: Optional[datetime] = None,
    extra_merge: Optional[Dict[str, Any]] = None,
    status: str = "discovered",
) -> Job:
    """Map one candidate onto a ``jobs`` row (not added to the session yet).

    ``extra_merge`` adds provenance the caller owns — the search attribution a
    live run carries, or ``job_pool`` for an instant match. The compact source
    payload stays out of ``extra`` (it goes to ``raw_payload``), the same split
    the discovery run has always used.
    """
    seen_at = now or datetime.utcnow()
    incoming_raw = candidate.get("raw")
    raw_payload: Dict[str, Any] = dict(incoming_raw) if isinstance(incoming_raw, dict) else {}
    extra: Dict[str, Any] = {
        "salary": candidate.get("salary", ""),
        "remote": candidate.get("remote", False),
        "freshness_relaxed": candidate.get("freshness_relaxed", False),
        "forms": {},
    }
    search_discovery = (candidate.get("extra") or {}).get("search_discovery")
    if search_discovery:
        extra["search_discovery"] = search_discovery
    if extra_merge:
        extra.update(extra_merge)
    ai_error = candidate.get("ai_error")
    return Job(
        user_id=user_id,
        title=candidate["title"],
        company=candidate["company"],
        # SQL-side company filter identity (see Job model / migration
        # e5f6a7b8c9d0) — stored at import time, not derived per query.
        company_name_normalized=candidate.get("company_name_normalized")
        or normalize_company_name(candidate["company"]),
        location=candidate.get("location", ""),
        description=candidate.get("description", ""),
        url=candidate.get("url", ""),
        source=candidate.get("source", "unknown"),
        external_id=candidate.get("external_id", ""),
        dedupe_key=candidate["dedupe_key"],
        status=status,
        score=float(candidate.get("score", 0.0)),
        score_reason=candidate.get("score_reason", ""),
        score_source=str(candidate.get("score_source") or "preliminary"),
        score_detail=candidate.get("score_detail") or ({"ai_error": ai_error} if ai_error else {}),
        persona_id=persona_id,
        company_size=candidate.get("company_size", "unknown"),
        company_info={"confidence": candidate.get("company_size_confidence", 0.0),
                      "industry": candidate.get("industry", "")},
        posted_at=candidate.get("posted_at"),
        freshness_hours=freshness_hours,
        extra=extra,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
        last_verified_at=seen_at,
        expired=False,
        source_kind=str(candidate.get("source_kind") or "")[:16],
        content_hash=str(candidate.get("content_hash") or "")[:64],
        title_normalized=str(candidate.get("title_normalized") or candidate["title"]).strip().lower()[:300],
        raw_payload=raw_payload,
    )
