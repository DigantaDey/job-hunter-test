"""
Account, consent, privacy and audit endpoints.

Implements the data-subject rights a launch product needs: full export, hard
delete, consent capture with timestamps, and an audit trail for both.

The two data-rights endpoints share one constraint: an account's data is not
bounded by anything the user can see. The export therefore streams the document
in per-table chunks (keyset pagination, projections rather than entities)
instead of loading the whole account into memory, and the delete lets
:mod:`app.services.erasure` derive the FK-safe statement order from the schema
instead of trusting a hand-written table list.
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession, client_ip
from app.core import audit
from app.core.config import settings
from app.core.logging import get_logger
from app.db import SessionLocal
from app.models.models import (
    AICreditLedger,
    AuditLog,
    CompanyIntel,
    Email,
    EmailEvent,
    EmailOptOut,
    ErrorLog,
    FundingCompany,
    FundingScan,
    InterviewPrep,
    Job,
    JobEvent,
    Notification,
    Persona,
    PipelineJob,
    Profile,
    Resume,
    ResumeDocument,
    ScheduledRun,
    SettingsModel,
    Subscription,
    UsageCounter,
    UserInputRequest,
    VaultEntry,
)
from app.services.erasure import count_user_rows, purge_user_data

router = APIRouter(prefix="/account", tags=["account"])
log = get_logger("app.account")

#: Rows read per statement while an export collection is streamed. Small enough
#: that one chunk is a few hundred KB of dicts; large enough that an account
#: with 100k events is ~200 round-trips rather than 100k.
EXPORT_CHUNK_ROWS = 500

DISCLOSURES = {
    "terms": {
        "title": "Terms of service",
        "summary": "You are responsible for the accuracy of the profile data and resumes this "
                   "system submits on your behalf.",
    },
    "automation": {
        "title": "Automated applications",
        "summary": "Automation acts with your credentials on third-party job portals. Some portals "
                   "prohibit automated submissions; you confirm you have the right to use each "
                   "portal this way and accept that accounts may be limited.",
    },
    "outreach": {
        "title": "Cold outreach",
        "summary": "Sending mail to hiring contacts must comply with anti-spam law (CAN-SPAM/GDPR). "
                   "You confirm you will only contact people you may lawfully contact, that your "
                   "postal address and unsubscribe link are configured, and that you will honour "
                   "opt-outs.",
    },
    "data_processing": {
        "title": "Data processing",
        "summary": "Your resume, profile and credentials are stored encrypted at rest. Credentials "
                   "use a per-user encryption key. You can export or delete all data at any time.",
    },
}


class ConsentPayload(BaseModel):
    terms: Optional[bool] = None
    automation: Optional[bool] = None
    outreach: Optional[bool] = None
    data_processing: Optional[bool] = None


@router.get("/disclosures")
def disclosures():
    """Public: the exact text the user is asked to accept."""
    return {"disclosures": DISCLOSURES, "environment": settings.environment}


@router.get("/consent")
def get_consent(user: CurrentUser):
    consents = user.consents or {}
    return {
        "consents": consents,
        "granted": {key: bool(consents.get(f"{key}_accepted_at")) for key in DISCLOSURES},
    }


@router.post("/consent")
def set_consent(payload: ConsentPayload, request: Request, user: CurrentUser, db: DbSession):
    consents = dict(user.consents or {})
    now = datetime.utcnow().isoformat()
    for key, value in payload.model_dump(exclude_none=True).items():
        field = f"{key}_accepted_at"
        if value:
            consents[field] = consents.get(field) or now
            audit.audit(db, "consent.accepted", user=user, target=key, request=request, ip=client_ip(request))
        else:
            consents.pop(field, None)
            consents[f"{key}_revoked_at"] = now
            audit.audit(db, "consent.revoked", user=user, target=key, request=request, ip=client_ip(request))
    user.consents = consents
    db.commit()
    return {"ok": True, "consents": consents}


@router.get("/audit")
def list_audit(user: CurrentUser, db: DbSession, limit: int = 100, action: Optional[str] = None):
    query = db.query(AuditLog).filter(AuditLog.user_id == user.id)
    if action:
        query = query.filter(AuditLog.action == action)
    rows = query.order_by(AuditLog.created_at.desc()).limit(min(500, max(1, limit))).all()
    return [
        {"id": r.id, "action": r.action, "target": r.target, "detail": r.detail,
         "ip": r.ip, "request_id": r.request_id, "created_at": r.created_at}
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# Export: chunked, streamed, and complete
#
# ``payload = {table: [dict(row) for row in db.query(Model).filter(...).all()]}``
# is the natural way to write this endpoint and the wrong way to run it: a
# GDPR export has to serve *every* row the account owns, and a heavy account
# (a year of discovery and outreach) owns tens of thousands of events, ledger
# rows and emails. Loaded as entities, all of those land in the request's
# memory twice — once as ORM objects in the session identity map, once as the
# dicts built from them — and the export that is supposed to be the user's
# escape hatch becomes the request that OOMs the worker.
#
# So each collection is read through :func:`_rows`: only the columns the export
# renders are selected (a ``Row`` is not an entity, so nothing enters the
# identity map), and they are read by keyset pagination — ``id > last_id``
# rather than ``OFFSET``, which keeps the last chunk as cheap as the first and
# cannot skip or repeat a row when the account is being written to while its
# own export runs. The chunks are serialised into a single JSON document as
# they arrive, so the response is byte-identical in shape to the old one.
# --------------------------------------------------------------------------- #
def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _closing(session: Session, inner: Iterator[str]) -> Iterator[str]:
    """Drain ``inner``, then close ``session`` — including on a cancelled download."""
    try:
        yield from inner
    finally:
        session.close()


def _json(value: Any) -> str:
    """
    One JSON value, tolerant of the two things that used to break this endpoint.

    ``default=str`` keeps the exporter honest about a column it did not expect
    (datetimes, Decimal, JSON blobs) — an export that 500s is worse than one
    that stringifies. ``allow_nan=False`` refuses to emit ``NaN``, which is not
    valid JSON; a score written by a model as a float NaN then falls back to
    ``null`` rather than truncating the document mid-stream.
    """
    try:
        return json.dumps(value, default=str, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(_finite_only(value), default=str, ensure_ascii=False, separators=(",", ":"))


def _finite_only(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _finite_only(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_only(v) for v in value]
    return value


def _rows(db: Session, model, columns: Tuple, user_id: int, *, serialize,
          chunk: Optional[int] = None) -> Iterator[Dict[str, Any]]:
    """
    Yield ``serialize(row)`` for every row of ``model`` owned by ``user_id``.

    Chunked by primary key: the query is ``WHERE user_id = :uid AND id > :last
    ORDER BY id LIMIT :chunk``, so memory is bounded by ``chunk`` rows however
    many the account has, and the tables only need the ``(user_id, id)`` path
    they already have an index on.
    """
    size = int(chunk or EXPORT_CHUNK_ROWS)  # read per call, so it can be retuned
    projection = tuple(column for column in columns if column is not model.id)
    last_id = 0
    while True:
        rows = (
            db.query(model.id, *projection)
            .filter(model.user_id == user_id, model.id > last_id)
            .order_by(model.id)
            .limit(size)
            .all()
        )
        if not rows:
            return
        for row in rows:
            yield serialize(row)
        last_id = rows[-1][0]


def _vault_rows(db: Session, user_id: int, *, chunk: Optional[int] = None) -> Iterator[Dict[str, Any]]:
    """
    Vault entries, decrypted — the one collection that needs whole entities.

    :func:`app.services.vault.reveal_password` re-keys an entry still encrypted
    with a legacy global key and writes that back through the session, so this
    section cannot be a projection. The table is the account's own credential
    list (bounded by what a person can have logins for), so it is chunked for
    uniformity, and each entry is detached from the session once rendered —
    the request's own objects (the authenticated user) stay exactly where they
    were, which a blanket ``expunge_all`` would not have guaranteed.
    """
    from app.services.vault import VaultDecryptionError, reveal_password

    size = int(chunk or EXPORT_CHUNK_ROWS)
    last_id = 0
    while True:
        entries = (
            db.query(VaultEntry)
            .filter(VaultEntry.user_id == user_id, VaultEntry.id > last_id)
            .order_by(VaultEntry.id)
            .limit(size)
            .all()
        )
        if not entries:
            return
        for entry in entries:
            row: Dict[str, Any] = {"domain": entry.domain, "username": entry.username,
                                   "created_at": _iso(entry.created_at)}
            try:
                row["password"] = reveal_password(entry, db=db)
            except VaultDecryptionError as exc:
                # ``null`` + a named error, never ``""``: a blank password in a data
                # export is indistinguishable from a credential that was never set,
                # which is exactly how a rotated VAULT_KEY used to go unnoticed.
                row["password"] = None
                row["password_error"] = "undecryptable"
                log.error("GDPR export could not decrypt vault entry %s (%s): %s",
                          entry.id, entry.domain, exc)
            yield row
            db.expunge(entry)
        last_id = entries[-1].id


def _stream_json(header: Dict[str, Any],
                 sections: List[Tuple[str, Iterator[Dict[str, Any]]]]) -> Iterator[str]:
    """
    Emit the whole export document, one fragment at a time.

    Hand-assembled because ``json.dumps`` needs the complete object: each item is
    serialised the moment its row is read and dropped straight after, so the peak
    memory is one chunk of one collection instead of the account.
    """
    yield "{"
    for key, value in header.items():
        yield f"{_json(key)}:{_json(value)},"
    for index, (name, items) in enumerate(sections):
        yield f"{_json(name)}:["
        first = True
        for item in items:
            if not first:
                yield ","
            yield _json(item)
            first = False
        yield "]" if index == len(sections) - 1 else "],"
    yield "}"


@router.get("/export")
def export_account(request: Request, user: CurrentUser, db: DbSession):
    """
    GDPR-style machine-readable export of everything owned by the account.

    The document is *streamed*: every collection is a keyset-paginated
    projection read ``EXPORT_CHUNK_ROWS`` rows at a time, so a heavy account is
    served in bounded memory instead of loading all of it — twice — into one
    dict. The bytes on the wire are the same JSON document the old version built
    (``format_version`` is unchanged for that reason) with the collections it
    was missing added: personas, applications, interview prep, funding scans,
    company research, the scheduler's history, notifications and the whole
    billing side (subscription, monthly counters, the AI credit ledger). An
    export that silently omits tables is not "everything I have on you", which
    is the one promise this endpoint exists to keep.

    The audit row is written *before* the first byte goes out: a download the
    client abandons halfway is still a download that happened.
    """
    uid = user.id
    audit.audit(db, "account.exported", user=user, request=request, ip=client_ip(request))

    # Not ``db``: a ``StreamingResponse`` body is produced *after* the endpoint
    # returns, and FastAPI has already run the teardown of every dependency with
    # ``yield`` by then — the request's session is closed before the first chunk
    # is read. The download therefore opens, and closes, its own.
    stream_db = SessionLocal()

    header: Dict[str, Any] = {
        "exported_at": datetime.utcnow().isoformat(),
        "format_version": "2.0",
        "account": {"email": user.email, "name": user.name, "role": user.role,
                    "created_at": _iso(user.created_at), "consents": user.consents or {}},
    }
    # (key, rows) — the projections name only the columns the export renders.
    sections: List[Tuple[str, Iterator[Dict[str, Any]]]] = [
        ("profile", _rows(stream_db, Profile, (Profile.id, Profile.data, Profile.layout, Profile.created_at), uid,
                          serialize=lambda r: {"id": r.id, "data": r.data, "layout": r.layout,
                                              "created_at": _iso(r.created_at)})),
        ("personas", _rows(stream_db, Persona,
                           (Persona.id, Persona.name, Persona.target_role, Persona.is_active,
                            Persona.is_default, Persona.search_context, Persona.preferences,
                            Persona.memory, Persona.portrait, Persona.stats, Persona.created_at),
                           uid,
                           serialize=lambda r: {"id": r.id, "name": r.name, "target_role": r.target_role,
                                                "is_active": r.is_active, "is_default": r.is_default,
                                                "search_context": r.search_context,
                                                "preferences": r.preferences, "memory": r.memory,
                                                "portrait": r.portrait, "stats": r.stats,
                                                "created_at": _iso(r.created_at)})),
        ("resumes", _rows(stream_db, Resume,
                          (Resume.id, Resume.filename, Resume.type, Resume.status, Resume.tags,
                           Resume.created_at), uid,
                          serialize=lambda r: {"id": r.id, "filename": r.filename, "type": r.type,
                                               "status": r.status, "tags": r.tags,
                                               "created_at": _iso(r.created_at)})),
        ("jobs", _rows(stream_db, Job,
                       (Job.id, Job.title, Job.company, Job.url, Job.source, Job.status, Job.score,
                        Job.applied_at), uid,
                       serialize=lambda r: {"id": r.id, "title": r.title, "company": r.company,
                                             "url": r.url, "source": r.source, "status": r.status,
                                             "score": r.score, "applied_at": _iso(r.applied_at)})),
        ("job_events", _rows(stream_db, JobEvent,
                              (JobEvent.job_id, JobEvent.stage, JobEvent.status, JobEvent.message,
                               JobEvent.created_at), uid,
                              serialize=lambda r: {"job_id": r.job_id, "stage": r.stage,
                                                   "status": r.status, "message": r.message,
                                                   "created_at": _iso(r.created_at)})),
        # Decrypted for the owner: the export has to be usable, not merely
        # complete. (docs/COMPLIANCE.md — "vault (decrypted for the owner)".)
        ("vault", _vault_rows(stream_db, uid)),
        ("emails", _rows(stream_db, Email,
                         (Email.id, Email.to_email, Email.subject, Email.status, Email.sent_at,
                          Email.opens, Email.dry_run), uid,
                         serialize=lambda r: {"id": r.id, "to": r.to_email, "subject": r.subject,
                                              "status": r.status, "sent_at": _iso(r.sent_at),
                                              "opens": r.opens, "dry_run": r.dry_run})),
        ("email_events", _rows(stream_db, EmailEvent,
                               (EmailEvent.email_id, EmailEvent.kind, EmailEvent.detail,
                                EmailEvent.created_at), uid,
                               serialize=lambda r: {"email_id": r.email_id, "kind": r.kind,
                                                    "detail": r.detail,
                                                    "created_at": _iso(r.created_at)})),
        ("suppressions", _rows(stream_db, EmailOptOut, (EmailOptOut.email, EmailOptOut.reason), uid,
                               serialize=lambda r: {"email": r.email, "reason": r.reason})),
        ("applications", _rows(stream_db, UserInputRequest,
                               (UserInputRequest.job_id, UserInputRequest.fields,
                                UserInputRequest.status, UserInputRequest.created_at), uid,
                               serialize=lambda r: {"job_id": r.job_id, "fields": r.fields,
                                                     "status": r.status,
                                                     "created_at": _iso(r.created_at)})),
        ("interview_preps", _rows(stream_db, InterviewPrep,
                                  (InterviewPrep.job_id, InterviewPrep.job_title,
                                   InterviewPrep.company, InterviewPrep.questions,
                                   InterviewPrep.answers, InterviewPrep.status,
                                   InterviewPrep.created_at), uid,
                                  serialize=lambda r: {"job_id": r.job_id, "job_title": r.job_title,
                                                       "company": r.company, "questions": r.questions,
                                                       "answers": r.answers, "status": r.status,
                                                       "created_at": _iso(r.created_at)})),
        ("funding_companies", _rows(stream_db, FundingCompany,
                                    (FundingCompany.name, FundingCompany.stage,
                                     FundingCompany.source, FundingCompany.verified,
                                     FundingCompany.raised_at, FundingCompany.website), uid,
                                    serialize=lambda r: {"name": r.name, "stage": r.stage,
                                                         "source": r.source, "verified": r.verified,
                                                         "raised_at": _iso(r.raised_at),
                                                         "website": r.website})),
        ("funding_scans", _rows(stream_db, FundingScan,
                                (FundingScan.scanned_at, FundingScan.status,
                                 FundingScan.companies_found, FundingScan.events_seen), uid,
                                serialize=lambda r: {"scanned_at": _iso(r.scanned_at),
                                                     "status": r.status,
                                                     "companies_found": r.companies_found,
                                                     "events_seen": r.events_seen})),
        ("company_intel", _rows(stream_db, CompanyIntel,
                                (CompanyIntel.company, CompanyIntel.website, CompanyIntel.industry,
                                 CompanyIntel.summary, CompanyIntel.updated_at), uid,
                                serialize=lambda r: {"company": r.company, "website": r.website,
                                                     "industry": r.industry, "summary": r.summary,
                                                     "updated_at": _iso(r.updated_at)})),
        ("settings", _rows(stream_db, SettingsModel,
                           (SettingsModel.category, SettingsModel.key, SettingsModel.value), uid,
                           serialize=lambda r: {"category": r.category, "key": r.key,
                                                # The one secret the settings table can hold.
                                                "value": "***" if r.category == "email" and
                                                         r.key == "password" else r.value})),
        ("pipeline_jobs", _rows(stream_db, PipelineJob,
                                (PipelineJob.pipeline, PipelineJob.status, PipelineJob.dedupe_key,
                                 PipelineJob.created_at), uid,
                                serialize=lambda r: {"pipeline": r.pipeline, "status": r.status,
                                                     "dedupe_key": r.dedupe_key,
                                                     "created_at": _iso(r.created_at)})),
        ("scheduled_runs", _rows(stream_db, ScheduledRun,
                                 (ScheduledRun.workflow, ScheduledRun.state,
                                  ScheduledRun.triggered_at, ScheduledRun.reason), uid,
                                 serialize=lambda r: {"workflow": r.workflow, "state": r.state,
                                                       "triggered_at": _iso(r.triggered_at),
                                                       "reason": r.reason})),
        ("notifications", _rows(stream_db, Notification,
                                (Notification.kind, Notification.title, Notification.body,
                                 Notification.link, Notification.read, Notification.created_at), uid,
                                serialize=lambda r: {"kind": r.kind, "title": r.title, "body": r.body,
                                                     "link": r.link, "read": r.read,
                                                     "created_at": _iso(r.created_at)})),
        ("subscriptions", _rows(stream_db, Subscription,
                                (Subscription.plan, Subscription.status, Subscription.provider,
                                 Subscription.current_period_start, Subscription.current_period_end,
                                 Subscription.trial_end, Subscription.cancel_at_period_end), uid,
                                serialize=lambda r: {"plan": r.plan, "status": r.status,
                                                     "provider": r.provider,
                                                     "current_period_start": _iso(r.current_period_start),
                                                     "current_period_end": _iso(r.current_period_end),
                                                     "trial_end": _iso(r.trial_end),
                                                     "cancel_at_period_end": r.cancel_at_period_end})),
        ("usage_counters", _rows(stream_db, UsageCounter,
                                 (UsageCounter.period, UsageCounter.capability, UsageCounter.count,
                                  UsageCounter.limit), uid,
                                 serialize=lambda r: {"period": r.period, "capability": r.capability,
                                                      "count": r.count, "limit": r.limit})),
        ("ai_credit_ledger", _rows(stream_db, AICreditLedger,
                                   (AICreditLedger.workflow, AICreditLedger.model,
                                    AICreditLedger.prompt_tokens, AICreditLedger.completion_tokens,
                                    AICreditLedger.total_tokens, AICreditLedger.estimated_cost_usd,
                                    AICreditLedger.success, AICreditLedger.created_at), uid,
                                   serialize=lambda r: {"workflow": r.workflow, "model": r.model,
                                                        "prompt_tokens": r.prompt_tokens,
                                                        "completion_tokens": r.completion_tokens,
                                                        "total_tokens": r.total_tokens,
                                                        "estimated_cost_usd": r.estimated_cost_usd,
                                                        "success": r.success,
                                                        "created_at": _iso(r.created_at)})),
        ("logs", _rows(stream_db, ErrorLog,
                       (ErrorLog.pipeline, ErrorLog.level, ErrorLog.message, ErrorLog.timestamp), uid,
                       serialize=lambda r: {"pipeline": r.pipeline, "level": r.level,
                                           "message": r.message, "timestamp": _iso(r.timestamp)})),
        ("audit", _rows(stream_db, AuditLog,
                        (AuditLog.action, AuditLog.target, AuditLog.created_at), uid,
                        serialize=lambda r: {"action": r.action, "target": r.target,
                                             "created_at": _iso(r.created_at)})),
    ]
    filename = f"jobhunter-export-{uid}-{datetime.utcnow().strftime('%Y%m%d')}.json"
    return StreamingResponse(_closing(stream_db, _stream_json(header, sections)),
                             media_type="application/json",
                             headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.delete("")
def delete_account(request: Request, user: CurrentUser, db: DbSession, confirm: str = ""):
    """
    Hard-delete the account and every row it owns. Irreversible.

    Two rules the previous version broke, both of which failed in the wrong
    direction for a data subject exercising their right to erasure:

    * **no leftover row.** The child tables are enumerated from the schema (see
      :mod:`app.services.erasure`) rather than from a list maintained next to
      this function, so a table added to ``models.py`` cannot be forgotten here.
      A forgotten one aborted the whole erasure with an ``IntegrityError`` — a
      500 on the one endpoint the law says must work, and an account still
      sitting in the middle of its own deletion.
    * **no file loss before the data is gone.** Uploads and generated documents
      are removed *after* the transaction commits, best-effort and logged. The
      other way round, a failed commit left the account intact with its resumes
      already deleted from disk — data destroyed that the user had not, in the
      end, managed to delete.
    * **verified, not assumed.** What the transaction left behind is counted
      against the same derived table list; anything still owned is an ERROR log
      and an audit field, so a gap shows up as an incident rather than as a
      complaint months later.
    """
    if confirm != user.email:
        raise HTTPException(400, {"code": "confirmation_required",
                                  "message": "Pass ?confirm=<your email> to delete the account permanently"})

    user_id = user.id
    # The paths have to be read while the rows exist; the removal waits for the commit.
    paths = _account_files(db, user_id)
    try:
        erased = purge_user_data(db, user_id)
        db.commit()
    except IntegrityError as exc:
        # The transaction rolled back, so the account is intact and nothing on
        # disk was touched — that is the whole point of ordering it this way.
        db.rollback()
        log.error("account erasure for user %s was refused by the database: %s", user_id, exc)
        raise HTTPException(409, {"code": "deletion_blocked",
                                  "message": "Some rows still reference this account and could not be "
                                             "removed automatically. The deletion was not applied — "
                                             "contact support with this account's email."}) from exc

    # The plan is derived, which is what makes it verifiable: count what is left
    # and an unorderable future reference becomes an incident in the log instead
    # of a user discovering weeks later that their data is still sitting there.
    leftover = count_user_rows(db, user_id)
    if leftover:
        log.error("account erasure for user %s left rows behind: %s", user_id, sorted(leftover.items()))

    files_removed = _remove_files(paths)
    detail: Dict[str, Any] = {"email": user.email, "rows_removed": erased.rows_removed,
                              "references_detached": erased.references_detached,
                              "files_removed": files_removed}
    if leftover:
        detail["leftover_tables"] = sorted(leftover)
    audit.audit(db, "account.deleted", user_id=None, target=f"user:{user_id}",
                detail=detail, request=request, ip=client_ip(request))
    # The counts are not decoration: "everything was deleted" is a claim the
    # endpoint should be able to make about the transaction it just committed,
    # and a data subject (or support, hours later) can compare it against the
    # audit row and the export they downloaded.
    return {"ok": True, "deleted": True, "message": "Account and all associated data deleted",
            "rows_removed": erased.rows_removed, "tables": len(erased.deleted),
            "files_removed": files_removed}


def _account_files(db: Session, user_id: int) -> List[str]:
    """
    Everything on disk that belongs to this account: resume documents and the
    screenshots the autofill runs took.

    A resume row stores its own path, so that half is a column read — ``filepath``
    plus the rendered sibling, because a generated resume is a DOCX with a PDF next
    to it and both belong to the account, not to the folder.

    Screenshots are not rows at all. :mod:`app.services.apply_flow` writes
    ``{screenshot_dir}/job_{job_id}_{utc}.png`` and records the path only inside a
    JSON blob, so the tenant key those files have is the *job id* in their name.
    Listing the directory once against the account's job ids is therefore the
    honest way to find them — a per-job glob would be one readdir per application,
    and a ``job_*`` wildcard would delete other accounts' screenshots, which is a
    worse bug than the one being fixed. Without this, "deleted forever" quietly
    left screenshots of a person's filled-in application forms on the server.
    """
    paths: set[str] = set()
    for (filepath,) in db.execute(select(Resume.filepath).where(Resume.user_id == user_id)):
        if not filepath:
            continue
        paths.add(filepath)
        paths.add(os.path.splitext(filepath)[0] + ".pdf")

    # Onboarding resume documents: the original upload is stored once and shared
    # with its master Resume row, but an extraction that never finished has no
    # Resume row — the document row is then the only record of the file.
    for (filepath,) in db.execute(select(ResumeDocument.filepath).where(ResumeDocument.user_id == user_id)):
        if not filepath:
            continue
        paths.add(filepath)
        paths.add(os.path.splitext(filepath)[0] + ".pdf")

    job_ids = {row[0] for row in db.execute(select(Job.id).where(Job.user_id == user_id))}
    if job_ids and os.path.isdir(settings.screenshot_dir):
        owned = re.compile(r"^job_(\d+)_")
        try:
            for name in os.listdir(settings.screenshot_dir):
                match = owned.match(name)
                if match and int(match.group(1)) in job_ids:
                    paths.add(os.path.join(settings.screenshot_dir, name))
        except OSError as exc:  # pragma: no cover - unreadable directory
            log.error("account erasure could not list %s: %s", settings.screenshot_dir, exc)
    return sorted(paths)


def _remove_files(paths: List[str]) -> int:
    """Delete the account's files after the erasure committed: best effort, logged, never fatal."""
    removed = 0
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
                removed += 1
        except OSError as exc:  # pragma: no cover - depends on the filesystem
            log.error("account erasure could not delete %s: %s", path, exc)
    return removed


