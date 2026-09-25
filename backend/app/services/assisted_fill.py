"""
Assisted filling: the browser side of a session, and the pass loop around it.

Everything *policy* lives in :mod:`app.services.browser_session`; this module
knows how to drive a browser and nothing more. Two drivers:

``RecordingDriver``
    Deterministic, no network, no browser. Takes a script of observations and
    records what it was asked to type; it is what the test suite and a
    dry-run rehearsal use.
``PlaywrightDriver``
    The real thing, imported lazily. It is only constructed when Playwright is
    installed, ``AUTOFILL_ENABLED`` is true and the outbound URL policy passes —
    the same gates the shipped autofill honours, plus the session's own
    checkpoint.

What a driver may never do, at any layer
----------------------------------------
* Type into a field the classifier did not mark ``autofill``. The fill loop
  re-checks every instruction against the classification and refuses the rest.
* Overwrite a field the page already shows as user-filled.
* Solve, click, relay or "assist" a CAPTCHA; enter an MFA code.
* Type a password — unless the session policy carries the user's explicit
  ``create_accounts`` opt-in, the instruction's value came from the vault, and
  the driver's own re-classification (``allow_credentials``) clears it. Every
  other password instruction, at every layer, is refused as before.
* Read page text beyond the structural observation (labels/types/options), or
  keep a field value anywhere but in the fill call itself.
* Submit unless the *session* policy allows it. In this task auto-submit is off
  unless the deployment and the user both enabled it, and the at-most-once
  ledger still has to grant the reservation.
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import inc
from app.models.models import ApplicationSession, Job, User
from app.services import browser_session as sessions
from app.services import live_browser, net_guard
from app.services.autofill import assisted_apply_available, browser_launch_kwargs, browser_launch_plan
from app.services.field_classifier import classify_form
from app.services.form_detector import VAULT_DOMAINS, hosts_in_same_ats_family
from app.services.reliability import duration
from app.services.user_settings import get_setting

log = get_logger("app.assisted")

#: How many observe → fill → advance rounds a single pass may run before it
#: stops and asks a human. A form that keeps producing work is not a reason to
#: loop forever inside someone's session.
MAX_PASS_STEPS = 8

#: How many *pages* of a multi-step flow one pass may walk through. Real portals
#: are sign-up → profile → questions → review → submit; a pass that only ever
#: looks at the first page is the bug this constant exists to prevent, and a pass
#: that follows links forever is the one it bounds.
MAX_FLOW_PAGES = 6

#: Labels that mean "go to the next page of the application". Matched
#: case-insensitively against a control's accessible name; the *first* match in
#: the list wins, so the more specific phrasings come first.
NEXT_STEP_LABELS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("start_application", ("apply for this job", "apply for this position", "start application",
                           "start your application", "begin application", "apply now", "apply today",
                           "apply")),
    ("create_account", ("create an account", "create account", "sign up", "sign-up", "register",
                        "join now", "get started")),
    ("continue_application", ("continue application", "continue to application", "continue",
                              "next", "next step", "save and continue", "save & continue",
                              "proceed", "review application", "review and submit")),
)

#: Labels that submit the application. A pass never clicks one of these unless
#: the session policy allows submission — and even then the at-most-once ledger,
#: not this module, owns the decision to submit.
SUBMIT_LABELS: Tuple[str, ...] = (
    "submit application", "submit your application", "submit my application",
    "send application", "submit", "apply for this role", "finish application",
)

#: Accessibility roles that can carry a navigation control.
CONTROL_ROLES: Tuple[str, ...] = ("button", "link", "menuitem", "tab")

#: Observation script: structure only. Values are read as *presence*, never
#: returned — a driver that shipped values back would put page content (and
#: whatever the user typed) into the session row.
OBSERVE_SCRIPT = """
() => {
  const out = {url: location.href, host: location.host, title: document.title, fields: []};
  const nodes = document.querySelectorAll('input, select, textarea');
  for (const el of nodes) {
    const type = (el.getAttribute('type') || el.tagName.toLowerCase()).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) continue;
    const label = el.labels && el.labels.length ? el.labels[0].innerText
      : (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || '');
    let options = [];
    if (el.tagName.toLowerCase() === 'select') {
      options = Array.from(el.options || []).slice(0, 24).map(o => (o.text || '').trim());
    }
    const value = el.value;
    out.fields.push({
      name: el.name || el.id || '',
      label: (label || '').trim().slice(0, 200),
      type: type,
      required: el.required || el.getAttribute('aria-required') === 'true',
      options: options,
      autocomplete: el.getAttribute('autocomplete') || '',
      value_present: !!(value && String(value).trim().length),
      filled_by_user: false,
      classes: Array.from(el.classList).slice(0, 8),
    });
    if (out.fields.length >= 120) break;
  }
  const markers = [];
  if (document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], .g-recaptcha, [data-sitekey]')) markers.push('captcha');
  if (document.querySelector('input[autocomplete="one-time-code"], input[name*="otp" i], input[name*="mfa" i]')) markers.push('mfa');
  const pwFields = document.querySelectorAll('input[type=password]');
  if (pwFields.length >= 2) markers.push('signup');
  if (pwFields.length >= 1) markers.push('login');
  // "Sign up" / "Create account" copy in the form area = account creation.
  const frm = document.querySelector('form') || document.body;
  if (frm) {
    const ft = (frm.innerText || '').toLowerCase();
    if (ft.indexOf('create account') !== -1 || ft.indexOf('sign up') !== -1
        || ft.indexOf('register') !== -1 || ft.indexOf('confirm password') !== -1
        || ft.indexOf('new password') !== -1) markers.push('signup');
  }
  out.markers = markers;
  out.submit_present = !!document.querySelector('button[type=submit], input[type=submit]');

  // Did the portal itself say the application arrived? Only these exact
  // phrases count, and only a token is returned — never the page text.
  const body = (document.body ? document.body.innerText : '').toLowerCase();
  if (body.indexOf('thank you for applying') !== -1 || body.indexOf('application submitted') !== -1
      || body.indexOf('successfully submitted') !== -1) out.confirmation = 'submitted';
  else if (body.indexOf('we received your application') !== -1
           || body.indexOf('we have received your application') !== -1
           || body.indexOf('application received') !== -1) out.confirmation = 'received';
  else if (body.indexOf('thank you for your interest') !== -1) out.confirmation = 'thank_you';
  else if (body.indexOf('application is under review') !== -1
           || body.indexOf('under review') !== -1) out.confirmation = 'under_review';

  // "Step 2 of 4" style progress, as integers only.
  const stepMatch = body.match(/step\\s*(\\d+)\\s*(?:of|\\/)\\s*(\\d+)/);
  if (stepMatch) { out.step_index = Number(stepMatch[1]); out.steps_total = Number(stepMatch[2]); }
  return out;
}
""".strip()

#: Control discovery: which clickable things could move the flow on. Labels only
#: (a control's own text), capped, and stamped with an attribute so the click
#: itself goes through Playwright's actionability rather than a synthetic
#: ``element.click()``. Only structure is returned — never page text.
CONTROLS_SCRIPT = """
() => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 1 || rect.height <= 1) return false;
    const style = window.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
  };
  const out = {url: location.href, controls: []};
  const selector = 'button, a[href], input[type=submit], input[type=button], [role=button], [role=link]';
  for (const el of document.querySelectorAll(selector)) {
    if (out.controls.length >= 40) break;
    const label = norm(el.innerText || el.value || el.getAttribute('aria-label')
                       || el.getAttribute('title') || '');
    if (!label) continue;
    out.controls.push({
      index: out.controls.length,
      label: label.slice(0, 120),
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase(),
      href: el.getAttribute('href') || '',
      role: el.getAttribute('role') || '',
      form_action: el.form ? (el.form.getAttribute('action') || '') : '',
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      visible: visible(el),
    });
    el.setAttribute('data-jh-control', String(out.controls.length - 1));
  }
  return out;
}
""".strip()


#: What the *human* types into the shared handoff window, reported back so the
#: session can replay it. Installed once per context (survives navigations) and
#: deliberately conservative:
#:
#: * only ``change`` events — a committed value, never a keystroke stream;
#: * never password/hidden/file fields, never ``one-time-code``/
#:   ``current-password`` autocomplete — those are refused *in the page* as
#:   well as by the server, which re-checks every report anyway;
#: * field identity (name/label/type) plus one bounded value — no page text,
#:   no cookies, no DOM beyond the edited element.
RECORD_INPUT_SCRIPT = """
(() => {
  if (window.__jobhunterRecorder) return;
  window.__jobhunterRecorder = true;
  const BAD_TYPE = /^(password|hidden|file)$/i;
  const BAD_AC = /(password|one-time-code)/i;
  const report = (el) => {
    try {
      if (!window.__jobhunterInput) return;
      const name = el.name || el.id || '';
      if (!name) return;
      const type = String(el.type || el.tagName.toLowerCase() || '').toLowerCase();
      if (BAD_TYPE.test(type)) return;
      const ac = String(el.getAttribute('autocomplete') || '');
      if (BAD_AC.test(ac)) return;
      const label = el.labels && el.labels.length ? el.labels[0].innerText
        : (el.getAttribute('aria-label') || '');
      window.__jobhunterInput({
        name: String(name).slice(0, 200),
        label: String(label || '').trim().slice(0, 200),
        type: type.slice(0, 40),
        autocomplete: ac.toLowerCase().slice(0, 60),
        value: String(el.value == null ? '' : el.value).slice(0, 1000),
      });
    } catch (e) { /* recording must never break the page */ }
  };
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (!el || !el.tagName) return;
    const tag = el.tagName.toLowerCase();
    if (tag !== 'input' && tag !== 'select' && tag !== 'textarea') return;
    report(el);
  }, true);
})();
""".strip()


class DriverError(RuntimeError):
    """A browser step failed. Surfaced as a failed session, never as a fake success."""


class BrowserDriver(Protocol):
    """The browser surface a session pass needs. Implements no policy itself."""

    async def open(self, session: ApplicationSession) -> None: ...

    async def observe(self) -> Dict[str, Any]: ...

    async def fill(self, instructions: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]: ...

    async def controls(self) -> Dict[str, Any]: ...

    async def advance(self, decision: Mapping[str, Any]) -> Dict[str, Any]: ...

    async def submit(self) -> Dict[str, Any]: ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# Which control moves the flow on (pure policy, no browser)
# --------------------------------------------------------------------------- #
def _label_kind(label: str) -> Tuple[str, str]:
    """
    Classify a control's own label.

    Returns ``(kind, matched)`` where ``kind`` is ``"submit"``, one of the
    ``NEXT_STEP_LABELS`` keys, or ``""`` when the control is none of our
    business (a nav link, a cookie banner, a "Sign in" toggle…).
    """
    text = re.sub(r"\s+", " ", str(label or "")).strip().lower()
    if not text:
        return "", ""
    for phrase in SUBMIT_LABELS:
        if phrase in text:
            return "submit", phrase
    for kind, phrases in NEXT_STEP_LABELS:
        for phrase in phrases:
            if text == phrase or phrase in text:
                return kind, phrase
    return "", ""


def choose_advance_control(
    controls: Sequence[Mapping[str, Any]],
    *,
    allow_submit: bool = False,
    current_host: str = "",
    expected_host: str = "",
) -> Dict[str, Any]:
    """
    Pick the single control that carries the application to its next page.

    This is deliberately conservative, because a click is irreversible:

    * invisible and disabled controls are never candidates;
    * a *submit*-looking control is only a candidate when the session policy
      allows submission, and even then the at-most-once ledger still owns the
      decision — this function only says which button that would be;
    * an off-host link is never clicked (the pass stops and hands the flow to
      the human instead of wandering to another site);
    * the bare word "apply" only counts on a real button — an "Apply" nav link
      on a job board is how a naive automation ends up on a different posting.

    Returns ``{"click": bool, "index": int, "label": str, "kind": str,
    "reason": str}`` — ``reason`` is a code, never prose.
    """
    best: Optional[Dict[str, Any]] = None
    superseded: Optional[Dict[str, Any]] = None
    for control in controls or ():
        if not isinstance(control, Mapping):
            continue
        if not control.get("visible", True) or control.get("disabled"):
            continue
        label = str(control.get("label") or "")
        kind, matched = _label_kind(label)
        if not kind:
            continue
        tag = str(control.get("tag") or "").lower()
        href = str(control.get("href") or "").strip()
        if kind == "submit":
            if not allow_submit:
                superseded = superseded or {"index": int(control.get("index") or 0), "label": label[:120],
                                            "kind": "submit", "reason": "submit_not_allowed"}
                continue
            candidate = {"index": int(control.get("index") or 0), "label": label[:120],
                         "kind": "submit", "matched": matched, "reason": "submit_control"}
            return {"click": True, **candidate}
        # A link that leaves the application's host is not a step of this flow.
        # Same-family ATS redirects (Lever → auth.lever.co, Workday → company
        # subdomain) ARE allowed — they are the application flow.
        if href and not href.startswith(("#", "javascript:", "mailto:")):
            parsed = urlsplit(href)
            if parsed.hostname and parsed.scheme in ("http", "https"):
                target = parsed.hostname.lower()
                if expected_host and not net_guard.host_matches(target, expected_host) \
                        and not net_guard.host_matches(target, current_host) \
                        and not _host_in_family(target, expected_host, current_host):
                    superseded = superseded or {"index": int(control.get("index") or 0),
                                                "label": label[:120], "kind": kind,
                                                "reason": "off_host_control"}
                    continue
            if not parsed.hostname and parsed.path in ("", "#") and href in ("#", ""):
                continue
        # The generic readings ("apply", "continue", "next") are only trusted on
        # an actual button — a link with that word is usually navigation chrome.
        if matched in ("apply", "continue", "next", "proceed") and tag not in ("button", "input"):
            superseded = superseded or {"index": int(control.get("index") or 0), "label": label[:120],
                                        "kind": kind, "reason": "generic_label_on_link"}
            continue
        candidate = {"index": int(control.get("index") or 0), "label": label[:120], "kind": kind,
                     "matched": matched, "reason": "next_step_control"}
        if best is None:
            best = candidate
    if best is not None:
        return {"click": True, **best}
    if superseded is not None:
        return {"click": False, "reason": superseded["reason"], **superseded}
    return {"click": False, "reason": "no_control_found"}


# --------------------------------------------------------------------------- #
# Recording driver (tests, dry-run rehearsal)
# --------------------------------------------------------------------------- #
@dataclass
class RecordingDriver:
    """
    A driver that replays a script and records the fills it was asked to make.

    ``observations`` is a list of page observations (the same shape
    ``PlaywrightDriver.observe`` returns); each ``observe()`` pops the next one.
    ``existing_values`` marks fields the page already holds content in, so the
    no-overwrite rule is exercised without a browser.
    """

    observations: List[Dict[str, Any]] = dataclass_field(default_factory=list)
    #: One entry per ``controls()`` call: the clickable things the page offered.
    #: The last entry is reused once the script runs out (a page that keeps
    #: offering the same "Continue" button is the normal case).
    control_scripts: List[List[Dict[str, Any]]] = dataclass_field(default_factory=list)
    #: The same thing keyed by the page URL, which is what a multi-step script
    #: wants: the loop legitimately looks at one page twice (before and after
    #: filling it) and a positional script would drift by one observation.
    controls_by_url: Dict[str, List[Dict[str, Any]]] = dataclass_field(default_factory=dict)
    _last_url: str = ""
    existing_values: Dict[str, Any] = dataclass_field(default_factory=dict)
    fills: List[Dict[str, Any]] = dataclass_field(default_factory=list)
    advances: List[Dict[str, Any]] = dataclass_field(default_factory=list)
    opened: bool = False
    closed: bool = False
    submitted: bool = False
    fail_on_fill: bool = False
    launch_report: Optional[Dict[str, Any]] = None

    async def open(self, session: ApplicationSession) -> None:
        self.opened = True

    async def observe(self) -> Dict[str, Any]:
        if self.observations:
            page = dict(self.observations.pop(0))
            self._last_url = str(page.get("url") or "")
            return page
        return {"url": "", "fields": [], "markers": []}

    async def fill(self, instructions: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        if self.fail_on_fill:
            raise DriverError("recorded fill failure")
        recorded: List[Dict[str, Any]] = []
        for instruction in instructions:
            name = str(instruction.get("name") or "")
            entry = dict(instruction)
            self.fills.append(entry)
            recorded.append({"name": name, "status": "filled", "value_fingerprint":
                             sessions.fingerprint_value(instruction.get("value"))})
        return recorded

    async def controls(self) -> Dict[str, Any]:
        if self.controls_by_url:
            return {"url": self._last_url,
                    "controls": list(self.controls_by_url.get(self._last_url) or [])}
        if self.control_scripts:
            return {"url": "", "controls": list(self.control_scripts[0] if len(self.control_scripts) == 1
                                                 else self.control_scripts.pop(0))}
        return {"url": "", "controls": []}

    async def advance(self, decision: Mapping[str, Any]) -> Dict[str, Any]:
        self.advances.append(dict(decision))
        return {"advanced": True, "label": decision.get("label") or "", "url": "",
                "changed": True, "kind": decision.get("kind") or ""}

    async def submit(self) -> Dict[str, Any]:
        self.submitted = True
        return {"submitted": True, "receipt": {"kind": "recording_driver"}}

    async def close(self) -> None:
        self.closed = True

    def healthy(self) -> bool:
        return self.opened and not self.closed


# --------------------------------------------------------------------------- #
# Playwright driver (real; optional dependency)
# --------------------------------------------------------------------------- #
class PlaywrightDriver:
    """
    A real browser, isolated per session.

    * Its own context (never a shared one) and, when persistence is enabled, the
      user's encrypted ``storage_state``.
    * Navigation goes through the outbound URL policy and the session's expected
      host; a redirect to another host stops the run before anything is typed.
    * ``observe()`` runs the structural script above — no page text is returned.
    * ``fill()`` refuses any instruction that is not ``autofill``-classified, and
      refuses to overwrite a field whose value is already present.
    * ``controls()``/``advance()`` walk a multi-step application: the next page
      is reached by clicking the portal's own "Apply"/"Continue" control, decided
      in :func:`choose_advance_control` (never here).
    * The window is *visible* whenever a display exists. ``Assisted Apply`` is a
      human-in-the-loop feature: a browser the user cannot see is a browser the
      user cannot sign in to, and a headless run is exactly how a multi-step
      application looked like it "completed" while nothing had happened.
    """

    def __init__(self, session: ApplicationSession, *, target_url: str = "",
                 storage_state: Optional[Mapping[str, Any]] = None,
                 allow_submit: bool = False, allow_credentials: bool = False) -> None:
        self.session = session
        #: Where to navigate. Comes from the job the session is bound to, so the
        #: session's own binding (not a caller) decides the destination.
        self.target_url = target_url
        self.storage_state = dict(storage_state) if storage_state else None
        self.allow_submit = allow_submit
        #: Policy-backed: the user opted in to account creation. Only this
        #: lets ``fill()`` re-classify a password instruction as autofill —
        #: with it off (the default) passwords are refused here as ever.
        self.allow_credentials = allow_credentials
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        #: Whether this context already carries the human-input recorder.
        self._recorder_installed = False
        #: What the launch actually did (mode, engine, why) — surfaced in the
        #: pass result so the UI can say whether the user sees a window.
        self.launch_report: Optional[Dict[str, Any]] = None

    def healthy(self) -> bool:
        """A window that still exists — the user is allowed to close it."""
        try:
            if self._page is None or self._page.is_closed():
                return False
            if self._browser is None or not self._browser.is_connected():
                return False
            return True
        except Exception:  # pragma: no cover - defensive
            return False

    async def open(self, session: ApplicationSession) -> None:
        if self.healthy():
            # Adopted window: the human may be mid-step in it. Stay exactly
            # where the page is — reopening or renavigating would throw away
            # the login they are halfway through.
            self.session = session
            return
        availability = assisted_apply_available()
        if not availability["available"]:
            raise DriverError(f"assisted browser unavailable: {availability['reason']}")
        target = self.target_url or str((session.last_observation or {}).get("url") or "")
        verdict = await net_guard.preflight_navigation(target)
        if not verdict.allowed:
            raise DriverError(f"navigation refused by the outbound policy: {verdict.reason}")
        if session.expected_host and not net_guard.host_matches(verdict.host, session.expected_host) \
                and not _host_in_family(verdict.host, session.expected_host or ""):
            raise DriverError(f"{verdict.host} is not this application's host ({session.expected_host})")

        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except Exception as exc:  # pragma: no cover - optional dependency
            raise DriverError(f"playwright import failed: {exc}") from exc

        self._playwright = await async_playwright().start()
        plan = browser_launch_plan()
        self.launch_report = {key: plan.get(key) for key in
                              ("mode", "mode_reason", "headless", "display", "engine",
                               "executable_path")}
        self._browser = await self._launch(plan)
        self._context = await self._browser.new_context(
            user_agent=settings.http_user_agent,
            storage_state=self.storage_state or None,
        )
        await self._install_recorder()
        self._page = await self._context.new_page()
        self._page.set_default_timeout(settings.autofill_timeout_ms)
        await self._page.goto(target, wait_until="domcontentloaded", timeout=settings.autofill_timeout_ms)

    async def _install_recorder(self) -> None:
        """
        Bind the human-input recorder to this context.

        ``context``-level bindings and init scripts survive navigations, so a
        multi-step flow keeps reporting after every ``Continue``. A failure
        here degrades to *no recording* — never to a broken window.
        """
        if self._recorder_installed or self._context is None:
            return
        try:
            await self._context.add_init_script(script=RECORD_INPUT_SCRIPT)
            await self._context.expose_binding("__jobhunterInput", self._on_recorded_input)
            self._recorder_installed = True
        except Exception as exc:  # pragma: no cover - recorder is best effort
            log.warning("assisted: input recorder not installed (%s)", type(exc).__name__)

    async def _on_recorded_input(self, source: Any, payload: Any) -> None:
        """A field the human committed in this window — fold it into the session."""
        try:
            _record_input_from_browser(int(self.session.id), payload)
        except Exception as exc:  # pragma: no cover - never surface into the page
            log.warning("assisted: could not record browser input (%s)", type(exc).__name__)

    async def show(self, url: str) -> bool:
        """
        Navigate the live window to a human-step URL (the handoff target).

        The URL faces the same outbound policy as a pass's first navigation:
        a handoff descriptor can only ever point at the application's own host.
        Returns ``False`` when the window is gone (the user closed it).
        """
        if not self.healthy() or self._page is None:
            return False
        target = str(url or "").strip()
        if not target:
            return True
        verdict = await net_guard.preflight_navigation(target)
        if not verdict.allowed:
            raise DriverError(f"navigation refused by the outbound policy: {verdict.reason}")
        session = self.session
        if session is not None and session.expected_host \
                and not net_guard.host_matches(verdict.host, session.expected_host) \
                and not _host_in_family(verdict.host, session.expected_host or ""):
            raise DriverError(f"{verdict.host} is not this application's host ({session.expected_host})")
        await self._page.goto(target, wait_until="domcontentloaded", timeout=settings.autofill_timeout_ms)
        await self._settle()
        return True

    async def _launch(self, plan: Mapping[str, Any]) -> Any:
        """
        Start the browser the plan describes, falling back *honestly*.

        Two fallbacks, both reported in ``launch_report`` rather than hidden:

        * a frame the user supposedly watches must not turn into a stack trace —
          if the display is gone by the time the request lands (a laptop that
          closed its lid, an X server that restarted), the headless launch is
          used and the reason says so;
        * a channel/executable the operator configured but the host does not have
          falls back to Playwright's own Chromium, still reported.
        """
        kwargs = browser_launch_kwargs(plan)
        try:
            return await self._playwright.chromium.launch(**kwargs)
        except Exception as exc:
            first = f"{type(exc).__name__}: {exc}"
            log.warning("assisted: browser launch failed (%s) — retrying headless", first[:200])
            if self.launch_report is not None:
                self.launch_report["fallback_reason"] = first[:200]
            retry: Dict[str, Any] = {"headless": True}
            if kwargs.get("args"):
                retry["args"] = list(kwargs["args"])
            try:
                browser = await self._playwright.chromium.launch(**retry)
            except Exception as second:
                # Last resort: Playwright's own build, no channel/executable.
                try:
                    browser = await self._playwright.chromium.launch(headless=True)
                except Exception as third:
                    raise DriverError(
                        f"could not launch a browser: {first}; headless retry failed: "
                        f"{type(second).__name__}: {second}; bundled attempt failed: "
                        f"{type(third).__name__}: {third}"
                    ) from third
            if self.launch_report is not None:
                self.launch_report["mode"] = "headless"
                self.launch_report["headless"] = True
                self.launch_report["mode_reason"] = (
                    f"{self.launch_report.get('mode_reason', '')}; fell back to headless after a "
                    f"launch failure")
            return browser

    async def _settle(self) -> None:
        """
        Wait until the document is readable before asking it questions.

        A redirect commits its URL before the new document has rendered: an
        observation taken at that instant sees zero fields, a blank title and no
        controls — which is exactly how a pass "concludes" that a perfectly good
        page has nothing to fill. Cheap and guarded: a page that is already
        settled returns immediately.
        """
        if self._page is None:
            return
        try:
            await self._page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        try:
            ready = await self._page.evaluate(
                "() => document.readyState !== 'loading' && !!document.body")
            if not ready:
                await self._page.wait_for_timeout(250)
        except Exception:
            pass

    async def observe(self) -> Dict[str, Any]:
        if self._page is None:
            raise DriverError("browser not open")
        await self._settle()
        raw = await self._page.evaluate(OBSERVE_SCRIPT)
        return sessions.sanitize_observation(raw)

    async def fill(self, instructions: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """
        Type the instructions into the live page, after re-proving each is safe.

        The re-check is the *safety* half of the contract, not a second opinion
        on "does this field have a value": the fields are classified again **with
        the values we are about to type** (and the session checkpoint), so what
        reaches the page is exactly the set the classifier would call
        ``autofill``. Classifying without those values — the previous behavior —
        put every canonical field into ``ask_user``/``skip``, so the driver
        refused all of them, filled nothing, and the pass still reported
        success. Two defects, one line apart.

        Still refused here, whatever the plan says: one-time codes, bot checks,
        restricted identifiers, and any field the page already holds content in
        that this session did not put there. A password is refused too — unless
        the session policy opted in to account creation *and* the instruction
        carries a vault value, which the ``allow_credentials`` re-classification
        proves before a character is typed.
        """
        if self._page is None:
            raise DriverError("browser not open")
        recorded: List[Dict[str, Any]] = []
        current = await self.observe()
        planned = {str(i.get("name") or ""): i.get("value") for i in instructions}
        checkpoint = dict((self.session.checkpoint or {}).get("fields") or {})
        form = classify_form(current.get("fields") or [], values=planned, checkpoint=checkpoint,
                             allow_credentials=self.allow_credentials)
        allowed = {v.name: v for v in form.autofillable}
        held = {str(f.get("name") or ""): bool(f.get("value_present"))
                for f in (current.get("fields") or []) if isinstance(f, Mapping)}
        for instruction in instructions:
            name = str(instruction.get("name") or "")
            verdict = allowed.get(name)
            if verdict is None:
                # Defence in depth: the plan is not trusted to decide safety.
                log.warning("assisted fill refused '%s': not classified as autofillable", name)
                recorded.append({"name": name, "status": "refused_not_autofillable"})
                continue
            if held.get(name) and str((checkpoint.get(name) or {}).get("status") or "") != "filled":
                # Someone (the user) already typed here. Never type over it.
                recorded.append({"name": name, "status": "skipped_value_present"})
                continue
            selector = f'[name="{name}"]'
            locator = self._page.locator(selector).first
            if await locator.count() == 0:
                # The observation reports ``name || id``; an id-only field needs
                # the other half of that fallback or it is silently unfillable.
                selector = f'#{name}'
                locator = self._page.locator(selector).first
                if await locator.count() == 0:
                    recorded.append({"name": name, "status": "no_selector"})
                    continue
            try:
                await locator.fill(str(instruction.get("value") or ""), timeout=5000)
            except Exception as exc:
                recorded.append({"name": name, "status": f"error:{type(exc).__name__}"})
                continue
            # Field name only — a value can be personal data, and for a password
            # field it would be the credential. The driver never logs values.
            log.debug("assisted fill: '%s' filled via %s", name, selector)
            recorded.append({"name": name, "status": "filled"})
        return recorded

    async def controls(self) -> Dict[str, Any]:
        """Clickable controls on the page, as structure (labels only)."""
        if self._page is None:
            raise DriverError("browser not open")
        try:
            return await self._page.evaluate(CONTROLS_SCRIPT)
        except Exception as exc:  # a page that navigated mid-evaluate is not a failure
            log.debug("assisted controls: %s", type(exc).__name__)
            return {"url": "", "controls": []}

    async def advance(self, decision: Mapping[str, Any]) -> Dict[str, Any]:
        """
        Click the chosen control and wait for the page to actually move on.

        A click that changes nothing is reported as ``changed: false`` — a pass
        that keeps "advancing" onto the same page would otherwise loop until its
        step budget ran out and call that progress.
        """
        if self._page is None:
            raise DriverError("browser not open")
        index = int(decision.get("index") or 0)
        selector = f'[data-jh-control="{index}"]'
        before = await self._page.evaluate(
            "() => [location.href, document.querySelectorAll('input,select,textarea').length]")
        try:
            await self._page.click(selector, timeout=settings.autofill_timeout_ms // 3 or 5000)
        except Exception as exc:
            return {"advanced": False, "reason": "click_failed", "label": decision.get("label") or "",
                    "error": f"{type(exc).__name__}"}
        changed = False
        try:
            await self._page.wait_for_function(
                """(before) => {
                     const now = [location.href, document.querySelectorAll('input,select,textarea').length];
                     return now[0] !== before[0] || now[1] !== before[1];
                   }""",
                arg=before, timeout=15000)
            changed = True
        except Exception:
            # Some portals swap content without touching either signal; give the
            # page a moment and report honestly that we could not confirm it.
            pass
        await self._settle()
        return {"advanced": True, "changed": changed, "label": str(decision.get("label") or "")[:120],
                "kind": str(decision.get("kind") or ""),
                "url": await self._page.evaluate("() => location.href")}

    async def submit(self) -> Dict[str, Any]:
        if not self.allow_submit:
            return {"submitted": False, "reason": "policy_disallows_submit"}
        if self._page is None:
            raise DriverError("browser not open")
        for selector in ("button[type=submit]", "input[type=submit]"):
            try:
                await self._page.click(selector, timeout=5000)
                await self._page.wait_for_load_state("networkidle", timeout=20000)
                return {"submitted": True, "receipt": {"kind": "portal_ack", "selector": selector}}
            except Exception:
                continue
        return {"submitted": False, "reason": "no_submit_control"}

    async def export_storage_state(self) -> Optional[Dict[str, Any]]:
        """The context's own cookie jar, for the (opt-in) encrypted persistence."""
        if self._context is None:
            return None
        try:
            return await self._context.storage_state()
        except Exception:
            return None

    async def close(self) -> None:
        for closer in (getattr(self._context, "close", None), getattr(self._browser, "close", None)):
            try:
                if closer is not None:
                    await closer()
            except Exception:  # pragma: no cover - teardown is best effort
                pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:  # pragma: no cover
            pass
        self._page = self._context = self._browser = self._playwright = None


