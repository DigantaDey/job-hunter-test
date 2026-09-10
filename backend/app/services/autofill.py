"""
Browser autofill.

Two layers, deliberately separated so the risky part is testable and gated:

1. ``build_autofill_plan`` — pure, deterministic field mapping from the detected
   form schema + the user profile + the vault credential. Fully unit-testable and
   safe to run anywhere.
2. ``execute_autofill`` — optional Playwright execution. It is skipped unless the
   operator enabled autofill AND the user accepted the automation disclosure AND
   ``AUTOFILL_DRY_RUN`` is off. Submitting is a *third*, separate opt-in
   (``AUTOFILL_ALLOW_SUBMIT`` + per-user ``allow_auto_submit``).

When Playwright (or its browsers) is not installed the function returns an
honest ``status="unavailable"`` result instead of pretending to apply.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc

log = get_logger("app.autofill")

PROFILE_PATHS = {
    "firstName": ("firstName", "first_name"),
    "lastName": ("lastName", "last_name"),
    "fullName": ("name", "fullName"),
    "email": ("email",),
    "phone": ("phone", "mobile"),
    "location": ("location", "city"),
    "linkedin": ("linkedin", "linkedin_url"),
    "github": ("github", "website", "portfolio"),
    "salaryExpectation": ("salary_expectation", "expected_salary"),
    "noticePeriod": ("notice_period", "availability"),
    "workAuthorization": ("work_authorization", "workAuthorization"),
}


def _value_for(key: str, profile: Dict[str, Any], answers: Dict[str, Any]) -> Optional[str]:
    if key in answers and answers[key] not in (None, ""):
        return str(answers[key])
    for candidate in PROFILE_PATHS.get(key, (key,)):
        if profile.get(candidate) not in (None, "", []):
            value = profile[candidate]
            return ", ".join(map(str, value)) if isinstance(value, list) else str(value)

    # Derive name parts from the full name so a form asking for "First name"
    # does not needlessly push the application into the User Input queue.
    full_name = str(profile.get("name") or profile.get("fullName") or "").strip()
    if full_name:
        parts = full_name.split()
        if key == "firstName":
            return parts[0]
        if key == "lastName":
            return " ".join(parts[1:]) if len(parts) > 1 else None
        if key == "fullName":
            return full_name
    return None


def build_autofill_plan(
    *,
    schema: Dict[str, Any],
    profile: Dict[str, Any],
    resume_path: Optional[str] = None,
    cover_letter_path: Optional[str] = None,
    credential: Optional[Dict[str, str]] = None,
    answers: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Map every detected form field to a value we can fill (or flag as unknown)."""
    answers = answers or {}
    fields: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []

    for field in schema.get("fields") or []:
        key = field.get("profile_key")
        name = field.get("name")
        value: Any = None
        source = "profile"

        if field.get("type") == "file":
            if key == "coverLetter" or name in ("coverLetter", "cover_letter"):
                value = cover_letter_path
                source = "generated_cover_letter"
            else:
                value = resume_path
                source = "resume_file"
        elif key:
            value = _value_for(key, profile, answers)
        elif field.get("type") == "password":
            value = (credential or {}).get("password")
            source = "vault"
        else:
            # Voluntary/demographic questions are left blank unless the user
            # provided an explicit answer — never invented.
            value = answers.get(name)
            source = "user_answer"

        entry = {
            "name": name,
            "label": field.get("label", name),
            "type": field.get("type", "text"),
            "required": bool(field.get("required")),
            "profile_key": key,
            "value": value,
            "value_source": source if value not in (None, "") else "unknown",
        }
        fields.append(entry)
        if entry["required"] and value in (None, ""):
            missing.append({"name": name, "label": entry["label"], "type": entry["type"]})

    return {
        "portal_type": schema.get("portal_type", "custom"),
        "requires_login": bool(schema.get("requires_login")),
        "vault_domain": schema.get("vault_domain", ""),
        "fields": fields,
        "missing_required": missing,
        "fillable": sum(1 for f in fields if f["value"] not in (None, "")),
        "total_fields": len(fields),
        "generated_at": datetime.utcnow().isoformat(),
    }


