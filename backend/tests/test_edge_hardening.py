"""
The request edge: everything that runs *before* authentication.

That is the code whose only inputs are attacker-controlled, so this suite pins
the five properties that keep it from being a lever against the process:

1. **the rate limiter cannot be grown** — its key set is a hard-capped LRU keyed
   on a *verified* identity or the resolved client IP, never on a slice of a raw
   header (10 000 unique ``Authorization`` values are one IP bucket, not 10 000
   dict entries);
2. **the body cap holds on the wire** — ``Content-Length`` is only a fast reject;
   the bytes are counted as they stream in, so ``Transfer-Encoding: chunked``
   cannot walk past it;
3. **the client IP is not caller-chosen** — ``X-Forwarded-For`` is believed only
   from a configured proxy, and then only up to the rightmost hop that is not
   itself a proxy (the shipped Dockerfile no longer trusts it from ``*``);
4. **metric cardinality is bounded** — inbound series are method + route template
   + status, outbound hosts collapse to a fixed set, and the registry caps series
   per metric;
5. **one worker is the default** — and raising it says out loud which in-process
   budgets it multiplies.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core import metrics
from app.core.config import REPO_DIR, Settings
from app.core.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    SlidingWindowLog,
    is_trusted_proxy,
    path_label,
    resolve_client_ip,
)
from app.core.security import create_access_token


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _scope(path: str = "/api/jobs", *, method: str = "GET", client: str = "203.0.113.7",
           headers: Dict[str, str] | None = None) -> Dict[str, Any]:
    raw: List[tuple] = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    return {"type": "http", "method": method, "path": path, "headers": raw,
            "query_string": b"", "client": (client, 4321), "scheme": "http", "http_version": "1.1"}


def _request(path: str = "/api/jobs", *, method: str = "GET", client: str = "203.0.113.7",
             headers: Dict[str, str] | None = None) -> Request:
    return Request(_scope(path, method=method, client=client, headers=headers))


def _limiter(**kwargs) -> RateLimitMiddleware:
    """A middleware instance with no app behind it — only its keys/decisions matter."""
    kwargs.setdefault("max_keys", 256)
    return RateLimitMiddleware(Starlette(), **kwargs)


async def _call(app, scope: Dict[str, Any], body_chunks: List[bytes]) -> List[Dict[str, Any]]:
    """Drive an ASGI app with a chunked request body; return everything it sent."""
    queue = list(body_chunks)
    sent: List[Dict[str, Any]] = []

    async def receive():
        if not queue:
            return {"type": "http.disconnect"}
        chunk = queue.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(queue)}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _status(sent: List[Dict[str, Any]]) -> int:
    starts = [message["status"] for message in sent if message["type"] == "http.response.start"]
    assert len(starts) == 1, f"exactly one response must be written, got {starts}"
    return starts[0]


def _body(sent: List[Dict[str, Any]]) -> str:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body").decode()


# --------------------------------------------------------------------------- #
# 1. Rate limiter: bounded keys, identity-based, no raw header material
# --------------------------------------------------------------------------- #
def test_ten_thousand_unique_authorization_headers_do_not_grow_the_limiter():
    """
    The memory DoS, verbatim: unique random credentials, no valid token needed.

    The old key was ``auth:<last 24 characters of the header>`` and an entry was
    pruned only when the *same* key came back, so this loop grew a dict by 10 000
    entries — with pruning running before authentication, an unauthenticated
    caller could OOM the API process. Now every one of them is anonymous and
    shares the single client-IP bucket.
    """
    limiter = _limiter(limit=1000)
    for index in range(10_000):
        request = _request(headers={"Authorization": f"Bearer junk-{index}-{os.urandom(8).hex()}"})
        for key in limiter._keys(request):
            limiter._hits.allow(key, limiter.limit)

    assert len(limiter._hits) == 1, limiter.stats()
    assert limiter._hits.keys() == ["ip:203.0.113.7"]
    stats = limiter.stats()
    assert stats["keys"] == 1 and stats["decisions"] == 10_000
    assert stats["keys"] <= stats["max_keys"]


def test_distinct_client_ips_are_capped_by_the_lru():
    """A flood of *distinct IPs* is bounded the same way: LRU eviction, not growth."""
    log = SlidingWindowLog(max_keys=64, window_seconds=60)
    for index in range(5_000):
        log.allow(f"ip:10.0.{index // 250}.{index % 250}", 10)

    assert len(log) <= 64
    stats = log.stats()
    assert stats["evictions"] >= 5_000 - 64
    assert stats["keys"] <= stats["max_keys"]


def test_limiter_keys_never_carry_the_credential():
    """Keys are keyed hashes: a heap dump or a debug print yields nothing usable."""
    limiter = _limiter()
    token, _expires = create_access_token(4242, email="user@example.com")
    api_key = "jh_" + os.urandom(24).hex()

    user_keys = limiter._keys(_request(headers={"Authorization": f"Bearer {token}"}))
    key_keys = limiter._keys(_request(headers={"X-API-Key": api_key}))

    assert len(user_keys) == 1 and user_keys[0].startswith("u:")
    assert len(key_keys) == 2 and key_keys[0].startswith("k:")
    for key in (user_keys[0], key_keys[0]):
        prefix, _, fingerprint = key.partition(":")
        assert prefix in ("u", "k")
        assert re.fullmatch(r"[0-9a-f]{32}", fingerprint), key
        assert token not in key and api_key not in key and "4242" not in key
    # The second bucket for an unverified API key is the client IP, not a
    # fingerprint — and it is the one that stops a junk-key flood.
    assert key_keys[1] == "ip:203.0.113.7"


def test_a_verified_token_is_keyed_on_the_user_not_the_token_string():
    """Two tokens for the same user share one budget; another user does not."""
    limiter = _limiter()
    first, _ = create_access_token(7, email="a@example.com")
    second, _ = create_access_token(7, email="a@example.com", role="owner")
    other, _ = create_access_token(8, email="b@example.com")

    assert limiter._keys(_request(headers={"Authorization": f"Bearer {first}"})) == \
        limiter._keys(_request(headers={"Authorization": f"Bearer {second}"}))
    assert limiter._keys(_request(headers={"Authorization": f"Bearer {first}"})) != \
        limiter._keys(_request(headers={"Authorization": f"Bearer {other}"}))


def test_an_unverifiable_token_falls_back_to_the_client_ip():
    """Forged, expired, wrong-type or random: anonymous, i.e. the IP bucket."""
    limiter = _limiter()
    expired, _ = create_access_token(9, expires_minutes=-5)
    for credential in ("not-a-token", "a.b.c", expired, "jh_" + "x" * 8):
        keys = limiter._keys(_request(headers={"Authorization": f"Bearer {credential}"}))
        assert keys[-1] == "ip:203.0.113.7", keys
        assert all(not key.startswith("u:") for key in keys), keys


def test_junk_credentials_are_throttled_as_one_ip_end_to_end():
    """
    The attack through the whole stack: each request carries a fresh credential,
    so a per-credential bucket would never fill — the IP bucket does.
    """
    async def ping(request):
        return JSONResponse({"ok": True})

    app = RateLimitMiddleware(Starlette(routes=[Route("/api/ping", ping)]),
                              limit_per_minute=5, max_keys=32)
    client = TestClient(app)

    codes = [client.get("/api/ping", headers={"Authorization": f"Bearer junk-{i}"}).status_code
             for i in range(12)]
    assert codes[:5] == [200] * 5
    assert codes[5:] == [429] * 7
    limited = client.get("/api/ping", headers={"Authorization": "Bearer junk-final"})
    assert limited.status_code == 429
    assert limited.headers["Retry-After"].isdigit()
    assert limited.json()["code"] == "rate_limited"
    assert len(app._hits) == 1


def test_signed_in_users_get_their_own_budget_not_a_shared_ip_one():
    """A verified token is not charged to the IP: a household/NAT is not one user."""
    async def ping(request):
        return JSONResponse({"ok": True})

    app = RateLimitMiddleware(Starlette(routes=[Route("/api/ping", ping)]),
                              limit_per_minute=3, max_keys=32)
    client = TestClient(app)
    first, _ = create_access_token(1)
    second, _ = create_access_token(2)

    assert [client.get("/api/ping", headers={"Authorization": f"Bearer {first}"}).status_code
            for _ in range(3)] == [200, 200, 200]
    assert client.get("/api/ping", headers={"Authorization": f"Bearer {first}"}).status_code == 429
    # Same IP, different user: unaffected.
    assert client.get("/api/ping", headers={"Authorization": f"Bearer {second}"}).status_code == 200


def test_expired_windows_are_swept_without_waiting_for_the_same_key():
    """Reclamation is time-based, not hit-based — an idle key does not squat."""
    clock = {"now": 1000.0}
    log = SlidingWindowLog(max_keys=1000, window_seconds=60, sweep_interval_seconds=30,
                           clock=lambda: clock["now"])
    for index in range(50):
        log.allow(f"ip:10.0.0.{index}", 10)
    assert len(log) == 50

    clock["now"] += 61  # every window has elapsed
    log.allow("ip:10.0.0.200", 10)
    assert len(log) == 1
    assert log.stats()["expired_keys"] >= 50
    assert log.stats()["sweeps"] >= 1


def test_the_budget_reopens_when_the_window_elapses():
    clock = {"now": 0.0}
    log = SlidingWindowLog(max_keys=16, window_seconds=60, clock=lambda: clock["now"])
    for _ in range(5):
        assert log.allow("ip:1.2.3.4", 5)[0] is True
    allowed, retry_after = log.allow("ip:1.2.3.4", 5)
    assert allowed is False and 1 <= retry_after <= 60

    clock["now"] += 61
    assert log.allow("ip:1.2.3.4", 5)[0] is True


def test_health_and_preflight_stay_exempt():
    async def ping(request):
        return JSONResponse({"ok": True})

    app = RateLimitMiddleware(Starlette(routes=[Route("/api/health", ping), Route("/api/ping", ping)]),
                              limit_per_minute=1, max_keys=16)
    client = TestClient(app)
    assert [client.get("/api/health").status_code for _ in range(5)] == [200] * 5
    assert client.get("/api/ping").status_code == 200
    assert client.get("/api/ping").status_code == 429
    assert client.options("/api/ping").status_code in (200, 405)


def test_login_throttle_is_bounded_and_cannot_be_wiped():
    """
    Same shape one layer up: the per-email login window is caller-keyed, so it is
    an LRU with a TTL now — the old ``if len(...) > 2000: clear()`` let an
    attacker erase *everybody's* lockout state just by crossing the threshold.
    """
    from fastapi import HTTPException

    from app.api.routers import auth as auth_router

    attempts = auth_router._attempts
    attempts.clear()

    # Bounded: 5x the cap of distinct emails cannot grow it past the cap.
    for index in range(attempts.max_entries * 5):
        auth_router._throttle(f"noise{index}@example.com")
    assert len(attempts) <= attempts.max_entries

    # And a flood that fits inside the cap costs *its own* entries only: the
    # lockout established before it still holds afterwards.
    attempts.clear()
    for _ in range(auth_router._max_attempts()):
        auth_router._throttle("owner@example.com")
    for index in range(1_000):
        auth_router._throttle(f"flood{index}@example.com")
    with pytest.raises(HTTPException) as excinfo:
        auth_router._throttle("owner@example.com")
    assert excinfo.value.status_code == 429
    attempts.clear()


# --------------------------------------------------------------------------- #
# 2. Body cap enforced while streaming
# --------------------------------------------------------------------------- #
async def _echo_app(max_bytes: int) -> BodySizeLimitMiddleware:
    async def echo(request):
        body = await request.body()
        return JSONResponse({"bytes": len(body)})

    return BodySizeLimitMiddleware(Starlette(routes=[Route("/echo", echo, methods=["POST"])]),
                                   max_bytes=max_bytes)


async def test_a_chunked_body_over_the_cap_is_rejected_with_413():
    """No ``Content-Length`` anywhere — the bytes are counted as they arrive."""
    app = await _echo_app(1024)
    scope = _scope("/echo", method="POST")
    assert "content-length" not in {k.decode() for k, _ in scope["headers"]}

    sent = await _call(app, scope, [b"x" * 512 for _ in range(4)])  # 2048 > 1024
    assert _status(sent) == 413
    payload = __import__("json").loads(_body(sent))
    assert payload["code"] == "payload_too_large"
    assert app.rejected_by_stream == 1


async def test_a_chunked_body_under_the_cap_is_served():
    app = await _echo_app(1024)
    sent = await _call(app, _scope("/echo", method="POST"), [b"x" * 300, b"y" * 300])
    assert _status(sent) == 200
    assert __import__("json").loads(_body(sent))["bytes"] == 600
    assert app.rejected_by_stream == 0


async def test_the_abort_wins_even_when_the_handler_keeps_going():
    """
    A handler that swallows the unwind (or answers anyway) must not produce a
    second response: the 413 is written on the outer channel and later sends are
    dropped. This is also what keeps a chunked upload from being buffered whole.
    """
    seen: Dict[str, Any] = {}

    async def stubborn(request):
        try:
            body = await request.body()
        except Exception as exc:  # noqa: BLE001 - deliberately swallows the abort
            seen["swallowed"] = type(exc).__name__
            body = b""
        seen["buffered"] = len(body)
        return JSONResponse({"ok": True})  # would be a 200 if it were allowed out

    app = BodySizeLimitMiddleware(Starlette(routes=[Route("/echo", stubborn, methods=["POST"])]),
                                  max_bytes=256)
    sent = await _call(app, _scope("/echo", method="POST"), [b"z" * 200, b"z" * 200, b"z" * 200])
    assert _status(sent) == 413          # exactly one response, and it is the cap
    assert seen.get("buffered", 999) <= 400  # it never buffered the whole stream


async def test_a_declared_content_length_still_fast_rejects_before_the_app_runs():
    called = {"app": False}

    async def echo(request):
        called["app"] = True
        return JSONResponse({"ok": True})

    app = BodySizeLimitMiddleware(Starlette(routes=[Route("/echo", echo, methods=["POST"])]),
                                  max_bytes=1024)
    scope = _scope("/echo", method="POST", headers={"Content-Length": "99999999"})
    sent = await _call(app, scope, [b"small"])
    assert _status(sent) == 413
    assert called["app"] is False
    assert app.rejected_by_header == 1


async def test_a_lie_about_content_length_is_caught_by_the_stream_count():
    """Declares 10 bytes, streams 4 KB: the counter, not the header, decides."""
    app = await _echo_app(1024)
    scope = _scope("/echo", method="POST", headers={"Content-Length": "10"})
    sent = await _call(app, scope, [b"a" * 10] + [b"b" * 1024 for _ in range(3)])
    assert _status(sent) == 413
    assert app.rejected_by_stream == 1


async def test_no_further_bytes_are_handed_over_after_the_abort():
    """A handler that catches the abort and keeps reading must not get the body."""
    buffered = {"total": 0, "aborts": 0}

    async def greedy(request):
        while True:
            try:
                message = await request.receive()
            except Exception:  # noqa: BLE001 - counts the aborts and keeps going
                buffered["aborts"] += 1
                if buffered["aborts"] > 6:
                    break
                continue
            if message["type"] != "http.request":
                break
            buffered["total"] += len(message.get("body") or b"")
            if not message.get("more_body"):
                break
        return JSONResponse({"buffered": buffered["total"]})

    app = BodySizeLimitMiddleware(Starlette(routes=[Route("/echo", greedy, methods=["POST"])]),
                                  max_bytes=256)
    sent = await _call(app, _scope("/echo", method="POST"), [b"z" * 200 for _ in range(20)])
    assert _status(sent) == 413
    assert buffered["total"] <= 256, buffered  # never more than the cap reached the handler
    assert buffered["aborts"] >= 1


def test_an_oversized_chunked_upload_is_rejected_end_to_end(client, monkeypatch):
    """
    Through the real app and the real middleware stack, with a chunked body
    (httpx sends ``Transfer-Encoding: chunked`` for a generator, so there is no
    ``Content-Length`` to check).
    """
    from app.core.config import settings
    from app.main import create_app

    monkeypatch.setattr(settings, "max_upload_mb", 1)  # cap = 1 MB + 2 MB overhead
    small_app = create_app()
    with TestClient(small_app) as small_client:
        def oversized():
            for _ in range(8):
                yield b"0" * (512 * 1024)  # 4 MB, no content-length

        response = small_client.post("/api/auth/login", content=oversized())
        assert response.status_code == 413, response.text
        assert response.json()["code"] == "payload_too_large"
        assert response.request.headers.get("transfer-encoding") == "chunked"
        assert "content-length" not in response.request.headers

        # Under the cap the same route behaves normally (422: not valid JSON).
        def fine():
            yield b"{" * 1
            yield b"}" * 1

        assert small_client.post("/api/auth/login", content=fine()).status_code in (400, 422)


def test_the_cap_is_wired_from_max_upload_mb():
    from app.core.config import settings
    from app.main import app

    expected = settings.max_upload_mb * 1024 * 1024 + (2 * 1024 * 1024)
    middleware = [item for item in app.user_middleware if item.cls is BodySizeLimitMiddleware]
    assert middleware and middleware[0].kwargs["max_bytes"] == expected


# --------------------------------------------------------------------------- #
# 3. Client IP: the trust boundary
# --------------------------------------------------------------------------- #
def test_a_spoofed_xff_is_ignored_when_no_proxy_is_configured(monkeypatch):
    """The shipped default: nobody is a proxy, so the header is attacker input."""
    monkeypatch.setattr(settings_for_trust(), "trusted_proxies", [])
    scope = _scope(client="198.51.100.23", headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})
    assert resolve_client_ip(scope) == "198.51.100.23"


def test_no_forwarded_header_uses_the_peer():
    assert resolve_client_ip(_scope(client="198.51.100.23")) == "198.51.100.23"
    assert resolve_client_ip({"type": "http", "headers": []}) == ""


def test_a_trusted_proxy_yields_the_rightmost_untrusted_hop(monkeypatch):
    """
    ``X-Forwarded-For: <spoofed>, <the address our proxy actually saw>`` — the
    entry the proxy appended is the rightmost one, and the spoofed prefix is
    ignored.
    """
    settings_ = settings_for_trust()
    monkeypatch.setattr(settings_, "trusted_proxies", ["10.0.0.0/8"])
    scope = _scope(client="10.0.0.2", headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.9"})
    assert resolve_client_ip(scope) == "203.0.113.9"
    assert is_trusted_proxy("10.1.2.3") and not is_trusted_proxy("203.0.113.9")


def test_a_spoofed_prefix_cannot_move_the_answer(monkeypatch):
    """The victim-blaming attack: the client prepends someone else's address."""
    monkeypatch.setattr(settings_for_trust(), "trusted_proxies", ["172.18.0.0/16"])
    scope = _scope(client="172.18.0.5",
                   headers={"X-Forwarded-For": "8.8.8.8, 9.9.9.9"})  # both written by the client
    assert resolve_client_ip(scope) == "9.9.9.9"  # rightmost: the hop the proxy actually saw