def build_instruction_values(job: Job, session: ApplicationSession, *,
                             answers: Optional[Mapping[str, Any]] = None,
                             db: Optional[Session] = None) -> Dict[str, Any]:
    """
    Values the session may fill: the plan's non-credential values + user answers.

    With a database handle the user's **learned answers** for this host join
    underneath the session's own set — answers taught by an earlier browser
    window or in-app answer, replayed here. Session state always wins over
    learned state; learned always wins over the static plan.
    """
    merged: Dict[str, Any] = {}
    if db is not None:
        host = sessions.learning_host(session)
        merged.update(sessions.learned_values(db, user_id=session.user_id, host=host))
    merged.update(dict(answers or {}))
    return sessions.planned_values(job, answers=merged)


def _record_input_from_browser(session_id: int, payload: Any) -> Dict[str, Any]:
    """
    Persist one field the human committed in the shared browser window.

    Runs on the event loop inside Playwright's binding callback, so it opens
    its own short-lived database session rather than borrowing the pass's.
    """
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        session = db.query(ApplicationSession).filter(ApplicationSession.id == int(session_id)).first()
        if session is None:
            return {"recorded": [], "restricted": []}
        return sessions.record_browser_input(db, session, payload)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# The pass loop
# --------------------------------------------------------------------------- #
#: The queue pipeline an assisted pass runs under. A *pause* is a normal,
#: successful outcome for this pipeline: the work stops, the human acts, and a
#: later pass (a new queue item) continues from the checkpoint.
BROWSER_SESSION_PIPELINE = "browser_session"


