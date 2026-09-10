import re
import json
from typing import Dict, Any, Tuple
from pypdf import PdfReader
from pdfminer.high_level import extract_text as pdfminer_extract
import httpx
from app.core.config import settings

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
        except:
            raw = text
        # Heuristics
        lines = raw.splitlines() if raw else []
        has_bullets = sum(1 for l in lines if l.strip().startswith(("•", "-", "*", "·")))
        caps_ratio = sum(1 for c in raw if c.isupper()) / max(1, len(raw))
        hyperlinks = re.findall(r"https?://\S+|www\.\S+|\S+@\S+", raw)
        # crude margin guess: leading spaces
        leading_spaces = [len(l) - len(l.lstrip()) for l in lines if l.strip()]
        avg_margin = sum(leading_spaces)/max(1, len(leading_spaces))
        colors = ["#1a1a1a"]  # default; real would parse operators
        # try to detect fonts
        fonts = []
        try:
            if page and "/Resources" in page:
                fonts = list(page["/Resources"].get("/Font", {}).keys())
        except:
            pass
        return {
            "page_count": len(reader.pages) if reader.pages else 0,
            "avg_margin_pt": round(avg_margin * 3, 2),
            "margins": {"top": 36, "bottom": 36, "left": 54, "right": 54, "estimated": True},
            "lines": {"total": len(lines), "avg_chars_per_line": round(sum(len(l) for l in lines)/max(1,len(lines)),2)},
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
    Calls OpenAI-compatible API to extract structured profile.
    Falls back to heuristic if not configured.
    """
    if not settings.ai_api_key or not text.strip():
        return heuristic_profile_extract(text), {"ai_used": False, "reason": "no_api_key"}

    # Use OpenAI-compatible chat completions
    base_url = (ai_config or {}).get("base_url") or settings.ai_base_url
    api_key = (ai_config or {}).get("api_key") or settings.ai_api_key
    model = (ai_config or {}).get("model") or settings.ai_model

    prompt = f"""
You are a resume parser. Extract all possible profile details from the resume below as JSON.
Return JSON with keys: name, email, phone, location, summary, skills (array), experience (array of {{title, company, duration, description}}), education (array), projects (array), links (array), languages, certifications.
Resume:
\"\"\"{text[:8000]}\"\"\"
Respond ONLY with JSON.
"""
    try:
        async with httpx.AsyncClient(timeout=settings.ai_timeout) as client:
            resp = await client.post(f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type":"application/json"},
                json={
                    "model": model,
                    "messages": [{"role":"user","content": prompt}],
                    "temperature": 0.1,
                    "response_format": {"type":"json_object"}
                }
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                parsed["raw_text"] = text[:5000]
                parsed["heuristic"] = False
                return parsed, {"ai_used": True, "model": model}
            else:
                return heuristic_profile_extract(text), {"ai_used": False, "reason": f"ai_error {resp.status_code} {resp.text[:200]}"}
    except Exception as e:
        return heuristic_profile_extract(text), {"ai_used": False, "reason": str(e)}

def extract_text_from_pdf(filepath: str) -> str:
    try:
        return pdfminer_extract(filepath) or ""
    except:
        try:
            reader = PdfReader(filepath)
            return "\n".join([p.extract_text() or "" for p in reader.pages])
        except Exception as e:
            return ""
