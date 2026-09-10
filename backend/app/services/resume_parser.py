import re
from typing import Any, Dict, Tuple

from pdfminer.high_level import extract_text as pdfminer_extract
from pypdf import PdfReader

from app.services.ai_client import AIClientError, chat_completion


# Heuristic layout extractor
def extract_layout(filepath: str) -> Dict[str, Any]:
    """
    Extract layout like margins, lines, capitals, hyperlink style, bullet style, borders, colors etc.
    Uses PyPDF + heuristics. For demo, returns structured guess.
    """
    try:
        reader = PdfReader(filepath)
        page = reader.pages[0] if reader.pages else None
        text = ""
        if page:
            text = page.extract_text() or ""
        # pdfminer for more detail
        try:
            raw = pdfminer_extract(filepath, maxpages=1)
        except Exception:
            raw = text
        # Heuristics
        lines = raw.splitlines() if raw else []
        has_bullets = sum(1 for line in lines if line.strip().startswith(("•", "-", "*", "·")))
        caps_ratio = sum(1 for c in raw if c.isupper()) / max(1, len(raw))
        hyperlinks = re.findall(r"https?://\S+|www\.\S+|\S+@\S+", raw)
        # crude margin guess: leading spaces
        leading_spaces = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
        avg_margin = sum(leading_spaces)/max(1, len(leading_spaces))
        colors = ["#1a1a1a"]  # default; real would parse operators
        # try to detect fonts
        fonts = []
        try:
            if page and "/Resources" in page:
                fonts = list(page["/Resources"].get("/Font", {}).keys())
        except Exception:
            pass
        return {
            "page_count": len(reader.pages) if reader.pages else 0,
            "avg_margin_pt": round(avg_margin * 3, 2),
            "margins": {"top": 36, "bottom": 36, "left": 54, "right": 54, "estimated": True},
            "lines": {"total": len(lines), "avg_chars_per_line": round(sum(len(line) for line in lines)/max(1,len(lines)),2)},
            "capitals_ratio": round(caps_ratio,3),
            "all_caps_sections": caps_ratio > 0.3,
            "hyperlink_style": {"count": len(hyperlinks), "color": "#2563eb", "underline": True, "samples": hyperlinks[:3]},
            "bullet_style": "•" if has_bullets else "—",
            "bullet_count": has_bullets,
            "borders": {"has_border": False, "style": "none"},
            "colors": colors,
            "fonts": [str(f) for f in fonts][:5] or ["Helvetica", "Times"],
            "estimated": True,
            "raw_preview": raw[:1000]
        }
    except Exception as e:
        return {"error": str(e), "margins": {"top":36,"left":54},"bullet_style":"•","colors":["#000"]}

def heuristic_profile_extract(text: str) -> Dict[str, Any]:
    # very simple heuristics for fallback when AI not available
    email = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", text)
    phone = re.search(r"(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", text)
    name_match = re.search(r"^([A-Z][a-z]+(?:\s[A-Z][a-z]+){1,2})", text.strip())
    skills_keywords = ["python","java","javascript","typescript","react","node","aws","docker","kubernetes","sql","nosql","fastapi","django","spring","golang","rust","ml","ai","pytorch","tensorflow"]
    found_skills = [k for k in skills_keywords if k.lower() in text.lower()]
    # Experience: split by years
    exp_years = re.findall(r"(\d+)\+?\s*years", text, re.IGNORECASE)
    # Infer a current title from the first chunk of text
    title_match = re.search(
        r"\b((?:senior|junior|lead|staff|principal|associate)?\s*(?:software|backend|frontend|full[- ]?stack|data|ml|machine learning|devops|platform|product|qa)?\s*(?:engineer|developer|architect|scientist|manager|analyst|designer))\b",
        text[:600], re.IGNORECASE)
    current_title = title_match.group(1).strip().title() if title_match else ""
    return {
        "name": name_match.group(1) if name_match else "Unknown",
        "email": email.group(0) if email else "",
        "phone": phone.group(0) if phone else "",
        "location": "",
        "summary": text[:500],
        "skills": found_skills,
        "current_title": current_title,
        "experience": [{"title": current_title or "Experience", "years": exp_years[0] if exp_years else "", "raw": text[500:1000]}],
        "education": [],
        "projects": [],
        "links": re.findall(r"https?://\S+", text)[:5],
        "raw_text": text[:5000],
        "heuristic": True
    }

async def ai_extract_profile(text: str, ai_config: Dict[str, Any] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Calls the OpenAI-compatible API to extract a structured profile.
    Falls back to heuristic extraction when AI is unconfigured or fails.
    """
    if not text.strip():
        return heuristic_profile_extract(text), {"ai_used": False, "reason": "empty_text"}

    prompt = f"""
You are a resume parser. Extract all possible profile details from the resume below as JSON.
Return JSON with keys: name, email, phone, location, summary, skills (array), experience (array of {{title, company, duration, description}}), education (array), projects (array), links (array), languages, certifications.
Resume:
\"\"\"{text[:8000]}\"\"\"
Respond ONLY with JSON.
"""
    try:
        parsed = await chat_completion("parse", prompt, temperature=0.1, ai_config=ai_config)
        if not isinstance(parsed, dict):
            raise AIClientError("unexpected_non_object_response")
        parsed["raw_text"] = text[:5000]
        parsed["heuristic"] = False
        return parsed, {"ai_used": True, "model": (ai_config or {}).get("model") or "configured"}
    except Exception as exc:
        return heuristic_profile_extract(text), {"ai_used": False, "reason": str(exc)}

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
