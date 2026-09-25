"""The shared job pool — cross-user discovery with a hard 7-day memory.

The problem this solves is not one user's fetch latency, it is the *market*:
every account's discovery run was a cold start that had to re-fetch the same
public postings, so a brand-new user waited minutes for a board while the
workspace had already seen thousands of the same jobs. The pool is the union of
what every run has seen, deduplicated by canonical identity:

* **Discovery writes to it.** Every run folds the postings it scanned in
  (:func:`record_candidates`) — cheap upserts, batched, no extra fetching.
* **Discovery reads from it.** A run offers pool entries the user does not have
  yet as extra candidates (:func:`candidates_for_user`), which is coverage the
  live fan-out could not produce on its own *and* a latency win: they cost no
  network at all.
* **Onboarding matches against it instantly.** :func:`instant_match` puts the
  best matches on a fresh board from the pool alone — deterministic scoring
  (`score_source="preliminary"`, honestly labelled), no provider calls, no
  fetching — while the user's own live discovery run is still queued/processing.
  That is the "instant match while live discovery runs" contract.

Three boundaries are deliberate and tested:

* **Retention is 7 days** (``JOB_POOL_RETENTION_DAYS``). ``expires_at`` is
  ``last_seen_at + retention``; :func:`prune` deletes the content and folds it
  into :class:`~app.models.models.JobPoolMetric` — counters only, no title, no
  description, no URL, no company *content*, keyed by a SHA-256 of the canonical
  id. Those aggregates are what the owner console shows for "deleted jobs".
* **No tenant column on the entry.** A posting is public market data; the
  person-level link lives in ``job_pool_seen`` (a real ``user_id`` FK) so
  account erasure reaches it through the schema-derived plan.
* **Off means off.** With ``JOB_POOL_ENABLED=false`` discovery reads and writes
  nothing here and the instant match reports ``enabled: false`` — the pre-pool
  behaviour, byte for byte.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc
from app.models.models import Job, JobPoolEntry, JobPoolMetric, JobPoolSeen, User
from app.services.reliability import count, duration

log = get_logger("app.job_pool")

#: Keys the candidate dict may carry its canonical identity under, in order of
#: preference. The implementation is ``discovery.canonical_key`` (imported
#: lazily below, so this module and ``discovery`` can depend on each other
#: without an import cycle).
_KEY_FIELDS = ("canonical_id", "dedupe_key")

#: SQL parameter batch for the ``IN (...)`` lookups (SQLite's default limit is
#: 999; 200 keeps the statement small on both engines).
_BATCH = 200


def _document_fingerprint(document: Dict[str, Any]) -> str:
    """Content identity of a profile document, for the instant-match marker.

    Keyed on the document's *content*, not just the profile row: a profile keeps
    its id while a user reviews and corrects it, so an id-only marker would
    leave a corrected profile matched against the stale version. (The stored
    ``CandidateProfile.document_sha256`` column is serialised before later
    edits, so it is not a safe stand-in here.)
    """
    payload = json.dumps(document or {}, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Configuration / vocabulary
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    return bool(getattr(settings, "job_pool_enabled", True))


def retention_days() -> int:
    try:
        days = int(getattr(settings, "job_pool_retention_days", 7) or 7)
    except (TypeError, ValueError):
        days = 7
    return max(1, days)


def retention_window() -> timedelta:
    return timedelta(days=retention_days())


def expiry_for(now: datetime) -> datetime:
    """When an entry seen at *now* leaves the pool."""
    return now + retention_window()


def canonical_key(candidate: Dict[str, Any]) -> str:
    """Canonical identity of a candidate — the same key ``jobs.dedupe_key`` uses.

    Imported from :mod:`app.services.discovery` on purpose: two implementations
    of "what makes this posting the same posting" would be a silent duplicate
    generator (the pool would key a posting one way and the board another).
    """
    from app.services.discovery import canonical_key as _canonical  # noqa: PLC0415 - cycle guard

    return str(_canonical(candidate) or "").strip().lower()[:280]


def metric_key(dedupe_key: str) -> str:
    """Aggregate identity: SHA-256 of the canonical key.

    A pruned entry keeps counters under this key and nothing else — no title, no
    URL, no company name — so "what did we see that is gone now" aggregates
    without leaving a re-identifiable copy of the posting in the database.
    """
    return hashlib.sha256(str(dedupe_key or "").encode("utf-8", "replace")).hexdigest()


def _entry_payload(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """The compact, content-only slice of a candidate the pool keeps."""
    raw_extra = candidate.get("extra")
    extra: Dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
    return {
        "title": str(candidate.get("title") or "").strip()[:300],
        "company": str(candidate.get("company") or "").strip()[:200],
        "location": str(candidate.get("location") or "").strip()[:200],
        "description": str(candidate.get("description") or "").strip()[:6000],
        "url": str(candidate.get("url") or "").strip()[:1000],
        "source": str(candidate.get("source") or "unknown")[:64],
        "external_id": str(candidate.get("external_id") or "")[:200],
        "source_kind": str(candidate.get("source_kind") or "")[:16],
        "content_hash": str(candidate.get("content_hash") or "")[:64],
        "title_normalized": str(
            candidate.get("title_normalized") or candidate.get("title") or ""
        ).strip().lower()[:300],
        "company_name_normalized": str(candidate.get("company_name_normalized") or "")[:200],
        "posted_at": candidate.get("posted_at"),
        "extra": {
            "salary": str(extra.get("salary") or "")[:200],
            "remote": bool(extra.get("remote") or candidate.get("remote") or False),
            "industry": str(candidate.get("industry") or extra.get("industry") or "")[:120],
        },
    }


def _chunks(values: Sequence[Any], size: int = _BATCH) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


# --------------------------------------------------------------------------- #
# Writing: every run feeds the pool
# --------------------------------------------------------------------------- #
def record_candidates(
    db: Session,
    candidates: Sequence[Dict[str, Any]],
    *,
    user_id: Optional[int] = None,
    now: Optional[datetime] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """Fold a run's scanned postings into the pool (upsert by canonical key).

    ``user_id`` marks the contributor: the (entry, user) pair is recorded in
    ``job_pool_seen`` so ``users_seen`` is a *distinct user* count, and a user
    who scans the same posting on five consecutive days still counts once.

    Returns a small report (``received`` / ``created`` / ``updated`` /
    ``expired`` / ``contributors``) — the caller puts it in the run report, so an
    operator can see the pool growing rather than inferring it.
    """
    started = datetime.utcnow()
    now = now or started
    report: Dict[str, Any] = {"received": 0, "created": 0, "updated": 0,
                              "expired": 0, "contributors": 0, "enabled": enabled()}
    if not enabled() or not candidates:
        return report
    try:
        limit = max(1, int(getattr(settings, "job_pool_ingest_limit", 300) or 300))
    except (TypeError, ValueError):
        limit = 300

    prepared: List[Tuple[str, Dict[str, Any]]] = []
    seen_keys: set[str] = set()
    for candidate in candidates[:limit]:
        if not isinstance(candidate, dict) or not candidate.get("title") or not candidate.get("company"):
            continue
        key = canonical_key(candidate)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        prepared.append((key, _entry_payload(candidate)))
    report["received"] = len(prepared)
    if not prepared:
        return report

    expiry = expiry_for(now)
    existing: Dict[str, JobPoolEntry] = {}
    for chunk in _chunks([key for key, _ in prepared]):
        for row in db.query(JobPoolEntry).filter(JobPoolEntry.dedupe_key.in_(list(chunk))).all():
            existing[row.dedupe_key] = row

    touched: List[JobPoolEntry] = []
    for key, payload in prepared:
        entry = existing.get(key)
        if entry is None:
            entry = JobPoolEntry(
                dedupe_key=key,
                first_seen_at=now,
                last_seen_at=now,
                expires_at=expiry,
                times_seen=1,
                users_seen=0,
                **payload,
            )
            db.add(entry)
            existing[key] = entry
            report["created"] += 1
        else:
            entry.last_seen_at = now
            entry.expires_at = expiry
            entry.times_seen = int(entry.times_seen or 0) + 1
            # A source that still lists the posting clears a stale expiry flag.
            entry.expired = False
            if len(payload["description"]) > len(entry.description or ""):
                entry.description = payload["description"]
            for field in ("url", "external_id", "content_hash", "source_kind",
                          "title_normalized", "company_name_normalized", "location"):
                if payload[field] and not getattr(entry, field, ""):
                    setattr(entry, field, payload[field])
            if payload["posted_at"] and not entry.posted_at:
                entry.posted_at = payload["posted_at"]
            if payload["extra"] and entry.extra != payload["extra"]:
                entry.extra = payload["extra"]
            report["updated"] += 1
        touched.append(entry)
    db.flush()  # ids for the seen rows below

    if user_id is not None:
        contributor = _mark_seen(db, touched, int(user_id), now)
        report["contributors"] = contributor
    try:
        duration("jobhunter_job_pool_write_seconds", (datetime.utcnow() - started).total_seconds(),
                 operation="record")
    except Exception:  # pragma: no cover - metrics must never break a run
        pass
    if commit:
        db.commit()
    count("jobhunter_job_pool_entries_total", operation="recorded", value=report["created"])
    inc("jobhunter_job_pool_writes_total", operation="record")
    return report


def _mark_seen(db: Session, entries: List[JobPoolEntry], user_id: int, now: datetime) -> int:
    """Record which entries *this* user has seen. Returns new contributor rows."""
    if not entries:
        return 0
    ids = [entry.id for entry in entries if entry.id is not None]
    known: set[int] = set()
    for chunk in _chunks(ids):
        rows = (
            db.query(JobPoolSeen.entry_id)
            .filter(JobPoolSeen.user_id == user_id, JobPoolSeen.entry_id.in_(list(chunk)))
            .all()
        )
        known.update(int(row[0]) for row in rows)
    added = 0
    for entry in entries:
        if entry.id is None or entry.id in known:
            continue
        db.add(JobPoolSeen(entry_id=entry.id, user_id=user_id, first_seen_at=now, last_seen_at=now))
        entry.users_seen = int(entry.users_seen or 0) + 1
        added += 1
    return added


def record_posting_deleted(
    db: Session,
    *,
    dedupe_key: str,
    source: str = "",
    company_name_normalized: str = "",
    now: Optional[datetime] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """The posting is *gone* — content out, aggregate counters kept.

    Called when a run learns a posting was withdrawn (a source marks it expired,
    or a job is removed from a board). The pool entry is folded into
    :class:`~app.models.models.JobPoolMetric` **without** waiting for retention:
    a deleted job keeps only metrics, exactly like a pruned one.
    """
    now = now or datetime.utcnow()
    key = str(dedupe_key or "").strip().lower()[:280]
    if not enabled() or not key:
        return {"deleted": 0}
    entry = db.query(JobPoolEntry).filter(JobPoolEntry.dedupe_key == key).first()
    if entry is None:
        return {"deleted": 0}
    _fold_into_metrics(db, [entry], reason="deleted", now=now)
    db.flush()
    db.query(JobPoolSeen).filter(JobPoolSeen.entry_id == entry.id).delete(synchronize_session=False)
    db.delete(entry)
    if commit:
        db.commit()
    inc("jobhunter_job_pool_pruned_total", reason="deleted")
    return {"deleted": 1}


# --------------------------------------------------------------------------- #
# Retention: prune to 7 days, keep aggregates only
# --------------------------------------------------------------------------- #
def _fold_into_metrics(db: Session, entries: Sequence[JobPoolEntry], *, reason: str,
                       now: datetime) -> int:
    """Turn entries into aggregate rows (content dropped). Returns rows written."""
    if not entries:
        return 0
    keys = [metric_key(entry.dedupe_key) for entry in entries]
    metrics: Dict[str, JobPoolMetric] = {}
    for chunk in _chunks(keys):
        for row in db.query(JobPoolMetric).filter(JobPoolMetric.metric_key.in_(list(chunk))).all():
            metrics[row.metric_key] = row
    seen_counts = _seen_counts(db, [entry.id for entry in entries if entry.id is not None])
    written = 0
    for entry, key in zip(entries, keys, strict=False):
        distinct = max(int(entry.users_seen or 0), seen_counts.get(entry.id, 0))
        first_seen = entry.first_seen_at or now
        last_seen = entry.last_seen_at or now
        days_live = max(0, int((last_seen - first_seen).total_seconds() // 86400))
        existing_metric = metrics.get(key)
        if existing_metric is None:
            row = JobPoolMetric(metric_key=key, source=str(entry.source or "")[:64],
                                company_name_normalized=str(entry.company_name_normalized or "")[:200],
                                reason=reason, times_seen=int(entry.times_seen or 0),
                                distinct_users=distinct, days_live=days_live,
                                first_seen_at=first_seen, last_seen_at=last_seen)
            db.add(row)
            metrics[key] = row
        else:
            # Counters accumulate across sightings of the same canonical posting;
            # ``distinct_users`` / ``days_live`` are upper bounds (a distinct
            # count cannot be summed across windows), so both take the max.
            row = existing_metric
            row.times_seen = int(row.times_seen or 0) + int(entry.times_seen or 0)
            row.distinct_users = max(int(row.distinct_users or 0), distinct)
            row.days_live = max(int(row.days_live or 0), days_live)
            row.first_seen_at = min(row.first_seen_at, first_seen) if row.first_seen_at else first_seen
            row.last_seen_at = max(row.last_seen_at, last_seen) if row.last_seen_at else last_seen
            row.reason = reason
        written += 1
    return written


def _seen_counts(db: Session, entry_ids: Sequence[int]) -> Dict[int, int]:
    counts: Dict[int, int] = {}
    for chunk in _chunks([int(e) for e in entry_ids]):
        rows = (
            db.query(JobPoolSeen.entry_id, func.count(JobPoolSeen.id))
            .filter(JobPoolSeen.entry_id.in_(list(chunk)))
            .group_by(JobPoolSeen.entry_id)
            .all()
        )
        counts.update({int(entry_id): int(total) for entry_id, total in rows})
    return counts


def prune(
    db: Session,
    *,
    now: Optional[datetime] = None,
    batch: Optional[int] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """Enforce retention: content out at ``expires_at``, aggregates kept.

    Bounded by ``JOB_POOL_PRUNE_BATCH`` rows per call — the pool is a hot table
    written by every run, and an unbounded ``DELETE`` on the discovery path is
    the kind of thing that only hurts once the table is big. Returns
    ``{"pruned": n, "aggregated": m, "reason": {...}}``.
    """
    started = datetime.utcnow()
    now = now or started
    report: Dict[str, Any] = {"pruned": 0, "aggregated": 0, "deleted": 0, "expired": 0,
                             "enabled": enabled(), "retention_days": retention_days()}
    if not enabled():
        return report
    try:
        limit = int(batch or getattr(settings, "job_pool_prune_batch", 500) or 500)
    except (TypeError, ValueError):
        limit = 500
    rows = (
        db.query(JobPoolEntry)
        .filter(JobPoolEntry.expires_at <= now)
        .order_by(JobPoolEntry.expires_at)
        .limit(max(1, limit))
        .all()
    )
    if not rows:
        return report
    deleted = [row for row in rows if row.expired]
    expired = [row for row in rows if not row.expired]
    report["aggregated"] = _fold_into_metrics(db, deleted, reason="deleted", now=now)
    report["aggregated"] += _fold_into_metrics(db, expired, reason="expired", now=now)
    report["deleted"] = len(deleted)
    report["expired"] = len(expired)
    ids = [row.id for row in rows]
    for chunk in _chunks(ids):
        db.query(JobPoolSeen).filter(JobPoolSeen.entry_id.in_(list(chunk))).delete(synchronize_session=False)
        db.query(JobPoolEntry).filter(JobPoolEntry.id.in_(list(chunk))).delete(synchronize_session=False)
    if commit:
        db.commit()
    report["pruned"] = len(rows)
    inc("jobhunter_job_pool_pruned_total", reason="expired", value=len(expired))
    count("jobhunter_job_pool_entries_total", operation="pruned", value=len(rows))
    duration("jobhunter_job_pool_write_seconds", (datetime.utcnow() - started).total_seconds(),
             operation="prune")
    log.info("job pool: pruned %s entries (%s expired, %s deleted), %s aggregate rows",
             len(rows), len(expired), len(deleted), report["aggregated"])
    return report


# --------------------------------------------------------------------------- #
# Reading: pool entries as discovery candidates
# --------------------------------------------------------------------------- #
def live_entry_count(db: Session, *, now: Optional[datetime] = None) -> int:
    if not enabled():
        return 0
    now = now or datetime.utcnow()
    return int(
        db.query(func.count(JobPoolEntry.id))
        .filter(JobPoolEntry.expires_at > now, JobPoolEntry.expired.is_(False))
        .scalar() or 0
    )


def _board_keys(db: Session, user_id: int) -> Tuple[set[str], set[str]]:
    """Keys and content hashes the user's board already holds (for exclusion)."""
    keys = {
        str(row[0])
        for row in db.query(Job.dedupe_key).filter(Job.user_id == user_id, Job.dedupe_key != "").all()
        if row[0]
    }
    hashes = {
        str(row[0])
        for row in db.query(Job.content_hash)
        .filter(Job.user_id == user_id, Job.content_hash != "", Job.content_hash.isnot(None))
        .all()
        if row[0]
    }
    return keys, hashes


