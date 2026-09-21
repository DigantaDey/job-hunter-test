"""
The autofill browser is an outbound client, so it is policed like one.

``execute_autofill`` used to call ``page.goto(job.url)`` with a URL that came
straight from a feed or an import, bypassing the SSRF guard the HTTP client is
built on: the worker's headless Chromium would load ``169.254.169.254`` (cloud
credentials), ``127.0.0.1:<port>`` (anything else in the container) or any
RFC1918 host — and then type the user's vault password into whatever
password-looking field that host rendered.

This suite covers the three gates that replaced that, with Playwright mocked
(no browser is launched and no DNS is resolved):

1. the outbound URL policy runs **before** ``page.goto`` — and before a browser
   is even launched — with loopback/link-local/metadata hard-blocked even when
   ``OUTBOUND_ALLOW_PRIVATE`` is on or the host is allow-listed;
2. the run is restricted to the domains the job's *own metadata* names, and a
   vault credential is typed only when the page's live host is one of them;
3. every selector decision is logged at DEBUG (names and selectors only), so a
   failed run says what matched and what did not — and never leaks a password.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import types
from typing import Any, Dict, List

import pytest

from app.core.config import settings
from app.services import net_guard
from app.services.autofill import credential_decision, domain_policy, execute_autofill

SECRET_PASSWORD = "s3cr3t-vault-password"
#: The vault login is deliberately *not* the profile email, so a test can tell
#: "typed the credential" from "filled the email field".
SECRET_USERNAME = "vault-login@example.invalid"
PROFILE_EMAIL = "applicant@example.com"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeLocator:
    def __init__(self, page, selector, count=1):
        self._page = page
        self._selector = selector
        self._count = count
        self.first = self

    async def count(self):
        return self._count

    async def fill(self, value, **kwargs):
        self._page.filled[self._selector] = str(value)

    async def set_input_files(self, value, **kwargs):
        self._page.filled[self._selector] = str(value)

    async def select_option(self, *args, **kwargs):
        self._page.filled[self._selector] = str(args[0])

    async def check(self):
        self._page.filled[self._selector] = "True"


class FakePage:
    """A page that reports where it *ended up*, so a redirect can be simulated."""

    #: 0 makes every locator match nothing — the "no selector matched" path.
    locator_count = 1

    def __init__(self, redirect_to: str = ""):
        self.goto_urls: List[str] = []
        self.filled: Dict[str, str] = {}
        self.clicks: List[str] = []
        self.redirect_to = redirect_to
        self.url = "about:blank"

    def set_default_timeout(self, timeout):
        pass

    async def goto(self, url, **kwargs):
        self.goto_urls.append(url)
        self.url = self.redirect_to or url

    def locator(self, selector):
        return FakeLocator(self, selector, count=self.locator_count)

    async def fill(self, selector, value, **kwargs):
        self.filled[selector] = str(value)

    async def click(self, selector, **kwargs):
        self.clicks.append(selector)

    async def wait_for_load_state(self, *args, **kwargs):
        pass

    async def screenshot(self, path=None, **kwargs):
        pass


class NoMatchPage(FakePage):
    locator_count = 0


def _install_fake_playwright(monkeypatch, page: FakePage) -> None:
    class FakeContext:
        async def new_page(self):
            return page

        async def close(self):
            pass

    class FakeBrowser:
        async def new_context(self, **kwargs):
            return FakeContext()

        async def close(self):
            pass

    class FakeChromium:
        async def launch(self, **kwargs):
            return FakeBrowser()

    class FakePlaywrightCM:
        def __init__(self):
            self.chromium = FakeChromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async_api = types.ModuleType("playwright.async_api")
    async_api.async_playwright = FakePlaywrightCM
    pw = types.ModuleType("playwright")
    pw.async_api = async_api
    monkeypatch.setitem(sys.modules, "playwright", pw)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)

    # The availability check looks for a real Chromium build on disk; this fake
    # browser never downloads one, so answer the lookup directly.
    from app.services import autofill as autofill_service

    monkeypatch.setattr(autofill_service, "_chromium_browser_path", lambda: "/fake/chromium/chrome")


@pytest.fixture()
def make_browser(monkeypatch):
    """
    Automation on, no network: build a fake page (optionally one that "redirects")
    and keep DNS hermetic — the pre-flight resolves the posting host.
    """
    monkeypatch.setattr(settings, "autofill_enabled", True)
    monkeypatch.setattr(settings, "autofill_dry_run", False)
    monkeypatch.setattr(settings, "autofill_allow_submit", True)
    monkeypatch.setattr(settings, "outbound_allowed_hosts", [], raising=False)
    monkeypatch.setattr(settings, "outbound_allow_private", False, raising=False)

    async def _public_resolver(*_args, **_kwargs):
        return ["93.184.216.34"]

    monkeypatch.setattr(net_guard, "_resolve", _public_resolver)
    net_guard.clear_dns_cache()

    def _make(redirect_to: str = "", *, page: FakePage = None) -> FakePage:
        fake = page if page is not None else FakePage(redirect_to=redirect_to)
        _install_fake_playwright(monkeypatch, fake)
        return fake

    return _make


@pytest.fixture()
def browser(make_browser) -> FakePage:
    return make_browser()


def _plan(**overrides) -> Dict[str, Any]:
    """A Lever posting that needs a login — the shape that carries a credential."""
    plan = {
        "portal_type": "lever",
        "requires_login": True,
        "vault_domain": "jobs.lever.co",
        "fields": [
            {"name": "email", "label": "Email", "type": "email", "required": True,
             "value": PROFILE_EMAIL, "value_source": "profile"},
            {"name": "password", "label": "Password", "type": "password", "required": True,
             "value": SECRET_PASSWORD, "value_source": "vault"},
        ],
    }
    plan.update(overrides)
    return plan


def _credential(domain: str = "jobs.lever.co") -> Dict[str, str]:
    return {"username": SECRET_USERNAME, "password": SECRET_PASSWORD, "domain": domain}


def _run(**kwargs) -> Dict[str, Any]:
    return asyncio.run(execute_autofill(**kwargs))


# --------------------------------------------------------------------------- #
# Gate 1 — the URL policy runs before page.goto
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url,code",
    [
        ("http://169.254.169.254/latest/meta-data/iam/security-credentials/", "link_local"),
        ("http://169.254.169.254/", "link_local"),
        ("http://[fd00:ec2::254]/latest/meta-data/", "unsafe_address"),
        ("http://127.0.0.1:8000/admin", "loopback"),
        ("http://127.0.0.1:9/", "loopback"),
        ("http://localhost:8080/", "unroutable_host"),
        ("http://[::1]/", "loopback"),
        ("http://10.0.0.5/", "unsafe_address"),
        ("http://192.168.1.10/router", "unsafe_address"),
        ("http://172.16.9.9/", "unsafe_address"),
        ("http://100.64.7.7/", "unsafe_address"),
        ("http://metadata.google.internal/computeMetadata/v1/", "metadata_host"),
        ("http://metadata/latest/meta-data/", "metadata_host"),
        ("http://redis.local/", "metadata_host"),
        ("http://example.com:6379/", "internal_port"),
        ("http://example.com:5432/", "internal_port"),
        ("file:///etc/passwd", "scheme"),
        ("gopher://example.com/", "scheme"),
        ("http://user:password@example.com/", "credentials_in_url"),
        ("", "empty_url"),
    ],
)
def test_goto_is_refused_for_internal_and_metadata_targets(browser, url, code):
    """A job URL is attacker-influenced data: it never reaches the browser unchecked."""
    result = _run(url=url, plan=_plan(requires_login=False), credential=_credential())

    assert result["status"] == "blocked", result
    assert result["blocked_by"] == code, result
    assert browser.goto_urls == [], "page.goto must not be reached for a refused URL"
    assert result["filled"] == 0
    assert result["submitted"] is False
    assert result["login"]["injected"] is False
    assert browser.filled == {}, "nothing at all is typed into a refused page"


def test_metadata_ip_is_refused_even_when_it_arrives_via_dns(browser, monkeypatch):
    """A DNS answer pointing at the metadata range blocks the navigation too."""

    async def _metadata_resolver(*_args, **_kwargs):
        return ["169.254.169.254"]

    monkeypatch.setattr(net_guard, "_resolve", _metadata_resolver)
    net_guard.clear_dns_cache()

    result = _run(url="https://sneaky-jobs.example.com/apply", plan=_plan(requires_login=False))
    assert result["status"] == "blocked"
    assert result["blocked_by"] == "unsafe_address"
    assert browser.goto_urls == []


def test_outbound_allow_private_does_not_relax_the_browser(browser, monkeypatch):
    """
    ``OUTBOUND_ALLOW_PRIVATE`` is an HTTP-client escape hatch only.

    A headless browser that renders an internal service and can be fed a
    password is a different risk class, so the flag does not reach it.
    """
    monkeypatch.setattr(settings, "outbound_allow_private", True, raising=False)

    # The HTTP policy accepts it (documented operator risk)…
    asyncio.run(net_guard.check_url("http://10.0.0.5/internal-jobs"))
    # …the browser policy does not.
    result = _run(url="http://10.0.0.5/internal-jobs", plan=_plan(requires_login=False))
    assert result["status"] == "blocked"
    assert browser.goto_urls == []


def test_allow_listed_private_host_may_be_navigated(browser, monkeypatch):
    """An operator can still name an internal ATS explicitly (air-gapped deploys)."""
    monkeypatch.setattr(settings, "outbound_allowed_hosts", ["10.10.0.5"], raising=False)

    result = _run(url="http://10.10.0.5/apply", plan=_plan(requires_login=False))
    assert result["status"] != "blocked", result
    assert browser.goto_urls == ["http://10.10.0.5/apply"]
    assert result["navigation"]["allowlisted"] is True


def test_loopback_stays_blocked_even_when_allow_listed(browser, monkeypatch):
    """The hard block wins over the allow-list: nothing legit lives on 127.0.0.1."""
    monkeypatch.setattr(settings, "outbound_allowed_hosts", ["127.0.0.1"], raising=False)

    result = _run(url="http://127.0.0.1:8000/apply", plan=_plan(requires_login=False))
    assert result["status"] == "blocked"
    assert result["blocked_by"] == "loopback"
    assert browser.goto_urls == []


# --------------------------------------------------------------------------- #
# Gate 2 — the job's own metadata decides where the run may happen
# --------------------------------------------------------------------------- #
def test_posting_outside_the_company_domain_is_refused(browser):
    """The company website on record is independent evidence; the URL is not."""
    result = _run(url="https://jobs.lever.co/acme/1", plan=_plan(),
                  credential=_credential(), expected_domains=["https://careers.acme.com/"])

    assert result["status"] == "blocked"
    assert result["blocked_by"] == "domain_mismatch"
    assert browser.goto_urls == []
    assert "careers.acme.com" in result["reason"]


def test_posting_on_the_company_domain_runs(browser):
    result = _run(url="https://careers.acme.com/apply/7",
                  plan=_plan(portal_type="custom", vault_domain=""),
                  expected_domains=["careers.acme.com"])
    assert result["status"] != "blocked", result
    assert browser.goto_urls == ["https://careers.acme.com/apply/7"]
    assert result["navigation"]["enforced"] == ["careers.acme.com"]


def test_login_requiring_form_on_a_foreign_host_is_refused_before_navigation(browser):
    """
    "This is a Workday posting" + a URL that is not Workday = the confused
    deputy. It is refused before the browser is launched.
    """
    result = _run(url="https://careers.acme.com/apply",
                  plan=_plan(portal_type="workday", vault_domain="myworkdayjobs.com"))
    assert result["status"] == "blocked"
    assert result["blocked_by"] == "login_domain_mismatch"
    assert browser.goto_urls == []


def test_redirect_off_the_posting_domain_stops_the_run(make_browser):
    """The posting host was fine; the page sent the browser somewhere else."""
    page = make_browser(redirect_to="https://evil.example.net/collect")

    result = _run(url="https://jobs.lever.co/acme/1", plan=_plan(), credential=_credential(),
                  allow_submit=True)

    assert result["status"] == "blocked"
    assert result["blocked_by"] == "redirect_offsite"
    assert page.goto_urls == ["https://jobs.lever.co/acme/1"]
    assert page.filled == {}, "neither profile data nor a credential is typed on the redirect target"
    assert result["login"]["injected"] is False
    assert result["submitted"] is False
    assert SECRET_PASSWORD not in page.filled.values()


def test_lookalike_login_host_gets_no_credential(make_browser):
    """``jobs.lever.evil.example`` is not ``jobs.lever.co``, however it reads."""
    page = make_browser(redirect_to="https://jobs.lever.evil.example/login")

    result = _run(url="https://jobs.lever.co/acme/1", plan=_plan(), credential=_credential(),
                  allow_submit=True)

    assert result["login"]["injected"] is False
    assert SECRET_PASSWORD not in page.filled.values()
    assert SECRET_USERNAME not in page.filled.values()
    assert result["submitted"] is False


def test_redirect_inside_the_same_organisation_is_tolerated(make_browser, monkeypatch):
    """boards.greenhouse.io → job-boards.greenhouse.io is the ATS's own redirect."""
    page = make_browser(redirect_to="https://job-boards.greenhouse.io/acme")
    monkeypatch.setattr(settings, "autofill_dry_run", True)

    result = _run(url="https://boards.greenhouse.io/acme",
                  plan=_plan(portal_type="greenhouse", vault_domain="boards.greenhouse.io",
                             requires_login=False,
                             fields=[{"name": "email", "label": "Email", "type": "email",
                                      "required": True, "value": PROFILE_EMAIL,
                                      "value_source": "profile"}]))
    assert result["status"] == "dry_run", result
    assert page.goto_urls == ["https://boards.greenhouse.io/acme"]
    assert result["filled"] == 1


