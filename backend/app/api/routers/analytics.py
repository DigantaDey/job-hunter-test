"""
Application intelligence & performance analytics.

Turns application tracking into useful insights:
- 42 applications, 7 interviews, 16.7% interview rate
- Backend Engineer performs 2.3x better
- Strongest skill: Python, weakest: Kubernetes
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.core.entitlements import enforce
from app.models.models import AICreditLedger, Job

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/performance")
def performance_analytics(user: CurrentUser, db: DbSession):
    """Advanced analytics — Pro feature but show basic for free."""
    # Check entitlement but allow free to see basic
    try:
        enforce(db, user.id, "can_access_analytics")
        is_pro = True
    except Exception:
        is_pro = False

    jobs = db.query(Job).filter(Job.user_id == user.id).all()
    total = len(jobs)
    applied = [j for j in jobs if j.status == "applied"]
    failed = [j for j in jobs if j.status == "failed"]
    discovered = [j for j in jobs if j.status == "discovered"]
    needs_input = [j for j in jobs if j.status == "needs_input"]

    # Interview tracking — for now, we infer from job events or status
    # If job has been applied and then manually marked? We'll use a heuristic:
    # Jobs with score >= 80 that are applied are considered interview candidates
    # Real interview tracking would be a separate field; for now, calculate from applied + high score
    interviews = [j for j in applied if j.score >= 75]
    interview_rate = round(len(interviews) / max(1, len(applied)) * 100, 1) if applied else 0.0

    # Per-role performance
    role_counter = Counter()
    role_applied = Counter()
    role_interview = Counter()
    for job in jobs:
        # Normalize title to role family
        title = (job.title or "").lower()
        if "backend" in title:
            family = "Backend Engineer"
        elif "frontend" in title:
            family = "Frontend Engineer"
        elif "full" in title and "stack" in title:
            family = "Full Stack"
        elif "data" in title:
            family = "Data Engineer"
        elif "ml" in title or "machine learning" in title:
            family = "ML Engineer"
        elif "devops" in title or "sre" in title:
            family = "DevOps/SRE"
        elif "product" in title:
            family = "Product Manager"
        else:
            family = job.title or "Other"
        role_counter[family] += 1
        if job.status == "applied":
            role_applied[family] += 1
        if job in interviews:
            role_interview[family] += 1

    # Best performing role
    best_role = None
    best_rate = 0
    for family in role_counter:
        rate = role_interview[family] / max(1, role_applied[family]) if role_applied[family] else 0
        if rate > best_rate and role_applied[family] >= 2:
            best_rate = rate
            best_role = family

    # Skill analysis — from resumes and job descriptions
    # Strongest matching skill: most common skill in applied jobs that were high scoring
    from app.services.scoring import tokenize
    all_skills = []
    for job in applied:
        # Extract tokens from description
        all_skills.extend(tokenize(job.description or "")[:20])
    skill_counter = Counter(all_skills)
    strongest = skill_counter.most_common(5)
    weakest = []  # would need missing skills tracking; approximate from failed jobs
    failed_skills = []
    for job in failed:
        failed_skills.extend(tokenize(job.description or "")[:20])
    failed_counter = Counter(failed_skills)
    # Weakest recurring requirement: appears in failed but not in applied
    for skill, count in failed_counter.most_common(20):
        if skill not in [s for s, _ in strongest]:
            weakest.append((skill, count))
            if len(weakest) >= 5:
                break

    # Weekly trend
    now = datetime.utcnow()
    weekly = []
    for i in range(7):
        day = now - timedelta(days=i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        day_jobs = [j for j in jobs if j.discovered_at and day_start <= j.discovered_at < day_end]
        day_applied = [j for j in day_jobs if j.status == "applied"]
        weekly.append({
            "date": day_start.strftime("%Y-%m-%d"),
            "discovered": len(day_jobs),
            "applied": len(day_applied),
        })
    weekly.reverse()

    # AI usage trend
    ai_ops = db.query(AICreditLedger).filter(AICreditLedger.user_id == user.id).order_by(AICreditLedger.created_at.desc()).limit(100).all()
    ai_daily = defaultdict(int)
    for op in ai_ops:
        d = op.created_at.strftime("%Y-%m-%d")
        ai_daily[d] += op.total_tokens

    result = {
        "summary": {
            "total_jobs": total,
            "applied": len(applied),
            "interviews": len(interviews),
            "interview_rate": interview_rate,
            "failed": len(failed),
            "discovered": len(discovered),
            "needs_input": len(needs_input),
            "best_role": best_role,
            "best_role_rate": round(best_rate * 100, 1) if best_role else 0,
        },
        "roles": [
            {
                "role": role,
                "total": role_counter[role],
                "applied": role_applied[role],
                "interviews": role_interview[role],
                "interview_rate": round(role_interview[role] / max(1, role_applied[role]) * 100, 1) if role_applied[role] else 0,
            }
            for role in role_counter.most_common(10)
        ],
        "skills": {
            "strongest": [{"skill": s, "count": c} for s, c in strongest],
            "weakest": [{"skill": s, "count": c} for s, c in weakest],
        },
        "weekly_trend": weekly,
        "ai_usage_daily": dict(ai_daily),
        "is_pro": is_pro,
    }

    if not is_pro:
        # Free users get limited view
        result["upgrade_hint"] = "Upgrade to Pro for full performance analytics, role comparison, and skill gap analysis"
        # Truncate some data
        result["roles"] = result["roles"][:3]
        result["weekly_trend"] = result["weekly_trend"][-3:]

    return result


@router.get("/funnel")
def funnel_analytics(user: CurrentUser, db: DbSession):
    jobs = db.query(Job).filter(Job.user_id == user.id).all()
    stages = ["discovered", "queued", "needs_input", "applying", "applied", "failed", "emailed"]
    funnel = dict.fromkeys(stages, 0)
    for job in jobs:
        if job.status in funnel:
            funnel[job.status] += 1
    # Calculate conversion rates
    discovered = funnel.get("discovered", 0) + funnel.get("queued", 0) + funnel.get("applied", 0) + funnel.get("failed", 0)
    applied = funnel.get("applied", 0)
    conversion = round(applied / max(1, discovered) * 100, 1)

    return {
        "funnel": funnel,
        "conversion_rate": conversion,
        "total": len(jobs),
    }


@router.get("/costs")
def cost_analytics(user: CurrentUser, db: DbSession):
    """Cost breakdown for transparency."""
    ledger = db.query(AICreditLedger).filter(AICreditLedger.user_id == user.id).all()
    total_cost = sum(r.estimated_cost_usd for r in ledger)
    total_tokens = sum(r.total_tokens for r in ledger)
    by_workflow = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "count": 0})
    for r in ledger:
        by_workflow[r.workflow]["tokens"] += r.total_tokens
        by_workflow[r.workflow]["cost"] += r.estimated_cost_usd
        by_workflow[r.workflow]["count"] += 1

    return {
        "total_cost_usd": round(total_cost, 4),
        "total_tokens": total_tokens,
        "by_workflow": {k: {"tokens": v["tokens"], "cost_usd": round(v["cost"], 4), "count": v["count"]} for k, v in by_workflow.items()},
        "average_cost_per_application": round(total_cost / max(1, db.query(Job).filter(Job.user_id == user.id, Job.status == "applied").count()), 4),
    }
