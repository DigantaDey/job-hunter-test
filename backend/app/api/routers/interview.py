"""
Interview preparation endpoints.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession
from app.core.entitlements import enforce
from app.models.models import InterviewPrep, Job, Profile
from app.services.interview_prep import generate_feedback, generate_interview_questions

router = APIRouter(prefix="/interview", tags=["interview"])


class GenerateRequest(BaseModel):
    job_id: Optional[int] = None
    job_title: str = ""
    company: str = ""
    job_description: str = ""
    count: int = 10


@router.get("")
def list_sessions(user: CurrentUser, db: DbSession):
    rows = db.query(InterviewPrep).filter(InterviewPrep.user_id == user.id).order_by(InterviewPrep.created_at.desc()).all()
    return [
        {
            "id": r.id,
            "job_id": r.job_id,
            "job_title": r.job_title,
            "company": r.company,
            "questions": r.questions,
            "status": r.status,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]


@router.post("/generate")
async def generate_session(payload: GenerateRequest, user: CurrentUser, db: DbSession):
    enforce(db, user.id, "can_use_interview_prep")
    enforce(db, user.id, "interview_sessions_per_month")

    profile = db.query(Profile).filter(Profile.user_id == user.id).order_by(Profile.created_at.desc()).first()
    profile_data = (profile.data if profile else {}) or {}

    job_title = payload.job_title
    company = payload.company
    jd = payload.job_description

    if payload.job_id:
        job = db.query(Job).filter(Job.id == payload.job_id, Job.user_id == user.id).first()
        if not job:
            raise HTTPException(404, "Job not found")
        job_title = job.title
        company = job.company
        jd = job.description

    if not job_title:
        raise HTTPException(400, "job_title or job_id required")

    questions = await generate_interview_questions(
        profile_data, job_title, company, jd, count=min(20, max(3, payload.count)), db=db, user_id=user.id
    )

    from app.core.entitlements import increment_usage
    increment_usage(db, user.id, "interview_sessions_per_month", 1)

    session = InterviewPrep(
        user_id=user.id,
        job_id=payload.job_id,
        job_title=job_title,
        company=company,
        questions=questions,
        answers={},
        feedback={},
        status="draft",
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    return {
        "id": session.id,
        "job_title": session.job_title,
        "company": session.company,
        "questions": session.questions,
        "status": session.status,
        "created_at": session.created_at,
    }


@router.get("/{session_id}")
def get_session(session_id: int, user: CurrentUser, db: DbSession):
    row = db.query(InterviewPrep).filter(InterviewPrep.id == session_id, InterviewPrep.user_id == user.id).first()
    if not row:
        raise HTTPException(404, "Session not found")
    return {
        "id": row.id,
        "job_id": row.job_id,
        "job_title": row.job_title,
        "company": row.company,
        "questions": row.questions,
        "answers": row.answers,
        "feedback": row.feedback,
        "status": row.status,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


class AnswerRequest(BaseModel):
    question_index: int
    answer: str


@router.post("/{session_id}/answer")
async def submit_answer(session_id: int, payload: AnswerRequest, user: CurrentUser, db: DbSession):
    row = db.query(InterviewPrep).filter(InterviewPrep.id == session_id, InterviewPrep.user_id == user.id).first()
    if not row:
        raise HTTPException(404, "Session not found")

    questions = row.questions or []
    if payload.question_index < 0 or payload.question_index >= len(questions):
        raise HTTPException(400, "Invalid question_index")

    profile = db.query(Profile).filter(Profile.user_id == user.id).order_by(Profile.created_at.desc()).first()
    profile_data = (profile.data if profile else {}) or {}

    q = questions[payload.question_index]
    feedback = await generate_feedback(
        q.get("question", ""),
        payload.answer,
        profile_data,
        db=db,
        user_id=user.id,
    )

    answers = dict(row.answers or {})
    answers[str(payload.question_index)] = payload.answer
    row.answers = answers

    fb = dict(row.feedback or {})
    fb[str(payload.question_index)] = feedback
    row.feedback = fb
    row.status = "in_progress"
    row.updated_at = datetime.utcnow()
    db.commit()

    return {"ok": True, "feedback": feedback}


@router.post("/{session_id}/complete")
def complete_session(session_id: int, user: CurrentUser, db: DbSession):
    row = db.query(InterviewPrep).filter(InterviewPrep.id == session_id, InterviewPrep.user_id == user.id).first()
    if not row:
        raise HTTPException(404, "Session not found")
    row.status = "completed"
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "status": row.status}


@router.delete("/{session_id}")
def delete_session(session_id: int, user: CurrentUser, db: DbSession):
    row = db.query(InterviewPrep).filter(InterviewPrep.id == session_id, InterviewPrep.user_id == user.id).first()
    if not row:
        raise HTTPException(404, "Session not found")
    db.delete(row)
    db.commit()
    return {"ok": True}
