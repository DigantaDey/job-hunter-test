import os
import json
import re
import hashlib
from typing import Dict, Any, List, Tuple
from app.services.ai_client import chat_completion, AIClientError

# DOCX / PDF generation
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from reportlab.lib.pagesizes import LETTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch

def jd_fact_guard_prompt(profile: Dict[str,Any], jd: str, layout: Dict[str,Any], strict_skeleton: bool) -> str:
    skeleton_instruction = ""
    if strict_skeleton:
        skeleton_instruction = f"STRICTLY maintain this resume skeleton/layout: {json.dumps(layout)[:1500]}. Keep section order, heading capitalization, bullet style '{layout.get('bullet_style','•')}', and overall structure exactly. Only tailor bullet content."
    else:
        skeleton_instruction = "You may reorganize into a modern ATS-friendly format. Use clean sections: Summary, Skills, Experience, Education, Projects."

    return f"""
You are an expert resume writer with JD fact guard. Tailor the resume to the JD WITHOUT hallucinating.
Rules:
- NEVER invent new companies, degrees, dates, or skills not in the profile.
- You may rephrase and reorder existing bullets to highlight relevance to JD.
- You may add a tailored Summary and prioritize skills that match JD.
- Keep all dates, company names, and titles truthful.
{skeleton_instruction}

Memory context: Use the profile as ground truth.

Profile:
{json.dumps(profile, indent=2)[:5000]}

Job Description:
\"\"\"{jd[:4000]}\"\"\"

Return JSON with keys:
- tailored_profile: {{summary, skills[], experience[] (each with title, company, bullets[]), education[], projects[]}}
- tags: [3-5 short tags like 'backend-python', 'aws', 'fintech']
- reasoning: short why this tailoring
Respond ONLY with JSON.
"""

def _heuristic_tailoring(profile: Dict[str, Any], jd: str) -> Dict[str, Any]:
    """Offline fallback: reorder existing skills to surface JD-relevant ones."""
    jd_words = set(re.findall(r"[a-zA-Z]+", jd.lower()))
    skills = profile.get("skills", []) or []
    prioritized = sorted(skills, key=lambda s: (s.lower() in jd_words), reverse=True)
    tailored = {
        "summary": (profile.get("summary", "") or "")[:500] + " | Tailored for JD relevance.",
        "skills": prioritized[:15],
        "experience": (profile.get("experience") or [])[:3],
        "education": profile.get("education", []),
        "projects": (profile.get("projects") or [])[:3],
    }
    return {"tailored_profile": tailored, "tags": ["heuristic"] + prioritized[:2],
            "reasoning": "heuristic fallback (AI not configured / unreachable)"}


async def generate_tailored_profile(profile: Dict[str,Any], jd: str, layout: Dict[str,Any], strict_skeleton: bool, ai_config=None) -> Dict[str,Any]:
    prompt = jd_fact_guard_prompt(profile, jd, layout, strict_skeleton)
    try:
        data = await chat_completion("resume_gen", prompt, temperature=0.3, timeout=45, ai_config=ai_config)
        if isinstance(data, dict) and "tailored_profile" in data:
            data.setdefault("tags", ["tailored"])
            return data
        raise AIClientError("missing_tailored_profile")
    except Exception as exc:
        fallback = _heuristic_tailoring(profile, jd)
        fallback["reasoning"] = f"fallback ({exc})"
        return fallback

def _sanitize(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    # remove control chars except newline, tab
    return re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text)

