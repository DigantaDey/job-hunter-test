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
from app.services.ai_guardrails import (
    AIUnavailableError,
    FieldSpec,
    GuardrailError,
    SchemaSpec,
    build_fact_ledger,
    run_guarded_task,
    strip_ai_artifacts,
)

log = get_logger("app.email")

EMAIL_SCHEMA = SchemaSpec([
    FieldSpec("subject", "str", min_length=8, max_length=90),
    FieldSpec("body", "str", min_length=120, max_length=2200),
])

#: Words that must never survive into an outreach message — they are the
#: fingerprints of a degraded extraction, and a recruiter reads them as spam.
BANNED_BODY_TOKENS = ("unknown", "candidate", "lorem ipsum", "n/a", "null")
RESUME_SECTION_HEADERS = re.compile(
    r"^\s*(CAREER SUMMARY|PROFESSIONAL SUMMARY|SUMMARY|WORK EXPERIENCE|PROFESSIONAL EXPERIENCE|"
    r"EXPERIENCE|EDUCATION|SKILLS|PROJECTS|CERTIFICATIONS|OBJECTIVE)\s*$", re.IGNORECASE | re.MULTILINE)
_ADDRESS_BLOCK = re.compile(
    r"^\s*\d{4,6}\s*,?\s*[A-Za-z .]{2,40},?\s*[A-Za-z .]{2,30}\s*-?\s*\d{6}\s*$", re.MULTILINE)


def clean_message_body(body: str, profile: Dict[str, Any]) -> str:
    """
    Normalise a drafted body into something a human would actually send.

    Strips markdown/HTML residue, embedded resume headers, address blocks and
    duplicated signature material, then guarantees exactly one signature block
    built from the candidate's real contact details.
    """
    text = strip_ai_artifacts(body or "")
    text = RESUME_SECTION_HEADERS.sub("", text)
    text = _ADDRESS_BLOCK.sub("", text)

    email = str(profile.get("email") or "").strip()
    phone = str(profile.get("phone") or "").strip()
    name = str(profile.get("name") or "").strip()

    # Drop any signature the model invented (name/email/phone lines) — we
    # rebuild exactly one, so nothing is duplicated or wrong.
    lines: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if email and email.lower() in stripped.lower():
            continue
        if phone and re.sub(r"\D", "", phone) and re.sub(r"\D", "", phone) in re.sub(r"\D", "", stripped):
            continue
        if name and stripped.lower() in {name.lower(), f"best,", f"regards,", f"- {name.lower()}"}:
            continue
        if stripped.lower().rstrip(",") in {"best", "regards", "sincerely", "thanks", "thank you", "cheers"}:
            continue
        lines.append(stripped)

    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    signature = [f"Best regards,", name] if name else ["Best regards,"]
    if email:
        signature.append(email)
    if phone:
        signature.append(phone)
    return (text + "\n\n" + "\n".join(part for part in signature if part)).strip()


def build_fallback_subject(job_title: str, company: str, profile: Dict[str, Any]) -> str:
    """
    A specific, short subject — never the candidate's name as a suffix and never
    a placeholder like "Unknown".
    """
    years = ""
    summary = str(profile.get("summary") or "") + " " + " ".join(str(e.get("duration") or "")
                                                                for e in (profile.get("experience") or [])
                                                                if isinstance(e, dict))
    match = re.search(r"(\d{1,2})\+?\s*years", summary, re.IGNORECASE)
    if match:
        years = f"{match.group(1)} yrs "
    skills = [str(s) for s in (profile.get("skills") or [])][:2]
    hook = ", ".join(skills)
    title = (job_title or "the open role").strip()
    company = (company or "your team").strip()
    subject = f"{title} at {company}"
    if hook:
        subject = f"{title} at {company} — {years}{hook}"
    return subject[:88]


