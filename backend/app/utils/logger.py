from sqlalchemy.orm import Session
from app.models.models import ErrorLog
from datetime import datetime

def log_error(db: Session, pipeline: str, message: str, level: str = "error", job_id: int = None, meta: dict = None):
    entry = ErrorLog(pipeline=pipeline, message=message, level=level, job_id=job_id, meta=meta or {})
    db.add(entry)
    db.commit()
    return entry

def log_info(db: Session, pipeline: str, message: str, job_id: int = None, meta: dict = None):
    return log_error(db, pipeline, message, level="info", job_id=job_id, meta=meta)
