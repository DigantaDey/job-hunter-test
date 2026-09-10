from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, Query
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
from app.services.vault import generate_credential, save_vault_entry, list_vault_entries, export_chrome_csv, export_apple_csv, delete_all_vault
from app.services.job_scraper import discover_jobs, detect_form_structure
from app.services.email_pipeline import find_decision_maker, generate_cold_email, send_via_smtp, find_funded_companies
from app.services.ai_pipeline import ai_pipeline
from app.core.rate_limiter import rate_limiter
from app.utils.logger import log_error, log_info
from app.core.security import decrypt_password

router = APIRouter()

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

    return {"resume": {"id": resume.id, "filename": resume.filename, "tags": resume.tags}, "profile": profile_data, "layout": layout, "ai_meta": meta}

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
async def trigger_discovery(keywords: Optional[str]=None, freshness_hours: Optional[int]=None, db: Session = Depends(get_db)):
    # Get settings fallback
    fresh = freshness_hours or settings.default_freshness_hours
    kw_list = [k.strip() for k in (keywords or settings.default_keywords).split(",") if k.strip()]
    # Also merge profile skills
    profile = db.query(Profile).order_by(Profile.created_at.desc()).first()
    if profile and profile.data.get("skills"):
        for s in profile.data["skills"][:3]:
            if s.lower() not in [k.lower() for k in kw_list]:
                kw_list.append(s)

    await ai_pipeline.enqueue("form_detect", f"Discover jobs with {kw_list}", priority=3)
    # Enqueue discovery pipeline job
    pj = PipelineJob(pipeline="discovery", payload={"keywords": kw_list, "freshness_hours": fresh}, status="queued", priority=3)
    db.add(pj)
    db.commit()
    db.refresh(pj)

    # Actually run discovery async
    async def _do():
        # Need new session
        from app.db import SessionLocal
        dbs = SessionLocal()
        try:
            await rate_limiter.wait_and_acquire(1)
            jobs = await discover_jobs(kw_list, fresh)
            for jd in jobs:
                # score and classify
                prof = dbs.query(Profile).order_by(Profile.created_at.desc()).first()
                prof_data = prof.data if prof else {}
                score, reason = await ai_score(prof_data, jd["description"])
                size, conf = await ai_company_size(jd["company"], jd["description"])
                # dedup by url
                exists = dbs.query(Job).filter(Job.url==jd["url"]).first()
                if not exists:
                    job = Job(title=jd["title"], company=jd["company"], location=jd["location"], description=jd["description"], url=jd["url"], source=jd["source"], status="discovered", score=score, score_reason=reason, company_size=size, company_info={"confidence": conf, "forms": jd.get("forms_detected")}, freshness_hours=fresh, extra=jd.get("forms_detected",{}))
                    dbs.add(job)
            # mark pipeline job done
            pj2 = dbs.query(PipelineJob).filter(PipelineJob.id==pj.id).first()
            if pj2:
                pj2.status="done"
            dbs.commit()
            log_info(dbs, "discovery", f"Discovered {len(jobs)} jobs for {kw_list}")
        except Exception as e:
            pj2 = dbs.query(PipelineJob).filter(PipelineJob.id==pj.id).first()
            if pj2:
                pj2.status="failed"
                pj2.error=str(e)
                dbs.commit()
            log_error(dbs, "discovery", str(e))
        finally:
            dbs.close()
    asyncio.create_task(_do())
    return {"queued": True, "pipeline_job_id": pj.id, "keywords": kw_list, "freshness_hours": fresh}