def build_driver(
    db: Session,
    session: ApplicationSession,
    job: Job,
    *,
    user: User,
    policy: Optional[sessions.SessionPolicy] = None,
) -> Optional[BrowserDriver]:
    """
    The real driver for this session, or ``None`` when automation is unavailable.

    Constructed here — never by a request handler — so the destination, the
    stored state and the submit flag all come from the session's own binding.
    """
    availability = assisted_apply_available()
    if not availability["available"]:
        return None
    policy = policy or sessions.effective_policy(db, user.id)
    storage_state = sessions.load_storage_state(db, session, user=user, policy=policy)
    return PlaywrightDriver(
        session,
        target_url=job.url or "",
        storage_state=storage_state,
        allow_submit=policy.allow_submit,
        allow_credentials=policy.create_accounts,
    )


async def open_handoff_window(
    db: Session,
    *,
    user: User,
    session: ApplicationSession,
    url: str = "",
) -> Dict[str, Any]:
    """
    Open (or retarget) the session's **visible** window for a human-only step.

    This is what makes "Start a handoff window" mean what it says: instead of
    only minting a token and rendering a link into a browser that shares
    nothing with the automation, the session's own Playwright window — the one
    whose cookies the next pass will reuse — appears on this machine, parked on
    the step's URL, and stays open until the human closes the action (or the
    idle sweep takes it).

    The answer is always honest: ``{"window_opened": True}`` only when a
    headed window actually exists; otherwise the mode and the reason why not
    (no display, launch fell back to headless, runtime missing), so the UI can
    fall back to the plain link without pretending.
    """
    availability = assisted_apply_available()
    if not availability["available"]:
        return {"window_opened": False, "reason": str(availability.get("reason") or
                                                      "assisted browser unavailable")[:300]}
    plan = browser_launch_plan()
    if not plan.get("available"):
        return {"window_opened": False, "reason": str(plan.get("reason") or
                                                      "browser not available")[:300]}
    if plan.get("headless") or str(plan.get("mode") or "") != "headed":
        return {"window_opened": False, "mode": "headless",
                "reason": str(plan.get("mode_reason") or
                              "this host has no display to show a window on")[:300]}

    driver = live_browser.adopt(session.id)
    if driver is None:
        job = db.query(Job).filter(Job.id == session.job_id).first()
        if job is None:
            return {"window_opened": False, "reason": "the job for this session no longer exists"}
        policy = sessions.effective_policy(db, user.id)
        driver = build_driver(db, session, job, user=user, policy=policy)
        if driver is None:
            return {"window_opened": False, "reason": "assisted browser unavailable"}
        try:
            await driver.open(session)
        except DriverError as exc:
            await driver.close()
            return {"window_opened": False, "reason": str(exc)[:300]}
        launch = getattr(driver, "launch_report", None) or {}
        if str(launch.get("mode") or "") != "headed":
            # The launch fell back to headless: an invisible window is no
            # window at all for a human step, so close it and say why.
            reason = launch.get("fallback_reason") or launch.get("mode_reason") or \
                "the headed launch failed and fell back to headless"
            await driver.close()
            return {"window_opened": False, "mode": "headless", "reason": str(reason)[:300]}
        live_browser.remember(session.id, driver)

    if url:
        show = getattr(driver, "show", None)
        if callable(show):
            try:
                await show(url)
            except DriverError as exc:
                # The window is open — the user can still navigate it themselves.
                log.warning("assisted: could not park the handoff window (%s)", str(exc)[:120])
            except Exception as exc:  # pragma: no cover - portal-side failure
                log.warning("handoff navigation failed (%s)", type(exc).__name__)
    return {"window_opened": True, "mode": "headed"}


