"""
Resume ingestion: deterministic text/layout extraction + AI profile extraction.

Policy: **the profile is an AI artefact.** Text and layout extraction are
deterministic and always run, but the structured profile (name, roles, bullets,
dates) is only produced by the model, under a schema and an anti-hallucination
guardrail that checks every extracted entity against the source text.

When the model is unreachable the caller gets an ``AIUnavailableError`` carrying
the provider's own diagnosis — the product refuses to persist a regex-guessed
profile and present it as the candidate's record. ``diagnostic_preview_extract``
remains only as an explicitly-labelled *preview* helper for diagnostics/tests.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple, cast

from pdfminer.high_level import extract_text as pdfminer_extract
from pypdf import PdfReader

from app.services.ai_client import fit_prompt_part, input_budget_chars
from app.services.ai_guardrails import (
    AIUnavailableError,
    FieldSpec,
    SchemaSpec,
    build_fact_ledger,
    run_guarded_task,
    strip_ai_artifacts,
)

#: Starting output budget for a full resume profile (a large JSON document).
#: The user's configured output ceiling still caps it and any escalation.
#: (Explicit — the gateway's env default no longer silently sets per-request
#: budgets; PR #29's escalation still starts from this value.)
PARSE_OUTPUT_TOKENS = 4000
#: Storage cap for the raw resume text kept on the profile (not a prompt slice).
MAX_RAW_TEXT_CHARS = 20000

PROFILE_SCHEMA = SchemaSpec([
    FieldSpec("name", "str", min_length=2, max_length=120),
    FieldSpec("email", "str"),
    FieldSpec("phone", "str", required=False),
    FieldSpec("location", "str", required=False),
    FieldSpec("summary", "str", min_length=40, max_length=1400),
    FieldSpec("skills", "list", min_length=1),
    FieldSpec("experience", "list"),
    FieldSpec("education", "list", required=False),
    FieldSpec("projects", "list", required=False),
    FieldSpec("links", "list", required=False),
    FieldSpec("current_title", "str", required=False),
])

EMAIL_RE = re.compile(r"[\w\.\-+]+@[\w\.\-]+\.\w+")
PHONE_RE = re.compile(r"(?:\+\d{1,3}[-. ]?)?\(?\d{3}\)?[-. ]?\d{3,5}[-. ]?\d{4}")


# --------------------------------------------------------------------------- #
# Deterministic extraction
# --------------------------------------------------------------------------- #
def extract_layout(filepath: str) -> Dict[str, Any]:
    """
    Extract layout like margins, lines, capitals, hyperlink style, bullet style,
    borders, colors etc. Uses PyPDF + heuristics.
    """
    try:
        reader = PdfReader(filepath)
        page = reader.pages[0] if reader.pages else None
        text = ""
        if page:
            text = page.extract_text() or ""
        try:
            raw = pdfminer_extract(filepath, maxpages=1)
        except Exception:
            raw = text
        lines = raw.splitlines() if raw else []
        has_bullets = sum(1 for line in lines if line.strip().startswith(("•", "-", "*", "·", "▪", "‣")))
        caps_ratio = sum(1 for c in raw if c.isupper()) / max(1, len(raw))
        hyperlinks = re.findall(r"https?://\S+|www\.\S+|\S+@\S+", raw)
        leading_spaces = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
        avg_margin = sum(leading_spaces) / max(1, len(leading_spaces))
        fonts: List[str] = []
        try:
            if page and "/Resources" in page:
                # pypdf's PdfObject isn't subscript-typed; DictionaryObject has .get.
                resources = cast(Any, page["/Resources"])
                fonts = [str(f) for f in resources.get("/Font", {}).keys()]
        except Exception:
            pass
        return {
            "page_count": len(reader.pages) if reader.pages else 0,
            "avg_margin_pt": round(avg_margin * 3, 2),
            "margins": {"top": 36, "bottom": 36, "left": 54, "right": 54, "estimated": True},
            "lines": {"total": len(lines), "avg_chars_per_line": round(sum(len(line) for line in lines) / max(1, len(lines)), 2)},
            "capitals_ratio": round(caps_ratio, 3),
            "all_caps_sections": caps_ratio > 0.3,
            "hyperlink_style": {"count": len(hyperlinks), "color": "#2563eb", "underline": True, "samples": hyperlinks[:3]},
            "bullet_style": "•" if has_bullets else "—",
            "bullet_count": has_bullets,
            "borders": {"has_border": False, "style": "none"},
            "colors": ["#1a1a1a"],
            "fonts": [f for f in fonts if not f.startswith("/")][:5] or ["Calibri"],
            "estimated": True,
            "raw_preview": raw[:1000],
        }
    except Exception as exc:
        return {"error": str(exc), "margins": {"top": 36, "left": 54}, "bullet_style": "•",
                "colors": ["#000"], "fonts": ["Calibri"]}


def extract_text_from_pdf(filepath: str) -> str:
    try:
        return pdfminer_extract(filepath) or ""
    except Exception:
        try:
            reader = PdfReader(filepath)
            return "\n".join([p.extract_text() or "" for p in reader.pages])
        except Exception:
            return ""


def extract_text(filepath: str) -> str:
    """
    Extract plain text from a PDF or Word document.

    PDF: pdfminer first (best layout fidelity), pypdf as a fallback.
    DOCX/DOC: python-docx paragraphs + tables; DOC falls back to a raw byte
    scan so at least *something* is recovered from legacy files.
    """
    lower = (filepath or "").lower()
    if lower.endswith(".pdf"):
        for reader in (
            lambda: pdfminer_extract(filepath),
            lambda: "\n".join((page.extract_text() or "") for page in PdfReader(filepath).pages),
        ):
            try:
                text = reader() or ""
                if text.strip():
                    return text
            except Exception:
                continue
        return ""

    if lower.endswith((".docx", ".doc")):
        try:
            from docx import Document

            document = Document(filepath)
            parts = [p.text for p in document.paragraphs if p.text]
            for table in document.tables:
                for row in table.rows:
                    parts.append(" | ".join(cell.text for cell in row.cells))
            text = "\n".join(parts)
            if text.strip():
                return text
        except Exception:
            pass
        try:  # last resort for .doc without converters installed
            with open(filepath, "rb") as handle:
                raw = handle.read()
            decoded = raw.decode("latin-1", errors="ignore")
            words = re.findall(r"[A-Za-z0-9@._%+\-/ ]{4,}", decoded)
            return "\n".join(words)[:50000]
        except Exception:
            return ""
    return ""


# --------------------------------------------------------------------------- #
# AI extraction (the only supported source of a persisted profile)
# --------------------------------------------------------------------------- #
def _normalise_profile(data: Dict[str, Any], text: str) -> Dict[str, Any]:
    """Clean the model's answer into the canonical profile shape."""
    def clean(value: Any, limit: int = 400) -> str:
        return strip_ai_artifacts(str(value or ""))[:limit]

    experience: List[Dict[str, Any]] = []
    for entry in data.get("experience") or []:
        if not isinstance(entry, dict):
            entry = {"title": clean(entry, 200)}
        bullets = entry.get("bullets") or entry.get("description") or []
        if isinstance(bullets, str):
            bullets = [part for part in re.split(r"\n+|•|;\s", bullets) if part.strip()]
        experience.append({
            "title": clean(entry.get("title"), 200),
            "company": clean(entry.get("company") or entry.get("employer"), 200),
            "duration": clean(entry.get("duration") or entry.get("dates") or entry.get("period"), 80),
            "location": clean(entry.get("location"), 120),
            "bullets": [clean(b, 400) for b in (bullets or [])[:12] if clean(b, 400)],
        })

    education: List[Dict[str, Any]] = []
    for entry in data.get("education") or []:
        if not isinstance(entry, dict):
            entry = {"degree": clean(entry, 200)}
        education.append({
            "degree": clean(entry.get("degree") or entry.get("qualification"), 200),
            "school": clean(entry.get("school") or entry.get("institution") or entry.get("university"), 200),
            "year": clean(entry.get("year") or entry.get("graduation_year") or entry.get("duration"), 40),
            "field": clean(entry.get("field") or entry.get("major"), 160),
        })

    projects: List[Dict[str, Any]] = []
    for entry in data.get("projects") or []:
        if not isinstance(entry, dict):
            entry = {"name": clean(entry, 200)}
        projects.append({
            "name": clean(entry.get("name") or entry.get("title"), 200),
            "description": clean(entry.get("description") or entry.get("summary"), 600),
            "tech": [clean(t, 60) for t in (entry.get("tech") or [])][:12] if isinstance(entry.get("tech"), list) else [],
            "link": clean(entry.get("link") or entry.get("url"), 200),
        })

    skills = [clean(s, 60) for s in (data.get("skills") or []) if clean(s, 60)]
    links = [clean(link, 200) for link in (data.get("links") or []) if clean(link, 200)]
    for match in re.findall(r"https?://\S+|linkedin\.com/\S+|github\.com/\S+", text or ""):
        candidate = match.rstrip(".,);]")
        if candidate not in links:
            links.append(candidate)

    email = clean(data.get("email"), 200)
    if not EMAIL_RE.match(email or ""):
        found = EMAIL_RE.search(text or "")
        email = found.group(0) if found else ""
    phone = clean(data.get("phone"), 60)
    if not phone:
        found = PHONE_RE.search(text or "")
        phone = found.group(0).strip() if found else ""

    profile = {
        "name": clean(data.get("name"), 120),
        "email": email,
        "phone": phone,
        "location": clean(data.get("location"), 160),
        "summary": clean(data.get("summary"), 1400),
        "skills": list(dict.fromkeys(skills))[:40],
        "current_title": clean(data.get("current_title") or (experience[0]["title"] if experience else ""), 200),
        "years_experience": str(data.get("years_experience") or ""),
        "experience": [e for e in experience if e["title"] or e["company"]],
        "education": [e for e in education if e["degree"] or e["school"]],
        "projects": [p for p in projects if p["name"]],
        "links": links[:10],
        "languages": [clean(lang, 60) for lang in (data.get("languages") or []) if clean(lang, 60)][:12],
        "certifications": [clean(c, 160) for c in (data.get("certifications") or []) if clean(c, 160)][:12],
        "extraction_source": "ai",
    }
    return profile


