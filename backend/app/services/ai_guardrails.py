"""
AI guardrails — the reliability layer between every AI task and the user.

The product goal is "AI does the work, and the work is trustworthy". Two
guarantees live here and nowhere else:

1. **Honesty about availability.** When the model cannot be reached the product
   must say *why* (no key, bad key, wrong model, provider 5xx, breaker open,
   budget hit…) and refuse to produce a half-baked result. Silent heuristic
   fallbacks are exactly what produced resumes/emails nobody could use, so they
   are gone from the user-facing paths.

2. **Grounding.** Every task declares a *contract*: the system prompt that
   makes the same model behave like a specific specialist, the JSON schema the
   answer must satisfy, and the guardrail checks the answer must pass. Output is
   validated, checked against a fact ledger built from the user's own data, and
   — when it fails — sent back to the model once with the exact violations to
   repair. Only a passing result reaches the database or the UI.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.core.logging import get_logger

log = get_logger("app.guardrails")


# --------------------------------------------------------------------------- #
# 1. Availability diagnosis
# --------------------------------------------------------------------------- #
#: reason code → (what happened, how to fix it). Rendered verbatim in the UI so
#: the message is identical whether it comes from a REST error body or a banner.
DIAGNOSIS: Dict[str, Tuple[str, str]] = {
    "no_api_key": (
        "AI is not configured — no API key is set for this account.",
        "Open Settings → AI API, paste an OpenAI-compatible key (base_url like https://api.openai.com/v1, model like gpt-4o-mini) and save.",
    ),
    "invalid_api_key": (
        "The AI provider rejected the API key (401/403).",
        "Re-check the key in Settings → AI API, and make sure base_url and model belong to the same provider.",
    ),
    "stored_key_unreadable": (
        "The saved API key exists but cannot be decrypted on this server (its ENCRYPTION_KEY changed).",
        "Re-enter the key in Settings → AI API and save; the app is currently running without AI.",
    ),
    "unreachable": (
        "The AI endpoint could not be reached (DNS/TLS/connection failure).",
        "Check the base_url in Settings → AI API and the server's outbound network access.",
    ),
    "timeout": (
        "The AI endpoint did not answer in time.",
        "The provider may be overloaded — retry, switch model, or raise AI_TIMEOUT.",
    ),
    "provider_error": (
        "The AI provider returned a server error (5xx).",
        "Transient provider outage — retry shortly, or point the workflow at another provider.",
    ),
    "rate_limited": (
        "The AI provider rate-limited the account (429).",
        "Lower the RPM in Settings → AI API, or wait for the rate window to reset.",
    ),
    "quota_exceeded": (
        "The AI provider rejected the request for quota/billing reasons.",
        "Add credit to the provider account, or switch to a model with remaining quota.",
    ),
    "circuit_open": (
        "Too many consecutive AI failures — the circuit breaker is open and failing fast.",
        "Fix the underlying error shown, then retry; the breaker resets automatically after the cooldown.",
    ),
    "budget_exhausted": (
        "The daily AI token budget is used up.",
        "Raise AI_DAILY_TOKEN_BUDGET or wait for the next UTC day.",
    ),
    "limit_exceeded": (
        "Your plan limit for this operation is reached.",
        "Upgrade the plan or wait for the monthly counter to reset.",
    ),
    "malformed_response": (
        "The provider returned a response without a usable message.",
        "Usually a provider-side problem — retry, or try a different model.",
    ),
    "invalid_json": (
        "The model answered with text that is not valid JSON.",
        "Already retried automatically. A stronger model (or one that supports JSON mode) fixes this almost every time — switch model in Settings → AI API.",
    ),
    "empty_response": (
        "The model returned an empty answer.",
        "Already retried automatically. Free-tier and 'thinking' models do this intermittently — retry the upload, or switch to a model that reliably returns JSON (e.g. gpt-4o-mini).",
    ),
    "truncated_response": (
        "The model's answer was cut off before the JSON completed.",
        "The output-token limit is too small for this document — raise AI_MAX_OUTPUT_TOKENS (Settings → AI API) or use a model with a larger output limit. Reasoning models also burn tokens on 'thinking' before the JSON starts.",
    ),
    "content_filter": (
        "The provider blocked the answer with its content filter.",
        "Retry, or switch models in Settings → AI API if the document keeps triggering the filter.",
    ),
    "model_unavailable": (
        "The configured model does not exist on that provider (404).",
        "Set a valid model in Settings → AI API for this provider.",
    ),
    "guardrail_failed": (
        "The model answered, but the result failed the accuracy guardrail after a repair attempt.",
        "Retry — or use a stronger model for this workflow in Settings → Per-workflow AI override.",
    ),
    "unknown": (
        "The AI request failed for an unclassified reason.",
        "Check the AI status panel in Settings for the provider's own error text.",
    ),
}


class AIUnavailableError(Exception):
    """AI cannot produce a trustworthy result. Always carries a full diagnosis."""

    http_status = 503

    def __init__(self, reason: str, *, workflow: str = "", detail: str = "",
                 context: Optional[Dict[str, Any]] = None):
        message, fix = DIAGNOSIS.get(reason, DIAGNOSIS["unknown"])
        super().__init__(message)
        self.reason = reason
        self.workflow = workflow
        self.message = message
        self.fix = fix
        self.detail = (detail or "")[:500]
        self.context = context or {}

    def payload(self) -> Dict[str, Any]:
        return {
            "code": "ai_unavailable",
            "reason": self.reason,
            "message": self.message,
            "fix": self.fix,
            "workflow": self.workflow,
            "detail": self.detail,
            **self.context,
        }


class GuardrailError(Exception):
    """The model answered, but the answer is not safe to use."""

    http_status = 422

    def __init__(self, workflow: str, issues: Sequence[Dict[str, Any]], *, detail: str = ""):
        self.workflow = workflow
        self.issues = list(issues)
        self.detail = detail or ""
        top = "; ".join(str(i.get("message") or i.get("code")) for i in self.issues[:4])
        super().__init__(f"{workflow}: {top or 'guardrail failed'}")

    def payload(self) -> Dict[str, Any]:
        message, fix = DIAGNOSIS["guardrail_failed"]
        return {
            "code": "guardrail_failed",
            "reason": "guardrail_failed",
            "message": message,
            "fix": fix,
            "workflow": self.workflow,
            "issues": self.issues,
            "detail": (self.detail or "")[:500],
        }


def describe_ai_error(exc: BaseException, *, workflow: str = "",
                      probe: Optional[Dict[str, Any]] = None) -> AIUnavailableError:
    """Turn any exception from the AI layer into a fully explained outage."""
    from app.services.ai_client import AIClientError

    if isinstance(exc, AIUnavailableError):
        return exc
    reason = "unknown"
    detail = str(exc)
    if isinstance(exc, AIClientError):
        reason = exc.reason or "unknown"
        detail = str(exc)
    elif isinstance(exc, (TimeoutError,)):
        reason = "timeout"
    if probe:
        # A probe result is more precise than the exception text (it knows the
        # provider's own error message and whether /models vs chat failed).
        probed = str(probe.get("reason") or "")
        if probed in DIAGNOSIS and probed != reason:
            reason = probed
        detail = detail or str(probe.get("detail") or probe.get("error") or "")
    return AIUnavailableError(
        reason,
        workflow=workflow,
        detail=detail,
        context={
            "base_url": (probe or {}).get("base_url"),
            "model": (probe or {}).get("model"),
            "key_preview": (probe or {}).get("key_preview"),
            "key_source": (probe or {}).get("key_source"),
            "latency_ms": (probe or {}).get("latency_ms"),
            "hint": (probe or {}).get("hint"),
        },
    )


async def diagnose_outage(exc: BaseException, *, workflow: str = "",
                          db=None, user_id: Optional[int] = None) -> Dict[str, Any]:
    """Full, user-facing explanation of why AI is offline (never raises)."""
    from app.services.ai_client import ping

    probe: Dict[str, Any] = {}
    try:
        probe = await ping(workflow, db=db, user_id=user_id, timeout=6)
    except Exception as exc:  # pragma: no cover - diagnostics must never fail
        probe = {"online": False, "reason": "unreachable", "error": str(exc)}
    outage = describe_ai_error(exc, workflow=workflow, probe=probe)
    return outage.payload()


# --------------------------------------------------------------------------- #
# 2. Text hygiene — LLM output arrives with markdown/HTML/emoji noise
# --------------------------------------------------------------------------- #
_MD_LINK = re.compile(r"\[([^\]]*)\]\((?:mailto:)?([^)\s]+)\)")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]{1,120}>")
_HTML_ENTITY = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'",
    "&nbsp;": " ", "&mdash;": "—", "&ndash;": "–", "&bull;": "•", "&rsquo;": "'",
    "&lsquo;": "'", "&ldquo;": '"', "&rdquo;": '"', "&apos;": "'",
}
_ENTITIES_RE = re.compile("|".join(re.escape(k) for k in _HTML_ENTITY))
_FENCE = re.compile(r"^\s*(```|~~~)[a-zA-Z0-9_-]*\s*$", re.MULTILINE)
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


def markdown_to_text(value: str) -> str:
    """Flatten markdown emphasis/links/tables into clean plain text."""
    if not isinstance(value, str) or not value:
        return ""
    text = _MD_IMAGE.sub("", value)
    # [name](mailto:a@b.com) → "name <a@b.com>" only when they differ
    def _link(match: re.Match) -> str:
        label, target = match.group(1).strip(), match.group(2).strip()
        if not label or label.lower() in target.lower() or target.lower() in label.lower():
            return target
        return f"{label} {target}" if "@" in target else label

    text = _MD_LINK.sub(_link, text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)      # headings
    text = re.sub(r"(?<![\w*])\*\*([^*\n]{1,200})\*\*(?![\w*])", r"\1", text)  # bold
    text = re.sub(r"(?<![\w*])\*([^*\n]{1,200})\*(?![\w*])", r"\1", text)      # italic
    text = re.sub(r"(?<![\w_])__([^_\n]{1,200})__(?![\w_])", r"\1", text)
    text = re.sub(r"(?<![\w_])_([^_\n]{1,200})_(?![\w_])", r"\1", text)
    text = re.sub(r"`([^`\n]{1,200})`", r"\1", text)                          # inline code
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)                   # quotes
    # Normalise bullets, but never swallow a phone number's "+" or a hyphen in
    # "e-mail"-style prose: only treat - / + as a bullet when a space follows.
    text = re.sub(r"^\s*(?:•\s*|[-+]\s+)", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\|\s*-+.*$", "", text, flags=re.MULTILINE)             # table rules
    text = text.replace("|", " ").replace("---", "—")
    return text.strip()


def strip_ai_artifacts(value: str) -> str:
    """Remove everything an LLM should never have emitted into user content."""
    if not isinstance(value, str) or not value:
        return ""
    text = _FENCE.sub("", value)
    text = markdown_to_text(text)
    text = _HTML_TAG.sub("", text)
    text = _ENTITIES_RE.sub(lambda m: _HTML_ENTITY[m.group(0)], text)
    # emoji / pictographs / variation selectors — never belong on a resume
    text = "".join(ch for ch in text if ord(ch) < 0x2190 or ord(ch) in (0x2022, 0x2013, 0x2014))
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_json(text: str) -> Any:
    """Parse a model answer that may be wrapped in prose or a fenced block."""
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    fenced = _JSON_BLOCK.search(candidate)
    if fenced:
        candidate = fenced.group(1)
    try:
        return json.loads(candidate)
    except (ValueError, TypeError):
        pass
    start = min([i for i in (candidate.find("{"), candidate.find("[")) if i >= 0] or [-1])
    end = max(candidate.rfind("}"), candidate.rfind("]"))
    if start >= 0 and end > start:
        try:
            return json.loads(candidate[start:end + 1])
        except (ValueError, TypeError):
            return None
    return None


# --------------------------------------------------------------------------- #
# 3. Fact ledger — what the user's own data actually supports
# --------------------------------------------------------------------------- #
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{7,17}\d)")
DATE_RE = re.compile(
    r"\b((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{4}"
    r"|\d{4}\s*[-–/]\s*\d{4}|\d{4}\s*[-–/]\s*(?:present|current|now)"
    r"|(?:present|current|now)\s*[-–/]\s*\d{4}|\d{4})\b",
    re.IGNORECASE,
)
URL_RE = re.compile(r"(?:https?://|www\.|linkedin\.com/|github\.com/)\S+", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


@dataclass
class FactLedger:
    """Every entity the user's own data supports, normalised for comparison."""

    companies: Set[str] = field(default_factory=set)
    schools: Set[str] = field(default_factory=set)
    titles: Set[str] = field(default_factory=set)
    skills: Set[str] = field(default_factory=set)
    degrees: Set[str] = field(default_factory=set)
    emails: Set[str] = field(default_factory=set)
    phones: Set[str] = field(default_factory=set)
    links: Set[str] = field(default_factory=set)
    years: Set[str] = field(default_factory=set)
    raw: str = ""

    def knows(self, kind: str, value: Any) -> bool:
        bucket = getattr(self, kind, set())
        return _norm(value) in bucket

    def contains_text(self, value: Any) -> bool:
        """Free-form grounding: is this phrase present in the user's own data?"""
        token = _norm(value)
        return bool(token) and token in _norm(self.raw)