def enqueue_pass(
    db: Session,
    *,
    user: User,
    session: ApplicationSession,
    trigger: str = "user",
    delay_seconds: int = 0,
) -> Dict[str, Any]:
    """
    Queue one assisted pass for this session.

    Deduped per ``(session, resumed_count)``: a second request while a pass is
    in flight is a no-op, but a resume — which increments ``resumed_count`` —
    legitimately queues a *new* pass that continues from the checkpoint.
    """
    from app.services.job_queue import enqueue_or_existing

    item, duplicate = enqueue_or_existing(
        db,
        user_id=session.user_id,
        pipeline=BROWSER_SESSION_PIPELINE,
        job_id=session.job_id,
        payload={"session_id": session.id, "trigger": trigger},
        dedupe_key=f"browsersession:{session.id}:{session.resumed_count}",
        priority=6,
        delay_seconds=delay_seconds,
    )
    inc("jobhunter_browser_session_passes_enqueued_total", duplicate=str(duplicate).lower())
    return {"queued": item is not None, "duplicate": duplicate,
            "item_id": item.id if item else None, "session_id": session.id}


def report_queue_progress(db: Session, item: Any, step: str, **extra: Any) -> None:
    """
    Publish a ``QUEUE_PROGRESS_STEPS["browser_session"]`` step onto the item.

    The UI renders a stepper from ``payload.progress.step``; the step names are
    the contract, so a handler never invents one. Nothing but a step name, a
    timestamp and counters ever lands here — never a page's content.
    """
    payload = dict(item.payload or {})
    payload["progress"] = {"step": step, "at": datetime.utcnow().isoformat(), **extra}
    item.payload = payload
    db.commit()


