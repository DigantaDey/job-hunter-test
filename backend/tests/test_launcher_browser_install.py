"""Exercise launcher installation choices without pip, npm, or a real browser."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("overrides", "browser", "dependencies"),
    [({}, True, True), ({"WITH_AUTOFILL": "0"}, False, True),
     ({"AUTO_INSTALL": "0"}, False, False)],
)
def test_launcher_browser_install(tmp_path, overrides, browser, dependencies):
    root = Path(__file__).resolve().parents[2]
    shutil.copy(root / "run.sh", tmp_path / "run.sh")
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend" / "node_modules").mkdir(parents=True)
    (tmp_path / ".env").touch()
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("python", "npm"):
        executable = bin_dir / name
        executable.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$COMMAND_LOG"\n')
        executable.chmod(0o755)
    log = tmp_path / "commands.log"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "COMMAND_LOG": str(log), "PYTHON_BIN": ".venv/bin/python",
           "AUTO_INSTALL": "1", "WITH_AUTOFILL": "1", **overrides}

    subprocess.run(["bash", "run.sh"], cwd=tmp_path, env=env, check=True,
                   capture_output=True, text=True)

    commands = log.read_text()
    assert ("-r backend/requirements-dev.txt" in commands) is dependencies
    assert ("-r backend/requirements-autofill.txt" in commands) is browser
    assert ("-m playwright install --with-deps chromium" in commands) is browser
    assert "-m uvicorn app.main:app" in commands