def build_fact_ledger(profile: Dict[str, Any], extra_text: str = "") -> FactLedger:
    """Build the grounding whitelist from the user's own profile/resume text."""
    profile = profile or {}
    ledger = FactLedger()

    def add(bucket: Set[str], value: Any) -> None:
        token = _norm(value)
        if token:
            bucket.add(token)

    add(ledger.companies, profile.get("company"))
    add(ledger.titles, profile.get("current_title"))
    for entry in profile.get("experience") or []:
        if not isinstance(entry, dict):
            add(ledger.companies, entry)
            continue
        add(ledger.companies, entry.get("company"))
        add(ledger.titles, entry.get("title"))
        for key in ("duration", "dates", "period", "start", "end", "location"):
            for match in _YEAR_RE.finditer(str(entry.get(key) or "")):
                ledger.years.add(match.group(0))
        for bullet in _as_list(entry.get("bullets") or entry.get("description")):
            for match in _YEAR_RE.finditer(str(bullet)):
                ledger.years.add(match.group(0))
    for entry in profile.get("education") or []:
        if not isinstance(entry, dict):
            add(ledger.schools, entry)
            continue
        add(ledger.schools, entry.get("school") or entry.get("institution"))
        add(ledger.degrees, entry.get("degree"))
        for match in _YEAR_RE.finditer(str(entry.get("year") or entry.get("duration") or "")):
            ledger.years.add(match.group(0))
    for entry in profile.get("projects") or []:
        if isinstance(entry, dict):
            add(ledger.titles, entry.get("name"))
    for skill in _as_list(profile.get("skills")):
        add(ledger.skills, skill)
    for cert in _as_list(profile.get("certifications")):
        add(ledger.degrees, cert)
    for link in _as_list(profile.get("links")):
        ledger.links.add(_norm(link))
    for email in EMAIL_RE.findall(str(profile.get("email") or "")):
        ledger.emails.add(email.lower())
    for phone in PHONE_RE.findall(str(profile.get("phone") or "")):
        ledger.phones.add(re.sub(r"\D", "", phone))

    ledger.raw = " ".join([
        json.dumps(profile, default=str)[:20000],
        str(profile.get("raw_text") or "")[:20000],
        str(extra_text or "")[:20000],
    ])
    for match in _YEAR_RE.finditer(ledger.raw):
        ledger.years.add(match.group(0))
    return ledger


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)]