def test_a_chain_of_proxies_is_walked(monkeypatch):
    monkeypatch.setattr(settings_for_trust(), "trusted_proxies", ["10.0.0.1", "10.0.0.2"])
    scope = _scope(client="10.0.0.2", headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})
    assert resolve_client_ip(scope) == "203.0.113.7"


def test_every_hop_trusted_falls_back_to_the_leftmost(monkeypatch):
    monkeypatch.setattr(settings_for_trust(), "trusted_proxies", ["10.0.0.0/8"])
    scope = _scope(client="10.0.0.9", headers={"X-Forwarded-For": "10.1.1.1, 10.2.2.2"})
    assert resolve_client_ip(scope) == "10.1.1.1"


@pytest.mark.parametrize("header,expected", [
    ("203.0.113.7:5555", "203.0.113.7"),
    ("[2001:db8::9]", "2001:db8::9"),
    ("[2001:db8::9]:443", "2001:db8::9"),
    (" 203.0.113.7 , 10.0.0.1 ", "10.0.0.1"),
])
def test_forwarded_for_spellings_are_normalised(monkeypatch, header, expected):
    monkeypatch.setattr(settings_for_trust(), "trusted_proxies", ["10.0.0.0/8"])
    scope = _scope(client="10.0.0.2", headers={"X-Forwarded-For": header})
    resolved = resolve_client_ip(scope)
    assert resolved == (expected if expected != "10.0.0.1" else "203.0.113.7")


