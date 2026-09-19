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
        from app.services import persona as persona_service

        persona = persona_service.get_persona(db, user_id, job.persona_id)
        persona_payload = ({"name": persona.name, "target_role": persona.target_role,
                            "observed_skills": list((persona_service.memory_summary(persona)
                                                     .get("observed_skills") or {}).keys())[:12]}
                           if persona else None)
        tailored = await generate_tailored_profile(profile.data, job.description, profile.layout or {}, strict,
                                                   db=db, user_id=user_id, persona=persona_payload)
        guard = fact_guard_check(profile.data or {}, tailored.get("tailored_profile") or {})
        if not guard["passed"]:
            log.warning("fact guard violations: %s", guard["violations"])
            record_job_event(db, user_id=user_id, job_id=job.id, stage="resume_generated", status="warning",
                             message=f"Tailored resume flagged {len(guard['violations'])} potential fabrication(s)",
                             meta=guard)
        resume = build_and_save_resume(db, user_id=user_id, profile=profile, job=job, tailored=tailored,
                                       strict_skeleton=strict, status="pending",
                                       persona_id=persona.id if persona else None)
        persona_service.record_signal(db, user_id, resume.persona_id, "resume_generated",
                                      {"title": job.title, "company": job.company,
                                       "keywords": tailored.get("tags") or []})
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


async def prepare_form_plan(db: Session, user: User, job: Job) -> Dict[str, Any]:
    """
    Detect the form and build the field plan for a job — no resume, no AI.

    An assisted browser session needs the *form* facts (fields, portal, whether a
    login is required, the domain its credentials would belong to) so the live
    page can be compared against something. It deliberately does not choose or
    generate a resume: a session that pauses for a human should not spend AI
    budget before the human has even seen the form.
    """
    profile = latest_profile(db, user.id)
    if not profile:
        raise ValueError("profile_missing")

    forms = dict((job.extra or {}).get("forms") or {})
    if forms.get("detection_source") != "html" and (job.url or "").startswith("http"):
        detected = await detect_form_structure(job.url, job.source)
        if detected.get("detection_source") in ("html", "unavailable"):
            forms = detected

    plan = build_autofill_plan(schema=forms, profile=profile.data or {}, answers={})
    extra = dict(job.extra or {})
    extra["forms"] = forms
    extra["autofill_plan"] = plan
    job.extra = extra
    if job.status in ("discovered", "queued"):
        job.status = "preparing" if not plan["missing_required"] else "needs_input"
    db.commit()
    record_job_event(
        db, user_id=user.id, job_id=job.id, stage="form_prepared", status="info",
        message=f"Form detected ({forms.get('detection_source')}) — {plan['fillable']}/{plan['total_fields']} fields mapped",
        meta={"portal": plan["portal_type"], "requires_login": plan["requires_login"],
              "detection_source": forms.get("detection_source")},
    )
    return {"forms": forms, "plan": plan, "status": job.status}


