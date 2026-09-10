"""Pydantic response/request models."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProfileOut(ORMModel):
    id: int
    data: Dict[str, Any]
    layout: Dict[str, Any]
    created_at: datetime


class ResumeOut(ORMModel):
    id: int
    filename: str
    type: str
    status: Optional[str] = "approved"
    tags: List[str]
    created_at: datetime
    jd_hash: Optional[str] = None
    job_id: Optional[int] = None
    parent_resume_id: Optional[int] = None
    approved_at: Optional[datetime] = None


class JobOut(ORMModel):
    id: int
    title: str
    company: str
    location: str
    url: str
    source: str
    status: str
    score: float
    company_size: str
    discovered_at: datetime
    posted_at: Optional[datetime] = None
    applied_at: Optional[datetime] = None
    error: str


class JobDetail(JobOut):
    description: str
    score_reason: str
    company_info: Dict[str, Any]
    extra: Dict[str, Any]
    applied_with_resume_id: Optional[int] = None


class VaultOut(ORMModel):
    id: int
    domain: str
    username: str
    origin: str = "auto"
    created_at: datetime
    last_used_at: Optional[datetime] = None


class EmailOut(ORMModel):
    id: int
    to_email: str
    to_name: str
    subject: str
    body: str
    status: str
    company: str
    recipient_type: str
    source: str = "heuristic"
    confidence: float = 0.0
    opens: int = 0
    dry_run: bool = True
    created_at: datetime
    sent_at: Optional[datetime] = None


class ErrorLogOut(ORMModel):
    id: int
    timestamp: datetime
    pipeline: str
    level: str
    message: str
    job_id: Optional[int] = None
    request_id: str = ""


class PipelineStats(BaseModel):
    pipeline: str
    queued: int
    processing: int
    done: int
    failed: int
    needs_input: int
    dead: int = 0


class AIStatus(BaseModel):
    online: bool
    configured: bool = False
    rpm: int
    used_in_window: int
    remaining: int
    total_requests: int
    throttled: int
    latency_ms: Optional[int] = None


class UserOut(ORMModel):
    id: int
    email: str
    name: str
    role: str
    is_active: bool
    created_at: datetime


class JobEventOut(ORMModel):
    id: int
    job_id: int
    stage: str
    status: str
    message: str
    meta: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ApplicationResult(BaseModel):
    job_id: int
    status: str
    resume_id: Optional[int] = None
    resume_decision: Optional[str] = None
    portal_type: Optional[str] = None
    fields_mapped: Optional[int] = None
    fields_total: Optional[int] = None
    missing_fields: List[Dict[str, Any]] = Field(default_factory=list)
    vault_created: bool = False
    message: Optional[str] = None