def _email_checks(profile: Dict[str, Any], company: str, job_title: str, jd: str):
    """Guardrails for outreach copy."""
    name = str(profile.get("name") or "").strip()
    email = str(profile.get("email") or "").strip()

    def check(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        subject = strip_ai_artifacts(str(data.get("subject") or ""))
        body = strip_ai_artifacts(str(data.get("body") or ""))

        for token in BANNED_BODY_TOKENS:
            if token in subject.lower():
                issues.append({"code": "placeholder_subject", "severity": "error", "field": "subject",
                               "value": subject, "message": f"The subject contains the placeholder '{token}'."})
        if name and name.lower() not in subject.lower() and len(subject) < 12:
            issues.append({"code": "vague_subject", "severity": "error", "field": "subject",
                           "value": subject, "message": "The subject must name the role and the company."})
        if company and company.lower() not in subject.lower() and job_title.lower() not in subject.lower():
            issues.append({"code": "off_topic_subject", "severity": "error", "field": "subject",
                           "value": subject,
                           "message": "The subject must mention the role or the company being contacted."})

        if not name:
            issues.append({"code": "missing_candidate_name", "severity": "error", "field": "body",
                           "message": "The candidate profile has no name — fix the profile before outreach."})
        if "unknown" in body.lower():
            issues.append({"code": "placeholder_body", "severity": "error", "field": "body",
                           "message": "The body contains the placeholder 'Unknown'."})
        if len(body) > 1600:
            issues.append({"code": "body_too_long", "severity": "error", "field": "body",
                           "message": "Cold outreach must stay under ~1600 characters — recruiters do not read essays."})
        if "markdown" in body.lower() or "```" in body or "](mailto:" in body:
            issues.append({"code": "markdown_artifact", "severity": "error", "field": "body",
                           "message": "The body still contains markdown/link syntax."})
        # A pasted resume is the classic failure: detect section headers/address blocks.
        if RESUME_SECTION_HEADERS.search(body) or _ADDRESS_BLOCK.search(body):
            issues.append({"code": "resume_pasted", "severity": "error", "field": "body",
                           "message": "The body pastes the resume instead of writing a message."})
        if email and body.lower().count(email.lower()) > 2:
            issues.append({"code": "duplicated_contact", "severity": "error", "field": "body",
                           "message": "The candidate's email appears more than twice in the body."})
        if jd and not any(word in body.lower() for word in re.findall(r"[a-zA-Z]{5,}", jd.lower())[:40]):
            issues.append({"code": "not_jd_specific", "severity": "warning", "field": "body",
                           "message": "The body does not reference anything from the job description."})
        return issues

    return check


NO_JD_NOTE = ("No job description supplied — write to the company about the candidate's fit "
              "for their engineering team.")


def _jd_block(jd: str) -> str:
    """JD text for the prompt (never an empty quoted block)."""
    cleaned = strip_ai_artifacts(jd or "")[:3000]
    return cleaned or NO_JD_NOTE


async def generate_cold_email(
    profile: Dict[str, Any],
    company: str,
    job_title: str = "",
    founder: bool = False,
    *,
    recipient_name: str = "",
    extra_context: str = "",
    jd: str = "",
    job_url: str = "",
    ai_config=None,
    db=None,
    user_id=None,
) -> Dict[str, Any]:
    """
    Draft a personalised outreach email.

    Grounded in the job description, cleaned of every LLM/markdown artefact, and
    validated by the outreach guardrail (no placeholders, no pasted resume, no
    invented achievements). Raises ``AIUnavailableError`` when the model cannot
    be reached — a templated fallback that reads like spam is worse than an
    explicit error the user can act on.
    """
    name = str(profile.get("name") or "").strip()
    if not name:
        raise AIUnavailableError(
            "unknown", workflow="email_gen", detail="profile_name_missing",
            context={"message": "Your profile has no name, so outreach cannot be personalised.",
                     "fix": "Re-upload your master resume with AI online, or set your name in your profile."},
        )

    audience = "founder" if founder else "hiring manager"
    # The ledger grounds the message in the candidate's own history, but the
    # company being contacted, the role and the recipient are obviously allowed
    # to appear — otherwise every correct email fails as "fabricated employer".
    outreach_context = " ".join([jd or "", company or "", job_title or "",
                                 recipient_name or "", extra_context or ""])
    ledger = build_fact_ledger(profile, outreach_context)
    prompt = f"""Write a short cold email from this candidate to the {audience} of {company}.

Role: {job_title or "their open engineering role"}
{f"Recipient: {recipient_name}." if recipient_name else "Recipient name unknown — address the team, never 'Unknown'."}
{f"Job posting: {job_url}" if job_url else ""}

Job description (the reason for the email — reference it specifically):
\"\"\"{_jd_block(jd)}\"\"\"

Candidate profile (ground truth — never add anything not present):
{json.dumps(profile, default=str)[:3500]}

{extra_context[:600]}

Rules — an automated checker rejects violations:
- Plain text only. No markdown, no [links](url), no HTML, no emojis.
- 90-140 words. Greeting, 2 short paragraphs, one clear ask (a 15-minute call).
- Open with the specific role and one concrete reason the candidate fits it, quoting a real accomplishment.
- Never invent metrics, employers, skills or dates. Never write "Unknown" or paste the resume.
- Subject: under 80 characters, names the role and the company. Do NOT put the candidate's name in the subject.
- Do NOT write a signature — the system appends the candidate's real contact details.

Return JSON {{"subject": "...", "body": "..."}}"""

    data, report = await run_guarded_task(
        "email_gen",
        system=("You are a direct, respectful outreach writer for job seekers. You write like a person, "
                "reference the actual job, and never state anything the candidate's resume does not support."),
        prompt=prompt,
        schema=EMAIL_SCHEMA,
        checks=[_email_checks(profile, company, job_title, jd)],
        ledger=ledger,
        db=db,
        user_id=user_id,
        temperature=0.5,
        max_tokens=900,
    )

    subject = strip_ai_artifacts(str(data.get("subject") or ""))[:120] or \
        build_fallback_subject(job_title, company, profile)
    body = clean_message_body(str(data.get("body") or ""), profile)
    return {
        "subject": subject,
        "body": body,
        "ai_used": True,
        "fact_guard": report.to_dict(),
    }



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
