"""
Application flow: resume selection → vault credential → form schema → autofill
plan → user-input gate → execution.

Status machine (Job.status):
    discovered → queued → preparing → needs_input → ready_to_apply → applied
                                        ↘ failed / skipped
``applied`` is only set when the application was really submitted (automation)
or the user confirmed it manually — never as a simulation.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc
from app.models.models import Job, Profile, Resume, User, UserInputRequest
from app.services.autofill import build_autofill_plan, execute_autofill
from app.services.events import record_job_event
from app.services.form_detector import detect_form_structure
from app.services.resume_generator import generate_tailored_profile
from app.services.resume_service import build_and_save_resume, fact_guard_check
from app.services.scoring import jd_similarity
from app.services.user_settings import get_setting
from app.services.vault import credential_for_application

log = get_logger("app.apply")


def latest_profile(db: Session, user_id: int) -> Optional[Profile]:
    return db.query(Profile).filter(Profile.user_id == user_id).order_by(Profile.created_at.desc()).first()


def master_resume(db: Session, user_id: int) -> Optional[Resume]:
    return (
        db.query(Resume)
        .filter(Resume.user_id == user_id, Resume.type == "master")
        .order_by(Resume.created_at.desc())
        .first()
    )


def approved_generated_resumes(db: Session, user_id: int) -> List[Resume]:
    return (
        db.query(Resume)
        .filter(Resume.user_id == user_id, Resume.type.in_(("generated", "uploaded_polished")), Resume.status == "approved")
        .all()
    )


async def choose_resume(
    db: Session,
    user_id: int,
    profile: Profile,
    job: Job,
    choice: str = "auto",
    resume_id: Optional[int] = None,
) -> Tuple[Optional[int], str]:
    """Pick the resume for an application; may generate a tailored one (pending)."""
    master = master_resume(db, user_id)

    if choice == "master":
        return (master.id if master else None), "master"

    if resume_id:
        resume = db.query(Resume).filter(Resume.id == resume_id, Resume.user_id == user_id).first()
        if not resume:
            raise ValueError("resume not found")
        return resume.id, "selected"

    if choice == "generated":
        generated = (
            db.query(Resume)
            .filter(Resume.user_id == user_id, Resume.type == "generated", Resume.status == "approved")
            .order_by(Resume.created_at.desc())
            .first()
        )
        if generated:
            return generated.id, "generated"

    reuse_threshold = float(get_setting(db, user_id, "general", "reuse_similarity_threshold", 0.85) or 0.85)
    generate_min_score = float(get_setting(db, user_id, "general", "generate_min_score", 65) or 65)
    score = float(job.score or 0)

    best_resume, best_similarity = None, 0.0
    for resume in approved_generated_resumes(db, user_id):
        if not resume.job_id:
            continue
        source_job = db.query(Job).filter(Job.id == resume.job_id).first()
        if not source_job or not source_job.description:
            continue
        similarity = jd_similarity(job.description, source_job.description)
        if similarity > best_similarity:
            best_similarity, best_resume = similarity, resume

    if score >= generate_min_score and best_resume is not None and best_similarity >= reuse_threshold:
        record_job_event(db, user_id=user_id, job_id=job.id, stage="resume_reused", status="info",
                         message=f"Reusing approved resume #{best_resume.id} (JD similarity {best_similarity:.2f})")
        return best_resume.id, "reused"

    if score >= generate_min_score and (profile.data or {}):
        strict = bool(get_setting(db, user_id, "general", "strict_skeleton", False))
        tailored = await generate_tailored_profile(profile.data, job.description, profile.layout or {}, strict)
        guard = fact_guard_check(profile.data or {}, tailored.get("tailored_profile") or {})
        if not guard["passed"]:
            log.warning("fact guard violations: %s", guard["violations"])
            record_job_event(db, user_id=user_id, job_id=job.id, stage="resume_generated", status="warning",
                             message=f"Tailored resume flagged {len(guard['violations'])} potential fabrication(s)",
                             meta=guard)
        resume = build_and_save_resume(db, user_id=user_id, profile=profile, job=job, tailored=tailored,
                                       strict_skeleton=strict, status="pending")
        record_job_event(db, user_id=user_id, job_id=job.id, stage="resume_generated", status="info",
                         message=f"Generated tailored resume #{resume.id} (score {score:.0f}) — awaiting approval",
                         meta={"fact_guard": guard})
        return resume.id, "generated"

    return (master.id if master else None), "master"


def ensure_credential(db: Session, user_id: int, job: Job, forms: Dict[str, Any]) -> Tuple[Optional[Dict[str, str]], bool]:
    """Create (or reuse) a portal credential for this job's domain."""
    if not forms.get("requires_login"):
        return None, False
    if not bool(get_setting(db, user_id, "application", "auto_create_credentials", True)):
        return None, False
    domain = forms.get("vault_domain") or (job.url.split("/")[2] if "://" in (job.url or "") else "")
    if not domain:
        return None, False
    return credential_for_application(db, user_id=user_id, domain=domain, company=job.company)


