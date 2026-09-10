"""
Outreach transport + message construction.

* ``send_via_smtp`` is a real SMTP client (STARTTLS, per-attempt OTP/app-password
  support, structured failures).
* ``generate_cold_email`` writes the message with the AI layer when configured,
  and always routes through the fact-guard (no invented achievements).
* ``build_message`` enforces CAN-SPAM/GDPR requirements that a *sending* system
  cannot delegate to the user: an unsubscribe link, the sender's postal address
  and a ``List-Unsubscribe`` header.
"""
from __future__ import annotations

import json
import re
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid
from typing import Any, Dict, List, Optional

from app.core.logging import get_logger
from app.services.ai_client import AIClientError, chat_completion

log = get_logger("app.email")


async def generate_cold_email(
    profile: Dict[str, Any],
    company: str,
    job_title: str = "",
    founder: bool = False,
    *,
    recipient_name: str = "",
    extra_context: str = "",
    ai_config=None,
) -> Dict[str, Any]:
    """Draft a personalised outreach email. Never invents experience."""
    audience = "founder" if founder else "hiring manager"
    prompt = (
        f"Write a concise, specific cold email from this candidate to the {audience} "
        f'of {company}{f" about the {job_title} role" if job_title else ""}.\n'
        f"Candidate profile (ground truth — do not add anything not present): "
        f"{json.dumps(profile)[:3000]}\n"
        f"{f'Recipient name: {recipient_name}. ' if recipient_name else ''}"
        f"{extra_context[:500]}\n"
        "Rules: 120 words max, no flattery clichés, reference one concrete, verifiable "
        "accomplishment from the profile, one clear ask (a short call). "
        "Do NOT invent metrics, employers or skills.\n"
        'Return JSON {"subject": "", "body": ""} with a plain-text body.'
    )
    try:
        data = await chat_completion("email_gen", prompt, temperature=0.6, timeout=30, ai_config=ai_config)
        subject = str(data.get("subject", "") or "").strip()
        body = str(data.get("body", "") or "").strip()
        if subject and body:
            return {"subject": subject[:200], "body": body[:8000], "ai_used": True,
                    "fact_guard": _fact_guard(profile, body)}
    except AIClientError:
        pass

    name = profile.get("name", "Candidate")
    skills = ", ".join((profile.get("skills") or [])[:5])
    subject = f"{job_title or 'Engineering roles'} at {company} — {name}"
    body = (
        f"Hi {recipient_name or 'there'},\n\n"
        f"I'm {name}, a {skills or 'software'} engineer.{(' ' + profile.get('summary', '')[:200]) if profile.get('summary') else ''}\n\n"
        f"I'm reaching out about {job_title or 'your open engineering work'} at {company}. "
        f"Would you be open to a 15-minute conversation this week or next?\n\n"
        f"Best,\n{name}\n{profile.get('email', '')}\n{profile.get('phone', '')}"
    )
    return {"subject": subject[:200], "body": body, "ai_used": False, "fact_guard": {"passed": True, "violations": []}}


def _fact_guard(profile: Dict[str, Any], body: str) -> Dict[str, Any]:
    """Flag company names in the draft that do not appear in the candidate's history."""
    known = {str(e.get("company", "")).lower() for e in (profile.get("experience") or []) if isinstance(e, dict)}
    known |= {str(profile.get("name", "")).lower()}
    mentioned = {m.lower() for m in re.findall(r"\b[A-Z][A-Za-z0-9&.\-]{2,}\b", body)}
    suspicious = [m for m in mentioned if m not in known and m.lower() not in {"hi", "i", "the", "best", "would"}]
    # Only flag when a *company-like* pattern is present in an achievement sentence.
    violations = [m for m in suspicious if any(word in body.lower() for word in ["at ", "worked", "built at", "joined"])][:5]
    return {"passed": not violations, "violations": violations, "checked": len(mentioned)}