# --------------------------------------------------------------------------- #
# Gate 3 — a vault credential only goes to the application domain
# --------------------------------------------------------------------------- #
def test_credentials_are_injected_on_the_application_domain(browser):
    result = _run(url="https://jobs.lever.co/acme/1", plan=_plan(), credential=_credential(),
                  allow_submit=True)

    assert result["login"]["attempted"] is True
    assert result["login"]["injected"] is True
    assert result["login"]["reason"] == "login_submitted"
    assert browser.filled.get("input[type=password]") == SECRET_PASSWORD
    assert "button[type=submit]" in browser.clicks
    assert result["status"] == "submitted"


def test_unverified_domain_never_receives_a_credential(browser):
    """
    No verifiable application domain (a custom portal) → the browser fills the
    form but never types the password, neither through the login form nor
    through the plan's ``password`` field.
    """
    plan = _plan(portal_type="custom", vault_domain="", requires_login=True)
    result = _run(url="https://careers.acme.com/apply", plan=plan,
                  credential=_credential("careers.acme.com"))

    assert result["status"] != "blocked", result
    assert result["login"]["attempted"] is True
    assert result["login"]["injected"] is False
    assert result["login"]["reason"] == "no_verified_application_domain"
    assert "password:credential_host_mismatch" in result["skipped_fields"]
    assert SECRET_PASSWORD not in browser.filled.values()
    assert result["filled"] == 1


