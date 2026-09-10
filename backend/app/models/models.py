from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, Float, JSON, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from app.db import Base

class Profile(Base):
    __tablename__ = "profiles"
    id = Column(Integer, primary_key=True, index=True)
    data = Column(JSON, default=dict)  # extracted profile details
    layout = Column(JSON, default=dict)  # layout json
    master_resume_id = Column(Integer, ForeignKey("resumes.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Resume(Base):
    __tablename__ = "resumes"
    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, nullable=False)
    filepath = Column(String, nullable=False)
    type = Column(String, default="master")  # master, generated, uploaded_polished
    profile_snapshot = Column(JSON, default=dict)
    layout = Column(JSON, default=dict)
    tags = Column(JSON, default=list)
    jd_hash = Column(String, nullable=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Job(Base):
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    company = Column(String, nullable=False)
    location = Column(String, default="")
    description = Column(Text, default="")
    url = Column(String, default="")
    source = Column(String, default="unknown")  # linkedin, naukri, indeed, workday, lever, greenhouse, custom
    status = Column(String, default="discovered")  # discovered, queued, needs_input, applying, applied, failed, emailed
    score = Column(Float, default=0.0)
    score_reason = Column(Text, default="")
    company_size = Column(String, default="unknown")  # big, medium, small, startup
    company_info = Column(JSON, default=dict)
    discovered_at = Column(DateTime, default=datetime.utcnow)
    freshness_hours = Column(Integer, default=24)
    applied_with_resume_id = Column(Integer, ForeignKey("resumes.id"), nullable=True)
    extra = Column(JSON, default=dict)  # forms structure, questions, etc
    error = Column(Text, default="")

class VaultEntry(Base):
    __tablename__ = "vault_entries"
    id = Column(Integer, primary_key=True, index=True)
    domain = Column(String, nullable=False)
    username = Column(String, nullable=False)
    password_enc = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Email(Base):
    __tablename__ = "emails"
    id = Column(Integer, primary_key=True, index=True)
    to_email = Column(String, nullable=False)
    to_name = Column(String, default="")
    subject = Column(String, default="")
    body = Column(Text, default="")
    status = Column(String, default="draft")  # draft, pending_approval, queued, sent, failed, needs_otp
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    company = Column(String, default="")
    recipient_type = Column(String, default="hiring_manager")  # hiring_manager, founder
    created_at = Column(DateTime, default=datetime.utcnow)
    sent_at = Column(DateTime, nullable=True)
    error = Column(Text, default="")

class SettingsModel(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True)
    category = Column(String, nullable=False)
    key = Column(String, nullable=False)
    value = Column(JSON, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class ErrorLog(Base):
    __tablename__ = "error_logs"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    pipeline = Column(String, default="general")
    level = Column(String, default="error")
    message = Column(Text, default="")
    job_id = Column(Integer, nullable=True)
    meta = Column(JSON, default=dict)

class UserInputRequest(Base):
    __tablename__ = "user_input_requests"
    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"))
    fields = Column(JSON, default=list)  # [{name, label, type, required, value}]
    status = Column(String, default="pending")  # pending, completed
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

class PipelineJob(Base):
    __tablename__ = "pipeline_jobs"
    id = Column(Integer, primary_key=True, index=True)
    pipeline = Column(String, nullable=False)  # discovery, application, ai, email, funding
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    payload = Column(JSON, default=dict)
    status = Column(String, default="queued")  # queued, processing, done, failed, needs_input
    priority = Column(Integer, default=5)  # 1 highest
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    attempts = Column(Integer, default=0)
    error = Column(Text, default="")

class FundingCompany(Base):
    __tablename__ = "funding_companies"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    stage = Column(String, default="Series A")  # Seed, Series A-D
    raised_at = Column(DateTime, default=datetime.utcnow)
    website = Column(String, default="")
    industry = Column(String, default="")
    summary = Column(Text, default="")
    keywords_matched = Column(JSON, default=list)
    source = Column(String, default="curated-demo")  # ai, curated-demo
    has_open_positions = Column(Boolean, default=False)
    discovered_at = Column(DateTime, default=datetime.utcnow)
    meta = Column(JSON, default=dict)
