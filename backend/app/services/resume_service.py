"""
Resume domain service: build/save tailored resumes, text snapshots, diffs and
"polished version" uploads.

Kept separate from ``resume_generator`` (which is pure rendering) so that the
pipeline, API and workers all share one code path.
"""
from __future__ import annotations

import difflib
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import Job, Profile, Resume
from app.services.reliability import count, duration
from app.services.resume_generator import build_docx, build_pdf, hash_jd, professional_filename

log = get_logger("app.resume_service")


def render_profile_text(profile: Dict[str, Any]) -> str:
    """Flatten a profile dict into plain text (used for diffs/preview/embeddings)."""
    lines: List[str] = []
    if profile.get("name"):
        lines.append(str(profile["name"]))
    contact = " | ".join(filter(None, [profile.get("email", ""), profile.get("phone", ""), profile.get("location", "")]))
    if contact:
        lines.append(contact)
    if profile.get("summary"):
        lines.extend(["", "SUMMARY", str(profile["summary"])])
    skills = profile.get("skills") or []
    if skills:
        lines.extend(["", "SKILLS", ", ".join(map(str, skills))])
    for section, key in (("EXPERIENCE", "experience"), ("PROJECTS", "projects"), ("EDUCATION", "education")):
        entries = profile.get(key) or []
        if not entries:
            continue
        lines.extend(["", section])
        for entry in entries:
            if isinstance(entry, dict):
                header = " — ".join(filter(None, [str(entry.get("title") or entry.get("name") or ""),
                                                  str(entry.get("company") or entry.get("school") or ""),
                                                  str(entry.get("duration") or entry.get("year") or "")]))
                if header:
                    lines.append(header)
                bullets = entry.get("bullets") or entry.get("description") or []
                if isinstance(bullets, str):
                    bullets = [bullets]
                lines.extend(f"- {b}" for b in bullets if b)
            else:
                lines.append(f"- {entry}")
    links = profile.get("links") or []
    if links:
        lines.extend(["", "LINKS", " | ".join(map(str, links[:5]))])
    return "\n".join(lines).strip()


def resume_text(resume: Resume) -> str:
    if resume.text_snapshot:
        return resume.text_snapshot
    if resume.profile_snapshot:
        return render_profile_text(resume.profile_snapshot)
    return ""


def build_and_save_resume(
    db: Session,
    *,
    user_id: int,
    profile: Profile,
    job: Optional[Job],
    tailored: Dict[str, Any],
    strict_skeleton: bool = False,
    status: str = "pending",
    resume_type: str = "generated",
    persona_id: Optional[int] = None,
) -> Resume:
    """Render DOCX+PDF and persist the resume row (pending approval by default)."""
    tailored_profile = tailored.get("tailored_profile", tailored if isinstance(tailored, dict) else {})
    jd_hash = hash_jd(job.description if job and job.description else tailored_profile.get("summary", ""))
    # The path on disk stays opaque and collision-free; ``filename``/
    # ``display_name`` are what the user sees and downloads.
    stem = f"resume_{job.id if job else 'general'}_{jd_hash}"
    os.makedirs(settings.generated_dir, exist_ok=True)
    docx_path = os.path.join(settings.generated_dir, f"{stem}.docx")
    pdf_path = os.path.join(settings.generated_dir, f"{stem}.pdf")

    # Artifact generation is counted per format: a DOCX renderer regression and
    # a PDF one are different incidents with different symptoms, and the pair of
    # counters is what separates them.
    # Each entry closes over its own renderer, so the loop body calls one
    # uniform signature instead of two different ones.
    renders = (
        ("resume_docx",
         lambda path: build_docx(tailored_profile, profile.data or {}, profile.layout or {}, path),
         docx_path),
        ("resume_pdf",
         lambda path: build_pdf(tailored_profile, profile.data or {}, path),
         pdf_path),
    )
    for artifact, render, target in renders:
        started = time.perf_counter()
        try:
            render(target)
        except Exception:
            count("jobhunter_artifact_generation_total", artifact=artifact, outcome="failed")
            raise
        duration("jobhunter_artifact_generation_seconds", time.perf_counter() - started,
                 artifact=artifact)
        count("jobhunter_artifact_generation_total", artifact=artifact, outcome="ok")

    display_name = professional_filename(tailored_profile or profile.data or {}, job, "pdf")
    resume = Resume(
        user_id=user_id,
        filename=professional_filename(tailored_profile or profile.data or {}, job, "docx"),
        filepath=docx_path,
        type=resume_type,
        status=status,
        profile_snapshot=tailored_profile,
        layout=profile.layout or {},
        tags=tailored.get("tags", []) or [],
        jd_hash=jd_hash,
        job_id=job.id if job else None,
        persona_id=persona_id,
        display_name=display_name,
        guardrail_report=tailored.get("guardrail", {}) or {},
        text_snapshot=render_profile_text(tailored_profile),
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)
    # Shared by explicit generation and application preparation. Charge only
    # after rendering and persistence succeed, never for an AI retry alone.
    if resume_type == "generated":
        from app.core.entitlements import increment_usage

        try:
            increment_usage(db, user_id, "tailored_resumes_per_month", 1)
        except Exception as exc:  # never lose a saved resume over accounting
            log.warning("could not charge tailored_resumes_per_month for resume %s: %s", resume.id, exc)
    return resume