@router.post("/jobs/{job_id}/apply")
async def apply_job(job_id: int, resume_choice: str = "auto", resume_id: Optional[int]=None, db: Session = Depends(get_db)):
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
    elif resume_choice=="auto":
        # scoring decides reuse or generate
        # For demo, if score high, try generated else master
        if job.score >= 75:
            gen = db.query(Resume).filter(Resume.job_id==job_id).first()
            if gen:
                chosen_resume_id = gen.id
            else:
                # generate on fly
                tailored = await generate_tailored_profile(profile.data, job.description, profile.layout, False)
                # quick build
                safe_name = f"resume_{job.id}_{hash_jd(job.description)}"
                docx_path = os.path.join(settings.generated_dir, f"{safe_name}.docx")
                build_docx(tailored["tailored_profile"], profile.data, profile.layout, docx_path)
                build_pdf(tailored["tailored_profile"], profile.data, docx_path.replace(".docx",".pdf"))
                r = Resume(filename=f"{safe_name}.docx", filepath=docx_path, type="generated", profile_snapshot=tailored["tailored_profile"], tags=tailored.get("tags",[]), jd_hash=hash_jd(job.description), job_id=job.id)
                db.add(r)
                db.commit()
                db.refresh(r)
                chosen_resume_id = r.id
        else:
            master = db.query(Resume).filter(Resume.type=="master").order_by(Resume.created_at.desc()).first()
            chosen_resume_id = master.id if master else None

    # Create vault credential if needed
    forms = job.extra or {}
    if forms.get("requires_login"):
        domain = job.url.split("/")[2] if "://" in job.url else job.company.lower().replace(" ","")+".com"
        cred = generate_credential(domain)
        save_vault_entry(db, domain, cred["username"], cred["password"])
        log_info(db, "application", f"Auto-created vault credential for {domain}", job_id=job.id)

    # Simulate AI autofill + form detection
    await ai_pipeline.enqueue("application_autofill", f"Autofill {job.title} at {job.company}", priority=1)
    await rate_limiter.wait_and_acquire(1)

    # Check for missing fields -> user input needed queue
    required = forms.get("fields", [])
    missing = [f for f in required if f["name"] in ["workAuthorization","linkedin"] and f["required"]]
    # Also detect if external redirect
    is_external = job.source in ["linkedin","indeed","naukri"] and forms.get("portal_type")=="simple"  # simplified

    if missing:
        # Create user input request
        uir = UserInputRequest(job_id=job.id, fields=[{"name": f["name"], "label": f["name"], "type": f["type"], "required": True, "value": ""} for f in missing], status="pending")
        db.add(uir)
        job.status = "needs_input"
        # Also pipeline job
        pj = PipelineJob(pipeline="application", job_id=job.id, payload={"resume_id": chosen_resume_id, "reason": "needs_input"}, status="needs_input", priority=1)
        db.add(pj)
        db.commit()
        return {"status": "needs_input", "missing_fields": missing, "input_request_id": uir.id, "message": "Please complete required fields"}

    # Normal apply flow
    job.status = "applying"
    job.applied_with_resume_id = chosen_resume_id
    pj = PipelineJob(pipeline="application", job_id=job.id, payload={"resume_id": chosen_resume_id}, status="processing", priority=1)
    db.add(pj)
    db.commit()

    # Simulate autofill success after short delay via background
    async def _simulate_apply():
        await asyncio.sleep(1.5)
        from app.db import SessionLocal
        dbs = SessionLocal()
        try:
            j = dbs.query(Job).filter(Job.id==job_id).first()
            p = dbs.query(PipelineJob).filter(PipelineJob.job_id==job_id, PipelineJob.pipeline=="application").order_by(PipelineJob.created_at.desc()).first()
            if j:
                # 85% success
                import random
                if random.random() < 0.85:
                    j.status = "applied"
                    if p:
                        p.status="done"
                else:
                    j.status = "failed"
                    j.error = "Auto-fill failed: CAPTCHA detected"
                    if p:
                        p.status="failed"
                        p.error=j.error
                    log_error(dbs, "application", j.error, job_id=j.id)
                dbs.commit()
        finally:
            dbs.close()
    asyncio.create_task(_simulate_apply())

    # If external redirect, note handling
    if is_external:
        log_info(db, "application", f"LinkedIn/Indeed external redirect detected for {job.company}, switching to credential+autofill workflow", job_id=job.id)

    return {"status": job.status, "resume_id": chosen_resume_id, "vault_created": forms.get("requires_login", False)}

@router.post("/jobs/{job_id}/input")
def submit_input(job_id: int, payload: dict, db: Session = Depends(get_db)):
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
    job.status = "queued"
    # re-queue
    pj = PipelineJob(pipeline="application", job_id=job_id, payload={"resumed": True, "input": payload}, status="queued", priority=1)
    db.add(pj)
    db.commit()
    log_info(db, "application", f"User input completed for {job.company}, re-queued", job_id=job_id)
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
def send_email(email_id: int, smtp: Optional[dict]=None, otp: Optional[str]=None, db: Session = Depends(get_db)):
    e = db.query(Email).filter(Email.id==email_id).first()
    if not e:
        raise HTTPException(404)
    # Get SMTP from settings
    smtp_config = smtp or {}
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

# ---------- Funding ----------
@router.get("/funding/companies")
async def get_funding_companies(stage: Optional[str]=None, db: Session = Depends(get_db)):
    stages = [stage] if stage else None
    companies = await find_funded_companies(stages)
    # sync with db
    for c in companies:
        exists = db.query(FundingCompany).filter(FundingCompany.name==c["name"]).first()
        if not exists:
            fc = FundingCompany(name=c["name"], stage=c["stage"], website=c["website"], has_open_positions=c["has_open_positions"])
            db.add(fc)
    db.commit()
    return companies

@router.post("/funding/{company_name}/process")
async def process_funding_company(company_name: str, db: Session = Depends(get_db)):
    # Check if has open positions -> apply usual flow else cold email founder
    comps = await find_funded_companies()
    comp = next((c for c in comps if c["name"].lower()==company_name.lower()), None)
    if not comp:
        raise HTTPException(404)
    profile = db.query(Profile).order_by(Profile.created_at.desc()).first()
    if not profile:
        raise HTTPException(400, "No profile")
    if comp["has_open_positions"]:
        # create a mock job and trigger discovery apply
        job = Job(title="Open Role at "+comp["name"], company=comp["name"], location="Remote", description=f"Join {comp['name']} funded at {comp['stage']}", url=f"https://{comp['website']}/careers", source="custom", status="discovered", score=78, company_size="startup")
        db.add(job)
        db.commit()
        db.refresh(job)
        return {"action": "apply_flow", "job_id": job.id, "message": "Open position found, queued for application"}
    else:
        # cold email founder
        decision = await find_decision_maker(comp["name"], "founder")
        email_content = await generate_cold_email(profile.data, comp["name"], founder=True)
        email = Email(to_email=decision["email"], to_name=decision["name"], subject=email_content["subject"], body=email_content["body"], status="pending_approval", company=comp["name"], recipient_type="founder")
        db.add(email)
        db.commit()
        db.refresh(email)
        return {"action": "cold_email_founder", "email_id": email.id, "decision": decision, "message": "No open position, founder cold email drafted"}

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