# Known employer-ish words that must not be treated as fabricated companies.
_COMPANY_STOPWORDS = {
    "hi", "hello", "dear", "team", "company", "companies", "inc", "llc", "ltd", "gmbh",
    "the", "and", "for", "with", "you", "your", "our", "their", "this", "that", "would",
    "best", "regards", "sincerely", "thanks", "thank", "cheers", "looking", "forward",
    "engineering", "product", "design", "data", "hiring", "manager", "recruiter",
}


def check_grounded(text: str, ledger: FactLedger, *, check_companies: bool = True,
                   check_dates: bool = True, check_contacts: bool = True) -> List[Dict[str, Any]]:
    """Flag entities in generated text that the user's own data does not support."""
    issues: List[Dict[str, Any]] = []
    cleaned = strip_ai_artifacts(text or "")
    if not cleaned:
        return issues
    known = _norm(ledger.raw)

    if check_companies:
        for token in re.findall(r"\b([A-Z][A-Za-z0-9&.'-]{2,}(?:\s+[A-Z][A-Za-z0-9&.'-]{1,})?)\b", cleaned):
            name = token.strip(" .'")
            low = name.lower()
            if low in _COMPANY_STOPWORDS or len(low) < 3:
                continue
            if _norm(name) in ledger.companies or _norm(name) in known:
                continue
            # A single capitalised word inside a sentence is usually prose, not an
            # employer. Only flag it when it sits in an employment phrase.
            if not re.search(rf"\b(?:at|from|joined|worked)\s+{re.escape(name)}\b", cleaned, re.IGNORECASE):
                continue
            issues.append({"code": "fabricated_employer", "severity": "error",
                           "field": "text", "value": name,
                           "message": f"'{name}' is not in the candidate's own history — remove or correct it."})

    if check_dates:
        for match in _YEAR_RE.finditer(cleaned):
            year = match.group(0)
            if year not in ledger.years:
                issues.append({"code": "fabricated_date", "severity": "error", "field": "text",
                               "value": year,
                               "message": f"The year '{year}' does not appear anywhere in the source resume."})

    if check_contacts:
        for email in EMAIL_RE.findall(cleaned):
            if email.lower() not in ledger.emails:
                issues.append({"code": "fabricated_email", "severity": "error", "field": "contact",
                               "value": email, "message": f"'{email}' is not the candidate's email address."})
        for phone in PHONE_RE.findall(cleaned):
            digits = re.sub(r"\D", "", phone)
            if len(digits) >= 8 and not any(digits in stored or stored in digits for stored in ledger.phones):
                issues.append({"code": "fabricated_phone", "severity": "error", "field": "contact",
                               "value": phone, "message": f"'{phone}' is not the candidate's phone number."})

    for link in URL_RE.findall(cleaned):
        if _norm(link) not in ledger.links and _norm(link) not in known:
            issues.append({"code": "fabricated_link", "severity": "warning", "field": "links",
                           "value": link, "message": f"'{link}' is not in the candidate's profile."})
    return issues


