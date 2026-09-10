import json
from typing import Any, Dict, Tuple

from app.services.ai_client import AIClientError, chat_completion


def heuristic_company_size(company_info: Dict[str,Any], job: Dict[str,Any]) -> str:
    text = (job.get("description","") + " " + job.get("company","") + " " + json.dumps(company_info)).lower()
    # rules
    employees = company_info.get("employees") or company_info.get("size") or ""
    if isinstance(employees, int):
        if employees > 5000:
            return "big"
        if employees > 500:
            return "medium"
        if employees > 50:
            return "small"
        return "startup"
    # textual heuristics
    if any(k in text for k in ["fortune 500", "enterprise", "10000+ employees", "public company"]):
        return "big"
    if any(k in text for k in ["series a", "series b", "startup", "seed", "stealth"]):
        return "startup"
    if any(k in text for k in ["scaleup", "unicorn"]):
        return "medium"
    # default by company name frequency? mock
    return "small"

async def ai_company_size(company: str, jd: str, ai_config=None) -> Tuple[str, float]:
    prompt = f"""Classify company size for "{company}" based on job description.
Categories: big (>5000 employees/enterprise/public), medium (500-5000/scaleup), small (50-500), startup (<50 or seed-Series B).
Return JSON {{"size": "big|medium|small|startup", "confidence": 0-1, "reason": ""}}
JD: {jd[:2000]}"""
    try:
        data = await chat_completion("classify", prompt, temperature=0.1, timeout=15, ai_config=ai_config)
        size = str(data.get("size", "small")).strip().lower()
        if size not in ("big", "medium", "small", "startup"):
            size = "small"
        conf = float(data.get("confidence", 0.7))
        return size, max(0.0, min(1.0, conf))
    except (AIClientError, TypeError, ValueError):
        return heuristic_company_size({}, {"company": company, "description": jd}), 0.6