def test_credential_issued_for_another_domain_is_not_typed(browser):
    """The vault entry's own domain must match the page host as well."""
    plan = _plan(requires_login=True,
                 fields=[{"name": "email", "label": "Email", "type": "email", "required": True,
                          "value": PROFILE_EMAIL, "value_source": "profile"}])
    result = _run(url="https://jobs.lever.co/acme/1", plan=plan,
                  credential=_credential("boards.greenhouse.io"))

    assert result["login"]["attempted"] is True
    assert result["login"]["injected"] is False
    assert result["login"]["reason"] == "credential_domain_mismatch"
    assert SECRET_PASSWORD not in browser.filled.values()
    assert SECRET_USERNAME not in browser.filled.values()


@pytest.mark.parametrize(
    "host,application,credential_domain,expected,reason",
    [
        ("jobs.lever.co", ["jobs.lever.co"], "jobs.lever.co", True, "host_matches_application_domain"),
        ("sub.jobs.lever.co", ["jobs.lever.co"], "jobs.lever.co", True, "host_matches_application_domain"),
        ("jobs.lever.co.evil.net", ["jobs.lever.co"], "jobs.lever.co", False, "login_host_mismatch"),
        ("evil.example.net", ["jobs.lever.co"], "jobs.lever.co", False, "login_host_mismatch"),
        ("careers.acme.com", [], "careers.acme.com", False, "no_verified_application_domain"),
        ("careers.acme.com", ["careers.acme.com"], "jobs.lever.co", False, "credential_domain_mismatch"),
        ("careers.acme.com", ["careers.acme.com"], "", True, "host_matches_application_domain"),
    ],
)
def test_credential_decision_is_pure_and_deny_first(host, application, credential_domain,
                                                    expected, reason):
    policy = domain_policy(expected_domains=application)
    decision = credential_decision(host=host, policy=policy,
                                   credential={"username": "u", "password": "p",
                                               "domain": credential_domain})
    assert decision["inject"] is expected
    assert decision["reason"] == reason


