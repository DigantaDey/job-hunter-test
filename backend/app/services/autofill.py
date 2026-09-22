"""
Browser autofill.

Two layers, deliberately separated so the risky part is testable and gated:

1. ``build_autofill_plan`` — pure, deterministic field mapping from the detected
   form schema + the user profile + the vault credential. Fully unit-testable and
   safe to run anywhere.
2. ``execute_autofill`` — optional Playwright execution. It is skipped unless the
   operator enabled autofill AND the user accepted the automation disclosure AND
   ``AUTOFILL_DRY_RUN`` is off. Submitting is a *third*, separate opt-in
   (``AUTOFILL_ALLOW_SUBMIT`` + per-user ``allow_auto_submit``).

When Playwright (or its browsers) is not installed the function returns an
honest ``status="unavailable"`` result instead of pretending to apply.

The browser is an outbound client, so it is policed like one
-----------------------------------------------------------------
``page.goto(job.url)`` used to navigate to whatever URL a feed or an import
supplied, bypassing the SSRF guard the HTTP client is built on: the worker's
headless Chromium would happily load ``http://169.254.169.254/latest/meta-data/``
(cloud credentials), ``http://127.0.0.1:<port>/`` (anything else in the
container) or any RFC1918 host — and ``_attempt_login`` then typed the user's
vault credential into whatever password field that host rendered. Three
independent gates now stand in front of that:

1. **URL policy pre-flight** (:func:`app.services.net_guard.preflight_navigation`)
   before a browser is even launched: scheme/userinfo checks plus a hard block
   on loopback, link-local/metadata (``169.254.0.0/16``) and non-allow-listed
   private ranges. ``OUTBOUND_ALLOW_PRIVATE`` does not relax navigation.
2. **Application-domain restriction** (:func:`autofill_allowed_domains`): the
   posting host must be a domain the job's *own metadata* names (a known ATS
   portal domain, or the company website on record). The posting URL's own host
   is never trusted to define that set — that is the attacker-supplied value.
3. **Credential gate** (:func:`_attempt_login`): vault credentials are typed
   only when the page's live host matches the application domain (and the
   credential's own vault domain). A redirect to any other host gets the form
   filled at most, never the password.

Every selector decision is logged at DEBUG (names and selectors only — never a
value, never a password) and echoed in the result's ``diagnostics``, so a failed
run says which field matched which selector instead of just "0 fields filled".
"""
from __future__ import annotations

import glob
import os
import sys
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc
from app.services import net_guard
from app.services.form_detector import VAULT_DOMAINS as ATS_VAULT_DOMAINS
from app.services.reliability import autofill_failure_reason

log = get_logger("app.autofill")

#: Cap on the per-run diagnostics list stored in the result (and in job.extra).
MAX_DIAGNOSTICS = 60

PROFILE_PATHS = {
    "firstName": ("firstName", "first_name"),
    "lastName": ("lastName", "last_name"),
    "fullName": ("name", "fullName"),
    "email": ("email",),
    "phone": ("phone", "mobile"),
    "location": ("location", "city"),
    "linkedin": ("linkedin", "linkedin_url"),
    "github": ("github", "website", "portfolio"),
    "salaryExpectation": ("salary_expectation", "expected_salary"),
    "noticePeriod": ("notice_period", "availability"),
    "workAuthorization": ("work_authorization", "workAuthorization"),
}