def _extraction_checks(text: str):
    """Anti-hallucination: every entity must exist in the source document."""

    def check(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        haystack = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
        for field_name in ("name", "email", "phone", "location"):
            value = strip_ai_artifacts(str(data.get(field_name) or ""))
            if not value:
                continue
            token = re.sub(r"[^a-z0-9]+", "", value.lower())
            if token and token not in haystack:
                issues.append({"code": "not_in_source", "severity": "error", "field": field_name,
                               "value": value,
                               "message": f"'{value}' does not appear in the uploaded document."})
        for entry in data.get("experience") or []:
            if not isinstance(entry, dict):
                continue
            company = strip_ai_artifacts(str(entry.get("company") or ""))
            token = re.sub(r"[^a-z0-9]+", "", company.lower())
            if token and token not in haystack:
                issues.append({"code": "not_in_source", "severity": "error", "field": "experience.company",
                               "value": company,
                               "message": f"Employer '{company}' does not appear in the uploaded document."})
        if not (data.get("experience") or data.get("projects")):
            issues.append({"code": "no_experience", "severity": "error", "field": "experience",
                           "message": "No experience or projects were extracted — the document may be scanned."})
        return issues

    return check


async def ai_extract_profile(
    text: str,
    ai_config: Optional[Dict[str, Any]] = None,
    *,
    db=None,
    user_id: Optional[int] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Extract a structured profile with the model, under guardrails.

    Raises ``AIUnavailableError`` (mapped to HTTP 503 with a full diagnosis) when
    the model cannot be reached, and ``GuardrailError`` when the extraction is
    not supported by the document. It never returns a regex-guessed profile.
    """
    if not text.strip():
        raise AIUnavailableError(
            "unknown",
            workflow="parse",
            detail="empty_document",
            context={"message": "No readable text found in the document (is it a scanned image?)",
                     "fix": "Upload a text-based PDF or DOCX, or an OCR'd version of the scan."},
        )

    budget = input_budget_chars(db=db, user_id=user_id, ai_config=ai_config)
    resume_text, _truncated = fit_prompt_part(strip_ai_artifacts(text), budget, label="parse.resume")

    prompt = f"""Extract a complete, structured profile from this resume.

Rules:
- Copy facts exactly as written. NEVER invent, infer or "improve" employers, titles, dates, skills, degrees or metrics.
- Keep every role from the resume, most recent first. Split each role's accomplishments into separate bullets.
- Preserve numbers and metrics exactly (they are the candidate's proof).
- "summary" is 2-4 factual sentences about what the resume shows — no adjectives the resume does not support.
- Use empty strings / empty arrays when the resume does not state something. Do not guess.

Return JSON with keys:
name, email, phone, location, summary, current_title, years_experience,
skills (array of concrete technologies/competencies),
experience (array of {{title, company, duration, location, bullets[]}}),
education (array of {{degree, school, year, field}}),
projects (array of {{name, description, tech[], link}}),
links (array), languages (array), certifications (array).

Resume text:
\"\"\"{resume_text}\"\"\"
"""
    data, report = await run_guarded_task(
        "parse",
        system=("You are a precise resume parser. You transcribe what the document states and "
                "nothing else. You never add experience the document does not contain."),
        prompt=prompt,
        schema=PROFILE_SCHEMA,
        checks=[_extraction_checks(text)],
        ledger=build_fact_ledger({}, text),
        db=db,
        user_id=user_id,
        temperature=0.0,
        max_tokens=PARSE_OUTPUT_TOKENS,
        coerce=lambda payload: _normalise_profile(payload, text),
    )

    profile = _normalise_profile(data, text)
    profile["raw_text"] = text[:MAX_RAW_TEXT_CHARS]
    meta = {
        "ai_used": True,
        "source": "ai",
        "extraction_source": "ai",
        "guardrail": report.to_dict(),
    }
    return profile, meta


# --------------------------------------------------------------------------- #
# Diagnostic-only heuristic preview (never persisted as the master profile)
# --------------------------------------------------------------------------- #
def diagnostic_preview_extract(text: str) -> Dict[str, Any]:
    """
    Regex preview used for diagnostics/tests only.

    Marked ``degraded=True`` so it can never be mistaken for a real extraction —
    persisting this as a profile is exactly what produced unusable resumes and
    emails that addressed the candidate as "Unknown".
    """
    email = re.search(r"[\w\.\-]+@[\w\.\-]+\.\w+", text)
    phone = PHONE_RE.search(text)
    name_match = re.search(r"^\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\s*$", text.strip()[:400], re.MULTILINE)
    skills_keywords = ["python", "java", "javascript", "typescript", "react", "node", "aws", "docker",
                       "kubernetes", "sql", "nosql", "fastapi", "django", "spring", "golang", "rust",
                       "ml", "ai", "pytorch", "tensorflow"]
    found_skills = [k for k in skills_keywords if k.lower() in text.lower()]
    title_match = re.search(
        r"\b((?:senior|junior|lead|staff|principal|associate)?\s*(?:software|backend|frontend|full[- ]?stack|data|ml|machine learning|devops|platform|product|qa)?\s*(?:engineer|developer|architect|scientist|manager|analyst|designer))\b",
        text[:600], re.IGNORECASE)
    current_title = title_match.group(1).strip().title() if title_match else ""
    return {
        "name": name_match.group(1) if name_match else "",
        "email": email.group(0) if email else "",
        "phone": phone.group(0) if phone else "",
        "location": "",
        "summary": text[:500],
        "skills": found_skills,
        "current_title": current_title,
        "experience": [{"title": current_title or "Experience", "company": "", "duration": "",
                        "bullets": [text[500:1000]]}],
        "education": [],
        "projects": [],
        "links": re.findall(r"https?://\S+", text)[:5],
        "raw_text": text[:5000],
        "extraction_source": "heuristic_preview",
        "degraded": True,
        "degraded_reason": "AI extraction unavailable — this preview must not be used as ground truth",
    }