def download_filename(resume: Resume, fmt: str = "pdf") -> str:
    """
    The name the browser saves the file under.

    Prefers the professional display name; falls back to the stored filename and
    finally to a readable placeholder, so a user never downloads
    ``resume_26_a6786c6``.
    """
    base = resume.display_name or resume.filename or f"resume-{resume.id}"
    stem, extension = os.path.splitext(base)
    extension = (extension or f".{fmt}").lower()
    if extension not in (".pdf", ".docx", ".doc"):
        extension = f".{fmt}"
    return f"{stem}{extension}"


def create_polished_resume(
    db: Session,
    *,
    user_id: int,
    parent: Optional[Resume],
    filepath: str,
    filename: str,
    text: str,
    tags: Optional[List[str]] = None,
) -> Resume:
    """Register a user-edited ("polished") version of a resume as first-class."""
    resume = Resume(
        user_id=user_id,
        filename=filename,
        filepath=filepath,
        type="uploaded_polished",
        status="approved",  # the user uploaded it, so it is explicitly approved
        profile_snapshot=(parent.profile_snapshot if parent else {}) or {},
        layout=(parent.layout if parent else {}) or {},
        tags=tags or (["polished"] + ((parent.tags or [])[:3] if parent else [])),
        jd_hash=parent.jd_hash if parent else None,
        job_id=parent.job_id if parent else None,
        parent_resume_id=parent.id if parent else None,
        text_snapshot=text or "",
        approved_at=datetime.utcnow(),
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)
    return resume


def diff_text(before: str, after: str, *, context: int = 3) -> Dict[str, Any]:
    """Unified diff + change counters between two resume texts."""
    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()
    diff_lines = list(difflib.unified_diff(before_lines, after_lines, fromfile="before", tofile="after", lineterm="", n=context))
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    similarity = difflib.SequenceMatcher(None, before or "", after or "").ratio()
    return {
        "diff": "\n".join(diff_lines),
        "added_lines": added,
        "removed_lines": removed,
        "similarity": round(similarity, 4),
        "changed": bool(added or removed),
        "before_lines": len(before_lines),
        "after_lines": len(after_lines),
    }


def resume_preview(resume: Resume) -> Dict[str, Any]:
    """Structured preview payload for the Resume Studio UI."""
    profile = resume.profile_snapshot or {}
    return {
        "id": resume.id,
        "filename": resume.filename,
        "type": resume.type,
        "status": resume.status,
        "tags": resume.tags or [],
        "job_id": resume.job_id,
        "parent_resume_id": resume.parent_resume_id,
        "created_at": resume.created_at.isoformat() if resume.created_at else None,
        "display_name": resume.display_name or resume.filename,
        "guardrail_report": resume.guardrail_report or {},
        "text": resume_text(resume),
        "profile": profile,
        "facts": {
            "companies": [e.get("company") for e in (profile.get("experience") or []) if isinstance(e, dict) and e.get("company")],
            "titles": [e.get("title") for e in (profile.get("experience") or []) if isinstance(e, dict) and e.get("title")],
            "skills": profile.get("skills") or [],
        },
    }


def fact_guard_check(original: Dict[str, Any], tailored: Dict[str, Any]) -> Dict[str, Any]:
    """
    Post-generation guard: verify the tailored resume does not invent employers,
    titles, degrees or dates. Runs after the LLM so the guarantee does not depend
    on the model behaving.
    """
    def facts(profile: Dict[str, Any]) -> Dict[str, set]:
        return {
            "companies": {str(e.get("company", "")).strip().lower() for e in (profile.get("experience") or [])
                          if isinstance(e, dict) and e.get("company")},
            "titles": {str(e.get("title", "")).strip().lower() for e in (profile.get("experience") or [])
                       if isinstance(e, dict) and e.get("title")},
            "schools": {str(e.get("school", "")).strip().lower() for e in (profile.get("education") or [])
                        if isinstance(e, dict) and e.get("school")},
            "durations": {str(e.get("duration", "")).strip().lower() for e in (profile.get("experience") or [])
                          if isinstance(e, dict) and e.get("duration")},
        }

    source = facts(original or {})
    target = facts(tailored or {})
    violations: List[Dict[str, str]] = []
    for kind in ("companies", "titles", "schools", "durations"):
        for value in target[kind]:
            if value and value not in source[kind]:
                violations.append({"kind": kind, "value": value})
    return {"passed": not violations, "violations": violations}
