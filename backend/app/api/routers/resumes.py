"""Resume, profile and search-context endpoints."""
from __future__ import annotations

import os
import re
import shutil
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import CurrentUser, DbSession, get_owned_profile, invalidate_context, search_context
from app.core import audit
from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import Job, Profile, Resume
from app.schemas.schemas import ProfileOut, ResumeOut
from app.services.job_queue import enqueue
from app.services.resume_generator import generate_tailored_profile
from app.services.resume_parser import ai_extract_profile, extract_layout, extract_text
from app.services.resume_service import (
    build_and_save_resume,
    create_polished_resume,
    diff_text,
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
    """
    Reduce a client-supplied file name to a flat, predictable name.

    The stored file never inherits path separators, ``..``, control characters
    or unicode tricks: only ``[A-Za-z0-9._-]`` survives.
    """
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
    # Defence in depth: the final path must stay inside the upload directory.
    if os.path.commonpath([os.path.realpath(path), os.path.realpath(settings.upload_dir)]) != os.path.realpath(settings.upload_dir):
        raise HTTPException(400, "Invalid file name")
    with open(path, "wb") as handle:
        shutil.copyfileobj(file.file, handle)
    return path, safe_name


@router.post("/resume/upload")
async def upload_resume(request: Request, user: CurrentUser, db: DbSession, file: UploadFile = File(...)):
    """Upload the master resume: text + layout extraction, AI profile, keywords."""
    head = await file.read(8)
    await file.seek(0)
    extension = _validate_upload(file, head)

    path, safe_name = _store_upload(file, extension)
    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb > settings.max_upload_mb:
        os.remove(path)
        raise HTTPException(413, f"File is {size_mb:.1f}MB — the limit is {settings.max_upload_mb}MB")

    text = extract_text(path)
    if not text.strip():
        os.remove(path)
        raise HTTPException(422, "No readable text found in the document (is it a scanned image?)")

    layout = extract_layout(path) if safe_name.lower().endswith(".pdf") else {
        "bullet_style": "•", "colors": [], "fonts": ["Calibri"]}
    profile_data, meta = await ai_extract_profile(text)
    profile_data["raw_text"] = text[:20000]

    resume = Resume(
        user_id=user.id,
        filename=safe_name,
        filepath=path,
        type="master",
        status="approved",
        profile_snapshot=profile_data,
        layout=layout,
        tags=["master"],
        text_snapshot=render_profile_text(profile_data),
        approved_at=datetime.utcnow(),
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)

    # A user has exactly one active profile; replace it on each master upload.
    db.query(Profile).filter(Profile.user_id == user.id).delete(synchronize_session=False)
    profile = Profile(user_id=user.id, data=profile_data, layout=layout, master_resume_id=resume.id)
    db.add(profile)
    db.commit()
    db.refresh(profile)

    invalidate_context(user.id)
    context = await search_context(db, user.id, force_refresh=True)
    audit.audit(db, "resume.uploaded", user=user, target=safe_name,
                detail={"resume_id": resume.id, "size_kb": round(os.path.getsize(path) / 1024, 1),
                        "extraction": meta.get("source") if isinstance(meta, dict) else None},
                request=request)

    enqueue(db, user_id=user.id, pipeline="ai", payload={"task": "tag_resume", "resume_id": resume.id},
            priority=4, dedupe_key=f"tag_resume:{resume.id}")

    # Re-score jobs that were discovered before a profile existed.
    rescored = 0
    for job in db.query(Job).filter(Job.user_id == user.id, Job.score == 0).limit(50).all():
        from app.services.scoring import heuristic_score

        job.score, job.score_reason = heuristic_score(profile_data, job.description)
        rescored += 1
    if rescored:
        db.commit()

    return {
        "resume": {"id": resume.id, "filename": resume.filename, "tags": resume.tags, "status": resume.status},
        "profile": profile_data,
        "layout": layout,
        "ai_meta": meta,
        "search_context": context,
        "rescored_jobs": rescored,
    }


@router.get("/profile", response_model=List[ProfileOut])
def list_profiles(user: CurrentUser, db: DbSession):
    return db.query(Profile).filter(Profile.user_id == user.id).order_by(Profile.created_at.desc()).all()


@router.get("/profile/current")
def current_profile(user: CurrentUser, db: DbSession):
    profile = get_owned_profile(db, user.id)
    if not profile:
        raise HTTPException(404, "Upload a resume to create your profile")
    return {"id": profile.id, "data": profile.data, "layout": profile.layout, "created_at": profile.created_at}


@router.put("/profile/{profile_id}")
def update_profile(profile_id: int, data: dict, user: CurrentUser, db: DbSession):
    profile = db.query(Profile).filter(Profile.id == profile_id, Profile.user_id == user.id).first()
    if not profile:
        raise HTTPException(404, "Profile not found")
    profile.data = data
    db.commit()
    invalidate_context(user.id)
    return {"ok": True}


@router.get("/context/keywords")
async def keywords_context(user: CurrentUser, db: DbSession, force: bool = False):
    """AI-extracted keywords/roles/industries from profile + resume + context."""
    return await search_context(db, user.id, force_refresh=force)


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
):
    """Generate a tailored resume for a job (starts ``pending`` until approved)."""
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == user.id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    profile = get_owned_profile(db, user.id)
    if not profile:
        raise HTTPException(400, "Upload a master resume first")

    strict = bool(strict_skeleton or get_setting(db, user.id, "general", "strict_skeleton", False))
    tailored = await generate_tailored_profile(profile.data or {}, job.description or "", profile.layout or {}, strict)
    guard = fact_guard_check(profile.data or {}, tailored.get("tailored_profile") or {})
    resume = build_and_save_resume(db, user_id=user.id, profile=profile, job=job, tailored=tailored,
                                   strict_skeleton=strict, status="pending")

    audit.audit(db, "resume.generated", user=user, target=resume.filename,
                detail={"job_id": job.id, "resume_id": resume.id, "fact_guard": guard}, request=request)
    enqueue(db, user_id=user.id, pipeline="ai", payload={"task": "tag_resume", "resume_id": resume.id},
            priority=4, dedupe_key=f"tag_resume:{resume.id}")

    return {
        "resume_id": resume.id,
        "status": resume.status,
        "tags": resume.tags,
        "fact_guard": guard,
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
def download_resume(resume_id: int, user: CurrentUser, db: DbSession, format: str = "docx"):
    resume = db.query(Resume).filter(Resume.id == resume_id, Resume.user_id == user.id).first()
    if not resume:
        raise HTTPException(404, "Resume not found")
    path = resume.filepath
    if format == "pdf":
        path = os.path.splitext(resume.filepath)[0] + ".pdf"
    if not os.path.exists(path):
        raise HTTPException(404, f"{format.upper()} file is not available for this resume")
    return FileResponse(path, filename=os.path.basename(path))


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
        # The user filter is not optional: without it a row whose
        # parent_resume_id points at another tenant's resume would render that
        # resume's full text (name, email, phone, employment history) into the
        # diff. Every lookup in this endpoint stays inside the caller's tenant.
        other = (
            db.query(Resume)
            .filter(Resume.id == resume.parent_resume_id, Resume.user_id == user.id)
            .first()
        )
    else:
        other = (
            db.query(Resume)
            .filter(Resume.user_id == user.id, Resume.type == "master")
            .order_by(Resume.created_at.desc())
            .first()
        )
    if not other:
        raise HTTPException(404, "Nothing to compare against — upload a master resume first")

    before = resume_text(other)
    after = resume_text(resume)
    result = diff_text(before, after)
    # The fact guard is the real safety net: show whether the tailored version
    # introduced employers/degrees that are not in the source profile.
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
    resume = create_polished_resume(db, user_id=user.id, parent=parent, filepath=path,
                                    filename=safe_name, text=text or resume_text(parent))
    audit.audit(db, "resume.polished", user=user, target=safe_name,
                detail={"parent_id": parent.id, "resume_id": resume.id}, request=request)
    return {"resume": {"id": resume.id, "filename": resume.filename, "status": resume.status,
                       "parent_resume_id": parent.id},
            "diff": diff_text(resume_text(parent), text or resume_text(parent))}


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