async def prepare_application(
    db: Session,
    user: User,
    job: Job,
    *,
    resume_choice: str = "auto",
    resume_id: Optional[int] = None,
    answers: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Everything that must happen before an application can be submitted:
    resume choice, credential, form schema, field mapping, user-input gate.
    """
    profile = latest_profile(db, user.id)
    if not profile:
        raise ValueError("profile_missing")

    chosen_id, decision = await choose_resume(db, user.id, profile, job, resume_choice, resume_id)
    if not chosen_id:
        raise ValueError("resume_missing")

    # Refresh the form schema when we don't have a live-verified one.
    forms = dict((job.extra or {}).get("forms") or {})
    if forms.get("detection_source") != "html" and (job.url or "").startswith("http"):
        detected = await detect_form_structure(job.url, job.source)
        if detected.get("detection_source") in ("html", "unavailable"):
            forms = detected

    credential, credential_created = ensure_credential(db, user.id, job, forms)

    resume_row = db.query(Resume).filter(Resume.id == chosen_id).first()
    resume_path = None
    if resume_row:
        if resume_row.filepath.lower().endswith(".docx"):
            pdf_candidate = resume_row.filepath[:-5] + ".pdf"
            resume_path = pdf_candidate if os.path.exists(pdf_candidate) else resume_row.filepath
        else:
            resume_path = resume_row.filepath

    plan = build_autofill_plan(
        schema=forms,
        profile=profile.data or {},
        resume_path=resume_path,
        credential=credential,
        answers=answers or {},
    )

    extra = dict(job.extra or {})
    extra["forms"] = forms
    extra["autofill_plan"] = plan
    extra["resume_decision"] = decision
    job.extra = extra
    job.applied_with_resume_id = chosen_id
    job.status = "preparing" if not plan["missing_required"] else "needs_input"
    db.commit()

    record_job_event(
        db, user_id=user.id, job_id=job.id, stage="prepared", status="info",
        message=(f"Prepared application — {plan['fillable']}/{plan['total_fields']} fields mapped "
                 f"(resume: {decision})"),
        meta={"portal": plan["portal_type"], "requires_login": plan["requires_login"],
              "detection_source": forms.get("detection_source"), "resume_decision": decision},
    )

    result: Dict[str, Any] = {
        "job_id": job.id,
        "resume_id": chosen_id,
        "resume_decision": decision,
        "portal_type": plan["portal_type"],
        "requires_login": plan["requires_login"],
        "credential_created": credential_created,
        "vault_created": credential_created,
        "form_detection_source": forms.get("detection_source"),
        "fields_mapped": plan["fillable"],
        "fields_total": plan["total_fields"],
        "missing_fields": plan["missing_required"],
        "status": job.status,
    }

    if plan["missing_required"] and bool(get_setting(db, user.id, "application", "notify_unknown_fields", True)):
        existing = (
            db.query(UserInputRequest)
            .filter(UserInputRequest.user_id == user.id, UserInputRequest.job_id == job.id,
                    UserInputRequest.status == "pending")
            .first()
        )
        if existing:
            request = existing
        else:
            request = UserInputRequest(
                user_id=user.id,
                job_id=job.id,
                fields=[{"name": f["name"], "label": f["label"], "type": f["type"], "required": True, "value": ""}
                        for f in plan["missing_required"]],
                status="pending",
            )
            db.add(request)
            db.commit()
            db.refresh(request)
        record_job_event(db, user_id=user.id, job_id=job.id, stage="needs_input", status="warning",
                         message=f"{len(plan['missing_required'])} required field(s) need your input: "
                                 + ", ".join(f["label"] for f in plan["missing_required"][:5]))
        result["input_request_id"] = request.id
        result["status"] = "needs_input"
    return result


async def execute_application(
    db: Session,
    user: User,
    job: Job,
    *,
    allow_submit: bool = False,
    answers: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the autofill plan (dry-run unless automation + consent are enabled)."""
    extra = dict(job.extra or {})
    forms = extra.get("forms") or {}
    plan = extra.get("autofill_plan")
    credential = None
    if forms.get("requires_login"):
        domain = forms.get("vault_domain") or ""
        from app.services.vault import get_vault_entry_for_domain

        entry = get_vault_entry_for_domain(db, user.id, domain) if domain else None
        if entry:
            from app.core.security import decrypt_secret

            credential = {"username": entry.username,
                          "password": decrypt_secret(entry.password_enc, f"user:{user.id}:vault")}

    if not plan:
        prepared = await prepare_application(db, user, job, answers=answers)
        if prepared.get("status") == "needs_input":
            return prepared
        plan = (job.extra or {}).get("autofill_plan")

    screenshot = os.path.join(settings.screenshot_dir, f"job_{job.id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.png")
    user_allow_submit = bool(get_setting(db, user.id, "application", "allow_auto_submit", False))
    submit = bool(allow_submit and user_allow_submit and settings.autofill_allow_submit and not settings.autofill_dry_run)

    result = await execute_autofill(
        url=job.url,
        plan=plan or {},
        credential=credential,
        allow_submit=submit,
        screenshot_path=screenshot if settings.autofill_enabled else None,
    )

    extra["autofill_result"] = result
    job.extra = extra

    if result["status"] == "submitted" and result.get("submitted"):
        job.status = "applied"
        job.applied_at = datetime.utcnow()
        job.error = ""
        record_job_event(db, user_id=user.id, job_id=job.id, stage="applied", status="success",
                         message=f"Application submitted automatically ({result['filled']} fields filled)",
                         meta=result)
        inc("jobhunter_applications_total", result="automated")
    elif result["status"] in ("dry_run", "filled"):
        job.status = "ready_to_apply"
        job.error = ""
        record_job_event(db, user_id=user.id, job_id=job.id, stage="ready_to_apply", status="info",
                         message=(f"Prefilled {result['filled']} field(s) in dry-run mode"
                                  + (" (Playwright not installed — plan only)" if result["status"] == "filled" else "")
                                  + ". Review and submit, or enable automation."),
                         meta=result)
        inc("jobhunter_applications_total", result="dry_run")
    else:
        job.status = "failed"
        job.error = result.get("reason") or "autofill unavailable"
        record_job_event(db, user_id=user.id, job_id=job.id, stage="failed", status="error",
                         message=f"Autofill could not run: {job.error}", meta=result)
        inc("jobhunter_applications_total", result="failed")

    db.commit()
    return {
        "status": job.status,
        "autofill": result,
        "resume_id": job.applied_with_resume_id,
        "resume_decision": (job.extra or {}).get("resume_decision"),
        "vault_created": False,
        "message": result.get("reason") or result["status"],
    }


def mark_applied(db: Session, user: User, job: Job, *, note: str = "") -> Dict[str, Any]:
    """User-confirmed submission (the honest path when automation is off)."""
    job.status = "applied"
    job.applied_at = datetime.utcnow()
    job.error = ""
    db.commit()
    record_job_event(db, user_id=user.id, job_id=job.id, stage="applied", status="success",
                     message=note or "Marked as applied by the user", meta={"confirmed_by": "user"})
    inc("jobhunter_applications_total", result="manual")
    return {"status": job.status, "applied_at": job.applied_at.isoformat()}