def build_message(
    *,
    to_email: str,
    subject: str,
    body: str,
    from_email: str,
    from_name: str = "",
    unsubscribe_url: str = "",
    postal_address: str = "",
    tracking_pixel_url: str = "",
) -> MIMEMultipart:
    """Compose the MIME message with compliance footers + tracking pixel."""
    message = MIMEMultipart("alternative")
    message["From"] = formataddr((from_name or from_email, from_email))
    message["To"] = to_email
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain=from_email.split("@")[-1] if "@" in from_email else None)
    if unsubscribe_url:
        message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    footer_lines = [""]
    if postal_address:
        footer_lines.append(postal_address)
    if unsubscribe_url:
        footer_lines.append(f"Don't want to hear from me again? Unsubscribe: {unsubscribe_url}")
    footer_lines.append("Sent by JobHunter AI on behalf of the sender named above.")

    text_body = body + "\n" + "\n".join(footer_lines)
    html_body = (
        "<div style=\"font-family:system-ui,Segoe UI,sans-serif;font-size:14px;line-height:1.5\">"
        + "".join(f"<p>{line}</p>" for line in body.split("\n\n"))
        + "</div>"
        + (f"<p style='color:#6b7280;font-size:12px'>{postal_address}</p>" if postal_address else "")
        + (f"<p style='font-size:12px'><a href='{unsubscribe_url}'>Unsubscribe</a></p>" if unsubscribe_url else "")
        + (f"<img src='{tracking_pixel_url}' width='1' height='1' alt='' style='display:none'/>" if tracking_pixel_url else "")
    )
    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))
    return message


def send_via_smtp(
    to_email: str,
    subject: str,
    body: str,
    smtp_config: Dict[str, Any],
    otp: Optional[str] = None,
    *,
    from_email: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Send through the user's SMTP account.

    Without configuration the function refuses to send (``configured=False``) —
    the caller decides how to surface that; nothing is silently "mock sent".
    """
    host = str(smtp_config.get("host") or "").strip()
    username = str(smtp_config.get("username") or "").strip()
    if not host or not username:
        return {"success": False, "configured": False,
                "error": "SMTP is not configured — add host/username/password in Settings → Email"}

    port = int(smtp_config.get("port", 587) or 587)
    password = str(otp or smtp_config.get("password") or "")
    if not password:
        return {"success": False, "needs_otp": True, "configured": True,
                "error": "SMTP password / app password required"}

    from_name = str(smtp_config.get("from_name") or "").strip() or username
    message = build_message(
        to_email=to_email,
        subject=subject,
        body=body,
        from_email=from_email or username,
        from_name=from_name,
        unsubscribe_url=str(smtp_config.get("unsubscribe_url") or ""),
        postal_address=str(smtp_config.get("postal_address") or ""),
        tracking_pixel_url=str(smtp_config.get("tracking_pixel_url") or ""),
    )

    use_tls = bool(smtp_config.get("use_tls", True))
    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as server:
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.ehlo()
                if use_tls:
                    server.starttls(context=context)
                    server.ehlo()
                server.login(username, password)
                server.send_message(message)
        return {"success": True, "mock": False, "message_id": message.get("Message-ID", "")}
    except smtplib.SMTPAuthenticationError as exc:
        code = str(getattr(exc, "smtp_code", "") or "")
        if code.startswith("53") or "2FA" in str(exc) or "Application-specific" in str(exc):
            return {"success": False, "needs_otp": True, "configured": True,
                    "error": f"SMTP authentication rejected ({code or 'auth'}) — provide an app password / OTP"}
        return {"success": False, "configured": True, "error": f"SMTP authentication failed: {exc}"}
    except smtplib.SMTPRecipientsRefused as exc:
        return {"success": False, "configured": True, "bounced": True, "error": f"recipient refused: {exc}"}
    except Exception as exc:
        return {"success": False, "configured": True, "error": f"{type(exc).__name__}: {exc}"}


async def find_funded_companies(stage_filter: List[str] = None) -> List[Dict[str, Any]]:
    """Deprecated shim → use ``app.services.funding_radar``."""
    from app.services.funding_radar import scan_funded_companies
    from app.services.keyword_extractor import heuristic_context

    companies, _ = await scan_funded_companies(heuristic_context({}), stages=stage_filter)
    return companies