#: Queue-result → the bounded ``outcome`` label for the pass-duration histogram.
def _pass_outcome(result: Dict[str, Any]) -> str:
    payload = result if isinstance(result, dict) else {}
    if payload.get("noop"):
        return "noop"
    if payload.get("pause") or payload.get("status") in ("awaiting_user", "paused"):
        return "awaiting_user"
    if payload.get("status") in ("error", "failed"):
        return "failed"
    return "completed"


async def run_queued_pass(
    db: Session,
    item: Any,
    *,
    driver: Optional[BrowserDriver] = None,
) -> Dict[str, Any]:
    """
    Queue handler for one assist pass, timed on every path.

    A browser pass is the slowest and least predictable thing the worker does —
    a portal that renders slowly is indistinguishable from a hung driver unless
    the duration of the *failed* and *no-op* runs is recorded too. Both are.
    """
    started = time.perf_counter()
    outcome = "failed"
    try:
        result = await _run_queued_pass(db, item, driver=driver)
        outcome = _pass_outcome(result)
        return result
    finally:
        duration("jobhunter_browser_session_passes_seconds", time.perf_counter() - started,
                 outcome=outcome)


async def _run_queued_pass(
    db: Session,
    item: Any,
    *,
    driver: Optional[BrowserDriver] = None,
) -> Dict[str, Any]:
    """
    Pipeline handler body for :data:`BROWSER_SESSION_PIPELINE`.

    Refuses to open a browser while the session is terminal, expired or still
    waiting on the user: the pass is a no-op that reports *why*, never a retry
    that could type over a page the human is working in.
    """
    user = db.query(User).filter(User.id == item.user_id).first()
    session_id = int((item.payload or {}).get("session_id") or 0)
    session = (db.query(ApplicationSession)
               .filter(ApplicationSession.id == session_id,
                       ApplicationSession.user_id == item.user_id)
               .first())
    if user is None or session is None:
        return {"status": "not_found", "session_id": session_id}

    if session.state in sessions.APPLICATION_SESSION_TERMINAL_STATES:
        return {"status": session.state, "session_id": session.id, "noop": "terminal"}
    if sessions.is_expired(session):
        sessions.expire_session(db, session, reason="pass_after_expiry")
        return {"status": session.state, "session_id": session.id,
                "requires_reauthentication": True, "noop": "expired"}
    open_actions = sessions.pending_actions(db, session.user_id, session_id=session.id)
    if open_actions:
        return {
            "status": session.state,
            "session_id": session.id,
            "noop": "awaiting_user",
            "pause": session.pause_kind or (open_actions[0].kind if open_actions else ""),
            "action_ids": [action.id for action in open_actions],
        }

    job = db.query(Job).filter(Job.id == session.job_id, Job.user_id == user.id).first()
    if job is None:
        return {"status": "not_found", "session_id": session.id, "noop": "job_missing"}

    policy = sessions.effective_policy(db, user.id)
    if driver is None:
        # A window a previous pass (or a handoff) left open for the human is
        # the best driver there is: it is the context they just signed in to.
        driver = live_browser.adopt(session.id)
    active_driver = driver or build_driver(db, session, job, user=user, policy=policy)
    if active_driver is None:
        report_queue_progress(db, item, "done", detail="assisted_apply_unavailable")
        return {"status": "unavailable", "session_id": session.id,
                "noop": "assisted_apply_unavailable",
                "reason": assisted_apply_available().get("reason") or "assisted_apply_disabled"}
    report_queue_progress(db, item, "opening", session_state=session.state)
    try:
        result = await run_pass(db, user=user, session=session, driver=active_driver, policy=policy)
    except DriverError as exc:
        report_queue_progress(db, item, "done", detail=f"driver_error:{type(exc).__name__}")
        raise
    step = "awaiting_user" if result.get("pause") or session.state in (
        "awaiting_user", "paused") else "done"
    report_queue_progress(db, item, step, pause=result.get("pause"),
                          filled=len(result.get("filled") or []))
    return result


