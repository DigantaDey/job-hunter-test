"""
One canonical redaction boundary for every outbound text channel.

Three places in the codebase mask secrets before they leave the process:
``services/onboarding.safe_error_message`` (error strings persisted to the
database), ``core/audit.sanitize_audit_detail`` (the append-only audit trail)
and ``services/ai_client._scrub_prompt_secrets`` (prompts sent to a provider).
They each kept their own copy of the same four patterns, which is exactly how a
new secret shape (a provider's new key prefix, say) ends up masked on two of the
three paths and leaked on the third.

This module is the single list. The three call sites keep their own *policy*
(mask the whole value, mask inline, refuse the call) but share these patterns,
and the logging formatters — the one channel that had no masking at all beyond
control characters — use :func:`redact_text` on every rendered line.

What is redacted
----------------
* **Secrets**: provider keys (``sk-…``, ``ghp_…``, ``xoxb-…``, Stripe),
  ``Bearer`` / ``Basic`` credentials, ``key=value`` assignments for the known
  secret names, and JWTs.
* **Personal data**: email addresses, formatted telephone numbers, US SSNs and
  payment-card numbers. A log line naming a candidate is a data-protection
  incident as much as one naming their password, and the log aggregator is
  usually the least protected system in the deployment.

What is deliberately *not* redacted
-----------------------------------
User ids, job ids and request ids: they are the correlation keys an operator
needs and they identify nothing on their own. Redacting them would make the
audit trail useless without making it private. Contiguous digit runs that are
not payment cards (a unix timestamp, a long id) and IPv4 addresses are left
alone for the same reason — see the guards in :func:`_looks_like_a_phone`.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Mapping, Tuple

_MASK = "***"

# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #
#: ``(pattern, replacement)`` applied in order. Each replacement keeps enough of
#: the shape to recognise *what* leaked (``sk-***``, ``Bearer ***``) without
#: keeping the secret itself.
SECRET_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = (
    # Provider key shapes. ``sk-`` covers OpenAI-compatible keys; the others are
    # the shapes this stack can realistically see (GitHub, Slack, Stripe).
    (re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_\-]{6,}"), "sk-***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{10,}"), "gh***"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{6,}"), "xox***"),
    (re.compile(r"\b(?:pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}"), "stripe-***"),
    # HTTP credentials in any header-ish position.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{6,}"), r"\1 ***"),
    # ``api_key=…``, ``password: …``, ``Authorization=…`` and friends. The value
    # runs to whitespace, a comma/semicolon or a closing quote so a URL query
    # string is masked too.
    (re.compile(
        r"(?i)\b(api[_\-]?key|apikey|access[_\-]?token|auth[_\-]?token|refresh[_\-]?token|"
        r"token|secret|client[_\-]?secret|password|passwd|pwd|authorization|credential|"
        r"mfa[_\-]?code|otp|session[_\-]?cookie)\b(\s*[:=]\s*)"
        r"(\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)"),
     r"\1\2" + _MASK),
    # JWT-shaped tokens.
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}(?:\.[A-Za-z0-9_\-]{5,})?"), "jwt-***"),
)

# --------------------------------------------------------------------------- #
# Personal data
# --------------------------------------------------------------------------- #
#: Email addresses keep the first character of the local part and the whole
#: domain, so ``support@acme.example`` stays distinguishable from
#: ``sam@acme.example`` in a log without either being readable.
_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]*@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")

#: Phone numbers, formatted. A *contiguous* digit run is deliberately not
#: matched: a bare 10-digit number in a log is far more likely to be a unix
#: timestamp or an id than a phone number, and masking it would cost an operator
#: the one correlation key they were reading for. A formatted number
#: (``+44 20 7946 0958``, ``(415) 555-0132``, ``555-0132-99``) has no other
#: plausible reading, and the shape guards below drop dates and IPv4 literals.
_PHONE_RE = re.compile(
    r"(?<![\w.])(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?)?"
    r"\d{2,4}[\s.\-]\d{2,4}(?:[\s.\-]\d{2,4}){0,2}(?![\w.])"
)

#: US Social Security numbers (``123-45-6789``) — always fully masked.
_SSN_RE = re.compile(r"(?<![\w\-])\d{3}-\d{2}-\d{4}(?![\w\-])")

#: Payment-card *candidates*: 13–19 digits, separators allowed. Whether one
#: really is a card is decided by :func:`_looks_like_card` (Luhn), because a
#: millisecond timestamp is 13 digits and a long id can be 16.
_CARD_RE = re.compile(r"(?<![\w\-])\d(?:[ \-]?\d){12,18}(?![\w\-])")

_IPV4_SHAPE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_DATE_SHAPE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")

#: Key names whose values are secrets regardless of what they look like. Shared
#: with ``core.audit`` so a new sensitive field is masked on both paths.
SENSITIVE_KEYS: frozenset = frozenset({
    "password", "passwd", "pwd", "secret", "client_secret", "api_key", "apikey",
    "access_token", "refresh_token", "auth_token", "token", "authorization",
    "credential", "credentials", "private_key", "encryption_key", "secret_key",
    "mfa_code", "otp", "ssn", "social_security", "credit_card", "card_number",
    "cvv", "cvc", "routing_number", "account_number", "storage_state", "cookie",
})

#: Names that contain one of the above as a substring but are routinely logged
#: safely — ``error_code``, ``postal_code``, ``token_hash``, ``expires_at``.
#: :data:`SENSITIVE_KEYS` is therefore matched *exactly*, never by substring: a
#: redactor that masks every key containing ``code`` destroys the diagnostics
#: an operator is reading the log for.
def is_sensitive_key(key: Any) -> bool:
    """Is *key* a name whose value must never be logged, whatever it contains?"""
    return str(key or "").lower().replace("-", "_") in SENSITIVE_KEYS


def _luhn_ok(digits: str) -> bool:
    """The Luhn checksum — every real payment card passes it, ids rarely do."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _looks_like_card(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if not 13 <= len(digits) <= 19 or not _luhn_ok(digits):
        return False
    # A 13-digit number starting with 1 is a millisecond epoch for the next
    # decade, not a Visa. Luhn passes one in ten of those by chance, and a
    # redacted timestamp in the middle of an incident is a real cost.
    if len(digits) == 13 and digits.startswith("1"):
        return False
    return True