def build_docx(tailored: Dict[str,Any], profile: Dict[str,Any], layout: Dict[str,Any], out_path: str):
    doc = Document()
    style = doc.styles['Normal']
    font = style.font
    font.name = layout.get("fonts", ["Calibri"])[0] if layout.get("fonts") else "Calibri"
    font.size = Pt(10)

    # Header - Name
    name = _sanitize(profile.get("name","Candidate"))
    title = doc.add_heading(name, level=1)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in title.runs:
        run.font.color.rgb = RGBColor(0x1a,0x1a,0x1a)
        run.font.size = Pt(18)

    # Contact
    contact = _sanitize(" | ".join(filter(None, [profile.get("email",""), profile.get("phone",""), profile.get("location","")])))
    if contact:
        p = doc.add_paragraph(contact)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.runs[0].font.size = Pt(9)
        p.runs[0].font.color.rgb = RGBColor(0x4b,0x55,0x63)

    # Summary
    if tailored.get("summary"):
        doc.add_heading("Summary", level=2)
        doc.add_paragraph(_sanitize(tailored["summary"]))

    # Skills
    if tailored.get("skills"):
        doc.add_heading("Skills", level=2)
        # bullet style from layout
        p = doc.add_paragraph(_sanitize(", ".join(tailored["skills"])))

    # Experience
    if tailored.get("experience"):
        doc.add_heading("Experience", level=2)
        for exp in tailored["experience"]:
            if isinstance(exp, dict):
                h = doc.add_paragraph()
                run = h.add_run(_sanitize(f"{exp.get('title','')} — {exp.get('company','')}"))
                run.bold = True
                run.font.size = Pt(11)
                if exp.get("duration"):
                    p = doc.add_paragraph(_sanitize(exp["duration"]))
                    p.runs[0].italic = True
                    p.runs[0].font.size = Pt(9)
                bullets = exp.get("bullets") or exp.get("description") or []
                if isinstance(bullets, str):
                    bullets = [bullets]
                for b in bullets[:5]:
                    if b:
                        doc.add_paragraph(_sanitize(b), style='List Bullet')
            else:
                doc.add_paragraph(_sanitize(str(exp)), style='List Bullet')

    # Education
    if tailored.get("education"):
        doc.add_heading("Education", level=2)
        for edu in tailored["education"]:
            if isinstance(edu, dict):
                doc.add_paragraph(_sanitize(f"{edu.get('degree','')} — {edu.get('school','')} ({edu.get('year','')})"), style='List Bullet')
            else:
                doc.add_paragraph(_sanitize(str(edu)), style='List Bullet')

    # Projects
    if tailored.get("projects"):
        doc.add_heading("Projects", level=2)
        for proj in tailored["projects"]:
            if isinstance(proj, dict):
                p = doc.add_paragraph()
                run = p.add_run(_sanitize(proj.get("name","Project")))
                run.bold = True
                if proj.get("description"):
                    doc.add_paragraph(_sanitize(proj["description"]), style='List Bullet')
            else:
                doc.add_paragraph(_sanitize(str(proj)), style='List Bullet')

    # Footer hyperlink style
    if profile.get("links"):
        doc.add_paragraph("")
        p = doc.add_paragraph(_sanitize("Links: " + " | ".join(profile["links"][:3])))
        p.runs[0].font.size = Pt(8)
        p.runs[0].font.color.rgb = RGBColor(0x25,0x63,0xeb)

    doc.save(out_path)

def build_pdf(tailored: Dict[str,Any], profile: Dict[str,Any], out_path: str):
    doc = SimpleDocTemplate(out_path, pagesize=LETTER, leftMargin=0.6*inch, rightMargin=0.6*inch, topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('TitleCustom', parent=styles['Title'], fontSize=18, alignment=1, spaceAfter=6)
    heading_style = ParagraphStyle('HeadingCustom', parent=styles['Heading2'], fontSize=12, textColor='#1f2937', spaceBefore=10, spaceAfter=4)
    normal = styles['Normal']
    normal.fontSize = 9
    normal.leading = 11

    story = []
    name = _sanitize(profile.get("name","Candidate"))
    # escape for PDF paragraph (replace < > &)
    def esc(t): return _sanitize(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    story.append(Paragraph(esc(name), title_style))
    contact = " | ".join(filter(None, [profile.get("email",""), profile.get("phone",""), profile.get("location","")]))
    if contact:
        story.append(Paragraph(esc(contact), ParagraphStyle('contact', parent=normal, alignment=1, textColor='#4b5563', fontSize=8)))
        story.append(Spacer(1, 6))

    if tailored.get("summary"):
        story.append(Paragraph("Summary", heading_style))
        story.append(Paragraph(esc(tailored["summary"]), normal))

    if tailored.get("skills"):
        story.append(Paragraph("Skills", heading_style))
        story.append(Paragraph(esc(", ".join(tailored["skills"])), normal))

    if tailored.get("experience"):
        story.append(Paragraph("Experience", heading_style))
        for exp in tailored["experience"]:
            if isinstance(exp, dict):
                title = f"<b>{esc(exp.get('title',''))} — {esc(exp.get('company',''))}</b>"
                story.append(Paragraph(title, normal))
                if exp.get("duration"):
                    story.append(Paragraph(f"<i>{esc(exp['duration'])}</i>", ParagraphStyle('dur', parent=normal, fontSize=8, textColor='#6b7280')))
                bullets = exp.get("bullets") or exp.get("description") or []
                if isinstance(bullets, str):
                    bullets = [bullets]
                for b in bullets[:5]:
                    if b:
                        story.append(Paragraph(f"• {esc(b)}", ParagraphStyle('bullet', parent=normal, leftIndent=12, bulletIndent=6)))
            else:
                story.append(Paragraph(f"• {esc(exp)}", ParagraphStyle('bullet', parent=normal, leftIndent=12)))

    if tailored.get("education"):
        story.append(Paragraph("Education", heading_style))
        for edu in tailored["education"]:
            if isinstance(edu, dict):
                story.append(Paragraph(f"• {esc(edu.get('degree',''))} — {esc(edu.get('school',''))} ({esc(edu.get('year',''))})", normal))
            else:
                story.append(Paragraph(f"• {esc(edu)}", normal))

    if tailored.get("projects"):
        story.append(Paragraph("Projects", heading_style))
        for proj in tailored["projects"]:
            if isinstance(proj, dict):
                story.append(Paragraph(f"<b>{esc(proj.get('name','Project'))}</b>", normal))
                if proj.get("description"):
                    story.append(Paragraph(f"• {esc(proj['description'])}", normal))
            else:
                story.append(Paragraph(f"• {esc(proj)}", normal))
    doc.build(story)

def hash_jd(jd: str) -> str:
    return hashlib.sha256(jd.encode()).hexdigest()[:12]
