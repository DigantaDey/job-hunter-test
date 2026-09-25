"""Tests for the Laya warm-up script (backend/scripts/warm_laya.py)."""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "warm_laya.py"


def _load():
    spec = importlib.util.spec_from_file_location("warm_laya", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRouter:
    preloaded = None

    def preload(self, names=None):
        FakeRouter.preloaded = list(names) if names else ["english", "multilingual", "typed-decisions"]


@pytest.fixture()
def warm_laya(monkeypatch, tmp_path):
    module = _load()
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    FakeRouter.preloaded = None
    fake = types.ModuleType("laya")
    fake.Router = FakeRouter
    monkeypatch.setitem(sys.modules, "laya", fake)
    return module


def test_warm_writes_marker_for_default_set(warm_laya):
    assert warm_laya.main([]) == 0
    assert FakeRouter.preloaded == ["english", "multilingual"]
    assert warm_laya.warmed_set() == ["english", "multilingual"]
    assert warm_laya.marker_path().parent.is_dir()


def test_if_needed_skips_when_marker_covers_the_set(warm_laya):
    warm_laya.marker_path().parent.mkdir(parents=True, exist_ok=True)
    warm_laya.marker_path().write_text("english,multilingual\n", encoding="utf-8")
    assert warm_laya.main(["--if-needed"]) == 0
    assert FakeRouter.preloaded is None  # never even built a Router


def test_if_needed_warms_when_marker_is_partial(warm_laya):
    warm_laya.marker_path().parent.mkdir(parents=True, exist_ok=True)
    warm_laya.marker_path().write_text("english\n", encoding="utf-8")
    assert warm_laya.main(["--if-needed"]) == 0
    assert FakeRouter.preloaded == ["english", "multilingual"]
    assert set(warm_laya.warmed_set()) == {"english", "multilingual"}


def test_all_adds_typed_decisions_to_set_and_marker(warm_laya):
    assert warm_laya.main(["--all"]) == 0
    assert FakeRouter.preloaded == ["english", "multilingual", "typed-decisions"]
    assert warm_laya.warmed_set() == ["english", "multilingual", "typed-decisions"]
    assert warm_laya.main(["--all", "--if-needed"]) == 0
    assert FakeRouter.preloaded == ["english", "multilingual", "typed-decisions"]


def test_check_fails_without_marker(warm_laya, capsys):
    assert warm_laya.main(["--check"]) == 1
    assert FakeRouter.preloaded is None
    assert "run warm_laya.py first" in capsys.readouterr().err


def test_check_passes_offline_when_warmed(warm_laya, monkeypatch):
    warm_laya.marker_path().parent.mkdir(parents=True, exist_ok=True)
    warm_laya.marker_path().write_text("english,multilingual\n", encoding="utf-8")
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    assert warm_laya.main(["--check"]) == 0
    assert FakeRouter.preloaded == ["english", "multilingual"]
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
    # --check must not extend or rewrite the marker.
    assert warm_laya.warmed_set() == ["english", "multilingual"]


def test_missing_laya_is_reported(monkeypatch, tmp_path):
    module = _load()
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "laya", None)  # forces ImportError
    assert module.main([]) == 1
