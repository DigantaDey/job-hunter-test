from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime

class ProfileOut(BaseModel):
    id: int
    data: Dict[str, Any]
    layout: Dict[str, Any]
    created_at: datetime
    class Config:
        from_attributes = True

class ResumeOut(BaseModel):
    id: int
    filename: str
    type: str
    tags: List[str]
    created_at: datetime
    jd_hash: Optional[str]
    job_id: Optional[int]
    class Config:
        from_attributes = True

class JobOut(BaseModel):
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
    error: str
    class Config:
        from_attributes = True

class JobDetail(JobOut):
    description: str
    score_reason: str
    company_info: Dict[str, Any]
    extra: Dict[str, Any]
    applied_with_resume_id: Optional[int]

class VaultOut(BaseModel):
    id: int
    domain: str
    username: str
    created_at: datetime
    class Config:
        from_attributes = True

class EmailOut(BaseModel):
    id: int
    to_email: str
    to_name: str
    subject: str
    body: str
    status: str
    company: str
    recipient_type: str
    created_at: datetime
    class Config:
        from_attributes = True

class SettingsUpdate(BaseModel):
    category: str
    key: str
    value: Any

class ErrorLogOut(BaseModel):
    id: int
    timestamp: datetime
    pipeline: str
    level: str
    message: str
    job_id: Optional[int]
    class Config:
        from_attributes = True

class PipelineStats(BaseModel):
    pipeline: str
    queued: int
    processing: int
    done: int
    failed: int
    needs_input: int

class AIStatus(BaseModel):
    online: bool
    rpm: int
    used_in_window: int
    remaining: int
    total_requests: int
    throttled: int
    latency_ms: Optional[int]