# --------------------------------------------------------------------------- #
# 4. Schema validation
# --------------------------------------------------------------------------- #
@dataclass
class FieldSpec:
    name: str
    kind: str = "str"           # str | int | float | bool | list | dict
    required: bool = True
    min_length: int = 0
    max_length: int = 0
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Optional[Sequence[str]] = None


class SchemaSpec:
    """Minimal JSON contract validator — returns issues, never raises."""

    def __init__(self, fields: Iterable[FieldSpec]):
        self.fields = list(fields)

    def validate(self, data: Any) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        issues: List[Dict[str, Any]] = []
        if not isinstance(data, dict):
            return None, [{"code": "not_an_object", "severity": "error", "field": "$",
                           "message": "The model did not return a JSON object."}]
        for spec in self.fields:
            value = data.get(spec.name)
            if value is None or (isinstance(value, str) and not value.strip()):
                if spec.required:
                    issues.append({"code": "missing_field", "severity": "error", "field": spec.name,
                                   "message": f"'{spec.name}' is required and was missing or empty."})
                continue
            expected = {"str": str, "int": int, "float": (int, float), "bool": bool,
                        "list": list, "dict": dict}.get(spec.kind)
            if expected and not isinstance(value, expected):
                issues.append({"code": "wrong_type", "severity": "error", "field": spec.name,
                               "message": f"'{spec.name}' must be {spec.kind}, got {type(value).__name__}."})
                continue
            if spec.kind == "str":
                text = str(value)
                if spec.min_length and len(text.strip()) < spec.min_length:
                    issues.append({"code": "too_short", "severity": "error", "field": spec.name,
                                   "message": f"'{spec.name}' must be at least {spec.min_length} characters."})
                if spec.max_length and len(text) > spec.max_length * 3:
                    issues.append({"code": "too_long", "severity": "error", "field": spec.name,
                                   "message": f"'{spec.name}' is far longer than {spec.max_length} characters."})
            if spec.kind == "list" and spec.min_length and len(value) < spec.min_length:
                issues.append({"code": "too_few_items", "severity": "error", "field": spec.name,
                               "message": f"'{spec.name}' needs at least {spec.min_length} item(s)."})
            if spec.kind in ("int", "float") and spec.minimum is not None and float(value) < spec.minimum:
                issues.append({"code": "out_of_range", "severity": "error", "field": spec.name,
                               "message": f"'{spec.name}' must be >= {spec.minimum}."})
            if spec.kind in ("int", "float") and spec.maximum is not None and float(value) > spec.maximum:
                issues.append({"code": "out_of_range", "severity": "error", "field": spec.name,
                               "message": f"'{spec.name}' must be <= {spec.maximum}."})
            if spec.choices and str(value) not in spec.choices:
                issues.append({"code": "invalid_choice", "severity": "error", "field": spec.name,
                               "message": f"'{spec.name}' must be one of {list(spec.choices)}."})
        blocking = [i for i in issues if i.get("severity") == "error"]
        return (data, issues) if not blocking else (None, issues)