@router.get("/runtime")
def runtime_settings(user: CurrentUser, db: DbSession):
    """Effective (non-secret) platform configuration for the UI's status panel."""
    from app.services.autofill import autofill_available
    from app.services.funding_sources import provider_status
    from app.services.sources import list_sources

    return {
        **settings.public_settings(),
        "autofill_runtime": autofill_available(),
        "funding_providers": provider_status(),
        "sources": list_sources(),
        "consents": {key: bool((user.consents or {}).get(f"{key}_accepted_at")) for key in DISCLOSURES},
    }


@router.get("/billing-usage")
def usage(user: CurrentUser, db: DbSession):
    """Simple usage counters (the basis for plan limits / billing)."""
    from app.services.ai_client import usage_snapshot
    from app.services.outreach import utc_day_bounds

    # Same UTC calendar day the outreach daily cap is measured over — one
    # definition of "today" for the whole app, not one per call site.
    day_start, day_end = utc_day_bounds()
    return {
        "jobs": {
            "total": db.query(Job).filter(Job.user_id == user.id).count(),
            "today": db.query(Job).filter(Job.user_id == user.id, Job.discovered_at >= day_start).count(),
            "applied": db.query(Job).filter(Job.user_id == user.id, Job.status == "applied").count(),
        },
        "emails": {
            "sent_today": db.query(Email).filter(Email.user_id == user.id, Email.status == "sent",
                                                 Email.sent_at >= day_start,
                                                 Email.sent_at < day_end).count(),
            "pending_approval": db.query(Email).filter(Email.user_id == user.id,
                                                       Email.status == "pending_approval").count(),
        },
        "resumes": db.query(Resume).filter(Resume.user_id == user.id).count(),
        "vault_entries": db.query(VaultEntry).filter(VaultEntry.user_id == user.id).count(),
        "ai_tokens": usage_snapshot(),
    }