def test_credential_decision_without_a_credential():
    decision = credential_decision(host="jobs.lever.co",
                                   policy=domain_policy(expected_domains=["jobs.lever.co"]),
                                   credential=None)
    assert decision == {"inject": False, "reason": "no_credential"}


def test_the_posting_host_never_defines_its_own_policy():
    """
    For a custom portal ``vault_domain`` is just the posting host echoed back, so
    it is circular evidence and must not enable credential injection.
    """
    policy = domain_policy(plan={"portal_type": "custom", "vault_domain": "evil.example.net"})
    assert policy.application == []
    assert policy.as_dict()["state"] == "unverified"
    assert policy.allows_credentials("evil.example.net") is False


def test_known_ats_portal_domain_is_trusted():
    policy = domain_policy(plan={"portal_type": "greenhouse", "vault_domain": "boards.greenhouse.io"})
    assert policy.application == ["boards.greenhouse.io"]
    assert policy.allows_credentials("boards.greenhouse.io") is True
    assert policy.allows_credentials("evil.example.net") is False


# --------------------------------------------------------------------------- #
# Problem C — failures are diagnosable
# --------------------------------------------------------------------------- #
def test_diagnostics_and_debug_logs_name_the_selector_that_matched(browser, caplog):
    plan = _plan(requires_login=False,
                 fields=[
                     {"name": "email", "label": "Email", "type": "email", "required": True,
                      "value": PROFILE_EMAIL, "value_source": "profile"},
                     {"name": "missing_field", "label": "Portfolio", "type": "text",
                      "required": False, "value": "", "value_source": "unknown"},
                 ])
    with caplog.at_level(logging.DEBUG, logger="app.autofill"):
        result = _run(url="https://jobs.lever.co/acme/1", plan=plan)

    steps = {d["step"] for d in result["diagnostics"]}
    assert "goto" in steps and "field" in steps
    email = next(d for d in result["diagnostics"] if d.get("field") == "email")
    assert email["selector"] == '[name="email"]'
    assert email["outcome"] == "matched"
    assert {"field", "outcome"} <= set(next(d for d in result["diagnostics"]
                                            if d.get("field") == "missing_field"))

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert '[name="email"]' in logged, "the matched selector must be in the debug log"
    assert "matched" in logged