def company_domains(job: Job) -> List[str]:
    """
    The job's *independent* company metadata, for the autofill domain policy.

    The posting URL is deliberately not part of this: it is the value a feed
    supplies, so it cannot also be the evidence that the URL is trustworthy.
    """
    info = job.company_info or {}
    extra = job.extra or {}
    candidates = [info.get("website"), extra.get("company_website")]
    return [str(value).strip() for value in candidates if value]


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

            # ``domain`` travels with the credential so the browser can refuse to
            # type it on any host that is not the domain it was issued for.
            credential = {"username": entry.username,
                          "password": decrypt_secret(entry.password_enc, f"user:{user.id}:vault"),
                          "domain": domain}

    if not plan:
        prepared = await prepare_application(db, user, job, answers=answers)
        if prepared.get("status") == "needs_input":
            return prepared
        plan = (job.extra or {}).get("autofill_plan")

    screenshot = os.path.join(settings.screenshot_dir, f"job_{job.id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.png")
    user_allow_submit = bool(get_setting(db, user.id, "application", "allow_auto_submit", False))
    submit = bool(allow_submit and user_allow_submit and settings.autofill_allow_submit and not settings.autofill_dry_run)

    # Evaluate the automation-policy engine (contracts/10 §5) *before* the
    # browser is reached. ``allow_submit`` carries the queued intent; the engine
    # decides whether this application may actually go out against the *live*
    # policy, consent, confidence and quota (rule 2: evaluation happens twice —
    # at trigger and at execution — so an intent queued before a consent was
    # withdrawn still runs, but as a dry run, exactly as shipped).
    from app.services import automation_policy as policy_engine

    policy_decision: Dict[str, Any] = {"allowed": True, "mode": "prepare", "reason": "allowed",
                                        "policy_id": None, "policy_version": None,
                                        "consent_snapshot": {}, "downgrade": {}}
    try:
        policy_decision = policy_engine.evaluate_policy(
            db, user=user, job=job, workflow=policy_engine.SUBMIT_WORKFLOW,
            persona_id=job.persona_id, for_http=False,
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("automation policy evaluation failed (%s) — treating as prepare-only", exc)

    engine_submit = bool(
        submit and policy_decision.get("allowed") and policy_decision.get("mode") == "auto_submit"
    )

    result = await execute_autofill(
        url=job.url,
        plan=plan or {},
        credential=credential,
        allow_submit=engine_submit,
        screenshot_path=screenshot if settings.autofill_enabled else None,
        # Independent company metadata: restricts where the browser may act.
        expected_domains=company_domains(job),
    )

    extra["policy_decision"] = policy_decision
    extra["autofill_result"] = result
    job.extra = extra

    did_submit = _submit_result(result)
    # The at-most-once ledger records the decision (contracts/10 §5 rule 3),
    # whether this run submitted or was reduced to a dry run by the policy.
    submission_id = _record_application_submission(db, user, job, policy_decision, did_submit)

    if did_submit:
        job.status = "applied"
        job.applied_at = datetime.utcnow()
        job.error = ""
        record_job_event(db, user_id=user.id, job_id=job.id, stage="applied", status="success",
                         message=f"Application submitted automatically ({result['filled']} fields filled)",
                         meta={"submission_id": submission_id,
                               "policy_id": policy_decision.get("policy_id"),
                               "policy_version": policy_decision.get("policy_version"),
                               **result})
        inc("jobhunter_applications_total", result="automated")
    elif result["status"] in ("dry_run", "filled"):
        job.status = "ready_to_apply"
        job.error = ""
        record_job_event(db, user_id=user.id, job_id=job.id, stage="ready_to_apply", status="info",
                         message=(f"Prefilled {result['filled']} field(s) in dry-run mode"
                                  + (" (Playwright not installed — plan only)" if result["status"] == "filled" else "")
                                  + (". Review and submit, or enable automation."
                                     if policy_decision.get("allowed")
                                     else ".")),
                         meta={"submission_id": submission_id,
                               "policy_reason": policy_decision.get("reason"),
                               "policy_downgrade": policy_decision.get("downgrade", {}),
                               **result})
        inc("jobhunter_applications_total",
            result="dry_run" if policy_decision.get("allowed") else "blocked")
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


def _submit_result(report: Dict[str, Any]) -> bool:
    """Was the autofill run a real outbound submission?

    ``execute_autofill`` could not always be trusted to return ``submitted``:
    if the form was filled but the submit control never matched, the run was a
    dry run with the submit withheld. An application is only ever ``applied``
    when the click really happened — see the ledger below.
    """
    sent = bool(report.get("submitted") is True and report.get("status") == "submitted")
    if sent:
        diagnostics = report.get("diagnostics") or []
        sent = any(step.get("step") == "submit" and step.get("outcome") == "matched"
                   for step in diagnostics)
    return sent


def _record_application_submission(
    db: Session,
    user: User,
    job: Job,
    policy_decision: Dict[str, Any],
    submitted: bool,
) -> Optional[int]:
    """Persist the decision on the at-most-once ledger (contracts/10 §5 rule 3).

    A real submit records ``submitted``/``automation`` with the policy id,
    version and consent snapshot the engine evaluated with; a dry run records
    ``abandoned``/``assisted_dry_run`` so the *reason why it did not go out*
    (``refusal_reason``) is also a fact — every attempt acted on carries what
    it was decided under, never just the happy path.
    """
    try:
        from app.core.audit import audit as write_audit
        from app.models.models import ApplicationSubmission
        from app.services.browser_session import submission_idempotency_key

        live = (
            db.query(ApplicationSubmission)
            .filter(ApplicationSubmission.user_id == user.id,
                    ApplicationSubmission.job_id == job.id,
                    ApplicationSubmission.state.in_(("reserved", "submitted", "verified")))
            .first()
        )
        key = submission_idempotency_key(job)
        if live is not None and live.idempotency_key != key:
            # A different attempt (e.g. an assisted session) already holds the
            # slot — do not fight the ledger; the job status is the user's view.
            return live.id
        row = live or db.query(ApplicationSubmission).filter(
            ApplicationSubmission.user_id == user.id, ApplicationSubmission.idempotency_key == key
        ).first()
        policy_id = policy_decision.get("policy_id")
        policy_version = policy_decision.get("policy_version")
        consent_snapshot = dict(policy_decision.get("inputs", {}).get("consents", {})
                                or policy_decision.get("consent_snapshot") or {})
        if submitted:
            state, channel, refusal = "submitted", "automation", ""
        else:
            # A refusal is a *decision* worth recording; the daily counter never
            # charges refusals toward the run limit.
            state, channel = "abandoned", "assisted_dry_run"
            refusal = str(policy_decision.get("reason") or "") if not policy_decision.get("allowed") else ""
        if row is None:
            row = ApplicationSubmission(
                user_id=user.id,
                job_id=job.id,
                state=state,
                channel=channel,
                idempotency_key=key,
                dry_run=not submitted,
                refusal_reason=refusal[:40] or "",
                policy_id=policy_id,
                policy_version=policy_version,
                consent_snapshot=consent_snapshot or None,
            )
            db.add(row)
        else:
            row.state = state if submitted or row.state != "submitted" else row.state
            row.channel = channel if submitted else (row.channel or channel)
            row.dry_run = not submitted
            row.refusal_reason = (refusal[:40] or "") or row.refusal_reason
            row.policy_id = policy_id if policy_id is not None else row.policy_id
            row.policy_version = policy_version if policy_version is not None else row.policy_version
            row.consent_snapshot = consent_snapshot or row.consent_snapshot
        if submitted:
            row.submitted_at = row.submitted_at or datetime.utcnow()
            row.finished_at = datetime.utcnow()
        _flush_then_counter(db, user, policy_decision, submitted)
        write_audit(
            db,
            "application.submitted" if submitted else "application.submission_refused",
            user=user,
            target=f"job:{job.id}",
            detail={
                "submission_id": row.id,
                "policy_id": policy_id,
                "policy_version": policy_version,
                "reason": policy_decision.get("reason"),
                "downgrade": policy_decision.get("downgrade", {}),
            },
            commit=False,
        )
        return int(row.id) if row.id else None
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("could not record application submission for job %s: %s", job.id, exc)
        return None


def _flush_then_counter(
    db: Session,
    user: User,
    policy_decision: Dict[str, Any],
    submitted: bool,
) -> None:
    """Flush the ledger row, then charge (or not) the daily counter atomically.

    Only real submissions count against the daily/metered budget; a dry run or a
    refusal is recorded, never charged.
    """
    db.flush()
    if submitted:
        from app.services import automation_policy as policy_engine

        policy_engine.record_outcome(
            db, user_id=user.id, workflow=policy_engine.SUBMIT_WORKFLOW,
            allowed=True, rejected=False, count=1, commit=False,
        )


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
