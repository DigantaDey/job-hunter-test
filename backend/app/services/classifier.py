"""Company-size classification.

AI is a hard dependency on this path: when the model cannot be reached the
call raises (``AIClientError`` / ``AIUnavailableError``) and the product
pauses or reports it — it never substitutes a guessed size. The deterministic
estimator below exists only for the *bulk* labelling design (only the top-N
jobs of a discovery run get the AI verdict) and is always provenance-labelled
(``company_size_confidence``); it is not a substitute for the AI call.
"""
import json
from typing import Any, Dict, Tuple

from app.services.ai_client import fit_prompt_part, input_budget_chars


def deterministic_company_size(company_info: Dict[str, Any], job: Dict[str, Any]) -> str:
    """Bulk-labelling estimator for jobs that are *not* in the AI top-N slice.

    Prefers real data the source supplied (``employees``/``size``), then
    signals in the text. The result is provenance-labelled by the caller and
    is never presented as an AI verdict.
    """
    text = (job.get("description", "") + " " + job.get("company", "") + " " + json.dumps(company_info)).lower()
    employees = company_info.get("employees") or company_info.get("size") or ""
    if isinstance(employees, int):
        if employees > 5000:
            return "big"
        if employees > 500:
            return "medium"
        if employees > 50:
            return "small"
        return "startup"
    if any(k in text for k in ["fortune 500", "enterprise", "10000+ employees", "public company"]):
        return "big"
    if any(k in text for k in ["series a", "series b", "startup", "seed", "stealth"]):
        return "startup"
    if any(k in text for k in ["scaleup", "unicorn"]):
        return "medium"
    return "small"


async def ai_company_size(company: str, jd: str, ai_config=None, *, db=None, user_id=None) -> Tuple[str, float]:
    """Classify company size with the model.

    Raises ``AIClientError`` when AI is unavailable — callers either surface
    it (sync endpoints → pausable 503) or treat it as the discovery-run AI
    outage (the job keeps ``size='unknown'`` and its ``ai_error`` diagnosis).
    """
    budget = input_budget_chars(db=db, user_id=user_id, ai_config=ai_config)
    safe_jd, _truncated = fit_prompt_part(jd, budget, label="classify.jd")
    prompt = f"""Classify company size for "{company}" based on job description.
Categories: big (>5000 employees/enterprise/public), medium (500-5000/scaleup), small (50-500), startup (<50 or seed-Series B).
Return JSON {{"size": "big|medium|small|startup", "confidence": 0-1, "reason": ""}}
JD: {safe_jd}"""
    from app.services.ai_client import chat_completion

    data = await chat_completion("classify", prompt, temperature=0.1, ai_config=ai_config, db=db, user_id=user_id)
    size = str(data.get("size", "")).strip().lower()
    if size not in ("big", "medium", "small", "startup"):
        # The model answered but not with a usable category — that is an
        # unusable answer, not a size. Surface it, don't guess.
        from app.services.ai_client import AIClientError

        raise AIClientError(
            f"invalid_json: classify returned an unknown size category {size!r}",
            reason="invalid_json", retryable=True,
        )
    conf = float(data.get("confidence", 0.7))
    return size, max(0.0, min(1.0, conf))
