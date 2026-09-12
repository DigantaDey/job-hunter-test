"""Fake OpenAI-compatible provider for end-to-end verification.

Modes (set PROVIDER_MODE env var):
  normal      — /models 200 with valid key, chat works
  restricted  — /models 401 even with valid key (models listing disabled), chat works
  invalid     — everything 401
Logs every request's Authorization header to stdout so we can verify which key
the app actually sent.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODE = os.environ.get("PROVIDER_MODE", "normal")
VALID_KEY = "sk-e2e-valid-key-abcdef123456"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9101


# --------------------------------------------------------------------------- #
# Prompt-shaped answers
#
# The app does not send its workflow name to the provider, so the answer has to
# be inferred from the prompt. Each branch returns JSON that satisfies the real
# schema for that task, which is what makes this usable to drive the actual
# pipelines (profile extraction, tailoring, scoring, outreach, reflection)
# instead of only the scoring path.
# --------------------------------------------------------------------------- #
PROFILE = {
    "name": "Diganta Dey",
    "email": "diganta@example.com",
    "phone": "+49 151 0000000",
    "location": "Berlin, Germany",
    "current_title": "Senior Software Engineer",
    "summary": ("Backend engineer with 6 years building Python, FastAPI and PostgreSQL services "
                "for fintech payments platforms serving 2M users."),
    "skills": ["Python", "FastAPI", "PostgreSQL", "Docker", "Kubernetes", "AWS", "React"],
    "experience": [
        {"title": "Senior Software Engineer", "company": "FinCo", "duration": "2021 - present",
         "location": "Berlin", "bullets": ["Led the payments platform serving 2M users on AWS.",
                                          "Cut p95 latency by 40% migrating to FastAPI."]},
        {"title": "Software Engineer", "company": "ShopStack", "duration": "2018 - 2021",
         "location": "Berlin", "bullets": ["Built the order pipeline on AWS handling 500k orders/day."]},
    ],
    "education": [{"degree": "B.Tech Computer Science", "school": "VTU", "year": "2018", "field": "CS"}],
    "projects": [{"name": "Payments SDK", "description": "Open-source SDK with 1.2k stars.", "tech": ["Python"]}],
    "links": ["https://github.com/diganta"], "languages": ["English", "German"],
    "certifications": ["AWS Solutions Architect"],
}

SCORE = {"score": 88, "reason": "Strong Python/FastAPI/PostgreSQL overlap with 6 years of payments work.",
         "breakdown": {"skills": 90, "experience": 88, "seniority": 85, "domain": 90, "location": 85,
                       "education": 80},
         "strengths": ["python", "fastapi"], "missing_skills": [],
         "evidence": ["6 years building Python, FastAPI, PostgreSQL", "Payments platform at scale"],
         "recommendation": "HIGH PRIORITY", "recommendation_reason": "Matches the core stack and domain."}


def _payload_for(prompt: str):
    p = (prompt or "").lower()
    if "tailored_profile" in p:
        tailored = {k: PROFILE[k] for k in ("name", "email", "phone", "location", "links",
                                            "languages", "certifications", "education", "projects")}
        tailored.update({
            "current_title": PROFILE["current_title"],
            "summary": ("Senior backend engineer with 6 years on Python, FastAPI and PostgreSQL, "
                        "specialising in payments platforms at scale."),
            "skills": ["Python", "FastAPI", "PostgreSQL", "Kubernetes", "AWS"],
            "experience": PROFILE["experience"],
        })
        return {"tailored_profile": tailored, "tags": ["backend-python", "fintech", "aws"],
                "reasoning": "Reordered bullets to lead with payments-platform scale."}
    if "evidence pack" in p:
        return {"headline": "Senior Backend Engineer track: Python and FastAPI",
                "identity": ("Diganta Dey is tracked on the Senior Backend Engineer track. The evidence "
                             "on file covers Senior Software Engineer and Software Engineer at FinCo and "
                             "ShopStack, working with Python, FastAPI, PostgreSQL."),
                "strengths": ["Python and FastAPI delivery", "Recorded work as Senior Software Engineer"],
                "gaps": ["No recorded ownership of frontend delivery"],
                "positioning": "Position against Senior Backend Engineer requirements using recorded evidence.",
                "search_directives": ["prioritise Senior Backend Engineer roles",
                                      "prefer roles emphasising python backend"],
                "outreach_angle": "Python and FastAPI work at FinCo.",
                "evidence": ["python", "fastapi", "finco"]}
    if '"tracks"' in p or "tracks[]" in p:
        return {"tracks": [
            {"name": "Backend Engineer", "target_role": "Senior Backend Engineer",
             "keywords": ["python backend", "fastapi", "payments"], "industries": ["fintech"],
             "seniority": "senior", "why": "6 years of Python/FastAPI payments work at FinCo."},
            {"name": "Platform Engineer", "target_role": "Platform Engineer",
             "keywords": ["kubernetes", "aws", "docker"], "industries": ["developer tools"],
             "seniority": "senior", "why": "AWS and Kubernetes ownership across both roles."}]}
    if "score how well this candidate matches" in p:
        return dict(SCORE)
    if '"subject"' in p and '"body"' in p:
        return {"subject": "Senior Backend Engineer role — Python payments platform",
                "body": ("Hi team,\n\nI saw the Senior Backend Engineer opening and wanted to reach out "
                         "directly. I have spent 6 years building Python, FastAPI and PostgreSQL services, "
                         "most recently the payments platform at FinCo that serves 2M users.\n\nA FastAPI "
                         "migration there cut p95 latency by 40%, which is the kind of work this role "
                         "describes. Would you be open to a 15-minute call this week or next?\n\n"
                         "Best regards,\nDiganta Dey")}
    if "cover letter" in p:
        return {"cover_letter": ("Dear Hiring Manager,\n\nSix years of Python, FastAPI and PostgreSQL "
                                 "work on payments platforms at FinCo aligns with this role.\n\n"
                                 "Regards,\nDiganta Dey"),
                "highlights": ["python", "fastapi"]}
    if '"keywords"' in p:
        return {"keywords": ["python backend engineer", "fastapi", "payments"],
                "roles": ["Senior Backend Engineer"], "industries": ["fintech"],
                "tech_stack": ["python", "fastapi", "postgresql"], "locations": ["Berlin"],
                "seniority": "senior", "funding_focus": ["fintech"]}
    if '"tags"' in p:
        return {"tags": ["backend-python", "fintech", "aws"]}
    if '"questions"' in p:
        return {"questions": [{"q": "Describe a payments system you scaled.", "category": "system design",
                               "difficulty": "medium"}]}
    if '"portal_type"' in p:
        return {"portal_type": "unknown", "fields": [], "ai_confidence": 0.5}
    if '"size"' in p:
        return {"size": "small", "reason": "stub"}
    if '"summary"' in p and '"industry"' in p:
        return {"summary": "Berlin fintech building payment infrastructure.", "industry": "fintech",
                "size": "small", "tech_stack": ["python"]}
    return {"name": PROFILE["name"], **{k: PROFILE[k] for k in
            ("email", "phone", "location", "current_title", "summary", "skills",
             "experience", "education", "projects", "links", "languages", "certifications")}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"PROVIDER {MODE}: {fmt % args}", flush=True)

    def _authed(self):
        ok = self.headers.get("Authorization", "") == f"Bearer {VALID_KEY}"
        print(f"PROVIDER auth check: {self.headers.get('Authorization')!r} -> {ok} [{self.command} {self.path}]", flush=True)
        return ok

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            if MODE == "restricted":
                self._send(401, {"error": {"message": "models listing disabled for this key"}})
                return
            if self._authed():
                self._send(200, {"data": [{"id": "gpt-4o-mini"}]})
            else:
                self._send(401, {"error": {"message": "Incorrect API key provided"}})
            return
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self._authed():
            self._send(401, {"error": {"message": "Incorrect API key provided"}})
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            request = json.loads(raw or b"{}")
        except ValueError:
            request = {}
        prompt = " ".join(str(m.get("content") or "") for m in (request.get("messages") or []))
        self._send(200, {
            "choices": [{"message": {"content": json.dumps(_payload_for(prompt))}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })


if __name__ == "__main__":
    print(f"fake provider listening on :{PORT} mode={MODE} valid_key={VALID_KEY}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