def test_no_secret_ever_reaches_the_logs(browser, caplog):
    """Debug output names selectors; it never names values, and never a password."""
    with caplog.at_level(logging.DEBUG, logger="app.autofill"):
        _run(url="https://jobs.lever.co/acme/1", plan=_plan(), credential=_credential(),
             allow_submit=True)

    assert caplog.records, "the run is expected to log its selector decisions at DEBUG"
    for record in caplog.records:
        message = record.getMessage()
        assert SECRET_PASSWORD not in message, f"password leaked into a log line: {message}"
        assert SECRET_USERNAME not in message, f"username leaked into a log line: {message}"


def test_unmatched_selectors_are_reported_per_field(make_browser, monkeypatch):
    """A field nobody could match says so, with the selectors it tried."""
    page = make_browser(page=NoMatchPage())
    monkeypatch.setattr(settings, "autofill_dry_run", True)

    result = _run(url="https://jobs.lever.co/acme/1",
                  plan=_plan(requires_login=False,
                             fields=[{"name": "portfolio", "label": "Portfolio", "type": "text",
                                      "required": True, "value": "https://me.dev",
                                      "value_source": "profile"}]))

    assert result["filled"] == 0
    assert result["skipped_fields"] == ["portfolio:LookupError"]
    unmatched = [d for d in result["diagnostics"] if d.get("outcome") == "no_match"]
    assert {d["selector"] for d in unmatched} == {'[name="portfolio"]', "#portfolio"}
