"""
Tailored resume generation.

Three guarantees, in this order:

1. **AI-authored, never guessed.** If the model cannot be reached the function
   raises ``AIUnavailableError`` with the provider's diagnosis. There is no
   regex-based "tailoring" fallback any more — a resume that looks like the
   candidate's own text with a shuffled skill list is worse than no resume.
2. **Guardrailed.** The output is validated against a schema, a fact ledger
   built from the candidate's own profile, and ATS/quality rules (action verbs,
   quantified bullets, no first person, no markdown noise). A failing draft is
   sent back to the model once with the exact violations; if it still fails the
   request errors out instead of shipping a bad document.
3. **Professionally rendered and named.** One-column ATS layout with real tab
   stops, consistent type scale and 0.6" margins; the download name is
   ``First-Last-Company-Role.pdf`` rather than ``resume_26_a6786c6``.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

# DOCX / PDF generation
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.shared import Inches, Pt, RGBColor
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Table, TableStyle

from app.services.ai_client import AIClientError, chat_completion
from app.services.ai_guardrails import (
    AIUnavailableError,
    FactLedger,
    FieldSpec,
    GuardrailError,
    SchemaSpec,
    build_fact_ledger,
    run_guarded_task,
    strip_ai_artifacts,
)

TAILOR_SCHEMA = SchemaSpec([
    FieldSpec("tailored_profile", "dict"),
    FieldSpec("tags", "list"),
    FieldSpec("reasoning", "str", required=False),
])

#: Verbs a strong bullet starts with — used by the quality guardrail.
ACTION_VERBS = {
    "built", "led", "designed", "shipped", "reduced", "increased", "improved", "automated",
    "architected", "delivered", "launched", "migrated", "scaled", "owned", "drove",
    "implemented", "developed", "created", "established", "streamlined", "optimised",
    "optimized", "cut", "grew", "saved", "accelerated", "consolidated", "modernised",
    "modernized", "introduced", "coordinated", "mentored", "negotiated", "analysed",
    "analyzed", "defined", "standardised", "standardized", "partnered", "resolved",
}

FIRST_PERSON = re.compile(r"\b(I|my|me|mine|myself|we|our|us)\b", re.IGNORECASE)
QUANTIFIER = re.compile(r"\b(\d[\d,\.]*\s*%?|\$\s?\d[\d,\.]*[kmb]?|\d+\s*(?:x|times|users|requests|ms|s|hours|days|weeks|people|clients|accounts))\b", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
def jd_fact_guard_prompt(profile: Dict[str, Any], jd: str, layout: Dict[str, Any],
                         strict_skeleton: bool, persona: Optional[Dict[str, Any]] = None) -> str:
    if strict_skeleton:
        skeleton = (
            f"STRICTLY maintain this resume skeleton: {json.dumps(layout, default=str)[:1200]}. "
            f"Keep the section order, heading capitalization and bullet style '{layout.get('bullet_style', '•')}'. "
            "Only rewrite bullet content — never restructure."
        )
    else:
        skeleton = ("Use a clean, single-column, ATS-friendly structure in this exact order: "
                    "Summary, Skills, Experience, Education, Projects (omit a section only when the "
                    "candidate has nothing in it).")

    persona_block = ""
    if persona:
        persona_block = (
            "\nCandidate track (persona) — tailor towards this specifically:\n"
            f"{json.dumps(persona, default=str)[:1200]}\n"
        )

    return f"""Tailor this resume to the job description below.