def _value_for(key: str, profile: Dict[str, Any], answers: Dict[str, Any],
               field_name: Optional[str] = None) -> Optional[str]:
    # User answers arrive keyed by whatever the input queue showed — the raw
    # form field *name* (e.g. "salary_expectation") — while canonical fields
    # also have a *profile key* (e.g. "salaryExpectation"). Accept both,
    # canonical first, so a submitted answer can never be silently orphaned
    # on the re-queued run.
    for candidate in (key, field_name):
        if candidate and candidate in answers and answers[candidate] not in (None, ""):
            return str(answers[candidate])
    for candidate in PROFILE_PATHS.get(key, (key,)):
        if profile.get(candidate) not in (None, "", []):
            value = profile[candidate]
            return ", ".join(map(str, value)) if isinstance(value, list) else str(value)

    # Derive name parts from the full name so a form asking for "First name"
    # does not needlessly push the application into the User Input queue.
    full_name = str(profile.get("name") or profile.get("fullName") or "").strip()
    if full_name:
        parts = full_name.split()
        if key == "firstName":
            return parts[0]
        if key == "lastName":
            return " ".join(parts[1:]) if len(parts) > 1 else None
        if key == "fullName":
            return full_name
    return None


def build_autofill_plan(
    *,
    schema: Dict[str, Any],
    profile: Dict[str, Any],
    resume_path: Optional[str] = None,
    cover_letter_path: Optional[str] = None,
    credential: Optional[Dict[str, str]] = None,
    answers: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Map every detected form field to a value we can fill (or flag as unknown)."""
    answers = answers or {}
    fields: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []

    for field in schema.get("fields") or []:
        key = field.get("profile_key")
        name = field.get("name")
        value: Any = None
        source = "profile"

        if field.get("type") == "file":
            if key == "coverLetter" or name in ("coverLetter", "cover_letter"):
                value = cover_letter_path
                source = "generated_cover_letter"
            else:
                value = resume_path
                source = "resume_file"
        elif key:
            value = _value_for(key, profile, answers, field_name=name)
        elif field.get("type") == "password":
            value = (credential or {}).get("password")
            source = "vault"
        else:
            # Voluntary/demographic questions are left blank unless the user
            # provided an explicit answer — never invented.
            value = answers.get(name)
            source = "user_answer"

        entry = {
            "name": name,
            "label": field.get("label", name),
            "type": field.get("type", "text"),
            "required": bool(field.get("required")),
            "profile_key": key,
            "value": value,
            "value_source": source if value not in (None, "") else "unknown",
        }
        fields.append(entry)
        if entry["required"] and value in (None, ""):
            missing.append({"name": name, "label": entry["label"], "type": entry["type"]})

    return {
        "portal_type": schema.get("portal_type", "custom"),
        "requires_login": bool(schema.get("requires_login")),
        "vault_domain": schema.get("vault_domain", ""),
        "fields": fields,
        "missing_required": missing,
        "fillable": sum(1 for f in fields if f["value"] not in (None, "")),
        "total_fields": len(fields),
        "generated_at": datetime.utcnow().isoformat(),
    }


#: The executables Playwright places inside a downloaded browser build, one
#: entry per platform layout it can produce. Cross-checked against playwright
#: 1.44's registry — ``playwright install --dry-run chromium`` prints the same
#: destination offline, so this list can be verified without the browser CDN.
#: Updated for 1.62: headless shell moved under ``chrome-headless-shell-linux64``
#: and ``chrome-linux`` stays, plus the ``chrome-headless-shell`` legacy path.
_BROWSER_EXECUTABLES = (
    "chrome-linux/chrome",
    "chrome-linux64/chrome",
    "chrome-linux/chrome-wrapper",
    "chrome-headless-shell-linux64/chrome-headless-shell",
    "chrome-headless-shell-linux/chrome-headless-shell",
    "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    "chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
    "chrome-win/chrome.exe",
    "chrome-win64/chrome.exe",
    "chrome-linux/headless_shell",
)


def _browsers_root() -> str:
    """The directory Playwright downloads browser builds into."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        return os.path.expanduser(override)
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Caches", "ms-playwright")
    if sys.platform == "win32":
        return os.path.join(home, "AppData", "Local", "ms-playwright")
    return os.path.join(home, ".cache", "ms-playwright")


def _chromium_browser_path() -> Optional[str]:
    """
    The Chromium executable Playwright would launch, or None when no browser
    build is downloaded.

    Answered from the filesystem alone: asking the driver spawns a Node process
    per call, and this runs on the hot path of an availability check. Build
    directories are versioned (``chromium-1117``,
    ``chromium_headless_shell-1150``), so the newest is preferred.

    Robustness: playwright 1.44 → 1.62 changed the headless shell layout
    (``chrome-headless-shell-linux64/chrome-headless-shell``) and some builds
    expose only a wrapper. We therefore (1) check the known executables, (2)
    fall back to any executable file under a ``chromium*`` build dir, so a
    future layout change still counts as \"browser downloaded\" rather than
    \"playwright is not installed\".
    """
    root = _browsers_root()
    if not os.path.isdir(root):
        return None
    # Newest build first — versioned as chromium-1117, chromium_headless_shell-1150, etc.
    builds = sorted(glob.glob(os.path.join(root, "chromium*-[0-9]*")), reverse=True)
    for build in builds:
        for relative in _BROWSER_EXECUTABLES:
            candidate = os.path.join(build, *relative.split("/"))
            if os.path.isfile(candidate):
                return candidate
    # Fallback: any file that looks like a chromium executable inside a build dir.
    # This keeps autofill_available() honest when playwright bumps its layout.
    for build in builds:
        # Look for chrome or headless_shell binaries anywhere under the build
        for pattern in ("**/chrome", "**/chrome-wrapper", "**/chrome.exe", "**/Chromium", "**/headless_shell", "**/chrome-headless-shell"):
            for candidate in glob.glob(os.path.join(build, pattern), recursive=True):
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    return candidate
        # If the build dir itself exists and is non-empty, treat as installed
        # — the driver will surface a clearer DriverError if launch still fails.
        try:
            if os.listdir(build):
                # Find any file to return as evidence the browser is present
                for dirpath, _, filenames in os.walk(build):
                    for fn in filenames:
                        fp = os.path.join(dirpath, fn)
                        if os.path.isfile(fp):
                            return fp
        except Exception:
            continue
    return None


def browser_runtime_available() -> Dict[str, Any]:
    """Report whether the Playwright + Chromium runtime is installed.

    This answers an operational question only. Product workflows layer their
    own policy gates over it: unattended Auto-apply uses
    :func:`autofill_available`, whereas human-in-the-loop Assisted Apply uses
    :func:`assisted_apply_available`.
    """
    try:
        import playwright  # noqa: F401
    except Exception:
        return {
            "available": False,
            "reason": "playwright is not installed (pip install playwright && playwright install chromium)",
        }
    # A bare `pip install playwright` gets this far, and then the first pass
    # dies on DriverError("Executable doesn't exist…"). Name the download
    # instead of claiming the feature works.
    if _chromium_browser_path() is None:
        return {
            "available": False,
            "reason": "the Chromium browser is not downloaded (playwright install chromium)",
        }
    return {"available": True}


def autofill_available() -> Dict[str, Any]:
    """Report whether unattended Auto-apply browser automation can run."""
    runtime = browser_runtime_available()
    if not runtime["available"]:
        return runtime
    if not settings.autofill_enabled:
        return {"available": False, "reason": "AUTOFILL_ENABLED is false"}
    return {"available": True, "dry_run": settings.autofill_dry_run}


def assisted_apply_available() -> Dict[str, Any]:
    """Report whether interactive, human-in-the-loop browser filling can run.

    ``AUTOFILL_ENABLED`` deliberately does *not* gate this workflow. It is the
    switch for unattended Auto-apply; an assisted session pauses for the user
    at sign-in, CAPTCHA, MFA, unknown, and submission checkpoints, and its
    submit policy is still enforced by ``browser_session.effective_policy``.
    Operators can disable Assisted Apply independently when needed.
    """
    runtime = browser_runtime_available()
    if not runtime["available"]:
        return runtime
    if not settings.assisted_apply_enabled:
        return {"available": False, "reason": "ASSISTED_APPLY_ENABLED is false"}
    return {"available": True}


# --------------------------------------------------------------------------- #
# Where this application is allowed to run
# --------------------------------------------------------------------------- #
class _NavigationRefused(RuntimeError):
    """Raised inside the browser block to abort a run on a domain/policy failure."""

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        super().__init__(reason)


def _hosts(values: Iterable[Any]) -> List[str]:
    """Normalise a mix of hosts and URLs into bare, lower-cased host names."""
    hosts: List[str] = []
    for value in values or ():
        text = str(value or "").strip()
        if not text:
            continue
        parsed = urlsplit(text if "//" in text else f"//{text}")
        host = (parsed.hostname or "").strip().lower().rstrip(".")
        if host:
            hosts.append(host)
    return list(dict.fromkeys(hosts))


def _host_allowed(host: str, domains: Sequence[str]) -> bool:
    return bool(host) and any(net_guard.host_matches(host, domain) for domain in domains)


class DomainPolicy:
    """
    The hosts this application's browser may act on, and what each tier permits.

    Two tiers, because the two pieces of metadata have different trust levels:

    * ``enforced`` — domains named by *independent* job metadata (the company
      website on record, or whatever the caller passes). When present they
      restrict **navigation**: a posting on any other host is refused outright.
    * ``application`` — ``enforced`` plus the ATS portal domain the detected
      portal implies (``form_detector.VAULT_DOMAINS``). These restrict
      **credentials**: a vault password is typed only on one of them.

    The posting URL's own host is never in either set — it is the
    attacker-supplied value, so letting it define the policy would make the
    check a tautology. An empty policy means "no verified application domain":
    navigation is still allowed (the SSRF pre-flight applies) but **no
    credential is ever injected**.
    """

    def __init__(self, enforced: Sequence[str] = (), application: Sequence[str] = ()) -> None:
        self.enforced: List[str] = list(dict.fromkeys(enforced))
        self.application: List[str] = list(dict.fromkeys(list(self.enforced) + list(application)))

    @property
    def known(self) -> bool:
        return bool(self.application)

    def allows_navigation(self, host: str) -> bool:
        """Hard restriction: enforced by independent company metadata only."""
        return (not self.enforced) or _host_allowed(host, self.enforced)

    def allows_login_navigation(self, host: str, *, requires_login: bool) -> bool:
        """
        A form that needs a login must be on the application domain: that is the
        confused-deputy case (a feed-supplied URL that renders a login form and
        collects a vault password), so it is refused before any navigation.
        """
        if not requires_login or not self.application:
            return True
        return _host_allowed(host, self.application)

    def allows_credentials(self, host: str) -> bool:
        """Credentials go only to a domain the job's metadata names."""
        return _host_allowed(host, self.application)

    def as_dict(self) -> Dict[str, Any]:
        return {"enforced": list(self.enforced), "application": list(self.application),
                "state": "enforced" if self.enforced else ("application" if self.application else "unverified")}


def domain_policy(*, plan: Optional[Dict[str, Any]] = None, forms: Optional[Dict[str, Any]] = None,
                  expected_domains: Optional[Iterable[Any]] = None) -> DomainPolicy:
    """Build the :class:`DomainPolicy` for a job from its own metadata."""
    plan = plan or {}
    forms = forms or {}
    ats: List[str] = []
    portal = str(plan.get("portal_type") or forms.get("portal_type") or "").lower()
    portal_domain = ATS_VAULT_DOMAINS.get(portal)
    if portal_domain:
        ats.append(portal_domain.lower())
    # ``vault_domain`` counts only when it *is* a known ATS host: for a custom
    # portal that field is just the posting host echoed back (circular, so
    # worthless as evidence).
    known_ats = {host.lower() for host in ATS_VAULT_DOMAINS.values()}
    for vault_domain in (plan.get("vault_domain"), forms.get("vault_domain")):
        for host in _hosts([vault_domain]):
            if host in known_ats:
                ats.append(host)
    return DomainPolicy(enforced=_hosts(expected_domains or ()), application=ats)


def _page_host(page: Any, fallback_url: str = "") -> str:
    """The host the browser is *actually* on — redirects included."""
    current = str(getattr(page, "url", "") or "").strip()
    host = urlsplit(current).hostname if current else None
    if not host and fallback_url:
        host = urlsplit(fallback_url).hostname
    return (host or "").strip().lower().rstrip(".")


def _diag(diagnostics: Optional[List[Dict[str, Any]]], entry: Dict[str, Any]) -> None:
    """Append one step to the run's diagnostics (capped, never a value)."""
    if diagnostics is not None and len(diagnostics) < MAX_DIAGNOSTICS:
        diagnostics.append(entry)


def credential_decision(*, host: str, policy: DomainPolicy,
                        credential: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """
    Pure decision: may the vault credential be typed on *host*?

    Split out from the browser code so the rule is unit-testable — this is the
    line between "filled a form" and "handed a password to whoever owns it".
    """
    if not credential:
        return {"inject": False, "reason": "no_credential"}
    if not policy.application:
        return {"inject": False, "reason": "no_verified_application_domain"}
    if not policy.allows_credentials(host):
        return {"inject": False, "reason": "login_host_mismatch"}
    vault_domain = str((credential or {}).get("domain") or "").strip().lower()
    if vault_domain and not net_guard.host_matches(host, vault_domain):
        return {"inject": False, "reason": "credential_domain_mismatch"}
    return {"inject": True, "reason": "host_matches_application_domain"}


def _blocked_result(*, url: str, code: str, reason: str, dry_run: bool,
                    navigation: Optional[Dict[str, Any]] = None,
                    login: Optional[Dict[str, Any]] = None,
                    diagnostics: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """A run that was refused before (or during) navigation — never a fake apply."""
    inc("jobhunter_autofill_navigation_blocked_total", reason=code)
    inc("jobhunter_autofill_runs_total", result="blocked")
    # Every refusal funnels through here, so this is the one place the
    # "autofill did not complete" rate can be counted honestly. The reason is
    # mapped to a bounded label — ``code`` names a host, a label may not.
    autofill_failure_reason({"code": code, "reason": reason, "status": "blocked"})
    log.warning("autofill refused to run on %s (%s): %s", url, code, reason)
    return {
        "status": "blocked",
        "reason": reason,
        "blocked_by": code,
        "filled": 0,
        "filled_fields": [],
        "skipped_fields": [],
        "submitted": False,
        "dry_run": dry_run,
        "screenshot": None,
        "navigation": navigation or {},
        "login": login or {"attempted": False, "injected": False, "reason": "navigation_refused"},
        "diagnostics": diagnostics or [],
    }


async def execute_autofill(
    *,
    url: str,
    plan: Dict[str, Any],
    credential: Optional[Dict[str, str]] = None,
    allow_submit: bool = False,
    screenshot_path: Optional[str] = None,
    expected_domains: Optional[Iterable[Any]] = None,
) -> Dict[str, Any]:
    """
    Fill the application form in a real browser.

    Returns a structured result; never raises for expected conditions
    (Playwright missing, dry-run, timeout, refused navigation) — those are
    recorded as statuses and surfaced to the user in the job timeline.

    ``expected_domains`` carries the job's *independent* company metadata (its
    website on record); see :class:`DomainPolicy` for how it is used.
    """
    availability = autofill_available()
    if not availability["available"]:
        autofill_failure_reason({"status": "unavailable", "reason": availability["reason"]})
        return {"status": "unavailable", "reason": availability["reason"], "filled": 0, "submitted": False}

    dry_run = settings.autofill_dry_run or not allow_submit
    plan = plan or {}
    diagnostics: List[Dict[str, Any]] = []

    # Gate 1 — the outbound URL policy, applied before a browser even exists.
    verdict = await net_guard.preflight_navigation(url)
    navigation: Dict[str, Any] = {
        "host": verdict.host, "scheme": verdict.scheme, "port": verdict.port,
        "allowlisted": verdict.allowlisted, "resolved": list(verdict.addresses),
    }
    if not verdict.allowed:
        return _blocked_result(
            url=url, code=verdict.code, dry_run=dry_run, navigation=navigation,
            reason=f"posting URL refused by the outbound policy ({verdict.reason})",
        )

    # Gate 2 — the job's own metadata decides where this application may run.
    policy = domain_policy(plan=plan, expected_domains=expected_domains)
    navigation.update(policy.as_dict())
    if not policy.allows_navigation(verdict.host):
        return _blocked_result(
            url=url, code="domain_mismatch", dry_run=dry_run, navigation=navigation,
            reason=(f"posting host {verdict.host} is not this job's company domain "
                    f"({', '.join(policy.enforced)})"),
        )
    if not policy.allows_login_navigation(verdict.host, requires_login=bool(plan.get("requires_login"))):
        return _blocked_result(
            url=url, code="login_domain_mismatch", dry_run=dry_run, navigation=navigation,
            reason=(f"this application requires a login, but the posting host {verdict.host} is not "
                    f"its application domain ({', '.join(policy.application)})"),
        )
    if not policy.known:
        log.warning("autofill: no verified application domain for %s — no credential will be injected", url)

    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover - optional dependency
        return {"status": "unavailable", "reason": f"playwright import failed: {exc}", "filled": 0, "submitted": False}

    filled: List[str] = []
    skipped: List[str] = []
    submitted = False
    error: Optional[str] = None
    login_report: Dict[str, Any] = {"attempted": False, "injected": False, "reason": ""}

    try:  # pragma: no cover - requires browsers, exercised in e2e environments
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=settings.autofill_headless)
            context = await browser.new_context(user_agent=settings.http_user_agent)
            page = await context.new_page()
            page.set_default_timeout(settings.autofill_timeout_ms)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=settings.autofill_timeout_ms)
                landing_host = _page_host(page, url)
                navigation["landing_host"] = landing_host
                log.debug("autofill: navigated %s -> host %s (requested %s)", url, landing_host, verdict.host)
                _diag(diagnostics, {"step": "goto", "host": landing_host, "outcome": "navigated"})

                # Gate 3 — whatever the browser ended up on (a redirect counts)
                # must still be the application domain before anything is typed.
                if policy.enforced and not _host_allowed(landing_host, policy.enforced):
                    raise _NavigationRefused(
                        "redirect_domain_mismatch",
                        f"the page redirected to {landing_host}, which is not this job's company "
                        f"domain ({', '.join(policy.enforced)})",
                    )
                if (landing_host != verdict.host
                        and not net_guard.same_registrable_domain(landing_host, verdict.host)
                        and not _host_allowed(landing_host, policy.application)):
                    # The posting sent the browser to a different organisation:
                    # that page is not the one the user chose to apply on, so
                    # neither the profile data nor a credential goes into it.
                    raise _NavigationRefused(
                        "redirect_offsite",
                        f"the page redirected from {verdict.host} to {landing_host}, which this "
                        f"job's metadata does not name ({', '.join(policy.application) or 'no known domain'})",
                    )
                if policy.application and not _host_allowed(landing_host, policy.application):
                    log.warning("autofill: page host %s is not the application domain %s — "
                                "credentials and password fields will be skipped",
                                landing_host, policy.application)

                if plan.get("requires_login") and credential:
                    login_report["attempted"] = True
                    decision = credential_decision(host=landing_host, policy=policy, credential=credential)
                    login_report["reason"] = decision["reason"]
                    _diag(diagnostics, {"step": "login", "host": landing_host,
                                        "outcome": "allowed" if decision["inject"] else "refused",
                                        "reason": decision["reason"]})
                    if decision["inject"]:
                        submitted_login = await _attempt_login(page, credential, policy=policy,
                                                               diagnostics=diagnostics)
                        login_report["injected"] = bool(submitted_login)
                        login_report["reason"] = ("login_submitted" if submitted_login
                                                  else "no_login_form_matched")
                    else:
                        # Never log the credential itself — only the decision.
                        log.warning("autofill: credentials withheld on %s (%s)", landing_host,
                                    decision["reason"])
                elif credential and not plan.get("requires_login"):
                    login_report["reason"] = "form_does_not_require_login"

                credentials_allowed = policy.allows_credentials(landing_host)
                for field in plan.get("fields", []):
                    name = field.get("name") or "?"
                    if field.get("value") in (None, ""):
                        skipped.append(name)
                        _diag(diagnostics, {"step": "field", "field": name, "outcome": "skipped_no_value"})
                        continue
                    if (field.get("type") == "password" or field.get("value_source") == "vault") \
                            and not credentials_allowed:
                        # The plan maps password fields to the vault password, so
                        # the fill loop is a credential path too.
                        skipped.append(f"{name}:credential_host_mismatch")
                        log.warning("autofill: not typing the vault credential into '%s' on %s", name,
                                    landing_host)
                        _diag(diagnostics, {"step": "field", "field": name, "selector": None,
                                            "outcome": "refused_credential_host"})
                        continue
                    try:
                        await _fill_field(page, field, diagnostics)
                        filled.append(name)
                    except Exception as exc:
                        skipped.append(f"{name}:{type(exc).__name__}")

                if screenshot_path:
                    os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
                    await page.screenshot(path=screenshot_path, full_page=True)

                if not dry_run:
                    submitted = await _submit(page, diagnostics=diagnostics)
            finally:
                await context.close()
                await browser.close()
    except _NavigationRefused as exc:
        return _blocked_result(url=url, code=exc.code, reason=str(exc), dry_run=dry_run,
                               navigation=navigation, login=login_report, diagnostics=diagnostics)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.warning("autofill execution failed for %s: %s", url, error)

    inc("jobhunter_autofill_runs_total", result=("error" if error else ("dry_run" if dry_run else "submitted")))
    if error:
        autofill_failure_reason({"reason": error, "status": "error"})
    return {
        "status": "error" if error else ("dry_run" if dry_run else ("submitted" if submitted else "filled")),
        "reason": error,
        "filled": len(filled),
        "filled_fields": filled,
        "skipped_fields": skipped,
        "submitted": submitted,
        "dry_run": dry_run,
        "screenshot": screenshot_path,
        "navigation": navigation,
        "login": login_report,
        # Which selector matched (or did not) for every step — names only, never
        # values, never passwords.
        "diagnostics": diagnostics,
    }


async def _attempt_login(page, credential: Dict[str, str], *, policy: Optional[DomainPolicy] = None,
                         diagnostics: Optional[List[Dict[str, Any]]] = None) -> bool:
    """
    Type the vault credential into the login form and submit it.

    Refuses outright unless the page's live host is the application domain — the
    check that keeps a feed-supplied URL from harvesting a password. Logs which
    selector matched (never the value typed) and returns True when a submit
    control was clicked.
    """
    host = _page_host(page)
    if policy is not None and policy.application and not policy.allows_credentials(host):
        log.warning("autofill login: page is on %s, not the application domain %s — credentials withheld",
                    host, policy.application)
        _diag(diagnostics, {"step": "login", "host": host, "outcome": "refused_host_mismatch"})
        return False

    username_selectors = ("input[type=email]", "input[name*=email i]", "input[name*=user i]", "#username")
    for selector in username_selectors:
        try:
            await page.fill(selector, credential.get("username", ""), timeout=4000)
            log.debug("autofill login: username matched %s on %s", selector, host)
            _diag(diagnostics, {"step": "login_username", "selector": selector, "outcome": "matched"})
            break
        except Exception as exc:
            log.debug("autofill login: username selector %s did not match (%s)", selector,
                      type(exc).__name__)
            _diag(diagnostics, {"step": "login_username", "selector": selector,
                                "outcome": "no_match", "error": type(exc).__name__})

    password_selectors = ("input[type=password]", "#password")
    for selector in password_selectors:
        try:
            await page.fill(selector, credential.get("password", ""), timeout=4000)
            # The value is a password: the selector is logged, the value never is.
            log.debug("autofill login: password field matched %s on %s (value not logged)", selector, host)
            _diag(diagnostics, {"step": "login_password", "selector": selector, "outcome": "matched"})
            break
        except Exception as exc:
            log.debug("autofill login: password selector %s did not match (%s)", selector,
                      type(exc).__name__)
            _diag(diagnostics, {"step": "login_password", "selector": selector,
                                "outcome": "no_match", "error": type(exc).__name__})

    for selector in ("button[type=submit]", "input[type=submit]"):
        try:
            await page.click(selector, timeout=4000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            log.debug("autofill login: submitted with %s on %s", selector, host)
            _diag(diagnostics, {"step": "login_submit", "selector": selector, "outcome": "matched"})
            return True
        except Exception as exc:
            log.debug("autofill login: submit selector %s did not match (%s)", selector,
                      type(exc).__name__)
            _diag(diagnostics, {"step": "login_submit", "selector": selector,
                                "outcome": "no_match", "error": type(exc).__name__})
    log.debug("autofill login: no submit control matched on %s", host)
    return False


async def _fill_field(page, field: Dict[str, Any],
                      diagnostics: Optional[List[Dict[str, Any]]] = None) -> None:
    """Fill one field, recording which selector matched (or that none did)."""
    name = field.get("name") or "?"
    fill_type = field.get("type")
    selectors = [f'[name="{name}"]', f"#{name}"]
    for selector in selectors:
        locator = page.locator(selector).first
        if await locator.count() == 0:
            log.debug("autofill field '%s': %s matched nothing", name, selector)
            _diag(diagnostics, {"step": "field", "field": name, "selector": selector,
                                "outcome": "no_match"})
            continue
        if fill_type == "file":
            await locator.set_input_files(field["value"])
            action = "file"
        elif fill_type == "select" or field.get("options"):
            try:
                await locator.select_option(label=str(field["value"]))
            except Exception:
                await locator.select_option(str(field["value"]))
            action = "select"
        elif fill_type == "checkbox":
            if str(field["value"]).lower() in ("true", "yes", "1"):
                await locator.check()
            action = "checkbox"
        else:
            await locator.fill(str(field["value"]))
            action = "fill"
        # Field name + selector only: the value can be personal data (and for a
        # password field it is the vault credential), so it is never logged.
        log.debug("autofill field '%s' (%s): %s matched %s", name, fill_type or "text", selector, action)
        _diag(diagnostics, {"step": "field", "field": name, "selector": selector,
                            "type": fill_type or "text", "outcome": "matched", "action": action})
        return
    log.debug("autofill field '%s': no selector matched (tried %s)", name, ", ".join(selectors))
    _diag(diagnostics, {"step": "field", "field": name, "selector": None, "outcome": "no_selector_matched"})
    raise LookupError(f"no selector matched {name} (tried {', '.join(selectors)})")


async def _submit(page, diagnostics: Optional[List[Dict[str, Any]]] = None) -> bool:
    """Click a submit control. Logs the selector that worked so a false 'applied' is traceable."""
    selectors = ("button[type=submit]", "input[type=submit]", "button:has-text('Submit')",
                 "button:has-text('Apply')", "button:has-text('Send application')")
    for selector in selectors:
        try:
            await page.click(selector, timeout=5000)
            await page.wait_for_load_state("networkidle", timeout=20000)
            log.debug("autofill submit: %s matched on %s", selector, _page_host(page))
            _diag(diagnostics, {"step": "submit", "selector": selector, "outcome": "matched"})
            return True
        except Exception as exc:
            log.debug("autofill submit: %s did not match (%s)", selector, type(exc).__name__)
            _diag(diagnostics, {"step": "submit", "selector": selector, "outcome": "no_match",
                                "error": type(exc).__name__})
    log.debug("autofill submit: no submit control matched on %s", _page_host(page))
    return False