# --------------------------------------------------------------------------- #
# 5. Report
# --------------------------------------------------------------------------- #
@dataclass
class GuardrailReport:
    """What was checked, what failed, what was repaired — shown in the UI."""

    workflow: str
    passed: bool = True
    issues: List[Dict[str, Any]] = field(default_factory=list)
    repaired: List[str] = field(default_factory=list)
    checks: List[str] = field(default_factory=list)
    score: float = 100.0
    source: str = "ai"
    model: str = ""
    attempts: int = 1
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow": self.workflow, "passed": self.passed, "issues": self.issues,
            "repaired": self.repaired, "checks": self.checks, "score": round(self.score, 1),
            "source": self.source, "model": self.model, "attempts": self.attempts,
            "created_at": self.created_at,
        }


def _report_from_issues(workflow: str, issues: Sequence[Dict[str, Any]], checks: Sequence[str],
                        model: str = "", attempts: int = 1, source: str = "ai") -> GuardrailReport:
    errors = [i for i in issues if i.get("severity") == "error"]
    warnings = [i for i in issues if i.get("severity") == "warning"]
    score = max(0.0, 100.0 - 12.0 * len(errors) - 3.0 * len(warnings))
    return GuardrailReport(workflow=workflow, passed=not errors, issues=list(issues),
                           checks=list(checks), score=score, model=model, attempts=attempts,
                           source=source)


