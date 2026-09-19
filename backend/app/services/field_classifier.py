"""
Field classification for browser-assisted applications.

Filling a form with a browser is easy; deciding what may be typed is the whole
job. This module is the decision layer, kept pure and deterministic so it is
unit-testable without a browser and so the same verdict is reached by the API,
the worker and the recording driver.

Every detected control gets one :class:`FieldVerdict`:

``classification``
    A ``FIELD_CLASSIFICATIONS`` value — what the field *is*.
``action``
    A ``FIELD_ACTIONS`` value — what the executor may do with it. Only
    ``autofill`` ever types anything. ``ask_user`` and ``handoff`` pause the
    session; ``skip`` leaves the field blank; ``never`` forbids it outright.

The rules, in the order they are applied (order is the contract):

0. **Controls we never touch** — hidden/submit/button/reset/image/search inputs and
   the hidden ``*-response`` machinery behind a bot check are ``skip``; they are
   page furniture, not questions for the user.
1. **CAPTCHA** (reCAPTCHA/hCaptcha/Turnstile/Arkose markers) → ``handoff``.
   We never solve, outsource or relay a bot check — the user completes it in
   the browser, and the page's own challenge stays with the page.
2. **MFA / one-time codes** → ``handoff``. The code is typed by the user in the
   browser; it never reaches this process, this database or a log.
3. **Passwords / passphrases** → ``handoff``. Same rule, even when the user has a
   vault entry for the domain: this workflow does not type credentials into a
   page.
4. **Postal codes** ("PIN code", "zip code") → ``ask_user``. A PIN is a secret; a
   PIN *code* is an address field, and guessing which one a form meant is exactly
   the kind of guess this module refuses to make.
5. **The checkpoint** — what already happened decides before what the field is:
   ``filled`` (we typed it) and ``user_completed`` (the user did it in the
   browser) are ``skip``, ``declined`` is ``never``, and ``answered`` (the user
   gave us this value in the app) is ``autofill`` from the confirmed answer.
   This is the resume/no-duplicate rule and it is why re-running a pass is safe.
6. **Restricted identifiers** (national id, passport, visa number, …) → ``never``:
   the automation does not fill them at all; the user types them in the browser
   themselves. The two restricted fields the user *can* answer in the app (date
   of birth, security clearance) are asked explicitly.
7. **Option mismatch** — our confirmed value is not one of the portal's own
   ``options`` → ``ask_user`` with ``reason="ambiguous_option"``. Never
   auto-resolved: picking the wrong work-authorization wording is a false
   statement, and a near-match is still a guess.
8. **Legal / attestation / eligibility** (consent, terms, background-check,
   drug-test, criminal-history, work-authorization, sponsorship, visa,
   clearance, signature) → ``ask_user``. These are statements about a person's
   legal situation; a wrong autofill is a false declaration.
9. **Voluntary self-identification (EEO)** → ``ask_user`` with a decline
   option. Never inferred.
10. **Ambiguity** — a label that matches more than one canonical key, or one of
    the deliberately ambiguity-prone hints ("Location", "Website") → ``ask_user``.
    A plausible guess is still a guess.
11. **Unknown** — nothing recognised at all → ``ask_user``. Unknown fields stop
    the automation; that is the feature, not a gap.
12. Everything else is ``autofill`` when a confirmed value exists, and
    ``ask_user`` (required) / ``skip`` (optional) when it does not.

Sensitivity comes from :data:`app.contracts.vocabulary.SENSITIVE_FIELD_KEYS` /
``RESTRICTED_FIELD_KEYS`` so the classifier and the API cannot disagree about
what "sensitive" means.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.contracts import (
    EEO_FIELD_KEYS,
    FIELD_ACTIONS,
    FIELD_CLASSIFICATIONS,
    RESTRICTED_FIELD_KEYS,
    SENSITIVE_FIELD_KEYS,
)
from app.services.form_detector import KNOWN_FIELDS, map_field_name

#: Input types we never touch at all (the form machinery's own controls).
IGNORED_INPUT_TYPES = {"hidden", "submit", "button", "image", "reset", "search"}

#: Markers that a page is (or will render) a bot check. Detection only — the
#: module has no solving path, by design and by contract.
CAPTCHA_HINTS: Tuple[str, ...] = (
    "g-recaptcha", "grecaptcha", "recaptcha", "hcaptcha", "h-captcha",
    "cf-turnstile", "turnstile", "arkose", "funcaptcha", "captcha",
    "verify you are human", "are you a robot", "bot check", "cloudflare challenge",
)

#: Markers for a second factor / one-time code.
MFA_HINTS: Tuple[str, ...] = (
    "one-time", "one time", "onetime", "otp", "totp", "mfa", "2fa", "two-factor",
    "two factor", "verification code", "verify code", "auth code",
    "authentication code", "security code", "sms code", "text code", "passcode",
    "authenticator", "6-digit code", "digit code",
)

#: Markers for a password entry control.
CREDENTIAL_HINTS: Tuple[str, ...] = ("password", "passphrase", "passcode", "pin")

#: Markers for an attestation, consent or eligibility statement.
LEGAL_HINTS: Tuple[str, ...] = (
    "terms", "conditions", "privacy policy", "i agree", "i consent", "consent",
    "acknowledge", "certify", "attest", "declaration", "i confirm",
    "background check", "background screening", "drug test", "drug screen",
    "criminal", "conviction", "felony", "non-compete", "noncompete",
    "arbitration", "right to work", "work authorization", "work authorisation",
    "authorized to work", "authorised to work", "legally authorized",
    "sponsorship", "require sponsorship", "visa", "immigration",
    "security clearance", "reference check", "e-sign", "electronic signature",
    "digital signature", "signature", "gdpr", "data processing",
    "equal opportunity", "affirmative action",
)

#: Markers for a voluntary self-identification (EEO) question.
VOLUNTARY_HINTS: Tuple[str, ...] = (
    "gender", "race", "ethnicity", "veteran", "disability", "self-identification",
    "self identification", "voluntary", "prefer not to say", "pronouns",
)

#: Restricted identifiers that automation must not fill *at all* — the value is
#: typed by the user in the browser.
NEVER_FILL_KEYS: Tuple[str, ...] = (
    "governmentId", "nationalId", "passportNumber", "visaNumber", "otpCode", "portalPassword",
)

#: Restricted fields the user may answer in-app (asked every time, never reused).
ASK_ONLY_RESTRICTED_KEYS: Tuple[str, ...] = ("dateOfBirth", "securityClearance", "referenceContact")

#: Canonical keys whose *label* words are too generic to trust when the label is
#: the only evidence: "Location" can be the current city or the target market.
AMBIGUITY_PRONE_HINTS: Dict[str, Tuple[str, ...]] = {
    # "Location" is a current city, a target market or a relocation question.
    "location": ("location", "based", "city"),
    # "Website" is a portfolio, a personal site or the company's own site.
    "github": ("website", "portfolio", "personal site", "link"),
    # "Additional information" is frequently *not* a cover letter.
    "coverLetter": ("additional information", "anything else", "message"),
}

#: Restricted questions that are not part of the shipped canonical key set but
#: must still never be guessed. (hint → canonical key, whether we may ask in-app)
RESTRICTED_HINTS: Dict[str, Tuple[str, bool]] = {
    "date of birth": ("dateOfBirth", True),
    "birth date": ("dateOfBirth", True),
    "dob": ("dateOfBirth", True),
    "security clearance": ("securityClearance", True),
    "reference contact": ("referenceContact", True),
    "referees": ("referenceContact", True),
    "national id": ("nationalId", False),
    "national insurance": ("nationalId", False),
    "social security": ("governmentId", False),
    "ssn": ("governmentId", False),
    "government id": ("governmentId", False),
    "passport": ("passportNumber", False),
    "visa number": ("visaNumber", False),
    "aadhaar": ("governmentId", False),
    "tax id": ("governmentId", False),
}

#: A field with no value in the profile but present in the form is still safe to
#: leave blank when it is optional — except these, which are always asked.
ALWAYS_ASK_KEYS: Tuple[str, ...] = (
    "currentCompensation", "salaryExpectation", "sponsorshipRequired",
    "relocationWilling", "workAuthorization", "noticePeriod", "startDate",
    "employmentType", "pronouns", "preferredName", "address",
)


@dataclass(frozen=True)
class FieldVerdict:
    """One field, classified. Safe to log: it holds no values."""

    name: str
    label: str
    field_type: str
    required: bool = False
    classification: str = "unknown"
    action: str = "ask_user"
    sensitivity: str = "internal"
    profile_key: Optional[str] = None
    reason: str = ""
    #: What the user is being asked, phrased for a queue item (no values).
    question: str = ""
    #: True when this field stops the automation (a pause) — required or not.
    blocks: bool = True
    #: Which ``USER_ACTION_KINDS`` a pause on this field raises.
    pause_kind: str = "unknown_field"

    def __post_init__(self) -> None:
        if self.classification not in FIELD_CLASSIFICATIONS:  # pragma: no cover - guarded
            raise ValueError(f"unknown field classification: {self.classification}")
        if self.action not in FIELD_ACTIONS:  # pragma: no cover - guarded
            raise ValueError(f"unknown field action: {self.action}")

    @property
    def pauses(self) -> bool:
        """True when the executor must hand this field to a human and stop."""
        return self.action in ("ask_user", "handoff")

    @property
    def autofillable(self) -> bool:
        return self.action == "autofill"

    def as_dict(self) -> Dict[str, Any]:
        """The API/checkpoint shape — field metadata only, never a value."""
        return {
            "name": self.name,
            "label": self.label,
            "type": self.field_type,
            "required": self.required,
            "classification": self.classification,
            "action": self.action,
            "sensitivity": self.sensitivity,
            "profile_key": self.profile_key,
            "reason": self.reason,
            "question": self.question,
            "blocks": self.blocks,
            "pause_kind": self.pause_kind,
        }


@dataclass
class FormVerdicts:
    """Every field of one form, plus the pause the executor must raise."""

    verdicts: List[FieldVerdict] = dataclass_field(default_factory=list)

    def _of(self, *actions: str) -> List[FieldVerdict]:
        return [v for v in self.verdicts if v.action in actions]

    @property
    def autofillable(self) -> List[FieldVerdict]:
        return self._of("autofill")

    @property
    def asking(self) -> List[FieldVerdict]:
        return self._of("ask_user")

    @property
    def handoffs(self) -> List[FieldVerdict]:
        return self._of("handoff")

    @property
    def skipping(self) -> List[FieldVerdict]:
        return self._of("skip", "never")

    @property
    def blocking(self) -> List[FieldVerdict]:
        """Fields that stop the automation (the pause the caller must raise)."""
        return [v for v in self.verdicts if v.pauses and v.blocks]

    @property
    def must_pause(self) -> bool:
        return bool(self.blocking)

    @property
    def pause_kind(self) -> str:
        """
        The single most important reason to stop.

        Handoff kinds win over field-answer kinds: a CAPTCHA in the middle of
        the form is not something an in-app answer can resolve, and reporting
        "unknown field" for it would send the user to the wrong surface.
        """
        for kind in ("captcha", "mfa", "login"):
            if any(v.pause_kind == kind for v in self.blocking):
                return kind
        for kind in ("legal_question", "sensitive_field", "ambiguous_field", "unknown_field"):
            if any(v.pause_kind == kind for v in self.blocking):
                return kind
        return ""

    @property
    def pause_reason(self) -> str:
        kinds = [v.pause_kind for v in self.blocking]
        return kinds[0] if kinds else ""

    def summary(self) -> Dict[str, Any]:
        """Safe progress counters for ``application_sessions.progress``."""
        return {
            "fields_total": len(self.verdicts),
            "autofillable": len(self.autofillable),
            "asking": len(self.asking),
            "handoffs": len(self.handoffs),
            "skipped": len(self.skipping),
            "blocking": len(self.blocking),
            "must_pause": self.must_pause,
            "pause_kind": self.pause_kind,
            "requires_browser_handoff": self.pause_kind in ("login", "mfa", "captcha"),
        }


def _hay(field: Mapping[str, Any]) -> str:
    """Normalised haystack for hint matching: label + name + placeholder/aria."""
    parts = [
        str(field.get("label") or ""),
        str(field.get("name") or ""),
        str(field.get("id") or ""),
        str(field.get("placeholder") or ""),
        str(field.get("aria_label") or ""),
    ]
    hay = " ".join(parts).lower()
    return re.sub(r"[_\-\[\]\.]+", " ", hay)


def _extra_hay(field: Mapping[str, Any]) -> str:
    """Markers that live outside the label: CSS classes, iframe srcs, attributes."""
    parts = [
        " ".join(str(v) for v in (field.get("classes") or [])),
        str(field.get("src") or ""),
        str(field.get("data_sitekey") or ""),
        str(field.get("role") or ""),
    ]
    return " ".join(parts).lower()


def _has(hay: str, hints: Iterable[str]) -> Optional[str]:
    """
    First hint that appears in *hay* as a whole word.

    Word boundaries, not ``in``: ``pin`` is a credential hint and "ship*ping*",
    "map*ping*" and "zip*ping*" are not pins — a substring match would classify
    a shipping address as a password field and pause for the wrong reason.
    """
    for hint in hints:
        pattern = r"(?<![a-z0-9])" + re.escape(hint) + r"(?![a-z0-9])"
        if re.search(pattern, hay):
            return hint
    return None


def is_captcha_marker(value: str) -> bool:
    """True when a string (url, class, selector) looks like a bot check."""
    return bool(_has(str(value or "").lower(), CAPTCHA_HINTS))


def candidate_matches(field: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """
    ``(canonical_key, matched_hint)`` for every key this field's label could mean.

    More than one candidate is *ambiguity*, not a coin flip — ``map_field_name``
    returns the first match, which is why the mapping itself is not trusted to
    decide. The matched hint is kept because it says *how* the label matched: a
    generic word ("Location", "Website") is weaker evidence than a specific one
    ("Current city", "GitHub profile").
    """
    label = str(field.get("label") or "")
    name = str(field.get("name") or "")
    field_type = str(field.get("type") or "text").lower()
    hay = f"{label} {name}".lower().replace("_", " ").replace("-", " ")
    matches: List[Tuple[str, str]] = []
    declared = field.get("profile_key")
    if isinstance(declared, str) and declared in KNOWN_FIELDS:
        matches.append((declared, "declared"))
    for canonical, hints in KNOWN_FIELDS.items():
        if any(canonical == existing for existing, _ in matches):
            continue
        hit = _has(hay, hints)
        if hit:
            matches.append((canonical, hit))
    if not matches:
        inferred = map_field_name(label, name, field_type)
        if inferred:
            matches.append((inferred, "inferred"))
    return _drop_generic_matches(matches)


def _drop_generic_matches(matches: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """
    Remove a candidate whose matched hint is a generic word inside another's.

    "First name" matches ``firstName`` ("first name") *and* ``fullName``
    ("name") — the second is the same evidence read more loosely, not a genuine
    second meaning. Without this, every first-name field on every form would be
    reported as ambiguous and pause the run.
    """
    if len(matches) < 2:
        return matches
    kept: List[Tuple[str, str]] = []
    for key, hint in matches:
        swallowed = any(
            other_hint != hint
            and hint != "declared"
            and len(hint.split()) < len(other_hint.split())
            and re.search(r"(?<![a-z0-9])" + re.escape(hint) + r"(?![a-z0-9])", other_hint)
            for _, other_hint in matches
        )
        if not swallowed:
            kept.append((key, hint))
    return kept or matches


def candidate_keys(field: Mapping[str, Any]) -> List[str]:
    """The canonical keys this field could mean, best first."""
    return [key for key, _ in candidate_matches(field)]


def _sensitivity_for(key: Optional[str]) -> str:
    if key and key in RESTRICTED_FIELD_KEYS:
        return "restricted"
    if key and key in SENSITIVE_FIELD_KEYS:
        return "sensitive"
    if key:
        return "internal"
    return "internal"


def _has_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def classify_field(
    field: Mapping[str, Any],
    *,
    value: Any = None,
    checkpoint_status: str = "",
) -> FieldVerdict:
    """
    Classify one detected field.

    ``value`` is the value we *could* fill (already resolved from the profile or
    an answer) — it never enters the verdict, only whether one exists.
    ``checkpoint_status`` is the session checkpoint's own record for the field:
    ``filled``/``user_completed``/``declined`` are skipped (never typed again),
    and ``answered`` (the user gave us the value in the app) is typed but never
    asked again.
    """
    name = str(field.get("name") or field.get("id") or "").strip()
    label = str(field.get("label") or name).strip()
    field_type = str(field.get("type") or "text").lower()
    required = bool(field.get("required")) or str(field.get("aria_required")).lower() == "true"
    options = [str(o) for o in (field.get("options") or [])][:24]

    hay = _hay(field)
    extra = _extra_hay(field)
    combined = f"{hay} {extra}"

    def verdict(classification: str, action: str, *, key: Optional[str] = None,
                reason: str = "", question: str = "", sensitivity: Optional[str] = None,
                blocks: bool = True, pause_kind: str = "unknown_field") -> FieldVerdict:
        return FieldVerdict(
            name=name, label=label, field_type=field_type, required=required,
            classification=classification, action=action,
            sensitivity=sensitivity or _sensitivity_for(key),
            profile_key=key, reason=reason, question=question or label,
            blocks=blocks, pause_kind=pause_kind,
        )

    # 0. Controls we never touch.
    if field_type in IGNORED_INPUT_TYPES or field_type == "submit":
        return verdict("ignored", "skip", reason=f"control_type_{field_type}", blocks=False)
    if _has(hay, ("g-recaptcha", "recaptcha", "captcha_response")):
        # A hidden captcha-response input is page machinery, not a field to fill.
        return verdict("captcha", "handoff", reason="captcha_response_field")

    # 1. Bot checks: never solved, never relayed.
    if is_captcha_marker(combined) or bool(field.get("captcha")) or field_type == "captcha":
        return verdict("captcha", "handoff", reason="captcha_detected",
                       question="Complete the bot check in the browser",
                       sensitivity="internal", pause_kind="captcha")

    # 2. One-time codes: typed by the user, in the browser.
    if _has(hay, MFA_HINTS) or str(field.get("autocomplete") or "") == "one-time-code":
        return verdict("mfa", "handoff", reason="mfa_code_required",
                       question="Enter the verification code yourself in the browser",
                       pause_kind="mfa")
    if "code" in hay and re.search(r"\b(otp|code)\b", hay) and field_type in ("tel", "number", "text"):
        if any(word in hay for word in ("verif", "security", "auth", "one", "otp", "sms", "email")):
            return verdict("mfa", "handoff", reason="mfa_code_required",
                           question="Enter the verification code yourself in the browser",
                           pause_kind="mfa")

    # 3. Credentials: this workflow never types a password, even a stored one.
    if field_type == "password" or str(field.get("autocomplete") or "") in ("current-password", "new-password") \
            or _has(hay, CREDENTIAL_HINTS):
        return verdict("credential", "handoff", reason="credential_entry_is_user_typed",
                       question="Sign in yourself in the browser",
                       pause_kind="login")

    # 2b. Postal codes are address data, not PINs: "PIN code" must not be read
    # as a passcode (it is the Indian/UK spelling of a ZIP code).
    if _has(hay, ("pin code", "postal code", "zip code", "zipcode", "postcode")):
        return verdict("unknown", "ask_user", reason="postal_code_field",
                       question=f"{label} — we do not have this on file")

    matches = candidate_matches(field)
    candidates = [key for key, _ in matches]
    key = candidates[0] if candidates else None
    matched_hint = matches[0][1] if matches else ""

    # 4. The checkpoint decides what has already happened — not the plan, and not
    # a fresh look at the page. This is the resume/no-duplicate rule.
    if checkpoint_status in ("filled", "user_completed"):
        return verdict("canonical" if key else "unknown", "skip", key=key,
                       reason=f"already_{checkpoint_status}", blocks=False)
    if checkpoint_status == "declined":
        return verdict("canonical" if key else "unknown", "never", key=key,
                       sensitivity="restricted", reason="declined_by_user", blocks=False,
                       pause_kind="sensitive_field")
    if checkpoint_status == "answered" and _has_value(value):
        # The user supplied this value in the app; it still has to be typed, and
        # it must never be asked for again.
        return verdict("sensitive" if _sensitivity_for(key) == "sensitive" else "canonical",
                       "autofill", key=key, reason="user_answer", blocks=False)

    legal_hint = _has(hay, LEGAL_HINTS)
    voluntary_hint = _has(hay, VOLUNTARY_HINTS)

    # 5. Restricted questions recognised by their own wording (they are not in
    # the shipped canonical key set, but the answer is just as sensitive).
    for hint, (restricted_key, askable) in RESTRICTED_HINTS.items():
        if _has(hay, (hint,)):
            if askable:
                return verdict("sensitive", "ask_user", key=restricted_key, sensitivity="restricted",
                               reason="restricted_field_asked_every_time", question=label,
                               pause_kind="sensitive_field")
            return verdict("sensitive", "never", key=restricted_key, sensitivity="restricted",
                           reason="restricted_identifier", question=label,
                           pause_kind="sensitive_field")
    if key in NEVER_FILL_KEYS or _has(hay, ("passport", "national id", "social security", "ssn",
                                            "government id", "tax id")):
        return verdict("sensitive", "never", key=key, sensitivity="restricted",
                       reason="restricted_field_never_autofilled",
                       question=f"{label} — enter this yourself in the browser",
                       pause_kind="sensitive_field")
    if key in ASK_ONLY_RESTRICTED_KEYS:
        return verdict("sensitive", "ask_user", key=key, sensitivity="restricted",
                       reason="restricted_field_asked_every_time", question=label,
                       pause_kind="sensitive_field")

    # 6. Our value is not one of the portal's own options. The contract is
    # explicit: never auto-resolve this. Choosing the wrong wording on a work
    # authorization (or any legal) question is a false statement, and a
    # near-match is still a guess.
    if options and _has_value(value) and str(value) not in options:
        restrictive = bool(legal_hint) or key in ("workAuthorization", "sponsorshipRequired",
                                                 "relocationWilling") \
            or key in EEO_FIELD_KEYS or _sensitivity_for(key) == "restricted"
        return verdict("ambiguous", "ask_user", key=key,
                       sensitivity="restricted" if restrictive else None,
                       reason="ambiguous_option",
                       question=f"{label} — pick one of the portal's own options",
                       pause_kind="legal_question" if legal_hint or key in (
                           "workAuthorization", "sponsorshipRequired") else "sensitive_field"
                       if restrictive else "ambiguous_field")

    # 7. Legal / attestation / eligibility statements: the user answers.
    if legal_hint:
        return verdict("legal", "ask_user", key=key, reason=f"legal_or_eligibility_question:{legal_hint}",
                       question=label, pause_kind="legal_question")

    # 8. Voluntary self-identification: asked, with a decline, never inferred.
    if voluntary_hint:
        eeo_key = next((k for k in EEO_FIELD_KEYS if k.lower() in hay), None)
        return verdict("voluntary", "ask_user", key=eeo_key or key, sensitivity="restricted",
                       reason="voluntary_self_identification", question=label,
                       pause_kind="sensitive_field")

    # 9. Ambiguity: several plausible keys, or a label that only matched a
    # generic word ("Location" is a current city, a target market or a
    # relocation question — the readings differ, so we ask).
    distinct = [k for k in candidates if k not in ("", None)]
    if len(distinct) > 1:
        return verdict("ambiguous", "ask_user", key=key, reason="ambiguous_mapping",
                       question=f"{label} — which of these does it mean?",
                       pause_kind="ambiguous_field")
    if key and matched_hint in AMBIGUITY_PRONE_HINTS.get(key, ()):
        return verdict("ambiguous", "ask_user", key=key, reason="ambiguous_label",
                       question=label, pause_kind="ambiguous_field")

    # 10. Unknown: stop and ask. Never guess a field's meaning.
    if not key:
        return verdict("unknown", "ask_user", reason="unclassified_field",
                       question=f"{label} — what should we put here?",
                       pause_kind="unknown_field")

    # 11. Known key: autofill from a confirmed value, otherwise ask (required)
    # or leave it blank (optional).
    sensitivity = _sensitivity_for(key)
    if key in ALWAYS_ASK_KEYS:
        return verdict("sensitive" if sensitivity == "sensitive" else "canonical", "ask_user",
                       key=key, reason="answer_required_every_time", question=label,
                       pause_kind="sensitive_field" if sensitivity == "sensitive" else "unknown_field")
    if field_type == "file":
        if key == "coverLetter":
            return verdict("file", "autofill", key=key, reason="attach_generated_cover_letter")
        return verdict("file", "autofill", key=key, reason="attach_resume_file")
    if _has_value(value):
        classification = "sensitive" if sensitivity == "sensitive" else "canonical"
        return verdict(classification, "autofill", key=key, reason="confirmed_profile_value",
                       blocks=False)
    if required:
        return verdict("canonical", "ask_user", key=key, reason="missing_required_value",
                       question=label,
                       pause_kind="sensitive_field" if sensitivity == "sensitive" else "unknown_field")
    return verdict("canonical", "skip", key=key, reason="optional_and_unset", blocks=False)




def classify_form(
    fields: Sequence[Mapping[str, Any]],
    *,
    values: Optional[Mapping[str, Any]] = None,
    checkpoint: Optional[Mapping[str, Any]] = None,
) -> FormVerdicts:
    """
    Classify a whole form.

    ``values`` maps a field name (or canonical key) to the value we could fill;
    ``checkpoint`` is ``application_sessions.checkpoint["fields"]`` (statuses
    only — the checkpoint never stores a typed value).
    """
    values = values or {}
    fields_checkpoint: Mapping[str, Any] = (checkpoint or {}).get("fields") or {}
    verdicts: List[FieldVerdict] = []
    for field in fields:
        name = str(field.get("name") or "")
        key = field.get("profile_key")
        value = values.get(name)
        if value is None and isinstance(key, str):
            value = values.get(key)
        status = ""
        entry = fields_checkpoint.get(name) if isinstance(fields_checkpoint, Mapping) else None
        if isinstance(entry, Mapping):
            status = str(entry.get("status") or "")
        elif isinstance(entry, str):
            status = entry
        verdicts.append(classify_field(field, value=value, checkpoint_status=status))
    return FormVerdicts(verdicts=verdicts)


def pause_kind_for_classification(classification: str) -> str:
    """The ``USER_ACTION_KINDS`` a classification raises when it pauses."""
    return {
        "captcha": "captcha",
        "mfa": "mfa",
        "credential": "login",
        "legal": "legal_question",
        "voluntary": "sensitive_field",
        "sensitive": "sensitive_field",
        "ambiguous": "ambiguous_field",
        "unknown": "unknown_field",
    }.get(classification, "unknown_field")


__all__ = [
    "CAPTCHA_HINTS",
    "CREDENTIAL_HINTS",
    "FieldVerdict",
    "FormVerdicts",
    "LEGAL_HINTS",
    "MFA_HINTS",
    "NEVER_FILL_KEYS",
    "VOLUNTARY_HINTS",
    "candidate_keys",
    "candidate_matches",
    "classify_field",
    "classify_form",
    "is_captcha_marker",
    "pause_kind_for_classification",
]