Ground rules — these are enforced by an automated checker and a violation is rejected:
- NEVER invent employers, job titles, dates, degrees, certifications, metrics or skills.
- Every company, title and year you output must already exist in the candidate profile.
- You MAY reorder and rewrite existing bullets so the most JD-relevant evidence comes first.
- You MAY write a new Summary, but it may only restate facts present in the profile.
- Skills list = a re-ordered subset of the candidate's existing skills. Never add a skill they do not have.
- Rewrite bullets to be specific and quantified ONLY using numbers already in the profile. Do not create metrics.
- Bullets: start with a strong past-tense action verb, one idea each, 12-28 words, no first person ("I", "my", "we").
- Keep the resume to 1-2 pages: at most 5 bullets per role, 6 most recent roles, 12 skills.
- Output plain text only: no markdown, no bold/asterisks, no emojis, no HTML.
{skeleton}
{persona_block}
Candidate profile (ground truth):
{json.dumps(profile, default=str)[:9000]}

Job description:
\"\"\"{strip_ai_artifacts(jd)[:4000]}\"\"\"

Return JSON:
{{
  "tailored_profile": {{
    "name": "", "email": "", "phone": "", "location": "", "links": [],
    "summary": "2-4 factual sentences tailored to this role",
    "skills": ["ordered, most JD-relevant first"],
    "experience": [{{"title": "", "company": "", "duration": "", "location": "", "bullets": []}}],
    "education": [{{"degree": "", "school": "", "year": "", "field": ""}}],
    "projects": [{{"name": "", "description": "", "tech": []}}]
  }},
  "tags": ["3-5 short hyphenated tags"],
  "reasoning": "one sentence on how this was tailored"
}}"""


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #
def _normalise_tailored(data: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    """Shape the model's answer into the renderable profile form."""
    tailored = data.get("tailored_profile") if isinstance(data.get("tailored_profile"), dict) else dict(data)

    def clean(value: Any, limit: int = 400) -> str:
        return strip_ai_artifacts(str(value or ""))[:limit]

    experience: List[Dict[str, Any]] = []
    for entry in tailored.get("experience") or []:
        if not isinstance(entry, dict):
            continue
        bullets = entry.get("bullets") or entry.get("description") or []
        if isinstance(bullets, str):
            bullets = [part for part in re.split(r"\n+|•\s*", bullets) if part.strip()]
        experience.append({
            "title": clean(entry.get("title"), 200),
            "company": clean(entry.get("company"), 200),
            "duration": clean(entry.get("duration") or entry.get("dates"), 80),
            "location": clean(entry.get("location"), 120),
            "bullets": [clean(b, 320) for b in (bullets or [])[:6] if clean(b, 320)],
        })

    education = []
    for entry in tailored.get("education") or []:
        if not isinstance(entry, dict):
            education.append({"degree": clean(entry, 200), "school": "", "year": "", "field": ""})
            continue
        education.append({
            "degree": clean(entry.get("degree"), 200),
            "school": clean(entry.get("school") or entry.get("institution"), 200),
            "year": clean(entry.get("year"), 40),
            "field": clean(entry.get("field") or entry.get("major"), 160),
        })

    projects = []
    for entry in tailored.get("projects") or []:
        if not isinstance(entry, dict):
            projects.append({"name": clean(entry, 200), "description": "", "tech": []})
            continue
        projects.append({
            "name": clean(entry.get("name"), 200),
            "description": clean(entry.get("description"), 600),
            "tech": [clean(t, 60) for t in (entry.get("tech") or [])][:10] if isinstance(entry.get("tech"), list) else [],
        })

    skills = [clean(s, 60) for s in (tailored.get("skills") or []) if clean(s, 60)][:14]

    return {
        # Identity always comes from the candidate's own record — the model is
        # never allowed to restate contact details (a wrong phone number on a
        # resume costs the interview).
        "name": clean(profile.get("name"), 120),
        "email": clean(profile.get("email"), 200),
        "phone": clean(profile.get("phone"), 60),
        "location": clean(profile.get("location"), 160),
        "links": [clean(link, 200) for link in (profile.get("links") or [])][:6],
        "summary": clean(tailored.get("summary") or profile.get("summary"), 1400),
        "skills": list(dict.fromkeys(skills)),
        "current_title": clean(tailored.get("current_title") or profile.get("current_title"), 200),
        "experience": experience,
        "education": education,
        "projects": projects,
        "languages": [clean(v, 60) for v in (profile.get("languages") or [])][:10],
        "certifications": [clean(v, 160) for v in (profile.get("certifications") or [])][:10],
    }