def test_repeated_forwarded_for_headers_are_read_in_order(monkeypatch):
    monkeypatch.setattr(settings_for_trust(), "trusted_proxies", ["10.0.0.0/8"])
    scope = _scope(client="10.0.0.2")
    scope["headers"] = [(b"x-forwarded-for", b"203.0.113.7"), (b"x-forwarded-for", b"10.0.0.1")]
    assert resolve_client_ip(scope) == "203.0.113.7"


def test_an_untrusted_forwarded_header_is_counted(monkeypatch):
    monkeypatch.setattr(settings_for_trust(), "trusted_proxies", [])
    metrics.reset()
    resolve_client_ip(_scope(client="198.51.100.23", headers={"X-Forwarded-For": "1.2.3.4"}))
    snapshot = metrics.snapshot()
    assert snapshot.get('jobhunter_edge_forwarded_for_total{reason="untrusted_peer"}') == 1


def test_an_upstream_resolved_client_is_not_reported_as_spoofing(monkeypatch):
    """
    uvicorn with ``--proxy-headers`` rewrites the peer to the client it resolved
    from the header, so the peer then appears *in* the chain: normal, and must not
    page anyone.
    """
    monkeypatch.setattr(settings_for_trust(), "trusted_proxies", [])
    metrics.reset()
    scope = _scope(client="203.0.113.7", headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.2"})
    assert resolve_client_ip(scope) == "203.0.113.7"
    snapshot = metrics.snapshot()
    assert snapshot.get('jobhunter_edge_forwarded_for_total{reason="resolved_upstream"}') == 1
    assert 'reason="untrusted_peer"' not in str(snapshot)


def test_audit_rows_record_the_real_actor_not_the_header(client, db, monkeypatch):
    """End to end: a failed login writes the resolved IP into ``audit_logs.ip``."""
    from app.models.models import AuditLog

    client.post("/api/auth/bootstrap",
                json={"email": "owner@example.com", "password": "owner-password-123"})
    client.post("/api/auth/login", json={"email": "owner@example.com", "password": "wrong-password"},
                headers={"X-Forwarded-For": "1.2.3.4"})
    row = db.query(AuditLog).filter(AuditLog.action == "auth.login_failed").first()
    assert row is not None
    assert row.ip == "testclient"  # the peer — the spoofed 1.2.3.4 is not believed
    assert "1.2.3.4" not in (row.ip or "")

    # And with the proxy configured, the forwarded client is attributed correctly.
    monkeypatch.setattr(settings_for_trust(), "trusted_proxies", ["10.0.0.0/8"])
    proxy_client = TestClient(client.app, client=("10.0.0.7", 51000))
    proxy_client.post("/api/auth/login", json={"email": "owner@example.com", "password": "wrong-password"},
                      headers={"X-Forwarded-For": "203.0.113.44, 10.0.0.7"})
    rows = db.query(AuditLog).filter(AuditLog.action == "auth.login_failed").order_by(AuditLog.id).all()
    assert rows[-1].ip == "203.0.113.44"


def test_deps_client_ip_uses_the_same_boundary(monkeypatch):
    from app.api.deps import client_ip

    monkeypatch.setattr(settings_for_trust(), "trusted_proxies", [])
    request = _request(client="198.51.100.5", headers={"X-Forwarded-For": "9.9.9.9"})
    assert client_ip(request) == "198.51.100.5"


def test_the_answer_is_resolved_once_per_request(monkeypatch):
    """The limiter, ``deps.client_ip()`` and the audit trail all ask; one counts."""
    monkeypatch.setattr(settings_for_trust(), "trusted_proxies", [])
    metrics.reset()
    scope = _scope(client="198.51.100.23", headers={"X-Forwarded-For": "1.2.3.4"})

    answers = {resolve_client_ip(scope) for _ in range(5)}
    assert answers == {"198.51.100.23"}
    assert metrics.snapshot().get('jobhunter_edge_forwarded_for_total{reason="untrusted_peer"}') == 1


def _production(**overrides) -> Settings:
    """A production Settings that satisfies every *other* contract, so the proxy
    trust boundary is the only thing under test."""
    kwargs: Dict[str, Any] = dict(
        environment="production", secret_key="a" * 40, encryption_key="b" * 40,
        cors_origins=["https://app.example.com"], public_base_url="https://app.example.com",
        database_url="postgresql+psycopg2://u:p@db:5432/j", metrics_enabled=False,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


@pytest.mark.parametrize("trusted,forwarded", [
    (["*"], "127.0.0.1"),
    (["0.0.0.0/0"], "127.0.0.1"),
    ([], "*"),
    (["10.0.0.0/8"], "0.0.0.0/0"),
    (["not-an-ip"], "127.0.0.1"),
])
def test_production_refuses_an_unbounded_proxy_trust(trusted, forwarded):
    with pytest.raises(ValueError) as excinfo:
        _production(trusted_proxies=trusted, forwarded_allow_ips=forwarded)
    message = str(excinfo.value)
    assert "TRUSTED_PROXIES" in message or "FORWARDED_ALLOW_IPS" in message


def test_a_real_proxy_cidr_is_accepted_in_production():
    config = _production(trusted_proxies=["172.18.0.0/16"], forwarded_allow_ips="172.18.0.0/16")
    assert config.trusted_proxies == ["172.18.0.0/16"]


def test_the_dockerfile_restricts_forwarded_allow_ips():
    """The shipped image must not trust ``X-Forwarded-For`` from ``*``."""
    text = open(os.path.join(REPO_DIR, "Dockerfile"), encoding="utf-8").read()
    assert "--forwarded-allow-ips='*'" not in text
    assert '--forwarded-allow-ips=\\"${FORWARDED_ALLOW_IPS:-127.0.0.1}\\"' in text
    assert "--proxy-headers" in text
    assert "--workers ${WEB_CONCURRENCY:-1}" in text
    assert "FORWARDED_ALLOW_IPS" in open(os.path.join(REPO_DIR, ".env.example"), encoding="utf-8").read()


def settings_for_trust():
    """The live settings object (monkeypatch target) for trust-boundary tests."""
    from app.core.config import settings as live

    return live


# --------------------------------------------------------------------------- #
# 4. Metric cardinality
# --------------------------------------------------------------------------- #
def test_the_registry_caps_series_per_metric():
    metrics.reset()
    cap = settings_for_trust().metrics_max_series_per_metric
    for index in range(cap * 4):
        metrics.counter("jobhunter_probe_total", {"host": f"h{index}.example.com", "status": "200"})

    assert metrics.series_count("jobhunter_probe_total") == cap
    stats = metrics.stats()
    assert stats["evicted"] >= cap * 3
    assert stats["by_metric"]["jobhunter_probe_total"]["series"] == cap


def test_the_most_recently_used_series_survive_the_cap():
    metrics.reset()
    cap = settings_for_trust().metrics_max_series_per_metric
    metrics.counter("jobhunter_probe_total", {"host": "hot.example.com"})
    for index in range(cap * 2):
        metrics.counter("jobhunter_probe_total", {"host": f"cold{index}.example.com"})
    metrics.counter("jobhunter_probe_total", {"host": "hot.example.com"})  # touched last
    rendered = metrics.render_prometheus()
    assert 'host="hot.example.com"' in rendered


def test_unique_host_headers_do_not_create_series(client):
    """``Host`` is caller-chosen: it must not be a label on the inbound metrics."""
    metrics.reset()
    for index in range(200):
        response = client.get("/api/health", headers={"Host": f"h{index}.attacker.example"})
        assert response.status_code == 200

    assert metrics.series_count("jobhunter_http_requests_total") == 1
    assert metrics.series_count("jobhunter_http_request_duration_seconds") == 1
    rendered = metrics.render_prometheus()
    assert "attacker.example" not in rendered
    assert 'jobhunter_http_requests_total{method="GET",path="/api/health",status="200"}' in rendered


def test_inbound_series_are_method_path_template_and_status_only(client, auth):
    metrics.reset()
    client.get("/api/jobs", headers=auth)
    client.get("/api/jobs", headers=auth)
    lines = [line for line in metrics.render_prometheus().splitlines()
             if line.startswith("jobhunter_http_requests_total{")]
    assert lines
    for line in lines:
        block = line.split("{", 1)[1].rsplit("}", 1)[0]
        names = {pair.split("=", 1)[0] for pair in block.split(",")}
        assert names == {"method", "path", "status"}, line


def test_matched_requests_are_labelled_with_their_route_template(client, auth):
    metrics.reset()
    client.get("/api/jobs", headers=auth)
    rendered = metrics.render_prometheus()
    assert 'path="/api/jobs"' in rendered
    # A route with a parameter keeps one series for every id.
    from app.db import SessionLocal
    from app.models.models import Job, User

    db = SessionLocal()
    try:
        user = db.query(User).first()
        jobs = [Job(user_id=user.id, title=f"Role {i}", company="Acme", source="test",
                    url=f"https://e.com/{i}", dedupe_key=f"edge-{i}")
                for i in range(5)]
        db.add_all(jobs)
        db.commit()
        ids = [job.id for job in jobs]
    finally:
        db.close()
    for job_id in ids:
        client.get(f"/api/jobs/{job_id}", headers=auth)
    series = [line for line in metrics.render_prometheus().splitlines()
              if line.startswith("jobhunter_http_requests_total{")]
    assert sum(1 for line in series if "{job_id}" in line) == 1, series


def test_unmatched_urls_cannot_grow_the_path_label(client):
    """A scanner inventing URLs gets a bounded number of series, then ``other``."""
    from app.core import middleware

    middleware._path_labels.clear()
    metrics.reset()
    for index in range(400):
        client.get(f"/api/nope-{index}/and/{index}/more")

    assert metrics.series_count("jobhunter_http_requests_total") <= middleware._MAX_UNMATCHED_PATH_LABELS + 1
    assert len(middleware._path_labels) <= middleware._MAX_UNMATCHED_PATH_LABELS
    assert path_label({}, "/anything") == "other"


def test_outbound_hosts_collapse_to_a_bounded_label_set():
    from app.services.http import SOURCE_API_DOMAINS, _host_label

    assert _host_label("boards-api.greenhouse.io") == "greenhouse.io"
    assert _host_label("careers.jobs.lever.co") == "lever.co"
    assert _host_label("acme.wd1.myworkdaysite.com") == "myworkdaysite.com"
    assert _host_label("efts.sec.gov") == "sec.gov"
    # Arbitrary hosts pulled out of a job feed are one series, not thousands.
    for host in ("random-company.example", "careers.some-startup.io", "1.2.3.4", "", "x" * 80):
        assert _host_label(host) == "other", host
    assert len(SOURCE_API_DOMAINS) < 64


async def test_outbound_metrics_stay_bounded_across_many_hosts(client, monkeypatch):
    """The worker's own sweep: thousands of distinct hosts, a bounded registry."""
    import httpx

    from app.services import http, net_guard

    async def _public_resolver(*_args, **_kwargs):
        return ["93.184.216.34"]

    class FakeClient:
        async def request(self, method, url, **kwargs):
            return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, url),
                                  headers={"content-type": "application/json"})

    async def _fake_client():
        return FakeClient()

    monkeypatch.setattr(net_guard, "_resolve", _public_resolver)
    monkeypatch.setattr(http, "get_client", _fake_client)
    net_guard.clear_dns_cache()
    metrics.reset()

    for index in range(600):
        await http.request("GET", f"https://company-{index}.example/widgets", respect_robots=False)

    assert metrics.series_count("jobhunter_http_requests_total") <= 2
    assert 'host="other"' in metrics.render_prometheus()


