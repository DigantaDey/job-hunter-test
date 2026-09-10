from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, Query, Body
from sqlalchemy.orm import Session
from typing import List, Optional
import os
import json
import shutil
import asyncio
from datetime import datetime

from app.db import get_db
from app.core.config import settings
from app.models.models import Profile, Resume, Job, VaultEntry, Email, SettingsModel, ErrorLog, UserInputRequest, PipelineJob, FundingCompany
from app.schemas.schemas import ProfileOut, ResumeOut, JobOut, JobDetail, VaultOut, EmailOut, ErrorLogOut
from app.services.resume_parser import extract_layout, extract_text_from_pdf, ai_extract_profile
from app.services.scoring import heuristic_score, ai_score
from app.services.classifier import heuristic_company_size, ai_company_size
from app.services.resume_generator import generate_tailored_profile, build_docx, build_pdf, hash_jd
from app.services.vault import generate_credential, save_vault_entry, list_vault_entries, export_chrome_csv, export_apple_csv, delete_all_vault, get_vault_entry_for_domain
from app.services.job_scraper import discover_jobs, detect_form_structure
from app.services.email_pipeline import find_decision_maker, generate_cold_email, send_via_smtp
from app.services import funding_radar
from app.services.keyword_extractor import ai_extract_context, heuristic_context, merge_user_keywords, _context_cache
from app.services.ai_pipeline import ai_pipeline
from app.core.rate_limiter import rate_limiter
from app.utils.logger import log_error, log_info
from app.core.security import decrypt_password

router = APIRouter()

# ---------- Search context (AI keyword extraction) ----------
async def get_search_context(db: Session, extra_context: str = "", force_refresh: bool = False) -> dict:
    """
    AI-extracted search context from the user profile + master resume text +
    any extra context. Cached in memory; invalidated on resume upload or when
    the profile changes. Never empty: heuristic mining is the fallback floor.
    """
    profile = db.query(Profile).order_by(Profile.created_at.desc()).first()
    profile_id = profile.id if profile else None
    cached = _context_cache.get("context")
    if (not force_refresh and cached and _context_cache.get("profile_id") == profile_id
            and _context_cache.get("extra_context", "") == (extra_context or "")):
        return cached
    profile_data = (profile.data if profile else {}) or {}
    resume_text = profile_data.get("raw_text", "") or ""
    context = await ai_extract_context(profile_data, resume_text=resume_text, extra_context=extra_context)
    context["profile_id"] = profile_id
    context["generated_at"] = datetime.utcnow().isoformat()
    _context_cache["context"] = context
    _context_cache["profile_id"] = profile_id
    _context_cache["extra_context"] = extra_context or ""
    return context

@router.get("/context/keywords")
async def get_keywords_context(force: bool = False, db: Session = Depends(get_db)):
    """AI-extracted keywords/roles/industries from the user profile + resume + context."""
    ctx = await get_search_context(db, force_refresh=force)
    return ctx

