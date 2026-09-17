"""
Interview preparation service — AI-generated questions grounded in resume + JD.
Never fabricates user experience; all questions derived from actual resume and job description.

AI is a hard dependency: on failure the calls raise and the endpoint reports
the outage (pausable 503) — there is no set of canned questions to pretend
with, because a guessed question list is exactly the "output the user never
asked for" this product must not produce.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.core.logging import get_logger
from app.services.ai_client import fit_prompt_part, input_budget_chars

log = get_logger("app.interview")

#: Starting output budgets (the user's configured output ceiling still caps
#: these and any escalation).
QUESTIONS_OUTPUT_TOKENS = 2000
FEEDBACK_OUTPUT_TOKENS = 1000


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
    # Sanitize JD as untrusted, capped at the user's input budget.
    budget = input_budget_chars(db=db, user_id=user_id)
    safe_jd, _t1 = fit_prompt_part((job_description or "").replace("```", "").replace("SYSTEM:", ""),
                                   budget, label="interview.jd")
    profile_json, _t2 = fit_prompt_part(json.dumps(profile, indent=2), budget, label="interview.profile")

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

    # AI failure propagates: the endpoint reports the outage instead of
    # serving canned questions dressed up as preparation.
    from app.services.ai_client import chat_completion

    data = await chat_completion(
        "interview",
        prompt,
        temperature=0.7,
        max_tokens=QUESTIONS_OUTPUT_TOKENS,
        db=db,
        user_id=user_id,
     stream=True)
    questions = data.get("questions", [])[:count] if isinstance(data, dict) else []
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
    if not cleaned:
        from app.services.ai_client import AIClientError

        raise AIClientError(
            "invalid_json: interview returned no usable questions",
            reason="invalid_json", retryable=True,
        )
    return cleaned


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
    budget = input_budget_chars(db=db, user_id=user_id)
    safe_answer, _t1 = fit_prompt_part((user_answer or "").replace("```", ""), budget, label="interview.answer")
    safe_q, _t2 = fit_prompt_part((question or "").replace("```", ""), budget, label="interview.question")
    profile_json, _t3 = fit_prompt_part(json.dumps(profile, indent=2), budget, label="interview.profile")
    prompt = f"""
You are an interview coach giving feedback.

Question: {safe_q}
Candidate answer: \"\"\"{safe_answer}\"\"\"

Profile (for grounding): {profile_json}

Return JSON: {{"score": 1-10, "strengths": ["..."], "improvements": ["..."], "suggested_answer": "improved version grounded in profile", "follow_up_questions": ["..."]}}

Rules:
- Be constructive, specific
- Don't invent experience
- Score honestly
"""

    # AI failure propagates — a fake 5/10 "score" would be a verdict the model
    # never gave.
    from app.services.ai_client import chat_completion

    data = await chat_completion(
        "interview",
        prompt,
        temperature=0.5,
        max_tokens=FEEDBACK_OUTPUT_TOKENS,
        db=db,
        user_id=user_id,
     stream=True)
    return {
        "score": int(data.get("score", 5)),
        "strengths": (data.get("strengths") or [])[:5] if isinstance(data, dict) else [],
        "improvements": (data.get("improvements") or [])[:5] if isinstance(data, dict) else [],
        "suggested_answer": str(data.get("suggested_answer", ""))[:2000] if isinstance(data, dict) else "",
        "follow_up_questions": (data.get("follow_up_questions") or [])[:3] if isinstance(data, dict) else [],
    }
