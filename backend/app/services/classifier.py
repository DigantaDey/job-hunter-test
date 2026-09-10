import re
import json
import httpx
from typing import Dict, Any, Tuple
from app.core.config import settings

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
    if not settings.ai_api_key:
        size = heuristic_company_size({}, {"company": company, "description": jd})
        return size, 0.6
    base_url = (ai_config or {}).get("base_url") or settings.ai_base_url
    api_key = (ai_config or {}).get("api_key") or settings.ai_api_key
    model = (ai_config or {}).get("model") or settings.ai_model
    prompt = f"""Classify company size for "{company}" based on job description.
Categories: big (>5000 employees/enterprise/public), medium (500-5000/scaleup), small (50-500), startup (<50 or seed-Series B).
Return JSON {{"size": "big|medium|small|startup", "confidence": 0-1, "reason": ""}}
JD: {jd[:2000]}"""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type":"application/json"},
                json={"model": model, "messages":[{"role":"user","content": prompt}], "temperature":0.1, "response_format":{"type":"json_object"}})
            if resp.status_code==200:
                data=json.loads(resp.json()["choices"][0]["message"]["content"])
                return data.get("size","small"), float(data.get("confidence",0.7))
    except:
        pass
    return heuristic_company_size({}, {"company": company, "description": jd}), 0.6
