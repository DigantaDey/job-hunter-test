"""
Static SPA ↔ API contract check.

Four core journeys (discover jobs, apply, draft outreach, send outreach) shipped
broken because the SPA sent **query parameters** to endpoints that declare a
required pydantic **body**. FastAPI rejected them with 422 before any handler
ran, so no backend test noticed: the suite always posted ``json=...``.

This test closes that blind spot without a browser. It parses every
``client.post|put|patch(...)`` call out of the SPA source and checks it against
the application's own OpenAPI schema:

* an endpoint with a required request body must be called with a body argument;
* an endpoint without a body must not be called with one;
* the path must exist in the schema at all (catches typos and renames).

It is deliberately conservative — anything it cannot parse confidently is
skipped rather than reported, so it does not become a flaky gate.
"""
from __future__ import annotations

import os
import re
from typing import Dict, Iterator, List, Optional, Tuple

import pytest

from app.main import app

FRONTEND_SRC = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "src")
)

#: `client.post('/api/x', body, config)` — capture the method and the URL
#: literal. The argument list after the URL is parsed separately from the
#: match end, so this pattern must NOT consume the rest of the file (a greedy
#: `.*` with DOTALL would leave only one match per file).
_CALL = re.compile(
    r"""client\.(?P<method>post|put|patch)\(\s*(?P<quote>['"`])(?P<url>/api/[^'"`]*)(?P=quote)"""
)


def _iter_source_files() -> Iterator[str]:
    for root, _dirs, files in os.walk(FRONTEND_SRC):
        for name in files:
            if name.endswith((".ts", ".tsx")):
                yield os.path.join(root, name)


def _split_top_level_args(text: str) -> Optional[List[str]]:
    """Return the argument list of a call whose opening paren is already consumed."""
    depth = 0
    args: List[str] = []
    current = ""
    string: Optional[str] = None
    previous = ""
    for char in text:
        if string:
            if char == string and previous != "\\":
                string = None
            current += char
        elif char in "'\"`":
            string = char
            current += char
        elif char in "([{":
            depth += 1
            current += char
        elif char in ")]}":
            if depth == 0:
                args.append(current)
                return args
            depth -= 1
            current += char
        elif char == "," and depth == 0:
            args.append(current)
            current = ""
        else:
            current += char
        previous = char
    return None


def _template_to_openapi(url: str) -> str:
    """`/api/emails/${id}/send` -> `/api/emails/{id}/send`."""
    normalised = re.sub(r"\$\{[^}]*\}", "{param}", url)
    return normalised.split("?", 1)[0].rstrip("/") or "/"


def _schema_paths() -> Dict[str, dict]:
    return app.openapi()["paths"]


def _match_schema_path(url: str, paths: Dict[str, dict]) -> Optional[str]:
    """Find the schema path matching a (possibly templated) SPA URL."""
    target = _template_to_openapi(url)
    if target in paths:
        return target
    target_parts = target.split("/")
    for candidate in paths:
        candidate_parts = candidate.split("/")
        if len(candidate_parts) != len(target_parts):
            continue
        for got, want in zip(target_parts, candidate_parts, strict=True):
            if want.startswith("{") and want.endswith("}"):
                continue  # a path parameter matches anything
            if got != want:
                break
        else:
            return candidate
    return None


def _collect_calls() -> List[Tuple[str, str, str, Optional[str]]]:
    """Return (file, method, url, body_expression|None) for every SPA write call."""
    calls: List[Tuple[str, str, str, Optional[str]]] = []
    for path in _iter_source_files():
        source = open(path, encoding="utf-8").read()
        for match in _CALL.finditer(source):
            rest = source[match.end():].lstrip()
            body: Optional[str] = None
            # After the URL literal the call either closes immediately -- ``)``,
            # meaning no further arguments -- or continues with ``, <body>...``.
            # Strip that separator *before* splitting, otherwise the split
            # yields an empty first element and every call looks body-less.
            if rest.startswith(","):
                args = _split_top_level_args(rest[1:].lstrip())
                if args:
                    body = args[0].strip() or None
            calls.append((os.path.relpath(path, FRONTEND_SRC), match.group("method"),
                          match.group("url"), body))
    return calls


@pytest.mark.skipif(not os.path.isdir(FRONTEND_SRC), reason="frontend sources not present")
def test_every_spa_write_call_matches_the_api_schema():
    paths = _schema_paths()
    calls = _collect_calls()
    assert calls, "no client.post/put/patch calls found — did the SPA layout change?"

    problems: List[str] = []
    for file, method, url, body in calls:
        schema_path = _match_schema_path(url, paths)
        if schema_path is None:
            problems.append(f"{file}: {method.upper()} {url} — no such route in the OpenAPI schema")
            continue

        operation = paths[schema_path].get(method)
        if operation is None:
            allowed = sorted(k for k in paths[schema_path] if k in {"get", "post", "put", "patch", "delete"})
            problems.append(f"{file}: {method.upper()} {url} — route exists but only allows {allowed}")
            continue

        requires_body = bool(operation.get("requestBody", {}).get("required"))
        sends_body = body is not None and body not in {"null", "undefined"}

        if requires_body and not sends_body:
            problems.append(
                f"{file}: {method.upper()} {url} sends {body or 'no body'} but the endpoint "
                f"requires a JSON request body — FastAPI will answer 422"
            )

    assert not problems, "SPA/API contract mismatches:\n  - " + "\n  - ".join(problems)


# --------------------------------------------------------------------------- #
# GET calls: path existence
#
# The write-call check above cannot catch a mistyped read path — a GET to a
# route that does not exist answers 404 and the page silently renders its empty
# state. `/api/resumes/context/keywords` (the real route is
# `/api/context/keywords`) shipped exactly like that: the Settings page showed
# "Nothing yet — upload a master resume" forever.
# --------------------------------------------------------------------------- #
_GET = re.compile(
    r"""client\.get\(\s*(?P<quote>['"`])(?P<url>/api/[^'"`]*)(?P=quote)"""
)


def _collect_reads() -> List[Tuple[str, str]]:
    """Return (file, url) for every SPA read call with a literal path."""
    calls: List[Tuple[str, str]] = []
    for path in _iter_source_files():
        source = open(path, encoding="utf-8").read()
        for match in _GET.finditer(source):
            calls.append((os.path.relpath(path, FRONTEND_SRC), match.group("url")))
    return calls


@pytest.mark.skipif(not os.path.isdir(FRONTEND_SRC), reason="frontend sources not present")
def test_every_spa_read_call_matches_the_api_schema():
    paths = _schema_paths()
    calls = _collect_reads()
    assert calls, "no client.get calls found — did the SPA layout change?"

    problems: List[str] = []
    for file, url in calls:
        schema_path = _match_schema_path(url, paths)
        if schema_path is None:
            problems.append(f"{file}: GET {url} — no such route in the OpenAPI schema")
            continue
        if "get" not in paths[schema_path]:
            allowed = sorted(k for k in paths[schema_path]
                             if k in {"get", "post", "put", "patch", "delete"})
            problems.append(f"{file}: GET {url} — route exists but only allows {allowed}")

    assert not problems, "SPA/API read mismatches:\n  - " + "\n  - ".join(problems)