# --------------------------------------------------------------------------- #
# 6. The contract runner — one code path for every AI task
# --------------------------------------------------------------------------- #
async def run_guarded_task(
    workflow: str,
    *,
    system: str,
    prompt: str,
    schema: Optional[SchemaSpec],
    checks: Sequence[Callable[[Dict[str, Any]], List[Dict[str, Any]]]] = (),
    ledger: Optional[FactLedger] = None,
    db=None,
    user_id: Optional[int] = None,
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
    timeout: Optional[int] = None,
    repairs: int = 1,
    coerce: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Any], GuardrailReport]:
    """
    Run one AI task under its contract.

    * raises :class:`AIUnavailableError` when the model cannot answer (with the
      provider's own diagnosis) — never a silent heuristic substitute;
    * validates the JSON schema, runs the task's guardrail checks and the fact
      ledger, and asks the model once to repair the exact violations;
    * raises :class:`GuardrailError` when the repaired answer still fails.
    """
    from app.services.ai_client import chat_completion

    attempt = 0
    last_issues: List[Dict[str, Any]] = []
    current_prompt = prompt

    while attempt <= repairs:
        attempt += 1
        try:
            result = await chat_completion(
                workflow, current_prompt, system=system, temperature=temperature,
                max_tokens=max_tokens, timeout=timeout, db=db, user_id=user_id,
            )
        except Exception as exc:
            raise describe_ai_error(exc, workflow=workflow) from exc

        payload = result.get("content") if isinstance(result, dict) and "content" in result else result
        parsed = payload if isinstance(payload, dict) else extract_json(str(payload or ""))
        if not isinstance(parsed, dict):
            last_issues = [{"code": "invalid_json", "severity": "error", "field": "$",
                            "message": "The model did not return a JSON object."}]
            if attempt <= repairs:
                current_prompt = _repair_prompt(prompt, last_issues)
                continue
            raise GuardrailError(workflow, last_issues)

        if coerce:
            parsed = coerce(parsed)

        issues: List[Dict[str, Any]] = []
        if schema is not None:
            parsed, schema_issues = schema.validate(parsed)
            issues.extend(schema_issues)
            if parsed is None and attempt <= repairs:
                current_prompt = _repair_prompt(prompt, schema_issues)
                continue
            if parsed is None:
                raise GuardrailError(workflow, schema_issues)
        for check in checks:
            issues.extend(check(parsed) or [])
        if ledger is not None:
            issues.extend(check_grounded(json.dumps(parsed, default=str), ledger,
                                         check_companies=True, check_dates=True, check_contacts=False))

        errors = [i for i in issues if i.get("severity") == "error"]
        if not errors:
            report = _report_from_issues(workflow, issues, _check_names(checks, schema, ledger),
                                         attempts=attempt)
            return parsed, report

        last_issues = issues
        if attempt <= repairs:
            current_prompt = _repair_prompt(prompt, errors)
            continue
        raise GuardrailError(workflow, errors, detail=str(parsed)[:300])

    raise GuardrailError(workflow, last_issues)


def _check_names(checks: Sequence[Callable], schema: Optional[SchemaSpec],
                 ledger: Optional[FactLedger]) -> List[str]:
    names = ["schema"] if schema is not None else []
    names += [getattr(c, "__name__", "check") for c in checks]
    if ledger is not None:
        names.append("fact_ledger")
    return names


def _repair_prompt(original: str, issues: Sequence[Dict[str, Any]]) -> str:
    listing = "\n".join(f"- {i.get('field', '?')}: {i.get('message', i.get('code'))}" for i in issues[:8])
    return (
        f"{original}\n\n"
        "Your previous answer was REJECTED by the accuracy guardrail. Fix exactly these problems "
        "and answer again with JSON only:\n"
        f"{listing}\n"
        "Do not add any information that is not present in the source material."
    )


__all__ = [
    "AIUnavailableError", "GuardrailError", "DIAGNOSIS",
    "describe_ai_error", "diagnose_outage",
    "strip_ai_artifacts", "markdown_to_text", "extract_json",
    "FactLedger", "build_fact_ledger", "check_grounded",
    "FieldSpec", "SchemaSpec", "GuardrailReport", "run_guarded_task",
]