# --------------------------------------------------------------------------- #
# 5. One worker by default, and it says so
# --------------------------------------------------------------------------- #
def test_raising_the_worker_count_warns_about_multiplied_budgets():
    warnings = Settings(web_concurrency=4).scaling_warnings()
    joined = " ".join(warnings)
    assert len(warnings) >= 1
    for topic in ("rate limit", "token", "override", "metrics"):
        assert topic in joined.lower(), joined
    assert "WEB_CONCURRENCY=4" in joined


def test_a_single_worker_warns_about_nothing():
    assert Settings(web_concurrency=1).scaling_warnings() == []


def test_the_pipeline_worker_doubling_is_called_out():
    joined = " ".join(Settings(web_concurrency=3, run_worker_in_api=True).scaling_warnings())
    assert "RUN_WORKER_IN_API" in joined and "3 copies" in joined


def test_compose_pins_a_single_worker_and_passes_the_proxy_boundary():
    text = open(os.path.join(REPO_DIR, "docker-compose.prod.yml"), encoding="utf-8").read()
    api_block = text.split("  worker:")[0]
    assert 'WEB_CONCURRENCY: "${WEB_CONCURRENCY:-1}"' in api_block
    assert 'FORWARDED_ALLOW_IPS: "${FORWARDED_ALLOW_IPS:-127.0.0.1}"' in api_block
    assert 'TRUSTED_PROXIES: "${TRUSTED_PROXIES:-}"' in api_block


