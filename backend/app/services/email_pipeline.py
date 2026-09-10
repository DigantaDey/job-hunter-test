import re
import asyncio
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, Any, List
import httpx
import json
from app.core.config import settings

async def find_decision_maker(company: str, department: str = "engineering", ai_config=None) -> Dict[str,Any]:
    # Mock + AI: finds hiring manager email
    if not settings.ai_api_key:
        # heuristic
        domain = re.sub(r'\W+','', company.lower()) + ".com"
        names = ["hiring","talent","recruiting","cto","hiring-manager"]
        email = f"{department}@{domain}"
        return {"name": "Hiring Manager", "email": email, "title": "Engineering Manager", "confidence": 0.5, "source": "heuristic"}
    # AI path - would use hunter api + LLM
    base_url = (ai_config or {}).get("base_url") or settings.ai_base_url
    api_key = (ai_config or {}).get("api_key") or settings.ai_api_key
    model = (ai_config or {}).get("model") or settings.ai_model
    prompt = f"""Find the email of the possible decision making person for {department} at company "{company}". Especially for startups/small companies, identify founder or hiring manager. Return JSON {{"name": "", "email": "", "title":"", "confidence":0-1, "reason":""}}. If not found, guess pattern like firstname@company.com ."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type":"application/json"},
                json={"model": model, "messages":[{"role":"user","content": prompt}], "temperature":0.2, "response_format":{"type":"json_object"}})
            if resp.status_code==200:
                data=json.loads(resp.json()["choices"][0]["message"]["content"])
                return data
    except:
        pass
    domain = re.sub(r'\W+','', company.lower()) + ".com"
    return {"name":"Hiring Manager","email":f"careers@{domain}","title":"Hiring Manager","confidence":0.4,"source":"fallback"}

async def generate_cold_email(profile: Dict[str,Any], company: str, job_title: str = "", founder: bool=False, ai_config=None) -> Dict[str,str]:
    if not settings.ai_api_key:
        subject = f"Interested in {job_title or 'opportunities'} at {company} — {profile.get('name','Candidate')}"
        body = f"""Hi team at {company},

I'm {profile.get('name','Candidate')}, a {', '.join(profile.get('skills',[])[:5])} engineer.
I admire what you're building and would love to explore how I can contribute to {company}'s {job_title or 'engineering'} efforts.

Quick highlights:
- {profile.get('summary','')[:300]}
- Skills: {', '.join(profile.get('skills',[])[:8])}

Happy to share a tailored resume and hop on a quick call. Would you be open to a 15-min chat next week?

Best,
{profile.get('name','Candidate')}
{profile.get('email','')}
{profile.get('phone','')}
"""
        return {"subject": subject, "body": body, "ai_used": False}
    base_url = (ai_config or {}).get("base_url") or settings.ai_base_url
    api_key = (ai_config or {}).get("api_key") or settings.ai_api_key
    model = (ai_config or {}).get("model") or settings.ai_model
    context = "founder" if founder else "hiring manager"
    prompt = f"""
Write a concise, personalized cold email from the candidate to the {context} of {company} for role "{job_title}".
Candidate profile: {json.dumps(profile)[:3000]}
Job/company: {company}
Tone: warm, confident, not pushy, startup-appropriate. Mention specific accomplishments, not generic fluff. Include a clear CTA.
Return JSON {{"subject":"", "body":""}} plain text body.
Do NOT hallucinate achievements not in profile.
"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type":"application/json"},
                json={"model": model, "messages":[{"role":"user","content": prompt}], "temperature":0.7, "response_format":{"type":"json_object"}})
            if resp.status_code==200:
                data=json.loads(resp.json()["choices"][0]["message"]["content"])
                return {"subject": data.get("subject",""), "body": data.get("body",""), "ai_used": True}
    except Exception as e:
        pass
    return {"subject": f"Opportunity at {company}", "body": "Hello,\n\nI would love to connect.\n\nBest,\n"+profile.get("name","")}

def send_via_smtp(to_email: str, subject: str, body: str, smtp_config: Dict[str,Any], otp: str = None) -> Dict[str,Any]:
    """
    Sends via SMTP. Handles 2FA by raising needs_otp if server requires it.
    smtp_config: {host, port, username, password, use_tls}
    For demo, mock success unless config missing.
    """
    if not smtp_config.get("host") or not smtp_config.get("username"):
        # mock send for demo
        return {"success": True, "mock": True, "message": f"Mock sent to {to_email}"}
    # Real SMTP flow (simplified)
    try:
        host = smtp_config["host"]
        port = int(smtp_config.get("port",587))
        user = smtp_config["username"]
        pwd = smtp_config["password"]
        msg = MIMEMultipart()
        msg["From"] = user
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=10) as server:
            if smtp_config.get("use_tls", True):
                server.starttls(context=context)
            # If OTP provided, try handling 2FA: some providers need app password; we simulate
            if otp:
                # In real 2FA, provider would send OTP challenge; we just append to password or use separate flow
                # Here we treat OTP as app password suffix for demo
                pwd = pwd + otp if otp else pwd
            server.login(user, pwd)
            server.send_message(msg)
        return {"success": True, "mock": False}
    except smtplib.SMTPAuthenticationError as e:
        if "2FA" in str(e) or "Application-specific" in str(e) or "534" in str(e):
            return {"success": False, "needs_otp": True, "error": str(e)}
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": str(e)}

async def find_funded_companies(stage_filter: List[str] = None) -> List[Dict[str, Any]]:
    """Deprecated: moved to app/services/funding_radar.py (AI-context-driven, fresh)."""
    from app.services.funding_radar import scan_funded_companies
    from app.services.keyword_extractor import heuristic_context
    companies = await scan_funded_companies(heuristic_context({}), stages=stage_filter)
    return companies