def _keyword_score(keywords: Sequence[str], entry: JobPoolEntry) -> float:
    """Cheap term overlap over title + description (no profile needed)."""
    if not keywords:
        return 0.0
    haystack = f"{entry.title or ''} {entry.description or ''}".lower()
    hits = sum(1 for word in keywords if word and str(word).lower() in haystack)
    return hits / max(1, len(keywords))


def candidates_for_user(
    db: Session,
    user_id: int,
    *,
    profile: Optional[Dict[str, Any]] = None,
    keywords: Sequence[str] = (),
    limit: int = 60,
    now: Optional[datetime] = None,
    scan: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Live pool entries the user does not have yet, best-first.

    Ranking is deterministic: with a profile the same pre-rank a live run uses
    (:func:`app.services.scoring.preliminary_score`), otherwise plain term
    overlap against the run's keywords, then recency. Pool entries are marked in
    ``extra["job_pool"]`` so the row that lands on the board says where it came
    from — a shared-pool match is never presented as a live fetch.
    """
    if not enabled() or limit <= 0:
        return []
    now = now or datetime.utcnow()
    scan = int(scan or max(limit * 4, 100))
    rows = (
        db.query(JobPoolEntry)
        .filter(JobPoolEntry.expires_at > now, JobPoolEntry.expired.is_(False))
        .order_by(JobPoolEntry.posted_at.desc().nullslast(), JobPoolEntry.last_seen_at.desc())
        .limit(max(1, scan))
        .all()
    )
    if not rows:
        return []
    board_keys, board_hashes = _board_keys(db, user_id)
    from app.services.scoring import preliminary_score  # noqa: PLC0415 - keep import graph flat

    scored: List[Tuple[float, JobPoolEntry]] = []
    for entry in rows:
        if entry.dedupe_key in board_keys:
            continue
        if entry.content_hash and entry.content_hash in board_hashes:
            continue
        if profile:
            try:
                score, _reason = preliminary_score(profile, entry.description or "")
            except Exception:  # pragma: no cover - a scoring failure must not hide a job
                score = _keyword_score(keywords, entry) * 100.0
        else:
            score = _keyword_score(keywords, entry) * 100.0
        scored.append((float(score), entry))
    scored.sort(key=lambda pair: (pair[0], pair[1].posted_at or datetime.min), reverse=True)
    out: List[Dict[str, Any]] = []
    for score, entry in scored[:limit]:
        out.append(_entry_to_candidate(entry, score))
    return out


def _entry_to_candidate(entry: JobPoolEntry, score: float) -> Dict[str, Any]:
    extra = entry.extra if isinstance(entry.extra, dict) else {}
    return {
        "title": entry.title,
        "company": entry.company,
        "company_name_normalized": entry.company_name_normalized or "",
        "location": entry.location or "",
        "description": entry.description or "",
        "url": entry.url or "",
        "source": entry.source or "pool",
        "external_id": entry.external_id or "",
        "source_kind": entry.source_kind or "",
        "content_hash": entry.content_hash or "",
        "title_normalized": entry.title_normalized or "",
        "posted_at": entry.posted_at,
        "salary": str(extra.get("salary") or ""),
        "remote": bool(extra.get("remote")),
        "industry": str(extra.get("industry") or ""),
        # Pool entries carry no raw source payload by design (the pool is an
        # index, not a second copy of every tenant's board).
        "raw": {},
        "dedupe_key": entry.dedupe_key,
        "canonical_id": entry.dedupe_key,
        "extra": {"job_pool": {"entry_id": entry.id, "first_seen_at": _iso(entry.first_seen_at),
                               "last_seen_at": _iso(entry.last_seen_at),
                               "times_seen": int(entry.times_seen or 1),
                               "users_seen": int(entry.users_seen or 0),
                               "retention_days": retention_days(),
                               "status": "pool"}},
        # The pool's own pre-rank; the run's scoring pass overwrites it with the
        # full verdict exactly like any other candidate.
        "score": float(score),
        "score_source": "preliminary",
    }


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


# --------------------------------------------------------------------------- #
# Instant match: a fresh board before the first fetch returns
# --------------------------------------------------------------------------- #
def instant_match(
    db: Session,
    user: User,
    *,
    profile: Optional[Dict[str, Any]] = None,
    keywords: Sequence[str] = (),
    limit: Optional[int] = None,
    persona_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Match a (new) user against the pool now — no network, no provider calls.

    Deterministic scoring only: the point is *latency*, and an honest badge
    matters more than an unverified AI number, so rows land with
    ``score_source="preliminary"`` and the run's normal live pass re-scores the
    top slice later. The board's storage cap (``jobs_max``) and the discovery
    quota are respected — a refusal is reported as ``skipped``, never raised,
    because "your board is full" must not fail an onboarding step.
    """
    now = now or datetime.utcnow()
    report: Dict[str, Any] = {
        "enabled": enabled(),
        "retention_days": retention_days(),
        "considered": 0,
        "inserted": 0,
        "skipped": None,
        "jobs": [],
    }
    if not enabled():
        report["skipped"] = "disabled"
        return report
    from app.core.entitlements import check_storage, enforce, increment_usage  # noqa: PLC0415

    try:
        if limit is None:
            limit = int(getattr(settings, "job_pool_instant_match_limit", 25) or 25)
        limit = max(0, int(limit))
        if limit <= 0:
            report["skipped"] = "disabled"
            return report
        allowed, used, cap, _hint = check_storage(db, user.id, "jobs_max")
        if not allowed:
            report["skipped"] = "jobs_cap_reached"
            report["jobs_cap"] = {"used": used, "limit": cap}
            return report
        headroom = limit if cap <= 0 else min(limit, max(0, cap - used))
        if headroom <= 0:
            report["skipped"] = "jobs_cap_reached"
            report["jobs_cap"] = {"used": used, "limit": cap}
            return report
        for gate in ("jobs_discovered_per_month", "jobs_discovered_per_day"):
            try:
                enforce(db, user.id, gate)
            except Exception:  # HTTPException(402) or a lookup failure
                report["skipped"] = "quota"
                return report
    except Exception as exc:  # noqa: BLE001 - onboarding never fails on a quota read
        log.warning("instant match: gate evaluation failed for user %s: %s", user.id, exc)
        report["skipped"] = "gate_error"
        return report

    candidates = candidates_for_user(db, user.id, profile=profile, keywords=keywords,
                                     limit=headroom, now=now)
    report["considered"] = len(candidates)
    if not candidates:
        report["skipped"] = "pool_empty"
        return report

    from app.services.events import record_job_event  # noqa: PLC0415
    from app.services.job_insert import build_job_row  # noqa: PLC0415

    created: List[Job] = []
    for candidate in candidates:
        row = build_job_row(
            candidate,
            user_id=int(user.id),
            freshness_hours=max(24 * retention_days(), 72),
            persona_id=persona_id,
            now=now,
            extra_merge={"job_pool": {**candidate["extra"]["job_pool"], "instant_match": True}},
        )
        db.add(row)
        try:
            db.commit()
        except Exception as exc:  # noqa: BLE001 - one bad row must not discard the batch
            db.rollback()
            log.warning("instant match insert failed for %s (%s): %s",
                        candidate.get("company"), candidate.get("title"), exc)
            continue
        created.append(row)
        count("jobhunter_job_pool_matches_total", outcome="created")

    report["inserted"] = len(created)
    if created:
        try:
            increment_usage(db, user.id, "jobs_discovered_per_month", len(created))
        except Exception as exc:  # pragma: no cover - accounting never blocks a board
            log.debug("instant match usage increment failed: %s", exc)
    for row in created:
        try:
            db.refresh(row)
            record_job_event(
                db, user_id=int(user.id), job_id=row.id, stage="discovered", status="success",
                message=f"Matched from the shared pool (score {row.score:.0f})",
                meta={"source": row.source, "origin": "job_pool"},
            )
        except Exception as exc:  # pragma: no cover - the trail must never fail the match
            log.debug("instant match event failed for job %s: %s", row.id, exc)
    report["jobs"] = [
        {"id": row.id, "title": row.title, "company": row.company, "source": row.source,
         "score": row.score, "location": row.location, "url": row.url}
        for row in created
    ]
    if not created:
        report["skipped"] = "nothing_new"
    inc("jobhunter_job_pool_instant_match_total", result="ok" if created else "empty")
    log.info("instant match: user %s got %s job(s) from the pool (%s considered)",
             user.id, len(created), len(candidates))
    return report


def maybe_instant_match(
    db: Session,
    user: User,
    *,
    profile_document: Optional[Dict[str, Any]] = None,
    keywords: Sequence[str] = (),
    persona_id: Optional[int] = None,
    trigger: str = "onboarding",
) -> Optional[Dict[str, Any]]:
    """Run the instant match once per profile, then queue the live run.

    Called when a profile becomes ``active`` (onboarding's last gate). Three
    properties the call sites rely on:

    * **Idempotent.** A per-user settings marker remembers which profile version
      was already matched, so polling or re-resolving a field never re-inserts
      jobs. The check is why this is safe to call from a request handler.
    * **Bounded.** The match is deterministic and pool-only; the *slow* half —
      the live source fan-out — is enqueued as the normal ``discovery`` item, so
      "instant matches now, full run in the background" is literally what
      happens.
    * **Failure-isolated.** Anything unexpected is logged and reported as
      ``skipped``; onboarding completion never depends on it.
    """
    if not enabled():
        return None
    from app.services.candidate_profile import get_current_profile  # noqa: PLC0415
    from app.services.user_settings import get_setting, set_setting  # noqa: PLC0415

    current = get_current_profile(db, int(user.id), persona_id=persona_id)
    if current is None:
        return None
    profile_id = int(current.id)
    document = profile_document if profile_document is not None else dict(current.document or {})
    fingerprint = _document_fingerprint(document)
    try:
        already = get_setting(db, int(user.id), "job_pool", "instant_match_profile", None)
        if isinstance(already, dict) and already.get("profile_id") == profile_id \
                and already.get("document") == fingerprint:
            return None
    except Exception as exc:  # pragma: no cover - a settings read must not block matching
        log.debug("instant match marker read failed for user %s: %s", user.id, exc)

    report = instant_match(db, user, profile=document or None, keywords=keywords,
                           persona_id=persona_id or current.persona_id)
    report["trigger"] = trigger
    report["profile_id"] = profile_id
    report["discovery_queued"] = False
    report["pipeline_job_id"] = None
    try:
        queued, pipeline_job_id = _queue_live_discovery(db, user, keywords=keywords,
                                                        persona_id=persona_id or current.persona_id)
        report["discovery_queued"] = queued
        report["pipeline_job_id"] = pipeline_job_id
    except Exception as exc:  # noqa: BLE001 - the background run is best-effort here
        log.warning("instant match: could not queue the live run for user %s: %s", user.id, exc)
    try:
        set_setting(db, int(user.id), "job_pool", "instant_match_profile",
                    {"profile_id": profile_id, "document": fingerprint,
                     "at": datetime.utcnow().isoformat()})
        set_setting(db, int(user.id), "job_pool", "last_instant_match",
                    {"at": datetime.utcnow().isoformat(), "inserted": report["inserted"],
                     "trigger": trigger, "profile_id": profile_id})
        db.commit()
    except Exception as exc:  # pragma: no cover
        db.rollback()
        log.warning("instant match marker write failed for user %s: %s", user.id, exc)
    return report


def maybe_instant_match_after_review(db: Session, user: User, profile_id: int,
                                    *, keywords: Sequence[str] = ()) -> Optional[Dict[str, Any]]:
    """Instant-match hook for the profile-review endpoints (call *after* commit).

    Review ends onboarding for most users: the last resolved field is what flips
    the profile to ``active``, and a correction changes the document that
    matching reads. The pool call itself is idempotent per (profile, document),
    so calling this on every review decision is cheap — one settings read when
    nothing changed — and never re-inserts a job the user already has.

    Failure-isolated by contract, like :func:`maybe_instant_match`: a review
    request must succeed even if the pool is unreachable.
    """
    try:
        from app.models.models import CandidateProfile  # noqa: PLC0415

        profile = (db.query(CandidateProfile)
                   .filter(CandidateProfile.id == profile_id,
                           CandidateProfile.user_id == int(user.id))
                   .first())
        if profile is None or profile.state != "active":
            return None
        return maybe_instant_match(db, user, profile_document=dict(profile.document or {}),
                                   keywords=keywords, persona_id=profile.persona_id,
                                   trigger="profile_active")
    except Exception as exc:  # noqa: BLE001 - matching is never allowed to fail a review
        log.warning("instant match after profile review failed for user %s: %s", user.id, exc)
        return None


def _queue_live_discovery(db: Session, user: User, *, keywords: Sequence[str],
                          persona_id: Optional[int]) -> Tuple[bool, Optional[int]]:
    """Queue the full live run that the instant match complements.

    Uses the same payload shape the ``POST /api/jobs/discover`` endpoint builds
    (``discovery_sources_config`` + the user's keyword settings), so the
    background half of "instant + live" is the product's own run — not a second,
    private code path.
    """
    from app.core.entitlements import enforce, increment_usage  # noqa: PLC0415
    from app.services.discovery import discovery_sources_config  # noqa: PLC0415
    from app.services.job_queue import enqueue_or_existing  # noqa: PLC0415
    from app.services.user_settings import get_setting  # noqa: PLC0415

    for gate in ("jobs_discovered_per_month", "jobs_discovered_per_day", "jobs_max"):
        enforce(db, int(user.id), gate)
    config = discovery_sources_config(db, int(user.id))
    run_keywords = [str(word) for word in keywords if str(word).strip()][:20]
    if not run_keywords:
        stored = get_setting(db, int(user.id), "scraping", "keywords", settings.default_keywords)
        if isinstance(stored, list):
            stored = ", ".join(str(item) for item in stored)
        run_keywords = [token.strip() for token in str(stored or "").split(",") if token.strip()][:20]
    increment_usage(db, int(user.id), "jobs_discovered_per_day", 1)
    item, duplicate = enqueue_or_existing(
        db,
        user_id=int(user.id),
        pipeline="discovery",
        payload={
            "keywords": run_keywords,
            "freshness_hours": int(get_setting(db, int(user.id), "scraping", "freshness_hours",
                                               settings.default_freshness_hours)),
            "limit": 30,
            "live_enabled": bool(config["live_enabled"]),
            "sources": list(config["sources"]),
            "board_tokens": list(config["board_tokens"]),
            "persona_id": persona_id,
            "trigger": "instant_match",
        },
        priority=3,
        dedupe_key=f"discovery:instant:{persona_id or 0}",
    )
    return (not duplicate), (item.id if item else None)


# --------------------------------------------------------------------------- #
# Coverage / metrics (owner-facing aggregates)
# --------------------------------------------------------------------------- #
def stats(db: Session, *, user_id: Optional[int] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Compact pool block for a discovery run report."""
    now = now or datetime.utcnow()
    if not enabled():
        return {"enabled": False}
    out: Dict[str, Any] = {
        "enabled": True,
        "retention_days": retention_days(),
        "live_entries": live_entry_count(db, now=now),
    }
    if user_id is not None:
        try:
            _, board_hashes = _board_keys(db, int(user_id))
            out["board_content_hashes"] = len(board_hashes)
        except Exception as exc:  # pragma: no cover
            log.debug("pool stats: board key read failed: %s", exc)
    return out


def metrics_snapshot(db: Session, *, window_days: int = 30,
                     now: Optional[datetime] = None) -> Dict[str, Any]:
    """Owner-only aggregates: what the pool holds, and what it no longer holds.

    The second half is the point of retention: a pruned or deleted posting keeps
    counters (how often it was seen, by how many users, how long it lived) and
    nothing else, so the operator can read market churn without the platform
    keeping a copy of a job posting that is gone.
    """
    now = now or datetime.utcnow()
    if not enabled():
        return {"enabled": False, "retention_days": retention_days()}
    window_start = now - timedelta(days=max(1, window_days))
    live_entries = live_entry_count(db, now=now)
    by_source_rows = (
        db.query(JobPoolEntry.source, func.count(JobPoolEntry.id))
        .filter(JobPoolEntry.expires_at > now, JobPoolEntry.expired.is_(False))
        .group_by(JobPoolEntry.source)
        .order_by(func.count(JobPoolEntry.id).desc())
        .limit(20)
        .all()
    )
    contributors = int(
        db.query(func.count(func.distinct(JobPoolSeen.user_id))).scalar() or 0
    )
    added_window = int(
        db.query(func.count(JobPoolEntry.id)).filter(JobPoolEntry.first_seen_at >= window_start).scalar() or 0
    )
    companies = int(
        db.query(func.count(func.distinct(JobPoolEntry.company_name_normalized)))
        .filter(JobPoolEntry.expires_at > now)
        .scalar() or 0
    )
    pruned_rows = (
        db.query(
            JobPoolMetric.reason,
            func.count(JobPoolMetric.id),
            func.coalesce(func.sum(JobPoolMetric.times_seen), 0),
            func.coalesce(func.max(JobPoolMetric.distinct_users), 0),
        )
        .group_by(JobPoolMetric.reason)
        .all()
    )
    pruned_total = int(
        db.query(func.count(JobPoolMetric.id)).filter(JobPoolMetric.last_seen_at >= window_start).scalar() or 0
    )
    churn = (
        db.query(JobPoolMetric.company_name_normalized, func.count(JobPoolMetric.id),
                 func.coalesce(func.sum(JobPoolMetric.times_seen), 0))
        .filter(JobPoolMetric.company_name_normalized != "")
        .group_by(JobPoolMetric.company_name_normalized)
        .order_by(func.count(JobPoolMetric.id).desc())
        .limit(10)
        .all()
    )
    return {
        "enabled": True,
        "retention_days": retention_days(),
        "live_entries": live_entries,
        "distinct_companies": companies,
        "contributors": contributors,
        "by_source": [{"source": source or "unknown", "entries": int(total)}
                      for source, total in by_source_rows],
        "window_days": window_days,
        "added_in_window": added_window,
        # Aggregates only — the posting content itself is gone.
        "gone": {
            "entries_in_window": pruned_total,
            "all_time_by_reason": [
                {"reason": reason, "entries": int(total), "times_seen": int(seen),
                 "max_distinct_users": int(users)}
                for reason, total, seen, users in pruned_rows
            ],
            "companies": [{"company": name, "entries": int(total), "times_seen": int(seen)}
                          for name, total, seen in churn],
        },
    }
