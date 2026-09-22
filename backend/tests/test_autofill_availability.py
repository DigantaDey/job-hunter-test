"""
Assisted Apply's availability check must name the thing that is missing.

Three failure modes, three different fixes: the pip package is absent, the
Chromium download is absent — the state right after a bare ``pip install
playwright`` — or the operator flag is off. The old check only answered the
first one, so a bare install reported ``available=true`` and the first pass died
on a Playwright ``DriverError`` ("Executable doesn't exist…").

Every test is hermetic: ``PLAYWRIGHT_BROWSERS_PATH`` points at a temp directory
and ``sys.modules["playwright"]`` is a stand-in, so the suite answers the same
way with and without the optional extra installed.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import autofill


@pytest.fixture(autouse=True)
def _no_inherited_browser_cache(monkeypatch):
    """Never read the host's (or CI's) real browser cache."""
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)


def _install_fake_playwright(monkeypatch) -> None:
    """Make ``import playwright`` succeed without the optional extra installed."""
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))


def _make_browser_build(tmp_path: Path, build: str, executable: str) -> Path:
    """Materialise the shape ``playwright install chromium`` leaves behind."""
    path = tmp_path / build / executable
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def test_missing_pip_package_names_pip_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)

    status = autofill.autofill_available()

    assert status["available"] is False
    assert "pip install playwright" in status["reason"]


def test_bare_pip_install_names_the_browser_download(monkeypatch, tmp_path):
    """The state the old check wrongly reported as available."""
    _install_fake_playwright(monkeypatch)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))  # nothing downloaded

    status = autofill.autofill_available()

    assert status["available"] is False
    assert "playwright install chromium" in status["reason"]


def test_full_stack_reports_available_with_the_dry_run_flag(monkeypatch, tmp_path):
    _install_fake_playwright(monkeypatch)
    _make_browser_build(tmp_path, "chromium-1148", "chrome-linux/chrome")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "autofill_enabled", True)
    monkeypatch.setattr(settings, "autofill_dry_run", True)

    status = autofill.autofill_available()

    assert status["available"] is True
    assert status["dry_run"] is settings.autofill_dry_run


def test_headless_shell_build_counts_as_installed(monkeypatch, tmp_path):
    _install_fake_playwright(monkeypatch)
    _make_browser_build(tmp_path, "chromium_headless_shell-1150", "chrome-linux/headless_shell")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "autofill_enabled", True)

    status = autofill.autofill_available()

    assert status["available"] is True
    assert status["dry_run"] is settings.autofill_dry_run


def test_installed_but_disabled_names_the_flag(monkeypatch, tmp_path):
    _install_fake_playwright(monkeypatch)
    _make_browser_build(tmp_path, "chromium-1148", "chrome-linux/chrome")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "autofill_enabled", False)

    status = autofill.autofill_available()

    assert status["available"] is False
    assert status["reason"] == "AUTOFILL_ENABLED is false"


def test_assisted_apply_uses_the_browser_runtime_not_the_auto_apply_flag(monkeypatch, tmp_path):
    """A human-controlled session remains usable when unattended runs are off."""
    _install_fake_playwright(monkeypatch)
    _make_browser_build(tmp_path, "chromium-1148", "chrome-linux/chrome")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "autofill_enabled", False)
    monkeypatch.setattr(settings, "assisted_apply_enabled", True)

    assert autofill.autofill_available()["available"] is False
    status = autofill.assisted_apply_available()

    assert status == {"available": True}


def test_assisted_apply_can_be_explicitly_disabled(monkeypatch, tmp_path):
    _install_fake_playwright(monkeypatch)
    _make_browser_build(tmp_path, "chromium-1148", "chrome-linux/chrome")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "assisted_apply_enabled", False)

    status = autofill.assisted_apply_available()

    assert status == {"available": False, "reason": "ASSISTED_APPLY_ENABLED is false"}
