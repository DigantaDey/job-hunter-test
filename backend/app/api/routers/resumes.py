"""Resume, profile and search-context endpoints — with monetization."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import shutil
import time
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import CurrentUser, DbSession, get_owned_profile, invalidate_context, search_context
from app.core import audit
from app.core.config import settings
from app.core.entitlements import enforce, increment_usage
from app.core.logging import get_logger
from app.core.security import constant_time_equals
from app.models.models import Job, Profile, Resume
from app.schemas.schemas import ProfileOut, ResumeOut
from app.services import persona as persona_service
from app.services.job_queue import enqueue
from app.services.resume_generator import generate_tailored_profile
from app.services.resume_parser import ai_extract_profile, extract_layout, extract_text
from app.services.resume_service import (
    build_and_save_resume,
    create_polished_resume,
    diff_text,
    download_filename,
    fact_guard_check,
    render_profile_text,
    resume_preview,
    resume_text,
)
from app.services.user_settings import get_setting

router = APIRouter(tags=["resumes"])
log = get_logger("app.resumes")

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc"}
MAGIC_BYTES = {
    b"%PDF": ".pdf",
    b"PK\x03\x04": ".docx",
    b"\xd0\xcf\x11\xe0": ".doc",
}

# --------------------------------------------------------------------------- #
# Real-time upload progress (in-memory per user, polled by the SPA)
# --------------------------------------------------------------------------- #
import threading as _threading

_upload_progress: dict = {}
_upload_lock = _threading.Lock()

def _set_progress(user_id: int, step: str, detail: str = "", progress: int = 0, extra: dict | None = None):
    with _upload_lock:
        _upload_progress[user_id] = {
            "step": step,
            "detail": detail,
            "progress": max(0, min(100, progress)),
            "updated_at": datetime.utcnow().isoformat(),
            **(extra or {}),
        }

def _clear_progress(user_id: int):
    with _upload_lock:
        _upload_progress.pop(user_id, None)

def _get_progress(user_id: int):
    with _upload_lock:
        return dict(_upload_progress.get(user_id, {"step": "idle", "progress": 0, "detail": ""}))

#: Lifetime of a signed download URL. Long enough to survive a browser
#: "save as" dialog, short enough that a leaked link is useless soon after.
DOWNLOAD_TOKEN_TTL_SECONDS = 900


def _validate_upload(file: UploadFile, head: bytes) -> str:
    if not file.filename:
        raise HTTPException(400, "A file name is required")
    extension = os.path.splitext(file.filename)[1].lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, f"Unsupported file type '{extension}' — upload PDF or DOCX")
    detected = next((ext for magic, ext in MAGIC_BYTES.items() if head.startswith(magic)), None)
    if detected is None:
        raise HTTPException(415, "File content does not look like a PDF or Word document")
    if detected == ".pdf" and extension not in (".pdf",):
        raise HTTPException(415, "File extension does not match its content")
    return extension


_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def safe_upload_name(name: str, fallback_extension: str = ".pdf") -> str:
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = _UNSAFE_FILENAME_CHARS.sub("_", base)
    base = re.sub(r"_{2,}", "_", base)
    base = re.sub(r"\.{2,}", ".", base).lstrip("._") or "resume"
    stem, extension = os.path.splitext(base)
    stem = (stem.strip("._") or "resume")[:60]
    extension = extension.lower() if extension.lower() in ALLOWED_EXTENSIONS else fallback_extension
    return f"{stem}{extension}"


def _store_upload(file: UploadFile, extension: str = ".pdf") -> tuple[str, str]:
    os.makedirs(settings.upload_dir, exist_ok=True)
    safe_name = safe_upload_name(file.filename or "resume.pdf", extension)
    stored = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}_{safe_name}"
    path = os.path.join(settings.upload_dir, stored)
    if os.path.commonpath([os.path.realpath(path), os.path.realpath(settings.upload_dir)]) != os.path.realpath(settings.upload_dir):
        raise HTTPException(400, "Invalid file name")
    with open(path, "wb") as handle:
        shutil.copyfileobj(file.file, handle)
    return path, safe_name


# --------------------------------------------------------------------------- #
# Signed download URLs
#
# The SPA downloads with a plain <a href>, which carries no Authorization
# header — that is exactly why /download used to answer {"detail":"Not
# authenticated"}. The UI now asks for a short-lived, per-resume signed URL and
# the browser can follow it directly.
# --------------------------------------------------------------------------- #
def _sign_download(resume_id: int, user_id: int, fmt: str, expires: int) -> str:
    message = f"{resume_id}:{user_id}:{fmt}:{expires}".encode()
    return hmac.new(settings.secret_key.encode(), message, hashlib.sha256).hexdigest()


def create_download_token(resume_id: int, user_id: int, fmt: str = "pdf",
                          ttl: int = DOWNLOAD_TOKEN_TTL_SECONDS) -> str:
    expires = int(time.time()) + max(30, ttl)
    return f"{expires}.{_sign_download(resume_id, user_id, fmt, expires)}"


def verify_download_token(token: str, resume_id: int, user_id: int, fmt: str) -> bool:
    try:
        raw_expires, signature = str(token).split(".", 1)
        expires = int(raw_expires)
    except (ValueError, AttributeError):
        return False
    if expires < int(time.time()):
        return False
    expected = _sign_download(resume_id, user_id, fmt, expires)
    return constant_time_equals(expected, signature)


@router.get("/resumes/{resume_id}/download-url")
def download_url(resume_id: int, user: CurrentUser, db: DbSession, format: str = "pdf"):
    """Issue a short-lived signed URL the browser can download directly."""
    fmt = format.lower()
    if fmt not in ("pdf", "docx"):
        raise HTTPException(400, "format must be 'pdf' or 'docx'")
    resume = db.query(Resume).filter(Resume.id == resume_id, Resume.user_id == user.id).first()
    if not resume:
        raise HTTPException(404, "Resume not found")
    token = create_download_token(resume.id, user.id, fmt)
    return {
        "url": f"/api/resumes/{resume.id}/download?format={fmt}&token={token}",
        "filename": download_filename(resume, fmt),
        "expires_in": DOWNLOAD_TOKEN_TTL_SECONDS,
        "available_formats": _available_formats(resume),
    }


def _available_formats(resume: Resume) -> List[str]:
    available = []
    docx = resume.filepath
    pdf = os.path.splitext(resume.filepath)[0] + ".pdf"
    if docx and os.path.exists(docx):
        available.append("docx")
    if os.path.exists(pdf):
        available.append("pdf")
    return available


@router.get("/resume/progress")
def resume_upload_progress(user: CurrentUser):
    """Polled by the SPA while a resume upload is in flight — gives real-time steps."""
    return _get_progress(user.id)


@router.get("/resume/recent-ledger")
def resume_recent_ledger(user: CurrentUser, db: DbSession, limit: int = 5):
    """Last few AI ledger rows for the parse workflow — powers the live status feed."""
    from app.models.models import AICreditLedger
    rows = (db.query(AICreditLedger)
            .filter(AICreditLedger.user_id == user.id, AICreditLedger.workflow == "parse")
            .order_by(AICreditLedger.created_at.desc())
            .limit(min(20, max(1, limit)))
            .all())
    return [
        {
            "id": r.id,
            "workflow": r.workflow,
            "model": r.model,
            "total_tokens": r.total_tokens,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "cost_usd": r.estimated_cost_usd,
            "success": r.success,
            "latency_ms": r.latency_ms,
            "error": r.error,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.post("/resume/upload")
async def upload_resume(request: Request, user: CurrentUser, db: DbSession, file: UploadFile = File(...)):
    """
    Upload the master resume: deterministic text + layout extraction, then an
    AI-extracted profile under guardrails.

    AI is required for the profile: if the model is unreachable this returns 503
    with the exact reason instead of storing a regex-guessed profile that later
    produces resumes and emails addressed to "Unknown".

    Real-time: pushes step updates to ``GET /api/resume/progress`` so the UI can
    show what is happening while the request is in flight.
    """
    enforce(db, user.id, "resume_parses_per_month")
    enforce(db, user.id, "resumes_max")

    _set_progress(user.id, "validating", "Checking file type…", 5)

    head = await file.read(8)
    await file.seek(0)
    extension = _validate_upload(file, head)

    path, safe_name = _store_upload(file, extension)
    _set_progress(user.id, "uploaded", f"Saved {safe_name}", 15)

    # Use a guard so an AI or DB failure never leaves an orphan file with no DB row.
    # The file is only kept when the resume row has been committed.
    keep_file = False
    try:
        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb > settings.max_upload_mb:
            raise HTTPException(413, f"File is {size_mb:.1f}MB — the limit is {settings.max_upload_mb}MB")

        _set_progress(user.id, "extracting", "Extracting text & layout…", 30)
        text = extract_text(path)
        if not text.strip():
            raise HTTPException(422, "No readable text found in the document (is it a scanned image?)")

        _set_progress(user.id, "layout", "Detecting layout…", 40)
        layout = extract_layout(path) if safe_name.lower().endswith(".pdf") else {
            "bullet_style": "•", "colors": [], "fonts": ["Calibri"], "estimated": True}

        _set_progress(user.id, "ai_parsing", "Contacting AI to extract structured profile…", 55, extra={"model": "parsing"})
        # AI-required. ``AIUnavailableError`` bubbles to the 503 handler with the
        # provider's own diagnosis (no key / bad key / unreachable / breaker open…).
        try:
            profile_data, meta = await ai_extract_profile(text, ai_config=None, db=db, user_id=user.id)
        except Exception as exc:
            # Surface the progress as failed before the exception propagates to the
            # global handler (which returns the correct status + diagnosis).
            _set_progress(user.id, "failed", f"AI extraction failed: {str(exc)[:180]}", 0, extra={"error": str(exc)[:500]})
            raise
        profile_data["raw_text"] = text[:20000]
        _set_progress(user.id, "ai_done", "AI extraction succeeded — validating…", 80, extra={"ai_meta": meta})

        _set_progress(user.id, "saving", "Saving resume & profile…", 90)
        resume = Resume(
            user_id=user.id,
            filename=safe_name,
            filepath=path,
            type="master",
            status="approved",
            profile_snapshot=profile_data,
            layout=layout,
            tags=["master"],
            display_name=safe_name,
            text_snapshot=render_profile_text(profile_data),
            approved_at=datetime.utcnow(),
        )
        db.add(resume)
        db.commit()
        db.refresh(resume)
        keep_file = True  # file is now owned by a DB row

        db.query(Profile).filter(Profile.user_id == user.id).delete(synchronize_session=False)
        profile = Profile(user_id=user.id, data=profile_data, layout=layout, master_resume_id=resume.id,
                          extraction_source="ai")
        db.add(profile)
        db.commit()
        db.refresh(profile)

        invalidate_context(user.id)
        _set_progress(user.id, "context", "Building search context…", 95)
        context = await search_context(db, user.id, force_refresh=True)
        persona = persona_service.ensure_default_persona(db, user.id, profile_data)
        audit.audit(
            db,
            "resume.uploaded",
            user=user,
            target=safe_name,
            detail={"resume_id": resume.id, "size_kb": round(os.path.getsize(path) / 1024, 1),
                    "extraction": meta.get("source") if isinstance(meta, dict) else None,
                    "guardrail": (meta or {}).get("guardrail", {}).get("passed")},
            request=request,
        )

        enqueue(db, user_id=user.id, pipeline="ai", payload={"task": "tag_resume", "resume_id": resume.id},
                priority=4, dedupe_key=f"tag_resume:{resume.id}")

        # Re-rank previously unscored jobs against the fresh profile. These are
        # explicitly labelled "preliminary" — the real verdict comes from the AI
        # scorer (see /api/jobs/{id}/intelligence).
        from app.services.scoring import heuristic_score_detailed

        rescored = 0
        for job in db.query(Job).filter(Job.user_id == user.id, Job.score == 0).limit(50).all():
            detail = heuristic_score_detailed(profile_data, job.description or "")
            job.score = float(detail["overall"])
            job.score_reason = detail["reason"]
            job.score_source = "preliminary"
            job.score_detail = detail
            rescored += 1
        if rescored:
            db.commit()

        increment_usage(db, user.id, "resume_parses_per_month", 1)

        _set_progress(user.id, "done", f"Profile extracted ({len(profile_data.get('skills', []))} skills, {len(profile_data.get('experience', []))} roles)", 100,
                      extra={"resume_id": resume.id})
        return {
            "resume": {"id": resume.id, "filename": resume.filename, "tags": resume.tags, "status": resume.status},
            "profile": profile_data,
            "layout": layout,
            "ai_meta": meta,
            "search_context": context,
            "persona": persona_service.to_dict(persona) if persona else None,
            "rescored_jobs": rescored,
        }
    except HTTPException as exc:
        # Validation errors (415, 413, 422) already removed file above or need removal here
        if not keep_file and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
        # Don't overwrite the detailed AI failure progress set above
        if _get_progress(user.id).get("step") not in ("failed",):
            detail = exc.detail if hasattr(exc, "detail") else str(exc)
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("detail") or str(detail)
            _set_progress(user.id, "failed", str(detail)[:200], 0)
        raise
    except Exception as exc:
        if not keep_file and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
        # If the exception is already a handled AI error (AIUnavailableError etc.) it will be
        # mapped to 503/422 by the global handler — still mark progress as failed.
        if _get_progress(user.id).get("step") not in ("failed",):
            _set_progress(user.id, "failed", f"Unexpected error: {str(exc)[:200]}", 0, extra={"error": str(exc)[:500]})
        raise


@router.get("/profile", response_model=List[ProfileOut])
def list_profiles(user: CurrentUser, db: DbSession):
    return db.query(Profile).filter(Profile.user_id == user.id).order_by(Profile.created_at.desc()).all()


@router.get("/profile/current")
def current_profile(user: CurrentUser, db: DbSession):
    profile = get_owned_profile(db, user.id)
    if not profile:
        raise HTTPException(404, "Upload a resume to create your profile")
    return {"id": profile.id, "data": profile.data, "layout": profile.layout,
            "extraction_source": profile.extraction_source or "ai",
            "created_at": profile.created_at}


@router.put("/profile/{profile_id}")
def update_profile(profile_id: int, data: dict, user: CurrentUser, db: DbSession):
    profile = db.query(Profile).filter(Profile.id == profile_id, Profile.user_id == user.id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")
    profile.data = data
    profile.extraction_source = "user_edited"
    db.commit()
    invalidate_context(user.id)
    return {"ok": True}


@router.get("/context/keywords")
async def keywords_context(user: CurrentUser, db: DbSession, force: bool = False,
                           persona_id: Optional[int] = None):
    """
    Search keywords: AI-extracted from the resume, plus whatever the user added.

    The extracted set is the base and is never replaced by the editable field —
    the user's extra terms are appended, and nothing generic is prefilled.
    """
    context = await search_context(db, user.id, force_refresh=force)
    if persona_id:
        persona = persona_service.get_persona(db, user.id, persona_id)
        context = persona_service.context_for_persona(context, persona)

    user_keywords = get_setting(db, user.id, "scraping", "keywords", settings.default_keywords) or []
    extracted = [str(k) for k in (context.get("keywords") or []) if str(k).strip()]
    merged = list(dict.fromkeys(extracted + [str(k).strip() for k in user_keywords if str(k).strip()]))
    context["extracted_keywords"] = extracted
    context["user_keywords"] = [str(k) for k in user_keywords]
    context["keywords"] = merged
    context["profile_id"] = context.get("profile_id")
    return context


# --------------------------------------------------------------------------- #
# Resumes
# --------------------------------------------------------------------------- #
@router.get("/resumes", response_model=List[ResumeOut])
def list_resumes(user: CurrentUser, db: DbSession):
    return db.query(Resume).filter(Resume.user_id == user.id).order_by(Resume.created_at.desc()).all()


@router.get("/resumes/{resume_id}")
def get_resume(resume_id: int, user: CurrentUser, db: DbSession):
    resume = db.query(Resume).filter(Resume.id == resume_id, Resume.user_id == user.id).first()
    if not resume:
        raise HTTPException(404, "Resume not found")
    return resume_preview(resume)


@router.post("/resumes/generate")
async def generate_resume(
    request: Request,
    user: CurrentUser,
    db: DbSession,
    job_id: int = Query(...),
    strict_skeleton: bool = False,
    persona_id: Optional[int] = None,
):
    """
    Generate a tailored resume for a job (starts ``pending`` until approved).

    AI is required. If the model is offline the caller gets a 503 explaining
    exactly why; if the model's draft cannot be made accurate the caller gets a
    422 listing the guardrail violations. Nothing half-made is ever saved.
    """
    enforce(db, user.id, "can_generate_resume")
    enforce(db, user.id, "tailored_resumes_per_month")

    job = db.query(Job).filter(Job.id == job_id, Job.user_id == user.id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    profile = get_owned_profile(db, user.id)
    if not profile:
        raise HTTPException(400, "Upload a master resume first")
    if not (job.description or "").strip():
        raise HTTPException(422, "This job has no description — paste the JD into the job first")

    persona = persona_service.get_persona(db, user.id, persona_id) or persona_service.ensure_default_persona(db, user.id)
    persona_context = persona_service.memory_summary(persona) if persona else {}
    strict = bool(strict_skeleton or get_setting(db, user.id, "general", "strict_skeleton", False))

    tailored = await generate_tailored_profile(
        profile.data or {}, job.description or "", profile.layout or {}, strict,
        db=db, user_id=user.id,
        persona={"name": persona.name if persona else "", "target_role": persona.target_role if persona else "",
                 "observed_skills": list((persona_context.get("observed_skills") or {}).keys())[:12],
                 "observed_titles": list((persona_context.get("observed_titles") or {}).keys())[:8]}
        if persona else None,
    )
    guard = fact_guard_check(profile.data or {}, tailored.get("tailored_profile") or {})
    if not guard["passed"]:
        raise HTTPException(422, {"code": "fact_guard_failed",
                                  "message": "The tailored resume contained facts not present in your profile.",
                                  "violations": guard["violations"]})

    resume = build_and_save_resume(db, user_id=user.id, profile=profile, job=job, tailored=tailored,
                                   strict_skeleton=strict, status="pending",
                                   persona_id=persona.id if persona else None)

    audit.audit(db, "resume.generated", user=user, target=resume.filename,
                detail={"job_id": job.id, "resume_id": resume.id, "fact_guard": guard,
                        "guardrail": (tailored.get("guardrail") or {}).get("passed"),
                        "persona_id": resume.persona_id}, request=request)
    enqueue(db, user_id=user.id, pipeline="ai", payload={"task": "tag_resume", "resume_id": resume.id},
            priority=4, dedupe_key=f"tag_resume:{resume.id}")
    persona_service.record_signal(db, user.id, resume.persona_id, "resume_generated",
                                  {"title": job.title, "company": job.company,
                                   "keywords": tailored.get("tags") or []})

    increment_usage(db, user.id, "tailored_resumes_per_month", 1)

    return {
        "resume_id": resume.id,
        "status": resume.status,
        "tags": resume.tags,
        "display_name": resume.display_name,
        "fact_guard": guard,
        "guardrail": tailored.get("guardrail", {}),
        "reasoning": tailored.get("reasoning", ""),
        "tailored": resume.profile_snapshot,
        "files": {"docx": f"/api/resumes/{resume.id}/download?format=docx",
                  "pdf": f"/api/resumes/{resume.id}/download?format=pdf"},
    }


@router.post("/resumes/{resume_id}/approve")
def approve_resume(resume_id: int, request: Request, user: CurrentUser, db: DbSession):
    resume = db.query(Resume).filter(Resume.id == resume_id, Resume.user_id == user.id).first()
    if not resume:
        raise HTTPException(404, "Resume not found")
    resume.status = "approved"
    resume.approved_at = datetime.utcnow()
    db.commit()
    audit.audit(db, "resume.approved", user=user, target=resume.filename, detail={"resume_id": resume.id}, request=request)
    return {"ok": True, "status": resume.status}


@router.post("/resumes/{resume_id}/reject")
def reject_resume(resume_id: int, request: Request, user: CurrentUser, db: DbSession):
    resume = db.query(Resume).filter(Resume.id == resume_id, Resume.user_id == user.id).first()
    if not resume:
        raise HTTPException(404, "Resume not found")
    resume.status = "rejected"
    db.commit()
    audit.audit(db, "resume.rejected", user=user, target=resume.filename, detail={"resume_id": resume.id}, request=request)
    return {"ok": True, "status": resume.status}


@router.put("/resumes/{resume_id}/tags")
def update_tags(resume_id: int, tags: List[str], user: CurrentUser, db: DbSession):
    resume = db.query(Resume).filter(Resume.id == resume_id, Resume.user_id == user.id).first()
    if not resume:
        raise HTTPException(404, "Resume not found")
    resume.tags = [str(tag)[:60] for tag in tags][:20]
    db.commit()
    return {"ok": True, "tags": resume.tags}


@router.get("/resumes/{resume_id}/download")
def download_resume(
    resume_id: int,
    request: Request,
    db: DbSession,
    format: str = "docx",
    token: Optional[str] = None,
):
    """
    Serve a resume file.

    Authorised either by the normal bearer token (API clients) or by a
    short-lived signed token from ``/resumes/{id}/download-url`` (browser
    ``<a href>`` downloads, which cannot send an Authorization header). The
    served filename is the professional display name, never the opaque path.
    """
    fmt = (format or "docx").lower()
    if fmt not in ("pdf", "docx"):
        raise HTTPException(400, "format must be 'pdf' or 'docx'")

    user_id = _resolve_downloader(request, db, resume_id, fmt, token)
    resume = db.query(Resume).filter(Resume.id == resume_id, Resume.user_id == user_id).first()
    if not resume:
        raise HTTPException(404, "Resume not found")

    path = resume.filepath
    if fmt == "pdf":
        path = os.path.splitext(resume.filepath)[0] + ".pdf"
    if not path or not os.path.exists(path):
        raise HTTPException(404, f"The {fmt.upper()} file is not available for this resume — regenerate it")

    audit.audit(db, "resume.downloaded", user_id=user_id, target=download_filename(resume, fmt),
                detail={"resume_id": resume.id, "format": fmt}, request=request)
    return FileResponse(
        path,
        filename=download_filename(resume, fmt),
        media_type="application/pdf" if fmt == "pdf" else
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


def _resolve_downloader(request: Request, db, resume_id: int, fmt: str, token: Optional[str]) -> int:
    """Bearer token first; otherwise a valid signed download token."""
    from app.core.auth import get_current_user
    from app.db import get_db  # noqa: F401  (db already injected)

    try:
        user = get_current_user(request=request, credentials=_bearer_credentials(request), db=db)
        if user:
            return user.id
    except HTTPException:
        pass

    if token:
        # The signed token is bound to (resume_id, user_id, format) — but the
        # user id is inside the signature, so verify against every candidate by
        # checking the resume's owner.
        resume = db.query(Resume).filter(Resume.id == resume_id).first()
        if resume and verify_download_token(token, resume_id, resume.user_id, fmt):
            return resume.user_id
    raise HTTPException(401, {"code": "not_authenticated",
                              "message": "Sign in again, or fetch a fresh download link from Resume Studio."})


def _bearer_credentials(request: Request):
    from fastapi.security import HTTPAuthorizationCredentials

    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=header[7:].strip())
    return None


@router.get("/resumes/{resume_id}/preview")
def preview_resume(resume_id: int, user: CurrentUser, db: DbSession):
    resume = db.query(Resume).filter(Resume.id == resume_id, Resume.user_id == user.id).first()
    if not resume:
        raise HTTPException(404, "Resume not found")
    return resume_preview(resume)


@router.get("/resumes/{resume_id}/diff")
def diff_resume(resume_id: int, user: CurrentUser, db: DbSession, against_id: Optional[int] = None):
    """Textual diff of a resume against another (defaults to its parent/master)."""
    resume = db.query(Resume).filter(Resume.id == resume_id, Resume.user_id == user.id).first()
    if not resume:
        raise HTTPException(404, "Resume not found")

    other = None
    if against_id:
        other = db.query(Resume).filter(Resume.id == against_id, Resume.user_id == user.id).first()
    elif resume.parent_resume_id:
        other = db.query(Resume).filter(Resume.id == resume.parent_resume_id, Resume.user_id == user.id).first()
    else:
        other = db.query(Resume).filter(Resume.user_id == user.id, Resume.type == "master").order_by(Resume.created_at.desc()).first()
    if not other:
        raise HTTPException(404, "Nothing to compare against — upload a master resume first")

    before = resume_text(other)
    after = resume_text(resume)
    result = diff_text(before, after)
    guard = fact_guard_check(other.profile_snapshot or {}, resume.profile_snapshot or {})
    return {
        "resume_id": resume.id,
        "against_id": other.id,
        "against_label": f"{other.type} #{other.id}",
        **result,
        "fact_guard": guard,
    }


@router.post("/resumes/{resume_id}/polish")
async def upload_polished(
    resume_id: int,
    request: Request,
    user: CurrentUser,
    db: DbSession,
    file: UploadFile = File(...),
):
    """Upload the user-edited version of a generated resume as a first-class resume."""
    enforce(db, user.id, "resume_polish_per_month")
    parent = db.query(Resume).filter(Resume.id == resume_id, Resume.user_id == user.id).first()
    if not parent:
        raise HTTPException(404, "Resume not found")
    head = await file.read(8)
    await file.seek(0)
    extension = _validate_upload(file, head)
    path, safe_name = _store_upload(file, extension)
    if os.path.getsize(path) / (1024 * 1024) > settings.max_upload_mb:
        os.remove(path)
        raise HTTPException(413, "File too large")
    text = extract_text(path)
    resume = create_polished_resume(db, user_id=user.id, parent=parent, filepath=path, filename=safe_name,
                                    text=text or resume_text(parent))
    resume.display_name = safe_name
    db.commit()
    audit.audit(db, "resume.polished", user=user, target=safe_name, detail={"parent_id": parent.id, "resume_id": resume.id}, request=request)
    persona_service.record_signal(db, user.id, parent.persona_id, "resume_edited",
                                  {"note": f"candidate rewrote the tailored resume for {safe_name}"})
    increment_usage(db, user.id, "resume_polish_per_month", 1)
    return {
        "resume": {"id": resume.id, "filename": resume.filename, "status": resume.status,
                   "parent_resume_id": parent.id, "display_name": resume.display_name},
        "diff": diff_text(resume_text(parent), text or resume_text(parent)),
    }


@router.delete("/resumes/{resume_id}")
def delete_resume(resume_id: int, request: Request, user: CurrentUser, db: DbSession):
    resume = db.query(Resume).filter(Resume.id == resume_id, Resume.user_id == user.id).first()
    if not resume:
        raise HTTPException(404, "Resume not found")
    for path in {resume.filepath, os.path.splitext(resume.filepath)[0] + ".pdf"}:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as exc:  # pragma: no cover
            log.warning("could not delete %s: %s", path, exc)
    db.delete(resume)
    db.commit()
    audit.audit(db, "resume.deleted", user=user, target=resume.filename, request=request)
    return {"ok": True}


@router.post("/resumes/generate-cover-letter")
async def generate_cover_letter(
    request: Request,
    user: CurrentUser,
    db: DbSession,
    job_id: int = Query(...),
):
    """Generate cover letter — monetized, AI-required and guardrailed."""
    enforce(db, user.id, "can_generate_cover_letter")
    enforce(db, user.id, "cover_letters_per_month")

    job = db.query(Job).filter(Job.id == job_id, Job.user_id == user.id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    profile = get_owned_profile(db, user.id)
    if not profile:
        raise HTTPException(400, "Upload a master resume first")

    from app.services.resume_generator import generate_cover_letter as gen_cl

    letter = await gen_cl(profile.data or {}, job.description or "", job.title, job.company,
                          db=db, user_id=user.id)
    increment_usage(db, user.id, "cover_letters_per_month", 1)
    audit.audit(db, "cover_letter.generated", user=user, target=f"{job.company}:{job.title}",
                detail={"job_id": job.id}, request=request)
    return {"cover_letter": letter, "job_id": job.id}
