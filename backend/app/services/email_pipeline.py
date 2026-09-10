import re
import asyncio
import smtplib
import ssl
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from typing import Dict, Any, List
from app.services.ai_client import chat_completion, AIClientError


async def find_decision_maker(company: str, department: str = "engineering", ai_config=None) -> Dict[str, Any]:
    """Identify the most likely decision-maker email for a department.

    AI path (when configured) returns name/email/title/confidence. Heuristic
    fallback derives a conventional alias from the company domain.
    """
    prompt = (
        f'Find the email of the possible decision making person for {department} '
        f'at company "{company}". Especially for startups/small companies, identify '
        f'the founder or hiring manager. Return JSON '
        f'{{"name": "", "email": "", "title": "", "confidence": 0-1, "reason": ""}}. '
        f'If not found, guess a conventional pattern like firstname@company.com.'
    )
    try:
        data = await chat_completion("email_gen", prompt, temperature=0.2, timeout=20, ai_config=ai_config)
        if isinstance(data, dict) and data.get("email"):
            data.setdefault("confidence", 0.7)
            data["source"] = "ai"
            return data
    except AIClientError:
        pass

    domain = re.sub(r'\W+', '', company.lower()) + ".com"
    return {
        "name": "Hiring Manager",
        "email": f"{department}@{domain}",
        "title": "Hiring Manager",
        "confidence": 0.5,
        "source": "heuristic",
    }


async def generate_cold_email(profile: Dict[str, Any], company: str, job_title: str = "", founder: bool = False, ai_config=None) -> Dict[str, str]:
    context = "founder" if founder else "hiring manager"
    prompt = (
        f"Write a concise, personalized cold email from the candidate to the {context} "
        f'of {company} for role "{job_title}".\n'
        f"Candidate profile: {json.dumps(profile)[:3000]}\n"
        f"Job/company: {company}\n"
        f"Tone: warm, confident, not pushy, startup-appropriate. Mention specific "
        f"accomplishments, not generic fluff. Include a clear CTA.\n"
        f'Return JSON {{"subject":"", "body":""}} plain text body.\n'
        f"Do NOT hallucinate achievements not in profile.\n"
    )
    try:
        data = await chat_completion("email_gen", prompt, temperature=0.7, timeout=30, ai_config=ai_config)
        subject = str(data.get("subject", "") or "").strip()
        body = str(data.get("body", "") or "").strip()
        if subject and body:
            return {"subject": subject, "body": body, "ai_used": True}
    except AIClientError:
        pass

    subject = f"Interested in {job_title or 'opportunities'} at {company} — {profile.get('name', 'Candidate')}"
    body = f"""Hi team at {company},

I'm {profile.get('name', 'Candidate')}, a {', '.join(profile.get('skills', [])[:5])} engineer.
I admire what you're building and would love to explore how I can contribute to {company}'s {job_title or 'engineering'} efforts.

Quick highlights:
- {profile.get('summary', '')[:300]}
- Skills: {', '.join(profile.get('skills', [])[:8])}

Happy to share a tailored resume and hop on a quick call. Would you be open to a 15-min chat next week?

Best,
{profile.get('name', 'Candidate')}
{profile.get('email', '')}
{profile.get('phone', '')}
"""
    return {"subject": subject, "body": body, "ai_used": False}


def send_via_smtp(to_email: str, subject: str, body: str, smtp_config: Dict[str, Any], otp: str = None) -> Dict[str, Any]:
    """
    Send an email through the user's own SMTP account.

    - With no SMTP host/username configured, returns a *mock* success so the
      demo flow is fully exercisable offline.
    - With real SMTP credentials, sends for real via STARTTLS.
    - 2FA/OTP: when the provider rejects the primary password (app passwords,
      534/535 auth challenges), we surface `needs_otp` so the UI can prompt the
      user for an OTP / app password / device verification, then retry.
    """
    if not smtp_config.get("host") or not smtp_config.get("username"):
        return {"success": True, "mock": True, "message": f"Mock send to {to_email}"}

    host = str(smtp_config["host"])
    port = int(smtp_config.get("port", 587))
    user = str(smtp_config["username"])
    pwd = str(smtp_config.get("password", ""))

    if otp:
        # Treat a user-supplied OTP/app password as the auth secret for this attempt.
        pwd = str(otp)

    from_name = str(smtp_config.get("from_name") or "").strip() or user

    msg = MIMEMultipart()
    msg["From"] = formataddr((from_name, user))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=15) as server:
            if smtp_config.get("use_tls", True):
                server.starttls(context=context)
            server.login(user, pwd)
            server.send_message(msg)
        return {"success": True, "mock": False}
    except smtplib.SMTPAuthenticationError as exc:
        code = str(getattr(exc, "smtp_code", ""))
        if "534" in code or "535" in code or "2FA" in str(exc) or "Application-specific" in str(exc):
            return {"success": False, "needs_otp": True, "error": f"SMTP auth rejected ({code}) — provide an OTP / app password / verify on device."}
        return {"success": False, "error": f"SMTP authentication failed: {exc}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def find_funded_companies(stage_filter: List[str] = None) -> List[Dict[str, Any]]:
    """Deprecated: moved to app/services/funding_radar.py (AI-context-driven, fresh)."""
    from app.services.funding_radar import scan_funded_companies
    from app.services.keyword_extractor import heuristic_context
    companies = await scan_funded_companies(heuristic_context({}), stages=stage_filter)
    return companies
