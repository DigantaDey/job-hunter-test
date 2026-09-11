"""
Interview preparation service — AI-generated questions grounded in resume + JD.
Never fabricates user experience; all questions derived from actual resume and job description.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.services.ai_client import AIClientError, chat_completion

log = get_logger("app.interview")


INTERVIEW_CATEGORIES = [
    "technical",
    "behavioral",
    "situational",
    "company",
    "role_specific",
]


async def generate_interview_questions(
    profile: Dict[str, Any],
    job_title: str,
    company: str,
    job_description: str,
    count: int = 10,
    db=None,
    user_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Generate interview questions based on profile + JD.
    Grounded in user-provided info — never invents jobs/education.
    """
    # Sanitize JD as untrusted
    safe_jd = (job_description or "")[:4000].replace("```", "").replace("SYSTEM:", "")
    profile_json = json.dumps(profile, indent=2)[:4000]

    prompt = f"""
You are an expert interview coach. Generate {count} interview questions for this candidate.

RULES:
- Never fabricate experience, education, skills not in the profile
- Ground questions in actual resume + job description
- Mix technical, behavioral, situational
- Be specific to the role and company
- Return JSON: {{"questions": [{{"question": "...", "category": "technical|behavioral|situational|company|role_specific", "difficulty": "easy|medium|hard", "hint": "what interviewer looks for", "sample_answer_outline": "bullet outline grounded in profile"}}]}}

Profile:
{profile_json}

Job: {job_title} at {company}
JD:
\"\"\"{safe_jd}\"\"\"
"""

    try:
        data = await chat_completion(
            "interview",
            prompt,
            temperature=0.7,
            max_tokens=2000,
            db=db,
            user_id=user_id,
        )
        questions = data.get("questions", [])[:count]
        # Validate structure
        cleaned = []
        for q in questions:
            if not isinstance(q, dict):
                continue
            if "question" not in q:
                continue
            cleaned.append({
                "question": str(q["question"])[:500],
                "category": q.get("category", "technical") if q.get("category") in INTERVIEW_CATEGORIES else "technical",
                "difficulty": q.get("difficulty", "medium"),
                "hint": str(q.get("hint", ""))[:500],
                "sample_answer_outline": str(q.get("sample_answer_outline", ""))[:1000],
            })
        if cleaned:
            return cleaned
    except (AIClientError, Exception) as exc:
        log.warning("interview question generation failed: %s", exc)

    # Fallback heuristic questions
    skills = profile.get("skills", [])[:5]
    return [
        {"question": f"Tell me about your experience with {skills[0]} as it relates to {job_title}?" if skills else f"Tell me about yourself and why you're interested in {job_title} at {company}?", "category": "behavioral", "difficulty": "easy", "hint": "STAR method", "sample_answer_outline": "Situation, Task, Action, Result grounded in your experience"},
        {"question": f"How would you handle a challenging situation in {job_title} role?", "category": "situational", "difficulty": "medium", "hint": "Problem solving", "sample_answer_outline": "Describe approach"},
        {"question": f"What do you know about {company} and why do you want to work here?", "category": "company", "difficulty": "easy", "hint": "Company research", "sample_answer_outline": f"Mention {company} mission, recent news"},
    ][:count]


async def generate_feedback(
    question: str,
    user_answer: str,
    profile: Dict[str, Any],
    job_description: str = "",
    db=None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Generate feedback for user's interview answer.
    Grounded, constructive, no fabrication.
    """
    safe_answer = (user_answer or "")[:3000].replace("```", "")
    safe_q = (question or "")[:500].replace("```", "")
    prompt = f"""
You are an interview coach giving feedback.

Question: {safe_q}
Candidate answer: \"\"\"{safe_answer}\"\"\"

Profile (for grounding): {json.dumps(profile, indent=2)[:2000]}

Return JSON: {{"score": 1-10, "strengths": ["..."], "improvements": ["..."], "suggested_answer": "improved version grounded in profile", "follow_up_questions": ["..."]}}

Rules:
- Be constructive, specific
- Don't invent experience
- Score honestly
"""

    try:
        data = await chat_completion(
            "interview",
            prompt,
            temperature=0.5,
            max_tokens=1000,
            db=db,
            user_id=user_id,
        )
        return {
            "score": int(data.get("score", 5)),
            "strengths": data.get("strengths", [])[:5],
            "improvements": data.get("improvements", [])[:5],
            "suggested_answer": str(data.get("suggested_answer", ""))[:2000],
            "follow_up_questions": data.get("follow_up_questions", [])[:3],
        }
    except Exception as exc:
        log.warning("feedback generation failed: %s", exc)
        return {
            "score": 5,
            "strengths": ["Answer provided"],
            "improvements": ["Add specific examples using STAR method"],
            "suggested_answer": "",
            "follow_up_questions": [],
        }