def _looks_like_a_phone(value: str) -> bool:
    """
    Guard the deliberately loose phone pattern.

    ``192.168.0.1`` and ``2026-09-20`` both satisfy the shape; neither is a
    phone number, and masking them would cost an operator the client IP or the
    date they were reading for. Digit count plus the two shape guards below keep
    the pattern useful instead of merely aggressive.
    """
    text = value.strip()
    digits = re.sub(r"\D", "", text)
    if not 7 <= len(digits) <= 15:
        return False
    if _IPV4_SHAPE.match(text):
        return False
    match = _DATE_SHAPE.match(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        if 1900 <= year <= 2200 and 1 <= month <= 12 and 1 <= day <= 31:
            return False
    return True


def _redact_email(match: "re.Match[str]") -> str:
    return f"{match.group(1)}{_MASK}@{match.group(2)}"


def redact_text(value: Any, *, redact_pii: bool = True) -> Any:
    """
    Return *value* with secrets (and, by default, personal data) masked.

    Idempotent and non-raising: a redactor that throws would take the log line —
    or the error message, or the audit row — with it, which is a worse outcome
    than the leak it was preventing. Non-strings pass through untouched.
    """
    if not isinstance(value, str) or not value:
        return value
    text = value
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    if not redact_pii:
        return text
    text = _SSN_RE.sub("ssn-" + _MASK, text)
    text = _EMAIL_RE.sub(_redact_email, text)
    text = _mask_matching(text, _CARD_RE, _looks_like_card, "card-" + _MASK)
    return _mask_matching(text, _PHONE_RE, _looks_like_a_phone, "phone-" + _MASK)


def _mask_matching(text: str, pattern: "re.Pattern[str]", keep, replacement: str) -> str:
    """
    Mask every match of *pattern* that *keep* accepts, one substitution at a time.

    A single ``re.sub`` cannot be used for these two patterns: each candidate
    needs a predicate (Luhn, the date/IPv4 guards), and every substitution
    shifts the offsets of everything after it.
    """
    out = text
    for _ in range(8):  # a log line with more than eight of these is a data dump
        match = next((m for m in pattern.finditer(out) if keep(m.group(0))), None)
        if match is None:
            break
        out = out[:match.start()] + replacement + out[match.end():]
    return out


def redact_value(value: Any, *, redact_pii: bool = True) -> Any:
    """
    Recursively redact strings inside a log field (dict / list / scalar).

    Non-string leaves are returned untouched: a ``user_id`` is an ``int`` and
    stringifying it would make the JSON record less useful, not safer.
    """
    if isinstance(value, str):
        return redact_text(value, redact_pii=redact_pii)
    if isinstance(value, Mapping):
        return {
            key: (_MASK if is_sensitive_key(key) else redact_value(item, redact_pii=redact_pii))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item, redact_pii=redact_pii) for item in value]
    return value


def redact_fields(fields: Dict[str, Any], *, redact_pii: bool = True) -> Dict[str, Any]:
    """Redact the ``extra=`` attributes of a log record, key names included."""
    out: Dict[str, Any] = {}
    for key, value in (fields or {}).items():
        out[key] = _MASK if is_sensitive_key(key) else redact_value(value, redact_pii=redact_pii)
    return out


def secret_shapes() -> Iterable[str]:
    """The pattern sources, for documentation and drift tests."""
    return [pattern.pattern for pattern, _ in SECRET_PATTERNS]