def _password_field_names(observation: Mapping[str, Any]) -> List[str]:
    """Password controls on this page, in order, names only."""
    names: List[str] = []
    for field in observation.get("fields") or []:
        if not isinstance(field, Mapping):
            continue
        if str(field.get("type") or "").lower() != "password":
            continue
        name = str(field.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _portal_credential(db: Session, *, session: ApplicationSession, job: Job,
                       observation: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """
    The vault credential for the portal this page belongs to.

    Reused when the domain already has one, created when the user's settings
    allow it (``application.auto_create_credentials``, default on) — either way
    the entry lands in the user's vault, so the existing Chrome/Apple CSV
    export already covers it. Returns ``None`` when there is nothing to type
    with: no readable host, nothing stored and creation refused, or an
    undecryptable vault (the caller then keeps the login pause — never a
    partial or empty password). The password travels only in the returned
    dict, in memory for the single fill that follows; it is never logged.
    """
    from app.services.events import record_job_event
    from app.services.vault import (
        VaultDecryptionError,
        credential_for_application,
        get_vault_entry_for_domain,
        reveal_password,
    )

    host = (str(observation.get("host") or "").strip()
            or sessions.host_of(str(observation.get("url") or ""))
            or sessions.host_of(job.url or ""))
    if not host:
        return None
    domain = host.lower().rstrip(".")
    allow_create = bool(get_setting(db, session.user_id, "application", "auto_create_credentials", True))
    try:
        if allow_create:
            credential, created = credential_for_application(
                db, user_id=session.user_id, domain=domain, company=job.company or "")
        else:
            entry = get_vault_entry_for_domain(db, session.user_id, domain)
            if entry is None:
                return None
            credential = {"username": entry.username, "password": reveal_password(entry, db=db),
                          "domain": domain}
            created = False
    except VaultDecryptionError:
        # The stored password cannot be read — an empty string here would type
        # garbage into a sign-up. The login pause is the honest outcome.
        log.warning("vault credential for '%s' unreadable — keeping the login pause", domain)
        return None
    if created:
        # Journal evidence that an account credential was made — domain only,
        # never the username and certainly never the password.
        record_job_event(db, user_id=session.user_id, job_id=job.id, stage="credential_created",
                         status="info",
                         message=f"Portal credential created in your vault for {domain}",
                         meta={"domain": domain, "origin": "assisted_session"})
    if not credential or not str(credential.get("password") or ""):
        return None
    return {"password": str(credential["password"]), "created": bool(created), "domain": domain}


async def run_pass(
    db: Session,
    *,
    user: User,
    session: ApplicationSession,
    driver: BrowserDriver,
    policy: Optional[sessions.SessionPolicy] = None,
    open_driver: bool = True,
    max_steps: int = MAX_PASS_STEPS,
    max_pages: int = MAX_FLOW_PAGES,
) -> Dict[str, Any]:
    """
    One assisted pass: open → observe → (fill → advance → observe)* → stop.

    The unit of an assisted pass is a *step of the application flow*, not a
    single page. A real posting is `landing page → account sign-up → profile →
    questions → review → submit`; an assistant that only ever looked at the first
    document, typed nothing, and reported "completed" is the bug this loop fixes.

    Rules, in order of importance:

    1. **Nothing is claimed that did not happen.** The pass ends in one of three
       honest ways: the portal confirmed the application (:func:`_portal_confirmed`),
       the human has a step to take (a pause — a first-class product outcome), or
       the flow could not be advanced (a ``review_required`` pause saying why).
       A session is *never* marked ``completed`` for merely having nothing left
       to type.
    2. **A pause is the product.** Sign-in, MFA, a bot check, an unknown field —
       the run stops there, the queue row says which, and the resume path
       continues from the checkpoint without retyping anything.
    3. **The flow moves on by the portal's own controls.** :func:`choose_advance_control`
       decides which "Apply"/"Continue" is safe; a submit-looking control is only
       ever clicked when the session policy allows submission, and the at-most-once
       ledger still owns the act of submitting.
    4. **Everything is journalled.** Each page, fill, advance, pause and
       confirmation lands in the session's flow journal (structure only), so the
       user can read afterwards what the run actually did.

    Re-running it after the user acts resumes from the checkpoint, so nothing
    already typed is typed again.
    """
    policy = policy or sessions.effective_policy(db, user.id)
    if sessions.is_expired(session):
        sessions.expire_session(db, session, reason="pass_after_expiry")
        raise sessions.SessionExpired(session)

    job = db.query(Job).filter(Job.id == session.job_id).first()
    if job is None:
        raise sessions.SessionError("job_missing", "the job this session belongs to no longer exists",
                                    status_code=404)

    values = build_instruction_values(job, session, answers=sessions.session_values(session))
    result: Dict[str, Any] = {
        "session_id": session.id, "steps": [], "filled": [], "pause": None,
        "status": session.state, "pages": 0, "advanced": [], "confirmed": "",
        "next_step": None, "browser": None,
    }
    expect_host = session.expected_host or sessions.host_of(session.last_observation.get("url") or "")
    last_url = str((session.last_observation or {}).get("url") or "")
    try:
        if open_driver:
            await driver.open(session)
            if session.state in ("created", "launching"):
                sessions.transition(session, "preparing", actor="system_worker", reason="browser_opened")
                db.commit()
            launch = getattr(driver, "launch_report", None)
            if launch:
                result["browser"] = dict(launch)
                sessions.record_flow_step(db, session, "page", reason="browser_opened",
                                          mode=str(launch.get("mode") or "")[:20])
        for step in range(max_steps):
            observation = await driver.observe()
            # Account creation (opt-in): when the policy allows it and this
            # page actually asks for a password, pull the portal's credential
            # from the vault — creating it on first sight — and hand the
            # password to this step's classification only. It lives in this
            # local ``values`` dict for the fill and nowhere else.
            allow_credentials = False
            password_names = _password_field_names(observation) if policy.create_accounts else []
            if password_names:
                credential = _portal_credential(db, session=session, job=job, observation=observation)
                if credential:
                    for field_name in password_names:
                        values[field_name] = credential["password"]
                    allow_credentials = True
            outcome = sessions.record_observation(db, session, observation, user=user, values=values,
                                                  actor="system_worker",
                                                  allow_credentials=allow_credentials)
            progress = outcome.get("progress") or {}
            host = str(observation.get("host") or "") or expect_host
            url_now = str(observation.get("url") or "")
            result["steps"].append({"step": step, "status": outcome["status"], "host": host,
                                    "progress": progress})
            if url_now != last_url:
                # "Pages" counts documents visited, not observations taken: a
                # second look at the same page (after filling it, say) is not a
                # new step of the flow — and the journal says "page" once per
                # document for the same reason.
                result["pages"] += 1
                last_url = url_now
                sessions.record_flow_step(
                    db, session, "page", step=step, host=host,
                    url=str(observation.get("url") or ""), title=str(observation.get("title") or ""),
                    fields_total=progress.get("fields_total"), filled_total=progress.get("filled"),
                    reason=str(observation.get("steps_total") and
                               f"{observation.get('step_index') or 0}/{observation.get('steps_total')}" or ""))

            if not url_now and not (observation.get("fields") or []) \
                    and not (observation.get("markers") or []):
                # Nothing to identify, nothing on it: the document is not
                # readable (still navigating, or a blank frame). Advancing from
                # here would walk the pass onto ghost pages.
                result["status"] = session.state
                result["next_step"] = {"action": "browser", "reason": "page_not_readable"}
                sessions.record_flow_step(db, session, "blocked", reason="page_not_readable")
                break

            confirmation = str(observation.get("confirmation") or "")
            if confirmation in ("submitted", "received"):
                # The portal itself says the application arrived. This is the
                # only page-level evidence that lets a pass close the session.
                result["confirmed"] = confirmation
                sessions.record_flow_step(db, session, "confirmed", reason=confirmation, host=host)
                break

            # Fill first, then honour the pause: a sign-in page legitimately
            # carries the profile fields of the sign-up beside it, and refusing to
            # type them would only make the user retype data we already hold.
            # (An *answer* pause — unknown/ambiguous/legal — clears nothing, so
            # nothing is typed on it.)
            instructions = sessions.fill_instructions(session, observations=[observation], values=values,
                                                      allow_credentials=allow_credentials)
            filled_here: List[str] = []
            if instructions:
                recorded = await driver.fill(instructions)
                filled = [row for row in recorded if row.get("status") == "filled"]
                filled_here = [str(row["name"]) for row in filled]
                by_name = {str(i["name"]): i for i in instructions}
                sessions.record_fills(db, session, [
                    {"name": row["name"],
                     "value": by_name.get(str(row["name"]), {}).get("value", ""),
                     "classification": by_name.get(str(row["name"]), {}).get("classification", "canonical"),
                     "profile_key": by_name.get(str(row["name"]), {}).get("profile_key")}
                    for row in filled
                ], journal=False)
                result["filled"].extend(str(row["name"]) for row in filled)
                result["steps"].append({"step": step, "status": "filled",
                                        "filled": [row["name"] for row in filled],
                                        "failed": [f"{row['name']}:{row.get('status')}" for row in recorded
                                                   if row.get("status") != "filled"]})
                if filled:
                    sessions.record_flow_step(db, session, "fill", step=step, host=host,
                                              fields=[str(row["name"]) for row in filled])
                    values = build_instruction_values(job, session,
                                                      answers=sessions.session_values(session), db=db)
                    if policy.persist_state and isinstance(driver, PlaywrightDriver):
                        state = await driver.export_storage_state()
                        if state:
                            sessions.persist_storage_state(db, session, state, policy=policy)
                if not filled_here:
                    held = [row for row in recorded
                            if row.get("status") == "skipped_value_present"]
                    if len(held) == len(recorded):
                        # Everything the plan still wanted here is content the
                        # page already holds — the human typed it (a password
                        # they entered themselves, data a browser filled).
                        # Nothing is broken; there is simply nothing left for
                        # us to type, so the flow may advance below.
                        result["steps"].append({"step": step, "status": "held",
                                                "held": [str(row["name"]) for row in held]})
                    else:
                        # The classifier cleared fields and the driver typed none of
                        # them: that is a broken pass, not a finished one.
                        result["status"] = session.state
                        sessions.record_flow_step(db, session, "blocked", reason="no_field_could_be_filled",
                                                  host=host)
                        result["next_step"] = {"action": "browser",
                                               "reason": "no_field_could_be_filled"}
                        break

            if outcome.get("pause"):
                result["pause"] = outcome["pause"]
                result["next_step"] = _pause_next_step(session, outcome)
                sessions.record_flow_step(db, session, "pause", kind=outcome["pause"],
                                          reason=str(session.pause_reason or ""), host=host)
                break

            if filled_here:
                # Something was just typed: look at the page again before
                # deciding anything. Portals commonly reveal the next step (or a
                # validation error) in place after the last field of a page.
                continue

            # Nothing left to type on this page: move the flow on, or stop.
            decision = choose_advance_control(
                (await _driver_controls(driver)).get("controls") or [],
                allow_submit=policy.allow_submit,
                current_host=str(observation.get("host") or ""),
                expected_host=expect_host,
            )
            if not decision.get("click"):
                result["status"] = session.state
                result["next_step"] = {"action": "browser", "reason": str(decision.get("reason") or "")}
                sessions.record_flow_step(db, session, "blocked", reason=str(decision.get("reason") or ""),
                                          host=host)
                break
            if result["pages"] >= max_pages:
                result["next_step"] = {"action": "browser", "reason": "page_budget_reached"}
                sessions.record_flow_step(db, session, "blocked", reason="page_budget_reached", host=host)
                break
            if decision.get("kind") == "submit":
                # A submission is the API's decision (policy + ledger), never a
                # side effect of "advancing".
                result["next_step"] = {"action": "submit", "reason": "submit_control_present",
                                       "control": str(decision.get("label") or "")[:120]}
                sessions.record_flow_step(db, session, "blocked", reason="submit_requires_reservation",
                                          host=host, control=str(decision.get("label") or ""))
                break
            moved = await driver.advance(decision)
            result["steps"].append({"step": step, "status": "advanced", **dict(moved)})
            if moved.get("advanced"):
                result["advanced"].append(str(moved.get("label") or ""))
                sessions.record_flow_step(
                    db, session, "advance", step=step, host=host,
                    control=str(moved.get("label") or ""), kind=str(moved.get("kind") or ""),
                    reason=("moved" if moved.get("changed") else "unconfirmed"))
                if not moved.get("changed"):
                    result["next_step"] = {"action": "browser", "reason": "advance_unconfirmed"}
                    sessions.record_flow_step(db, session, "blocked", reason="advance_unconfirmed",
                                              host=host)
                    break
                new_host = sessions.host_of(str(moved.get("url") or ""))
                if new_host and expect_host and not _host_in_family(new_host, expect_host, host):
                    result["next_step"] = {"action": "handoff", "reason": "left_application_host",
                                           "host": new_host}
                    sessions.record_flow_step(db, session, "blocked", reason="left_application_host",
                                              host=new_host)
                    break
            else:
                sessions.record_flow_step(db, session, "blocked", reason=str(moved.get("reason") or ""),
                                          host=host)
                result["next_step"] = {"action": "browser", "reason": str(moved.get("reason") or "")}
                break
    finally:
        if open_driver:
            failed = sys.exc_info()[1] is not None
            # A pass that died mid-run must not yank away a window the human
            # may be typing in — only a window *this pass* opened gets closed
            # on failure. A clean end closes whatever has no human step left.
            keep = _keep_window_open(session, result, driver) or (
                failed and live_browser.holds(session.id, driver))
            if keep:
                # The human's step starts where this pass ended: the window
                # stays (headed mode only) so they can sign in, solve the
                # check or press Submit in the very context we continue in.
                live_browser.remember(session.id, driver)
            else:
                live_browser.forget(session.id)
                await driver.close()

    result["status"] = session.state
    result = await _settle_pass(db, user=user, session=session, result=result, policy=policy)
    result["completed_fields"] = sessions.already_completed_fields(session)
    result["progress"] = session.progress or {}
    result["flow"] = sessions.flow_progress(session)
    inc("jobhunter_application_session_passes_total", result=str(result.get("outcome") or result["status"]))
    return result


def _keep_window_open(session: ApplicationSession, result: Mapping[str, Any],
                      driver: BrowserDriver) -> bool:
    """
    Whether this pass's window should outlive the pass.

    Three requirements, all of them honest:

    * the window is **headed** — a headless browser has nothing a human can
      watch or take over, so it keeps the old close-at-once behaviour;
    * the session still has a human step in front of it — a pause, or a next
      step that happens in the browser (handoff, review, Submit) — and the
      portal has not confirmed anything (a confirmed session closes);
    * the session itself is not terminal.
    """
    if session.state in sessions.APPLICATION_SESSION_TERMINAL_STATES:
        return False
    if result.get("confirmed"):
        return False
    launch = getattr(driver, "launch_report", None) or {}
    if str(launch.get("mode") or "") != "headed":
        return False
    if result.get("pause"):
        return True
    next_step = result.get("next_step") or {}
    return str(next_step.get("action") or "") in ("handoff", "browser", "submit")


def _host_in_family(new_host: str, *known: str) -> bool:
    """Whether a host is the application's own (its portal domain, the job
    board, or a sibling host in the same ATS family — Lever/Auth-Lever,
    Workday/Workday-IDP, etc.)."""
    if not new_host:
        return True
    for candidate in known:
        if candidate and (net_guard.host_matches(new_host, candidate)
                          or hosts_in_same_ats_family(new_host, candidate)):
            return True
    for ats in VAULT_DOMAINS.values():
        if net_guard.host_matches(new_host, ats) or hosts_in_same_ats_family(new_host, ats):
            return True
    return False


async def _driver_controls(driver: BrowserDriver) -> Dict[str, Any]:
    """``controls()`` on a driver that has it, empty structure otherwise."""
    controls = getattr(driver, "controls", None)
    if controls is None:
        return {"controls": []}
    try:
        return await controls()
    except Exception as exc:
        log.debug("assisted controls failed: %s", type(exc).__name__)
        return {"controls": []}


def _pause_next_step(session: ApplicationSession, outcome: Mapping[str, Any]) -> Dict[str, Any]:
    """What the user has to do, phrased as an action the UI can render."""
    kind = str(outcome.get("pause") or session.pause_kind or "")
    return {
        "action": "handoff" if kind in sessions.HANDOFF_ACTION_KINDS else "answer",
        "reason": kind,
        "action_id": outcome.get("action_id"),
        "requires_browser_handoff": kind in sessions.BROWSER_STEP_KINDS,
        "in_browser": True,
    }


async def _settle_pass(
    db: Session,
    *,
    user: User,
    session: ApplicationSession,
    result: Dict[str, Any],
    policy: sessions.SessionPolicy,
) -> Dict[str, Any]:
    """
    Decide — honestly — what a finished pass means.

    * the portal confirmed the application → ``completed`` (``portal_confirmed``);
    * the run paused for a human → ``awaiting_user``, already transitioned;
    * a submission control is waiting and the policy allows submission →
      ``active``, with ``next_step.action = "submit"`` (the API reserves it);
    * anything else → a ``review_required`` pause that says, in the queue, why
      the automation stopped. **Never** ``completed``.
    """
    if result.get("pause"):
        result["outcome"] = "awaiting_user"
        # Kind *and* machine reason, so a client never has to re-read the session
        # row to explain the stop.
        result["pause_reason"] = str(session.pause_reason or "")
        return result
    if result.get("confirmed") in ("submitted", "received"):
        if session.state in ("preparing", "launching", "created"):
            sessions.transition(session, "active", actor="system_worker", reason="observation_received")
        sessions.transition(session, "completed", actor="system_worker",
                           reason=f"portal_confirmed_{result['confirmed']}")
        sessions.purge_session_values(db, session, reason="portal_confirmed")
        db.commit()
        result["status"] = session.state
        result["outcome"] = "completed"
        return result
    next_step = result.get("next_step") or {}
    if next_step.get("action") == "submit" and policy.allow_submit:
        # Nothing left to type and the gates allow submission: the caller (the
        # API's /submit, which owns the ledger) decides whether to use it.
        result["status"] = session.state
        result["outcome"] = "ready_to_submit"
        return result

    reason = {
        "submit_requires_reservation": "submit_requires_reservation",
        "no_control_found": "flow_cannot_continue_automatically",
        "flow_cannot_continue_automatically": "flow_cannot_continue_automatically",
        "no_field_could_be_filled": "no_field_could_be_filled",
        "advance_unconfirmed": "advance_unconfirmed",
        "off_host_control": "flow_leaves_the_application",
        "left_application_host": "flow_leaves_the_application",
        "page_budget_reached": "flow_page_budget_reached",
        "generic_label_on_link": "flow_cannot_continue_automatically",
        "submit_not_allowed": "waiting_for_you_to_submit",
        "submit_control_present": "waiting_for_you_to_submit",
    }.get(str(next_step.get("reason") or ""), "flow_needs_a_human")
    instructions = _handoff_instructions_for_review(session, reason=reason, result=result)
    if session.state in ("preparing", "launching", "created"):
        sessions.transition(session, "active", actor="system_worker", reason="observation_received")
    sessions.pause_session(
        db, session, kind="review_required", reason=reason,
        instructions=instructions, actor="system_worker",
        checkpoint=session.checkpoint,
    )
    sessions.record_flow_step(db, session, "pause", kind="review_required", reason=reason)
    action = sessions.pending_actions(db, session.user_id, session_id=session.id)
    result["status"] = session.state
    result["outcome"] = "awaiting_user"
    result["pause"] = "review_required"
    result["pause_reason"] = reason
    result["next_step"] = {
        "action": "handoff",
        "reason": reason,
        "requires_browser_handoff": True,
        "action_id": action[0].id if action else None,
        "in_browser": True,
    }
    return result


def _handoff_instructions_for_review(session: ApplicationSession, *, reason: str,
                                     result: Mapping[str, Any]) -> str:
    """The sentence the user reads in the queue item — specific, never generic."""
    filled = len(set(result.get("filled") or []))
    advanced = len(result.get("advanced") or [])
    pages = int(result.get("pages") or 0)
    done = (f"The assistant filled {filled} field(s) across {pages} page(s)"
            if filled else f"The assistant could not fill anything on the {pages} page(s) it opened")
    if advanced:
        done = f"{done} and moved the flow on {advanced} time(s)"
    why = {
        "waiting_for_you_to_submit": ("the page is filled and the final Submit is yours to press"
                                      if filled else "the final Submit is yours to press"),
        "flow_cannot_continue_automatically": ("the next step of this portal is not a form field "
                                              "(account creation, a login wall or a wizard the "
                                              "assistant must not guess its way through)"),
        "no_field_could_be_filled": "the fields on this page could not be matched to your profile",
        "submit_requires_reservation": "submitting requires an explicit reservation in the app",
        "flow_leaves_the_application": "the next step leaves this application's own domain",
        "flow_page_budget_reached": "the flow is longer than one pass may walk",
        "advance_unconfirmed": "a click did not change the page",
    }.get(reason, "the automation reached a step it is not allowed to take for you")
    return (f"{done}. It stopped because {why}. "
            f"Open the application in your browser to continue — nothing here was submitted."
            )[:600]


def run_pass_sync(db: Session, *, user: User, session: ApplicationSession, driver: BrowserDriver,
                  **kwargs: Any) -> Dict[str, Any]:
    """Synchronous entry point for queue handlers and scripts."""
    return asyncio.run(run_pass(db, user=user, session=session, driver=driver, **kwargs))


def plan_for_session(job: Job, session: ApplicationSession, *,
                     answers: Optional[Mapping[str, Any]] = None,
                     db: Optional[Session] = None) -> Dict[str, Any]:
    """
    A reviewable description of what this session would do — no browser needed.

    Used by the API's dry-run surface and by tests: it answers "which fields
    would be typed, which pause, and why" without opening anything. Values come
    from the session's own confirmed working set (plus, with a database handle,
    the user's learned answers for this host) unless the caller supplies them.
    """
    values = build_instruction_values(
        job, session,
        answers=sessions.session_values(session) if answers is None else answers,
        db=db)
    observation = session.last_observation or {}
    form = classify_form(observation.get("fields") or [], values=values,
                         checkpoint=session.checkpoint or {})
    return {
        "session_id": session.id,
        "state": session.state,
        "pause_kind": form.pause_kind,
        "must_pause": form.must_pause,
        "autofillable": [v.name for v in form.autofillable],
        "asking": [v.as_dict() for v in form.asking],
        "handoffs": [v.as_dict() for v in form.handoffs],
        "skipped": [v.name for v in form.skipping],
        "already_completed": sessions.already_completed_fields(session),
        "progress": form.summary(),
    }


__all__ = [
    "BROWSER_SESSION_PIPELINE",
    "BrowserDriver",
    "DriverError",
    "MAX_PASS_STEPS",
    "OBSERVE_SCRIPT",
    "PlaywrightDriver",
    "RECORD_INPUT_SCRIPT",
    "RecordingDriver",
    "build_driver",
    "build_instruction_values",
    "enqueue_pass",
    "open_handoff_window",
    "plan_for_session",
    "report_queue_progress",
    "run_queued_pass",
    "run_pass",
    "run_pass_sync",
]