def autofill_available() -> Dict[str, Any]:
    """Report whether real browser automation can run in this deployment."""
    try:
        import playwright  # noqa: F401
    except Exception:
        return {"available": False, "reason": "playwright is not installed (pip install playwright && playwright install chromium)"}
    if not settings.autofill_enabled:
        return {"available": False, "reason": "AUTOFILL_ENABLED is false"}
    return {"available": True, "dry_run": settings.autofill_dry_run}


async def execute_autofill(
    *,
    url: str,
    plan: Dict[str, Any],
    credential: Optional[Dict[str, str]] = None,
    allow_submit: bool = False,
    screenshot_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fill the application form in a real browser.

    Returns a structured result; never raises for expected conditions
    (Playwright missing, dry-run, timeout) — those are recorded as statuses and
    surfaced to the user in the job timeline.
    """
    availability = autofill_available()
    if not availability["available"]:
        return {"status": "unavailable", "reason": availability["reason"], "filled": 0, "submitted": False}

    dry_run = settings.autofill_dry_run or not allow_submit
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover - optional dependency
        return {"status": "unavailable", "reason": f"playwright import failed: {exc}", "filled": 0, "submitted": False}

    filled: List[str] = []
    skipped: List[str] = []
    submitted = False
    error: Optional[str] = None

    try:  # pragma: no cover - requires browsers, exercised in e2e environments
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=settings.autofill_headless)
            context = await browser.new_context(user_agent=settings.http_user_agent)
            page = await context.new_page()
            page.set_default_timeout(settings.autofill_timeout_ms)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=settings.autofill_timeout_ms)

                if plan.get("requires_login") and credential:
                    await _attempt_login(page, credential)

                for field in plan.get("fields", []):
                    if field.get("value") in (None, ""):
                        skipped.append(field["name"])
                        continue
                    try:
                        await _fill_field(page, field)
                        filled.append(field["name"])
                    except Exception as exc:
                        skipped.append(f"{field['name']}:{type(exc).__name__}")

                if screenshot_path:
                    os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
                    await page.screenshot(path=screenshot_path, full_page=True)

                if not dry_run:
                    submitted = await _submit(page)
            finally:
                await context.close()
                await browser.close()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.warning("autofill execution failed for %s: %s", url, error)

    inc("jobhunter_autofill_runs_total", result=("error" if error else ("dry_run" if dry_run else "submitted")))
    return {
        "status": "error" if error else ("dry_run" if dry_run else ("submitted" if submitted else "filled")),
        "reason": error,
        "filled": len(filled),
        "filled_fields": filled,
        "skipped_fields": skipped,
        "submitted": submitted,
        "dry_run": dry_run,
        "screenshot": screenshot_path,
    }


async def _attempt_login(page, credential: Dict[str, str]) -> None:  # pragma: no cover - browser only
    for selector in ("input[type=email]", "input[name*=email i]", "input[name*=user i]", "#username"):
        try:
            await page.fill(selector, credential.get("username", ""), timeout=4000)
            break
        except Exception:
            continue
    for selector in ("input[type=password]", "#password"):
        try:
            await page.fill(selector, credential.get("password", ""), timeout=4000)
            break
        except Exception:
            continue
    for selector in ("button[type=submit]", "input[type=submit]"):
        try:
            await page.click(selector, timeout=4000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            return
        except Exception:
            continue


async def _fill_field(page, field: Dict[str, Any]) -> None:  # pragma: no cover - browser only
    name = field["name"]
    selectors = [f'[name="{name}"]', f"#{name}"]
    fill_type = field.get("type")
    for selector in selectors:
        locator = page.locator(selector).first
        if await locator.count() == 0:
            continue
        if fill_type == "file":
            await locator.set_input_files(field["value"])
        elif fill_type == "select" or field.get("options"):
            try:
                await locator.select_option(label=str(field["value"]))
            except Exception:
                await locator.select_option(str(field["value"]))
        elif fill_type == "checkbox":
            if str(field["value"]).lower() in ("true", "yes", "1"):
                await locator.check()
        else:
            await locator.fill(str(field["value"]))
        return
    raise LookupError(f"no selector matched {name}")


async def _submit(page) -> bool:  # pragma: no cover - browser only
    for selector in ("button[type=submit]", "input[type=submit]", "button:has-text('Submit')",
                     "button:has-text('Apply')", "button:has-text('Send application')"):
        try:
            await page.click(selector, timeout=5000)
            await page.wait_for_load_state("networkidle", timeout=20000)
            return True
        except Exception:
            continue
    return False
