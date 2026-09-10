"""Upload hardening: hostile file names must never escape the upload directory."""
from __future__ import annotations

import io
import os

import pytest

from app.api.routers.resumes import _store_upload, safe_upload_name


@pytest.mark.parametrize(
    "given, expected",
    [
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("..\\..\\windows\\system32\\evil.pdf", "evil.pdf"),
        ("/absolute/path/report.pdf", "report.pdf"),
        ("résumé final.pdf", "r_sum_final.pdf"),
        ("space name.pdf", "space_name.pdf"),
        ("dots...pdf", "dots.pdf"),
        ("....", "resume.pdf"),
        ("report.exe", "report.pdf"),
        ("report.docx", "report.docx"),
        ("\x00null\x1f.pdf", "null.pdf"),
        ("", "resume.pdf"),
        ("a" * 300 + ".pdf", "a" * 60 + ".pdf"),
    ],
)
def test_names_are_flattened(given, expected):
    assert safe_upload_name(given) == expected


def test_store_upload_stays_inside_the_upload_directory(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"), raising=False)
    os.makedirs(settings.upload_dir, exist_ok=True)

    class FakeUpload:
        filename = "../../../../tmp/escape.pdf"
        file = io.BytesIO(b"%PDF-1.4 fake")

    path, safe_name = _store_upload(FakeUpload(), ".pdf")

    assert safe_name == "escape.pdf"
    assert os.path.dirname(os.path.realpath(path)) == os.path.realpath(settings.upload_dir)
    assert os.path.exists(path)
    # Nothing was written outside the sandbox directory.
    assert not os.path.exists(os.path.join(os.path.dirname(settings.upload_dir), "escape.pdf"))


def test_upload_endpoint_rejects_non_resume_content(client, auth):
    response = client.post(
        "/api/resume/upload",
        files={"file": ("evil.pdf", b"MZ\x90\x00 not a pdf at all", "application/pdf")},
        headers=auth,
    )
    assert response.status_code == 415