# ---------- Profile & Resume ----------
@router.post("/resume/upload")
async def upload_resume(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename.lower().endswith((".pdf",".docx",".doc")):
        raise HTTPException(400, "Only PDF/DOCX supported")
    os.makedirs(settings.upload_dir, exist_ok=True)
    filepath = os.path.join(settings.upload_dir, f"{datetime.utcnow().timestamp()}_{file.filename}")
    with open(filepath, "wb") as f:
        shutil.copyfileobj(file.file, f)
    # Extract text and layout
    text = extract_text_from_pdf(filepath) if filepath.lower().endswith(".pdf") else ""
    if not text:
        # try reading docx text
        try:
            from docx import Document
            doc = Document(filepath)
            text = "\n".join([p.text for p in doc.paragraphs])
        except:
            text = ""
    layout = extract_layout(filepath) if filepath.lower().endswith(".pdf") else {"bullet_style":"•","colors":["#000"],"fonts":["Calibri"]}
    profile_data, meta = await ai_extract_profile(text)
    # Enqueue AI pipeline for tagging
    await ai_pipeline.enqueue("parse", f"Parse resume {file.filename}", priority=2)

    # Save resume
    resume = Resume(filename=file.filename, filepath=filepath, type="master", profile_snapshot=profile_data, layout=layout, tags=["master"])
    db.add(resume)
    db.commit()
    db.refresh(resume)

    # Save profile
    profile = Profile(data=profile_data, layout=layout, master_resume_id=resume.id)
    db.add(profile)
    db.commit()
    db.refresh(profile)

    log_info(db, "general", f"Resume uploaded: {file.filename}", meta={"resume_id": resume.id})

    # Refresh AI search context (keywords auto-extracted from resume + profile)
    ctx = await get_search_context(db, force_refresh=True)

    # Re-score jobs that were discovered before a profile existed
    unscored = db.query(Job).filter(Job.score == 0).limit(50).all()
    rescored = 0
    for j in unscored:
        try:
            score, reason = await ai_score(profile_data, j.description)
            j.score, j.score_reason = score, reason
            rescored += 1
        except Exception:
            pass
    if rescored:
        db.commit()
        log_info(db, "scoring", f"Re-scored {rescored} jobs after resume upload")

    return {"resume": {"id": resume.id, "filename": resume.filename, "tags": resume.tags}, "profile": profile_data, "layout": layout, "ai_meta": meta, "search_context": ctx}

@router.get("/profile", response_model=List[ProfileOut])
def get_profiles(db: Session = Depends(get_db)):
    return db.query(Profile).order_by(Profile.created_at.desc()).all()

@router.get("/profile/current")
def get_current_profile(db: Session = Depends(get_db)):
    p = db.query(Profile).order_by(Profile.created_at.desc()).first()
    if not p:
        raise HTTPException(404, "No profile")
    return {"id": p.id, "data": p.data, "layout": p.layout, "created_at": p.created_at}

@router.put("/profile/{profile_id}")
def update_profile(profile_id: int, data: dict, db: Session = Depends(get_db)):
    p = db.query(Profile).filter(Profile.id==profile_id).first()
    if not p:
        raise HTTPException(404, "Not found")
    p.data = data
    db.commit()
    return {"ok": True}

@router.get("/resumes", response_model=List[ResumeOut])
def list_resumes(db: Session = Depends(get_db)):
    return db.query(Resume).order_by(Resume.created_at.desc()).all()

@router.post("/resumes/generate")
async def generate_resume(job_id: int, strict_skeleton: bool = False, use_ai_format: bool = True, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id==job_id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    profile = db.query(Profile).order_by(Profile.created_at.desc()).first()
    if not profile:
        raise HTTPException(404, "No profile")
    # Check existing resume similarity heuristic: if we have generated resumes, decide reuse
    existing = db.query(Resume).filter(Resume.type=="generated").all()
    best_sim = 0  # placeholder
    # scoring to decide reuse
    # For demo, always generate if strict_skeleton false or score high
    score, reason = await ai_score(profile.data, job.description)
    if existing and score < 75 and not strict_skeleton:
        # suggest reuse highest scored? simplified
        pass

    await ai_pipeline.enqueue("resume_gen", f"Generate resume for {job.title} at {job.company}", priority=2)
    await rate_limiter.wait_and_acquire(1)

    tailored = await generate_tailored_profile(profile.data, job.description, profile.layout, strict_skeleton)
    tailored_profile = tailored.get("tailored_profile", {})
    tags = tailored.get("tags", ["tailored"])

    # Build files
    safe_name = f"resume_{job.id}_{hash_jd(job.description)}"
    docx_path = os.path.join(settings.generated_dir, f"{safe_name}.docx")
    pdf_path = os.path.join(settings.generated_dir, f"{safe_name}.pdf")
    os.makedirs(settings.generated_dir, exist_ok=True)
    build_docx(tailored_profile, profile.data, profile.layout, docx_path)
    build_pdf(tailored_profile, profile.data, pdf_path)

    # Save as generated resume (point to docx)
    resume = Resume(filename=f"{safe_name}.docx", filepath=docx_path, type="generated", profile_snapshot=tailored_profile, layout=profile.layout, tags=tags, jd_hash=hash_jd(job.description), job_id=job.id)
    db.add(resume)
    db.commit()
    db.refresh(resume)

    # Also enqueue tagging
    await ai_pipeline.enqueue("tagging", f"Tag resume {resume.id}", priority=4)

    return {"resume_id": resume.id, "tags": tags, "score": score, "reason": reason, "tailored": tailored_profile, "files": {"docx": f"/api/resumes/{resume.id}/download?format=docx", "pdf": f"/api/resumes/{resume.id}/download?format=pdf"}}

@router.get("/resumes/{resume_id}/download")
def download_resume(resume_id: int, format: str = "docx", db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse
    r = db.query(Resume).filter(Resume.id==resume_id).first()
    if not r:
        raise HTTPException(404, "Not found")
    if format=="pdf":
        pdf_path = r.filepath.replace(".docx",".pdf")
        if os.path.exists(pdf_path):
            return FileResponse(pdf_path, filename=os.path.basename(pdf_path))
    return FileResponse(r.filepath, filename=r.filename)

@router.put("/resumes/{resume_id}/tags")
def update_tags(resume_id: int, tags: List[str], db: Session = Depends(get_db)):
    r = db.query(Resume).filter(Resume.id==resume_id).first()
    if not r:
        raise HTTPException(404)
    r.tags = tags
    db.commit()
    return {"ok": True}

# ---------- Jobs & Discovery ----------
@router.get("/jobs", response_model=List[JobOut])
def get_jobs(status: Optional[str]=None, source: Optional[str]=None, company_size: Optional[str]=None, q: Optional[str]=None, db: Session = Depends(get_db)):
    query = db.query(Job)
    if status:
        query = query.filter(Job.status==status)
    if source:
        query = query.filter(Job.source==source)
    if company_size:
        query = query.filter(Job.company_size==company_size)
    if q:
        query = query.filter((Job.title.ilike(f"%{q}%")) | (Job.company.ilike(f"%{q}%")))
    return query.order_by(Job.discovered_at.desc()).limit(100).all()

@router.get("/jobs/{job_id}", response_model=JobDetail)
def get_job(job_id: int, db: Session = Depends(get_db)):
    j = db.query(Job).filter(Job.id==job_id).first()
    if not j:
        raise HTTPException(404)
    return j

@router.post("/jobs/discover")
async def trigger_discovery(keywords: Optional[str]=None, freshness_hours: Optional[int]=None, limit: int = 14, db: Session = Depends(get_db)):
    """
    Discovery keywords = user input (optional) + AI-extracted context from the
    user's profile, resume and stored context notes + settings defaults.
    """
    fresh = freshness_hours or settings.default_freshness_hours
    context = await get_search_context(db)

    # settings keywords (user-managed in Settings page)
    scraping = {s.key: s.value for s in db.query(SettingsModel).filter(SettingsModel.category=="scraping").all()}
    settings_kw = scraping.get("keywords", settings.default_keywords)
    if isinstance(settings_kw, list):
        settings_kw = ", ".join(str(k) for k in settings_kw)
    context = merge_user_keywords(context, settings_kw)
    # explicit user input for this run wins (prepended)
    context = merge_user_keywords(context, keywords)

    kw_list = context.get("keywords", [])[:16]
    context_for_run = context.get("user_keywords") and ", ".join(context["user_keywords"]) or (keywords or "")

    await ai_pipeline.enqueue("form_detect", f"Discover jobs with {kw_list}", priority=3)
    pj = PipelineJob(pipeline="discovery", payload={"keywords": kw_list, "freshness_hours": fresh, "context_source": context.get("source")}, status="queued", priority=3)
    db.add(pj)
    db.commit()
    pj_id = pj.id  # capture before the request session closes

    async def _do():
        from app.db import SessionLocal
        dbs = SessionLocal()
        try:
            await rate_limiter.wait_and_acquire(1)
            jobs = await discover_jobs(kw_list, fresh, limit=limit)
            prof = dbs.query(Profile).order_by(Profile.created_at.desc()).first()
            prof_data = prof.data if prof else {}
            added = 0
            for jd in jobs:
                if prof_data:
                    score, reason = await ai_score(prof_data, jd["description"])
                else:
                    score, reason = 0.0, "Awaiting profile — upload a resume to score"
                size, conf = await ai_company_size(jd["company"], jd["description"])
                # Dedup on stable id AND (title, company) so re-runs never duplicate
                exists = dbs.query(Job).filter(
                    ((Job.title == jd["title"]) & (Job.company == jd["company"])) | (Job.url == jd["url"])
                ).first()
                if not exists:
                    job = Job(title=jd["title"], company=jd["company"], location=jd["location"], description=jd["description"], url=jd["url"], source=jd["source"], status="discovered", score=score, score_reason=reason, company_size=size, company_info={"confidence": conf, "forms": jd.get("forms_detected")}, freshness_hours=fresh, extra=jd.get("forms_detected", {}))
                    dbs.add(job)
                    added += 1
            pj2 = dbs.query(PipelineJob).filter(PipelineJob.id==pj_id).first()
            if pj2:
                pj2.status = "done"
            dbs.commit()
            log_info(dbs, "discovery", f"Discovered {added} new jobs ({len(jobs)} scanned) for keywords {kw_list}", meta={"context_source": context.get("source")})
        except Exception as e:
            pj2 = dbs.query(PipelineJob).filter(PipelineJob.id==pj_id).first()
            if pj2:
                pj2.status = "failed"
                pj2.error = str(e)
                dbs.commit()
            log_error(dbs, "discovery", str(e))
        finally:
            dbs.close()
    asyncio.create_task(_do())
    return {"queued": True, "pipeline_job_id": pj.id, "keywords": kw_list, "freshness_hours": fresh, "context_source": context.get("source")}

@router.post("/jobs/{job_id}/apply")
async def apply_job(job_id: int, resume_choice: str = "auto", resume_id: Optional[int]=None, simulate_failure: bool = False, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id==job_id).first()
    if not job:
        raise HTTPException(404)
    profile = db.query(Profile).order_by(Profile.created_at.desc()).first()
    if not profile:
        raise HTTPException(400, "Upload resume first")

    # Decide resume
    chosen_resume_id = resume_id
    if resume_choice=="master":
        master = db.query(Resume).filter(Resume.type=="master").order_by(Resume.created_at.desc()).first()
        chosen_resume_id = master.id if master else None
    elif resume_choice=="generated":
        gen = db.query(Resume).filter(Resume.type=="generated").order_by(Resume.created_at.desc()).first()
        chosen_resume_id = gen.id if gen else None
        if not chosen_resume_id:
            tailored = await generate_tailored_profile(profile.data, job.description, profile.layout, False)
            chosen_resume_id = await _build_and_save_resume(db, profile, job, tailored)
    elif resume_choice=="auto":
        # scoring decides reuse or generate
        if job.score >= 75:
            gen = db.query(Resume).filter(Resume.job_id==job_id).first()
            if gen:
                chosen_resume_id = gen.id
            else:
                tailored = await generate_tailored_profile(profile.data, job.description, profile.layout, False)
                chosen_resume_id = await _build_and_save_resume(db, profile, job, tailored)
        else:
            master = db.query(Resume).filter(Resume.type=="master").order_by(Resume.created_at.desc()).first()
            chosen_resume_id = master.id if master else None
    if not chosen_resume_id:
        master = db.query(Resume).filter(Resume.type=="master").order_by(Resume.created_at.desc()).first()
        chosen_resume_id = master.id if master else None

    # Vault credential (reuse when the domain already has one)
    forms = job.extra or {}
    vault_created = False
    if forms.get("requires_login"):
        domain = forms.get("vault_domain") or (job.url.split("/")[2] if "://" in job.url else job.company.lower().replace(" ","")+".com")
        if not get_vault_entry_for_domain(db, domain):
            cred = generate_credential(domain)
            save_vault_entry(db, domain, cred["username"], cred["password"])
            vault_created = True
            log_info(db, "application", f"Auto-created vault credential for {domain}", job_id=job.id)

    await ai_pipeline.enqueue("application_autofill", f"Autofill {job.title} at {job.company}", priority=1)
    await rate_limiter.wait_and_acquire(1)

    # Missing required fields -> user input needed queue (no duplicate requests)
    required = forms.get("fields", [])
    missing = [f for f in required if f["name"] in ["workAuthorization","linkedin"] and f["required"]]
    is_external = job.source in ["linkedin","indeed","naukri"] and forms.get("portal_type")=="simple"

    if missing:
        existing_uir = db.query(UserInputRequest).filter(UserInputRequest.job_id==job.id, UserInputRequest.status=="pending").first()
        if not existing_uir:
            uir = UserInputRequest(job_id=job.id, fields=[{"name": f["name"], "label": f["name"], "type": f["type"], "required": True, "value": ""} for f in missing], status="pending")
            db.add(uir)
        job.status = "needs_input"
        pj = PipelineJob(pipeline="application", job_id=job.id, payload={"resume_id": chosen_resume_id, "reason": "needs_input"}, status="needs_input", priority=1)
        db.add(pj)
        db.commit()
        return {"status": "needs_input", "missing_fields": missing, "input_request_id": (existing_uir.id if existing_uir else uir.id), "vault_created": vault_created, "message": "Please complete required fields"}

    # Normal apply flow
    job.status = "applying"
    job.applied_with_resume_id = chosen_resume_id
    pj = PipelineJob(pipeline="application", job_id=job.id, payload={"resume_id": chosen_resume_id}, status="processing", priority=1)
    db.add(pj)
    db.commit()

    # Deterministic outcome; failures are opt-in via simulate_failure (demo/tests)
    async def _simulate_apply():
        await asyncio.sleep(1.2)
        from app.db import SessionLocal
        dbs = SessionLocal()
        try:
            j = dbs.query(Job).filter(Job.id==job_id).first()
            p = dbs.query(PipelineJob).filter(PipelineJob.job_id==job_id, PipelineJob.pipeline=="application").order_by(PipelineJob.created_at.desc()).first()
            if j:
                if not simulate_failure:
                    j.status = "applied"
                    j.error = ""
                    if p:
                        p.status = "done"
                else:
                    j.status = "failed"
                    j.error = "Auto-fill failed: CAPTCHA detected (simulated)"
                    if p:
                        p.status = "failed"
                        p.error = j.error
                    log_error(dbs, "application", j.error, job_id=j.id)
                dbs.commit()
        finally:
            dbs.close()
    asyncio.create_task(_simulate_apply())

    if is_external:
        log_info(db, "application", f"LinkedIn/Indeed external redirect detected for {job.company}, switching to credential+autofill workflow", job_id=job.id)

    return {"status": job.status, "resume_id": chosen_resume_id, "vault_created": vault_created}

async def _build_and_save_resume(db: Session, profile, job, tailored: dict) -> int:
    safe_name = f"resume_{job.id}_{hash_jd(job.description)}"
    docx_path = os.path.join(settings.generated_dir, f"{safe_name}.docx")
    os.makedirs(settings.generated_dir, exist_ok=True)
    build_docx(tailored["tailored_profile"], profile.data, profile.layout, docx_path)
    build_pdf(tailored["tailored_profile"], profile.data, docx_path.replace(".docx", ".pdf"))
    r = Resume(filename=f"{safe_name}.docx", filepath=docx_path, type="generated", profile_snapshot=tailored["tailored_profile"], layout=profile.layout, tags=tailored.get("tags", []), jd_hash=hash_jd(job.description), job_id=job.id)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r.id

@router.post("/jobs/{job_id}/input")
async def submit_input(job_id: int, payload: dict, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id==job_id).first()
    if not job:
        raise HTTPException(404)
    uir = db.query(UserInputRequest).filter(UserInputRequest.job_id==job_id, UserInputRequest.status=="pending").order_by(UserInputRequest.created_at.desc()).first()
    if not uir:
        raise HTTPException(404, "No pending input")
    # update fields
    for f in uir.fields:
        if f["name"] in payload:
            f["value"] = payload[f["name"]]
    uir.status = "completed"
    uir.completed_at = datetime.utcnow()
    job.status = "applying"
    job.error = ""
    # re-queue and actually process to completion this time
    pj = PipelineJob(pipeline="application", job_id=job_id, payload={"resumed": True, "input": payload}, status="processing", priority=1)
    db.add(pj)
    db.commit()
    pj_id = pj.id  # capture before the request session closes (detached-instance safety)
    log_info(db, "application", f"User input completed for {job.company}, re-queued", job_id=job_id)

    async def _finish_apply():
        await asyncio.sleep(1.2)
        from app.db import SessionLocal
        dbs = SessionLocal()
        try:
            j = dbs.query(Job).filter(Job.id==job_id).first()
            p = dbs.query(PipelineJob).filter(PipelineJob.id==pj_id).first()
            if j:
                j.status = "applied"
                j.error = ""
                if p:
                    p.status = "done"
                dbs.commit()
                log_info(dbs, "application", f"Application completed after user input: {j.company}", job_id=job_id)
        except Exception as e:
            log_error(dbs, "application", str(e), job_id=job_id)
        finally:
            dbs.close()
    asyncio.create_task(_finish_apply())
    return {"ok": True, "requeued": True}

# ---------- Vault ----------
@router.get("/vault", response_model=List[VaultOut])
def get_vault(db: Session = Depends(get_db)):
    return list_vault_entries(db)

@router.get("/vault/export/chrome")
def export_chrome(db: Session = Depends(get_db)):
    from fastapi.responses import PlainTextResponse
    entries = list_vault_entries(db)
    csv_data = export_chrome_csv(entries)
    return PlainTextResponse(csv_data, media_type="text/csv", headers={"Content-Disposition":"attachment; filename=jobhunter_chrome_passwords.csv"})

@router.get("/vault/export/apple")
def export_apple(db: Session = Depends(get_db)):
    from fastapi.responses import PlainTextResponse
    entries = list_vault_entries(db)
    csv_data = export_apple_csv(entries)
    return PlainTextResponse(csv_data, media_type="text/csv", headers={"Content-Disposition":"attachment; filename=jobhunter_apple_passwords.csv"})

@router.delete("/vault")
def delete_vault(db: Session = Depends(get_db)):
    delete_all_vault(db)
    log_info(db, "vault", "All credentials deleted forever")
    return {"ok": True}

# ---------- Email ----------
@router.get("/emails", response_model=List[EmailOut])
def get_emails(status: Optional[str]=None, db: Session = Depends(get_db)):
    q = db.query(Email)
    if status:
        q = q.filter(Email.status==status)
    return q.order_by(Email.created_at.desc()).all()

@router.post("/emails/generate")
async def create_email(company: str, job_id: Optional[int]=None, department: str="engineering", founder: bool=False, db: Session = Depends(get_db)):
    profile = db.query(Profile).order_by(Profile.created_at.desc()).first()
    if not profile:
        raise HTTPException(400, "No profile")
    job = db.query(Job).filter(Job.id==job_id).first() if job_id else None
    jd = job.description if job else ""
    await ai_pipeline.enqueue("email_gen", f"Email for {company}", priority=5)
    await rate_limiter.wait_and_acquire(1)
    decision = await find_decision_maker(company, department)
    email_content = await generate_cold_email(profile.data, company, job.title if job else "", founder)
    email = Email(to_email=decision["email"], to_name=decision["name"], subject=email_content["subject"], body=email_content["body"], status="pending_approval", job_id=job_id, company=company, recipient_type="founder" if founder else "hiring_manager")
    db.add(email)
    db.commit()
    db.refresh(email)
    pj = PipelineJob(pipeline="email", job_id=job_id, payload={"to": decision["email"]}, status="queued", priority=5)
    db.add(pj)
    db.commit()
    return {"email": {"id": email.id, "to": email.to_email, "subject": email.subject}, "decision_maker": decision}

@router.put("/emails/{email_id}")
def update_email(email_id: int, payload: dict, db: Session = Depends(get_db)):
    e = db.query(Email).filter(Email.id==email_id).first()
    if not e:
        raise HTTPException(404)
    if "subject" in payload:
        e.subject = payload["subject"]
    if "body" in payload:
        e.body = payload["body"]
    if "to_email" in payload:
        e.to_email = payload["to_email"]
    db.commit()
    return {"ok": True}

@router.post("/emails/{email_id}/approve")
def approve_email(email_id: int, db: Session = Depends(get_db)):
    e = db.query(Email).filter(Email.id==email_id).first()
    if not e:
        raise HTTPException(404)
    e.status = "queued"
    pj = db.query(PipelineJob).filter(PipelineJob.pipeline=="email", PipelineJob.job_id==e.job_id).order_by(PipelineJob.created_at.desc()).first()
    if pj:
        pj.status="queued"
    db.commit()
    return {"ok": True, "status": "queued"}

@router.post("/emails/{email_id}/send")
def send_email(email_id: int, otp: Optional[str]=None, payload: Optional[dict] = Body(None), db: Session = Depends(get_db)):
    e = db.query(Email).filter(Email.id==email_id).first()
    if not e:
        raise HTTPException(404)
    smtp_config = dict((payload or {}).get("smtp") or {})
    # Try to load from SettingsModel
    for s in db.query(SettingsModel).filter(SettingsModel.category=="email").all():
        if s.key not in smtp_config:
            smtp_config[s.key]=s.value
    result = send_via_smtp(e.to_email, e.subject, e.body, smtp_config, otp)
    if result.get("needs_otp"):
        e.status = "needs_otp"
        e.error = result["error"]
        db.commit()
        return {"needs_otp": True, "error": result["error"]}
    if result["success"]:
        e.status = "sent"
        e.sent_at = datetime.utcnow()
        db.commit()
        pj = PipelineJob(pipeline="email", job_id=e.job_id, payload={"sent": True}, status="done")
        db.add(pj)
        db.commit()
        return {"ok": True, "sent": True, "mock": result.get("mock", False)}
    else:
        e.status = "failed"
        e.error = result["error"]
        db.commit()
        log_error(db, "email", result["error"], job_id=e.job_id)
        return {"ok": False, "error": result["error"]}

# ---------- Funding Radar ----------
FUNDING_FRESHNESS_DAYS = settings.funding_freshness_days
FUNDING_STAGES = ["Seed", "Series A", "Series B", "Series C", "Series D"]

def _funding_stale(db: Session, window_days: int) -> bool:
    latest = db.query(FundingCompany).order_by(FundingCompany.discovered_at.desc()).first()
    if not latest or not latest.discovered_at:
        return True
    age_hours = (datetime.utcnow() - latest.discovered_at).total_seconds() / 3600
    return age_hours > max(1, window_days // 2)  # rescan after half the window

def _user_funding_context(db: Session) -> str:
    rows = db.query(SettingsModel).filter(SettingsModel.category=="funding").all()
    kv = {r.key: r.value for r in rows}
    parts = [str(v) for k, v in kv.items() if k in ("context_notes", "industries") and v]
    return ", ".join(parts)

async def _run_funding_scan(db: Session, stages: Optional[List[str]] = None, user_context: str = "", force: bool = True) -> dict:
    context = await get_search_context(db, extra_context=user_context, force_refresh=force)
    context = merge_user_keywords(context, user_context)
    window = FUNDING_FRESHNESS_DAYS
    companies = await funding_radar.scan_funded_companies(
        context, stages=stages, window_days=window, limit=settings.funding_limit
    )
    funding_radar.sync_funding_db(db, companies, window)
    log_info(db, "funding", f"Funding scan: {len(companies)} companies (source={context.get('source')})", meta={"focus": context.get("funding_focus", [])})
    return context

@router.get("/funding/context")
async def get_funding_context(db: Session = Depends(get_db)):
    """The AI-extracted context driving the radar (profile + resume + user context)."""
    context = await get_search_context(db, extra_context=_user_funding_context(db))
    return context

@router.get("/funding/companies")
async def get_funding_companies(stage: Optional[str]=None, refresh: bool = False, db: Session = Depends(get_db)):
    """Fresh Seed→Series D radar. Rescans automatically when stale or on ?refresh=1."""
    stages = [s.strip() for s in stage.split(",")] if stage else None
    if refresh or _funding_stale(db, FUNDING_FRESHNESS_DAYS):
        await _run_funding_scan(db, user_context=_user_funding_context(db), force=refresh)
    query = db.query(FundingCompany)
    if stages:
        query = query.filter(FundingCompany.stage.in_(stages))
    rows = query.order_by(FundingCompany.raised_at.desc()).limit(40).all()
    context = await get_search_context(db, extra_context=_user_funding_context(db))
    return {
        "companies": [funding_radar.row_to_dict(r) for r in rows],
        "count": len(rows),
        "context": context,
        "freshness_days": FUNDING_FRESHNESS_DAYS,
        "stages": FUNDING_STAGES,
        "refreshed_at": datetime.utcnow().isoformat(),
    }

@router.post("/funding/refresh")
async def refresh_funding(payload: Optional[dict] = Body(None), db: Session = Depends(get_db)):
    """Force an AI re-scan. Optional body: {"context": "free text", "stages": ["Seed","Series A"], "keywords": "a, b"}"""
    payload = payload or {}
    user_context = str(payload.get("context") or "")
    keywords = payload.get("keywords")
    if keywords:
        user_context = ", ".join(x for x in [user_context, str(keywords)] if x)
    stages = payload.get("stages") or None
    context = await _run_funding_scan(db, stages=stages, user_context=user_context, force=True)
    rows = db.query(FundingCompany).order_by(FundingCompany.raised_at.desc()).limit(40).all()
    return {
        "ok": True,
        "count": len(rows),
        "context": context,
        "companies": [funding_radar.row_to_dict(r) for r in rows],
    }

@router.post("/funding/{company_name}/process")
async def process_funding_company(company_name: str, db: Session = Depends(get_db)):
    row = db.query(FundingCompany).filter(FundingCompany.name.ilike(company_name)).first()
    if not row:
        # last resort: scan then retry
        await _run_funding_scan(db, force=True)
        row = db.query(FundingCompany).filter(FundingCompany.name.ilike(company_name)).first()
    if not row:
        raise HTTPException(404, "Company not in radar — refresh the scan")
    profile = db.query(Profile).order_by(Profile.created_at.desc()).first()
    if not profile:
        raise HTTPException(400, "No profile")
    if row.has_open_positions:
        prof_data = profile.data or {}
        score, reason = await ai_score(prof_data, f"Open engineering role at {row.name} ({row.stage}, {row.industry})")
        job = Job(title=f"Open Role at {row.name}", company=row.name, location="Remote", description=f"Join {row.name} — recently raised {row.stage} ({row.industry}). {row.summary}", url=f"https://{row.website}/careers", source="custom", status="discovered", score=score, score_reason=reason, company_size="startup", company_info={"stage": row.stage, "industry": row.industry})
        db.add(job)
        db.commit()
        db.refresh(job)
        return {"action": "apply_flow", "job_id": job.id, "message": f"Open position found at {row.name} — job created, apply from Jobs", "company": row.name}
    else:
        decision = await find_decision_maker(row.name, "founder")
        email_content = await generate_cold_email(profile.data, row.name, founder=True)
        email = Email(to_email=decision["email"], to_name=decision["name"], subject=email_content["subject"], body=email_content["body"], status="pending_approval", company=row.name, recipient_type="founder")
        db.add(email)
        db.commit()
        db.refresh(email)
        return {"action": "cold_email_founder", "email_id": email.id, "decision": decision, "message": f"No open position at {row.name} — founder cold email drafted for approval", "company": row.name}

# ---------- Settings ----------
@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    rows = db.query(SettingsModel).all()
    grouped = {}
    for r in rows:
        grouped.setdefault(r.category, {})[r.key] = r.value
    # defaults if empty
    defaults = {
        "ai": {"base_url": settings.ai_base_url, "model": settings.ai_model, "rpm": settings.ai_rpm, "api_key_set": bool(settings.ai_api_key)},
        "scraping": {"keywords": settings.default_keywords, "freshness_hours": settings.default_freshness_hours, "sources": ["linkedin","naukri","indeed","instahyre","lever","greenhouse","workday"]},
        "general": {"strict_skeleton": False, "auto_approve_email": False, "default_resume_format": "ai_generated"},
        "application": {"auto_create_credentials": True, "notify_unknown_fields": True},
        "email": {"host": "", "port": 587, "username": "", "use_tls": True, "from_name": ""},
        "funding": {"context_notes": "", "industries": "", "freshness_days": settings.funding_freshness_days},
        "workflows": {"ai_for_discovery": True, "ai_for_scoring": True, "ai_for_resume": True, "ai_for_emails": True},
    }
    for cat, vals in defaults.items():
        if cat not in grouped:
            grouped[cat]=vals
        else:
            for k,v in vals.items():
                if k not in grouped[cat]:
                    grouped[cat][k]=v
    return grouped

@router.put("/settings")
def put_settings(payload: dict, db: Session = Depends(get_db)):
    # payload: {category: {key: value}}
    for cat, kv in payload.items():
        if isinstance(kv, dict):
            for k,v in kv.items():
                row = db.query(SettingsModel).filter(SettingsModel.category==cat, SettingsModel.key==k).first()
                if row:
                    row.value = v
                else:
                    row = SettingsModel(category=cat, key=k, value=v)
                    db.add(row)
                # special handling: rpm update
                if cat=="ai" and k=="rpm":
                    try:
                        rate_limiter.update_rpm(int(v))
                    except:
                        pass
    db.commit()
    return {"ok": True}

@router.get("/settings/ai/status")
async def ai_status():
    health = await ai_pipeline.health_check()
    stats = rate_limiter.stats()
    online = health.get("online", False)
    # If no api key, we consider offline but not error; UI shows red dot
    return {
        "online": online,
        "rpm": stats["rpm"],
        "used_in_window": stats["used_in_window"],
        "remaining": stats["remaining"],
        "total_requests": stats["total_requests"],
        "throttled": stats["throttled"],
        "latency_ms": health.get("latency_ms"),
        "health": health
    }

# ---------- Pipelines & Queues ----------
@router.get("/pipelines/stats")
async def pipeline_stats(db: Session = Depends(get_db)):
    # aggregate PipelineJob
    stats = {}
    for pipeline in ["discovery","application","ai","email","funding"]:
        q = db.query(PipelineJob).filter(PipelineJob.pipeline==pipeline)
        stats[pipeline] = {
            "queued": q.filter(PipelineJob.status=="queued").count(),
            "processing": q.filter(PipelineJob.status=="processing").count(),
            "done": q.filter(PipelineJob.status=="done").count(),
            "failed": q.filter(PipelineJob.status=="failed").count(),
            "needs_input": q.filter(PipelineJob.status=="needs_input").count(),
        }
    # AI pipeline queue
    ai_stats = await ai_pipeline.get_stats()
    stats["ai"] = {**stats.get("ai", {}), **{"ai_queue": ai_stats}}
    stats["rate_limiter"] = rate_limiter.stats()
    return stats

@router.get("/pipelines/jobs")
def pipeline_jobs(pipeline: Optional[str]=None, status: Optional[str]=None, db: Session = Depends(get_db)):
    q = db.query(PipelineJob)
    if pipeline:
        q = q.filter(PipelineJob.pipeline==pipeline)
    if status:
        q = q.filter(PipelineJob.status==status)
    return q.order_by(PipelineJob.created_at.desc()).limit(50).all()

@router.get("/user-input-queue")
def get_input_queue(db: Session = Depends(get_db)):
    return db.query(UserInputRequest).filter(UserInputRequest.status=="pending").order_by(UserInputRequest.created_at.desc()).all()

# ---------- Error Logs ----------
@router.get("/logs", response_model=List[ErrorLogOut])
def get_logs(pipeline: Optional[str]=None, level: Optional[str]=None, db: Session = Depends(get_db)):
    q = db.query(ErrorLog)
    if pipeline:
        q = q.filter(ErrorLog.pipeline==pipeline)
    if level:
        q = q.filter(ErrorLog.level==level)
    return q.order_by(ErrorLog.timestamp.desc()).limit(100).all()

# ---------- Company Classifier ----------
@router.post("/classify/company")
async def classify_company(company: str, jd: str = "", db: Session = Depends(get_db)):
    await ai_pipeline.enqueue("classify", f"Classify {company}", priority=5)
    await rate_limiter.wait_and_acquire(1)
    size, conf = await ai_company_size(company, jd)
    return {"company": company, "size": size, "confidence": conf}

# ---------- Dashboard ----------
@router.get("/dashboard/summary")
def dashboard_summary(db: Session = Depends(get_db)):
    total_jobs = db.query(Job).count()
    applied = db.query(Job).filter(Job.status=="applied").count()
    needs_input = db.query(Job).filter(Job.status=="needs_input").count()
    failed = db.query(Job).filter(Job.status=="failed").count()
    discovered = db.query(Job).filter(Job.status=="discovered").count()
    pending_emails = db.query(Email).filter(Email.status=="pending_approval").count()
    vault_count = db.query(VaultEntry).count()
    resumes = db.query(Resume).count()
    return {
        "jobs": {"total": total_jobs, "discovered": discovered, "applied": applied, "needs_input": needs_input, "failed": failed},
        "emails": {"pending_approval": pending_emails, "total": db.query(Email).count()},
        "vault": vault_count,
        "resumes": resumes,
        "pipelines": {
            "discovery_queued": db.query(PipelineJob).filter(PipelineJob.pipeline=="discovery", PipelineJob.status=="queued").count(),
            "application_queued": db.query(PipelineJob).filter(PipelineJob.pipeline=="application", PipelineJob.status=="queued").count(),
        }
    }

@router.post("/ai/config")
def update_ai_config(payload: dict, db: Session = Depends(get_db)):
    # Allow different AI api for different workflows
    # payload: {workflow: {base_url, api_key, model}}
    for workflow, cfg in payload.items():
        row = db.query(SettingsModel).filter(SettingsModel.category=="ai_workflows", SettingsModel.key==workflow).first()
        if row:
            row.value = cfg
        else:
            row = SettingsModel(category="ai_workflows", key=workflow, value=cfg)
            db.add(row)
    db.commit()
    return {"ok": True}

@router.get("/ai/config")
def get_ai_config(db: Session = Depends(get_db)):
    rows = db.query(SettingsModel).filter(SettingsModel.category=="ai_workflows").all()
    return {r.key: r.value for r in rows}