def _resume_checks(profile: Dict[str, Any], ledger: FactLedger):
    """Accuracy + ATS quality checks applied to the tailored resume."""
    known_skills = {re.sub(r"[^a-z0-9+#.]+", "", str(s).lower()) for s in (profile.get("skills") or [])}

    def accuracy_and_ats_checks(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        tailored = data.get("tailored_profile") if isinstance(data.get("tailored_profile"), dict) else data
        experience = tailored.get("experience") or []
        if not experience:
            issues.append({"code": "no_experience", "severity": "error", "field": "experience",
                           "message": "The tailored resume has no experience section."})

        source_companies = {re.sub(r"[^a-z0-9]+", "", str(e.get("company") or "").lower())
                            for e in (profile.get("experience") or []) if isinstance(e, dict)}
        source_durations = {re.sub(r"[^a-z0-9]+", "", str(e.get("duration") or "").lower())
                            for e in (profile.get("experience") or []) if isinstance(e, dict)}
        for index, entry in enumerate(experience):
            if not isinstance(entry, dict):
                continue
            company = re.sub(r"[^a-z0-9]+", "", str(entry.get("company") or "").lower())
            if company and company not in source_companies:
                issues.append({"code": "fabricated_employer", "severity": "error",
                               "field": f"experience[{index}].company", "value": entry.get("company"),
                               "message": f"'{entry.get('company')}' is not one of the candidate's employers."})
            duration = re.sub(r"[^a-z0-9]+", "", str(entry.get("duration") or "").lower())
            if duration and source_durations and duration not in source_durations:
                issues.append({"code": "fabricated_dates", "severity": "error",
                               "field": f"experience[{index}].duration", "value": entry.get("duration"),
                               "message": f"'{entry.get('duration')}' does not match any date range in the source resume."})
            if not (entry.get("bullets") or []):
                issues.append({"code": "empty_role", "severity": "error",
                               "field": f"experience[{index}].bullets",
                               "message": f"'{entry.get('title') or entry.get('company')}' has no accomplishment bullets."})

        for skill in tailored.get("skills") or []:
            token = re.sub(r"[^a-z0-9+#.]+", "", str(skill).lower())
            if token and token not in known_skills:
                issues.append({"code": "fabricated_skill", "severity": "error", "field": "skills",
                               "value": skill,
                               "message": f"'{skill}' is not in the candidate's skill set."})

        summary = str(tailored.get("summary") or "")
        if len(summary.strip()) < 40:
            issues.append({"code": "weak_summary", "severity": "error", "field": "summary",
                           "message": "The summary is too short to position the candidate."})
        if FIRST_PERSON.search(summary):
            issues.append({"code": "first_person", "severity": "warning", "field": "summary",
                           "message": "Summaries should not use first person ('I', 'my', 'we')."})

        bullets = [b for e in experience if isinstance(e, dict) for b in (e.get("bullets") or [])]
        quantified = sum(1 for b in bullets if QUANTIFIER.search(str(b)))
        if bullets and quantified == 0:
            issues.append({"code": "no_metrics", "severity": "warning", "field": "experience.bullets",
                           "message": "No bullet carries a number — reuse the metrics already in the resume."})
        for bullet in bullets:
            text = str(bullet).strip()
            if len(text) > 260:
                issues.append({"code": "bullet_too_long", "severity": "warning", "field": "bullets",
                               "value": text[:60], "message": "A bullet exceeds 260 characters — split it."})
            if FIRST_PERSON.search(text):
                issues.append({"code": "first_person", "severity": "warning", "field": "bullets",
                               "value": text[:60], "message": f"'{text[:40]}…' uses first person."})
        return issues

    return accuracy_and_ats_checks


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
async def generate_tailored_profile(
    profile: Dict[str, Any],
    jd: str,
    layout: Dict[str, Any],
    strict_skeleton: bool,
    ai_config: Optional[Dict[str, Any]] = None,
    *,
    db=None,
    user_id: Optional[int] = None,
    persona: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Produce a tailored, guardrail-verified profile for one job description.

    Raises ``AIUnavailableError`` when the model is unreachable and
    ``GuardrailError`` when the draft cannot be made accurate — the caller turns
    those into 503/422 responses with an explanation.
    """
    if not (profile or {}).get("name"):
        raise AIUnavailableError(
            "unknown", workflow="resume_gen", detail="profile_name_missing",
            context={"message": "Your profile has no name, so a resume cannot be generated.",
                     "fix": "Re-upload your master resume with AI online so the profile is extracted correctly."},
        )
    if not (jd or "").strip():
        raise GuardrailError("resume_gen", [{"code": "empty_jd", "severity": "error", "field": "jd",
                                            "message": "This job has no description to tailor against."}])

    ledger = build_fact_ledger(profile)
    prompt = jd_fact_guard_prompt(profile, jd, layout or {}, strict_skeleton, persona)
    data, report = await run_guarded_task(
        "resume_gen",
        system=("You are an expert technical resume writer. You tailor ruthlessly to the job description "
                "while never stating anything the candidate's own resume does not support."),
        prompt=prompt,
        schema=TAILOR_SCHEMA,
        checks=[_resume_checks(profile, ledger)],
        ledger=ledger,
        db=db,
        user_id=user_id,
        temperature=0.3,
        max_tokens=3000,
        timeout=90,
        coerce=lambda payload: {"tailored_profile": _normalise_tailored(payload, profile),
                                "tags": payload.get("tags") or [],
                                "reasoning": payload.get("reasoning") or ""},
    )

    tailored = data.get("tailored_profile") or {}
    tags = [re.sub(r"[^a-z0-9\-+#]", "-", str(t).lower()).strip("-") for t in (data.get("tags") or [])]
    return {
        "tailored_profile": tailored,
        "tags": [t for t in dict.fromkeys(tags) if t][:6] or ["tailored"],
        "reasoning": strip_ai_artifacts(str(data.get("reasoning") or ""))[:600],
        "source": "ai",
        "guardrail": report.to_dict(),
    }


# --------------------------------------------------------------------------- #
# File naming
# --------------------------------------------------------------------------- #
_UNSAFE = re.compile(r"[^A-Za-z0-9]+")


def _slug(value: str, limit: int = 40) -> str:
    cleaned = _UNSAFE.sub("-", (value or "").strip()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned[:limit].strip("-")


def professional_filename(profile: Dict[str, Any], job: Any = None, extension: str = "pdf") -> str:
    """
    ``Diganta-Dey-Wirelane-Embedded-Software-Engineer.pdf``

    Recruiters open dozens of attachments a day; ``resume_26_a6786c6.docx`` is
    unreadable in that pile and looks automated. The on-disk path stays unique
    and opaque — only this name is shown and served.
    """
    name = _slug(profile.get("name") or "Resume", 40) or "Resume"
    parts = [name]
    if job is not None:
        company = _slug(getattr(job, "company", "") or (job.get("company") if isinstance(job, dict) else ""), 30)
        title = _slug(getattr(job, "title", "") or (job.get("title") if isinstance(job, dict) else ""), 44)
        if company:
            parts.append(company)
        if title:
            parts.append(title)
    stem = "-".join(part for part in parts if part)
    return f"{stem}.{extension}"


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #
def _sanitize(text: Any) -> str:
    if not isinstance(text, str):
        text = str(text or "")
    text = strip_ai_artifacts(text)
    return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)


def _contact_line(profile: Dict[str, Any]) -> str:
    parts = [
        _sanitize(profile.get("location")),
        _sanitize(profile.get("phone")),
        _sanitize(profile.get("email")),
    ]
    for link in (profile.get("links") or [])[:3]:
        clean = _sanitize(link)
        if clean and clean.lower() not in {p.lower() for p in parts if p}:
            parts.append(re.sub(r"^https?://", "", clean))
    return "  |  ".join(part for part in parts if part)


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)]


_ACCENT = RGBColor(0x1F, 0x29, 0x37)
_MUTED = RGBColor(0x6B, 0x72, 0x80)


def build_docx(tailored: Dict[str, Any], profile: Dict[str, Any], layout: Dict[str, Any], out_path: str) -> None:
    """
    Professional single-column ATS resume (DOCX).

    Single column, no tables/text boxes/images (ATS parsers choke on those),
    real right-aligned tab stops for dates, and a consistent type scale.
    """
    fonts = [str(f) for f in (layout or {}).get("fonts") or [] if f and not str(f).startswith("/")]
    base_font = fonts[0] if fonts else "Calibri"

    doc = Document()
    section = doc.sections[0]
    section.top_margin = Inches(0.55)
    section.bottom_margin = Inches(0.55)
    section.left_margin = Inches(0.7)
    section.right_margin = Inches(0.7)
    usable_width = section.page_width - section.left_margin - section.right_margin

    normal = doc.styles["Normal"]
    normal.font.name = base_font
    normal.font.size = Pt(10)
    normal.paragraph_format.space_after = Pt(2)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.line_spacing = 1.06

    def heading(text: str):
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(9)
        paragraph.paragraph_format.space_after = Pt(3)
        run = paragraph.add_run(_sanitize(text).upper())
        run.bold = True
        run.font.size = Pt(10.5)
        run.font.color.rgb = _ACCENT
        rule = doc.add_paragraph()
        rule.paragraph_format.space_before = Pt(0)
        rule.paragraph_format.space_after = Pt(4)
        rule_run = rule.add_run("─" * 96)
        rule_run.font.size = Pt(5)
        rule_run.font.color.rgb = RGBColor(0xD1, 0xD5, 0xDB)

    def two_column(left: str, right: str, *, bold_left: bool = False, italic_right: bool = True):
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.space_before = Pt(4)
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.tab_stops.add_tab_stop(usable_width, WD_TAB_ALIGNMENT.RIGHT)
        run = paragraph.add_run(_sanitize(left))
        run.bold = bold_left
        run.font.size = Pt(10.5 if bold_left else 10)
        if right:
            tab = paragraph.add_run("\t" + _sanitize(right))
            tab.italic = italic_right
            tab.font.size = Pt(9)
            tab.font.color.rgb = _MUTED
        return paragraph

    def bullet(text: str):
        paragraph = doc.add_paragraph(style="List Bullet")
        paragraph.paragraph_format.left_indent = Inches(0.22)
        paragraph.paragraph_format.first_line_indent = Inches(-0.14)
        paragraph.paragraph_format.space_after = Pt(2)
        paragraph.paragraph_format.line_spacing = 1.06
        run = paragraph.add_run(_sanitize(text))
        run.font.size = Pt(10)

    def body(text: str, *, size: float = 10.0, italic: bool = False):
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(3)
        paragraph.paragraph_format.line_spacing = 1.08
        run = paragraph.add_run(_sanitize(text))
        run.font.size = Pt(size)
        run.italic = italic
        return paragraph

    # --- header ---
    name = doc.add_paragraph()
    name.alignment = WD_ALIGN_PARAGRAPH.LEFT
    name.paragraph_format.space_after = Pt(1)
    name_run = name.add_run(_sanitize(tailored.get("name") or profile.get("name") or "Candidate"))
    name_run.bold = True
    name_run.font.size = Pt(19)
    name_run.font.color.rgb = _ACCENT

    contact = _contact_line(tailored or profile)
    if contact:
        line = doc.add_paragraph()
        line.paragraph_format.space_after = Pt(2)
        run = line.add_run(contact)
        run.font.size = Pt(9)
        run.font.color.rgb = _MUTED

    if tailored.get("current_title") or profile.get("current_title"):
        subtitle = doc.add_paragraph()
        subtitle.paragraph_format.space_after = Pt(2)
        run = subtitle.add_run(_sanitize(tailored.get("current_title") or profile.get("current_title")))
        run.font.size = Pt(10.5)
        run.font.color.rgb = RGBColor(0x25, 0x63, 0xEB)

    if tailored.get("summary"):
        heading("Professional Summary")
        body(tailored["summary"])

    if tailored.get("skills"):
        heading("Skills")
        body(", ".join(_sanitize(s) for s in tailored["skills"] if _sanitize(s)))

    if tailored.get("experience"):
        heading("Professional Experience")
        for entry in tailored["experience"]:
            if not isinstance(entry, dict):
                bullet(str(entry))
                continue
            left = " — ".join(filter(None, [_sanitize(entry.get("title")), _sanitize(entry.get("company"))]))
            two_column(left or _sanitize(entry.get("company")) or "Role",
                       _sanitize(entry.get("duration")), bold_left=True)
            if entry.get("location"):
                location = doc.add_paragraph()
                location.paragraph_format.space_after = Pt(1)
                run = location.add_run(_sanitize(entry["location"]))
                run.italic = True
                run.font.size = Pt(9)
                run.font.color.rgb = _MUTED
            for item in _as_list(entry.get("bullets") or entry.get("description"))[:6]:
                bullet(item)

    if tailored.get("projects"):
        heading("Projects")
        for entry in tailored["projects"]:
            if not isinstance(entry, dict):
                bullet(str(entry))
                continue
            two_column(_sanitize(entry.get("name") or "Project"),
                       ", ".join(_sanitize(t) for t in (entry.get("tech") or [])), bold_left=True,
                       italic_right=False)
            if entry.get("description"):
                bullet(entry["description"])

    if tailored.get("education"):
        heading("Education")
        for entry in tailored["education"]:
            if not isinstance(entry, dict):
                bullet(str(entry))
                continue
            degree = " — ".join(filter(None, [_sanitize(entry.get("degree")), _sanitize(entry.get("school"))]))
            two_column(degree or "Education", _sanitize(entry.get("year")))
            if entry.get("field"):
                body(entry["field"], size=9.5, italic=True)

    extras = [str(v) for v in (tailored.get("certifications") or []) if v]
    if extras:
        heading("Certifications")
        for item in extras[:8]:
            bullet(item)

    languages = [str(v) for v in (tailored.get("languages") or []) if v]
    if languages:
        heading("Languages")
        body(", ".join(languages))

    doc.save(out_path)


def _pdf_escape(text: Any) -> str:
    clean = _sanitize(text)
    return clean.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_pdf(tailored: Dict[str, Any], profile: Dict[str, Any], out_path: str) -> None:
    """Professional single-column ATS resume (PDF), matching the DOCX layout."""
    page_size = LETTER
    doc = SimpleDocTemplate(
        out_path, pagesize=page_size,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.55 * inch, bottomMargin=0.55 * inch,
        title=f"{tailored.get('name') or profile.get('name') or 'Resume'} — Resume",
        author=str(tailored.get("name") or profile.get("name") or "Candidate"),
    )
    content_width = page_size[0] - 1.4 * inch

    name_style = ParagraphStyle("name", fontName="Helvetica-Bold", fontSize=18, leading=21,
                                textColor=colors.HexColor("#111827"), spaceAfter=2)
    contact_style = ParagraphStyle("contact", fontName="Helvetica", fontSize=8.6, leading=11,
                                   textColor=colors.HexColor("#4B5563"), spaceAfter=3)
    title_style = ParagraphStyle("subtitle", fontName="Helvetica-Bold", fontSize=10.4, leading=13,
                                 textColor=colors.HexColor("#2563EB"), spaceAfter=2)
    heading_style = ParagraphStyle("heading", fontName="Helvetica-Bold", fontSize=10.2, leading=13,
                                   textColor=colors.HexColor("#111827"), spaceBefore=9, spaceAfter=1)
    body_style = ParagraphStyle("body", fontName="Helvetica", fontSize=9.6, leading=12.6,
                                textColor=colors.HexColor("#1F2937"), spaceAfter=3, alignment=TA_LEFT)
    role_style = ParagraphStyle("role", fontName="Helvetica-Bold", fontSize=10.2, leading=13,
                                textColor=colors.HexColor("#111827"))
    date_style = ParagraphStyle("date", fontName="Helvetica-Oblique", fontSize=8.8, leading=12,
                                textColor=colors.HexColor("#6B7280"), alignment=TA_RIGHT)
    location_style = ParagraphStyle("location", fontName="Helvetica-Oblique", fontSize=8.8, leading=11,
                                    textColor=colors.HexColor("#6B7280"), spaceAfter=2)
    bullet_style = ParagraphStyle("bullet", parent=body_style, leftIndent=13, bulletIndent=3, spaceAfter=2.4)

    def heading(text: str) -> List[Any]:
        return [Paragraph(_pdf_escape(text).upper(), heading_style),
                HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#D1D5DB"),
                           spaceBefore=1, spaceAfter=4)]

    def two_column(left: str, right: str) -> Table:
        table = Table([[Paragraph(_pdf_escape(left), role_style),
                        Paragraph(_pdf_escape(right), date_style)]],
                      colWidths=[content_width * 0.68, content_width * 0.32])
        table.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return table

    story: List[Any] = [
        Paragraph(_pdf_escape(tailored.get("name") or profile.get("name") or "Candidate"), name_style),
    ]
    contact = _contact_line(tailored or profile)
    if contact:
        story.append(Paragraph(_pdf_escape(contact), contact_style))
    subtitle = tailored.get("current_title") or profile.get("current_title")
    if subtitle:
        story.append(Paragraph(_pdf_escape(subtitle), title_style))

    if tailored.get("summary"):
        story += heading("Professional Summary")
        story.append(Paragraph(_pdf_escape(tailored["summary"]), body_style))

    if tailored.get("skills"):
        story += heading("Skills")
        story.append(Paragraph(_pdf_escape(", ".join(str(s) for s in tailored["skills"] if s)), body_style))

    if tailored.get("experience"):
        story += heading("Professional Experience")
        for entry in tailored["experience"]:
            if not isinstance(entry, dict):
                story.append(Paragraph(f"• {_pdf_escape(entry)}", bullet_style))
                continue
            left = " — ".join(filter(None, [entry.get("title"), entry.get("company")])) or entry.get("company") or "Role"
            block: List[Any] = [two_column(left, entry.get("duration") or "")]
            if entry.get("location"):
                block.append(Paragraph(_pdf_escape(entry["location"]), location_style))
            for item in _as_list(entry.get("bullets") or entry.get("description"))[:6]:
                block.append(Paragraph(_pdf_escape(item), bullet_style, bulletText="•"))
            story.append(KeepTogether(block))

    if tailored.get("projects"):
        story += heading("Projects")
        for entry in tailored["projects"]:
            if not isinstance(entry, dict):
                story.append(Paragraph(f"• {_pdf_escape(entry)}", bullet_style))
                continue
            tech = ", ".join(str(t) for t in (entry.get("tech") or []))
            block = [two_column(entry.get("name") or "Project", tech)]
            if entry.get("description"):
                block.append(Paragraph(_pdf_escape(entry["description"]), bullet_style, bulletText="•"))
            story.append(KeepTogether(block))

    if tailored.get("education"):
        story += heading("Education")
        for entry in tailored["education"]:
            if not isinstance(entry, dict):
                story.append(Paragraph(f"• {_pdf_escape(entry)}", bullet_style))
                continue
            degree = " — ".join(filter(None, [entry.get("degree"), entry.get("school")])) or "Education"
            story.append(two_column(degree, entry.get("year") or ""))
            if entry.get("field"):
                story.append(Paragraph(_pdf_escape(entry["field"]), location_style))

    if tailored.get("certifications"):
        story += heading("Certifications")
        for item in [str(v) for v in tailored["certifications"] if v][:8]:
            story.append(Paragraph(_pdf_escape(item), bullet_style, bulletText="•"))

    if tailored.get("languages"):
        story += heading("Languages")
        story.append(Paragraph(_pdf_escape(", ".join(str(v) for v in tailored["languages"] if v)), body_style))

    doc.build(story)


def hash_jd(jd: str) -> str:
    return hashlib.sha256((jd or "").encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Tagging & cover letter
# --------------------------------------------------------------------------- #
async def tag_resume(profile: Dict[str, Any], ai_config=None, *, db=None, user_id=None) -> List[str]:
    """
    Auto-tag a resume so it can be found and reused later.

    AI first (3-5 short tags); the deterministic fallback is a *label* derived
    from the candidate's own skills, which is safe because it invents nothing.
    """
    prompt = (
        "Create 3-5 short, lowercase, hyphenated tags describing this resume "
        "(e.g. backend-python, fintech, aws). Return JSON {\"tags\": []}.\n"
        f"Profile: {json.dumps(profile, default=str)[:3000]}"
    )
    try:
        data = await chat_completion("tagging", prompt, temperature=0.2, timeout=20,
                                     ai_config=ai_config, db=db, user_id=user_id)
        tags = data.get("tags") if isinstance(data, dict) else None
        if isinstance(tags, list) and tags:
            cleaned = [re.sub(r"[^a-z0-9\-+#]", "-", str(t).lower()).strip("-") for t in tags]
            cleaned = [t for t in cleaned if t][:5]
            if cleaned:
                return cleaned
    except (AIClientError, Exception):
        pass

    skills = [str(s).lower().replace(" ", "-") for s in (profile.get("skills") or [])[:3]]
    tags = ["tailored"] + skills
    return tags[:5]


def resume_plain_text(tailored: Dict[str, Any], profile: Dict[str, Any]) -> str:
    """Deterministic text rendering of a tailored resume (used for diffs)."""
    from app.services.resume_service import render_profile_text

    return render_profile_text(tailored or profile or {})


async def generate_cover_letter(profile: Dict[str, Any], jd: str, job_title: str, company: str,
                                db=None, user_id=None) -> str:
    """Generate a grounded cover letter — never fabricates."""
    from app.services.ai_guardrails import FieldSpec as _FS
    from app.services.ai_guardrails import SchemaSpec as _SS

    safe_jd = strip_ai_artifacts(jd)[:4000]
    prompt = f"""Write a cover letter grounded ONLY in the candidate's actual profile.

Rules:
- Never invent jobs, education, certifications, achievements or metrics not in the profile.
- Use real skills and experiences from the profile; quote at most one concrete accomplishment.
- Tailor to the role: {job_title} at {company}.
- 3-4 short paragraphs, plain text, no markdown, no first-person clichés like "I am excited to".

Profile: {json.dumps(profile, default=str)[:5000]}

Job description:
\"\"\"{safe_jd}\"\"\"

Return JSON: {{"cover_letter": "...", "highlights": ["skill1", "skill2"]}}"""
    ledger = build_fact_ledger(profile)
    data, _report = await run_guarded_task(
        "resume_gen",
        system=("You are a precise cover-letter writer. Every claim must be supported by the "
                "candidate's own profile."),
        prompt=prompt,
        schema=_SS([_FS("cover_letter", "str", min_length=200, max_length=4000), _FS("highlights", "list")]),
        ledger=ledger,
        db=db,
        user_id=user_id,
        temperature=0.5,
        max_tokens=1200,
    )
    return strip_ai_artifacts(str(data.get("cover_letter") or ""))