def test_the_deployment_guide_documents_the_edge():
    text = open(os.path.join(REPO_DIR, "docs", "DEPLOYMENT.md"), encoding="utf-8").read()
    for needle in ("TRUSTED_PROXIES", "FORWARDED_ALLOW_IPS", "WEB_CONCURRENCY",
                   "rightmost", "METRICS_MAX_SERIES_PER_METRIC", "RATE_LIMIT_MAX_KEYS"):
        assert needle in text, needle


def test_env_example_declares_the_edge_settings():
    text = open(os.path.join(REPO_DIR, ".env.example"), encoding="utf-8").read()
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]*)\s*=", text, re.M))
    for key in ("TRUSTED_PROXIES", "FORWARDED_ALLOW_IPS", "WEB_CONCURRENCY", "RATE_LIMIT_MAX_KEYS",
                "METRICS_MAX_SERIES_PER_METRIC"):
        assert key in declared, key
        assert key.lower() in {name.upper(): name for name in Settings.model_fields}.values() or \
            key.lower() in Settings.model_fields, key


def test_ops_status_reports_the_edge(client, auth):
    body = client.get("/api/ops/status", headers=auth).json()
    edge = body["edge"]
    assert edge["rate_limiter"]["max_keys"] >= 1
    assert edge["rate_limiter"]["keys"] <= edge["rate_limiter"]["max_keys"]
    assert edge["body_limit"]["max_bytes"] > 0
    assert edge["registry"]["max_series_per_metric"] >= 1
    assert edge["registry"]["series"] >= 1
    assert edge["login_throttle"]["max_entries"] >= 1
    assert edge["workers"] == 1
    assert edge["client_ip"]["resolution"] in ("peer", "rightmost_untrusted_hop")
    # Never the keys or the credentials themselves.
    assert "ip:" not in str(edge["rate_limiter"])
