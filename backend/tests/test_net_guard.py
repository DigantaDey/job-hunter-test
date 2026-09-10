"""
SSRF guard: the outbound URL policy that sits in front of every fetch.

The suite runs offline, so DNS is either monkeypatched or absent — which is
itself a documented behaviour: an unresolvable host is deferred to the HTTP
client instead of being reported as a policy violation.
"""
from __future__ import annotations

import socket

import pytest

from app.core.config import settings
from app.services import net_guard
from app.services.net_guard import OutboundURLBlocked, check_url, clear_dns_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_dns_cache()
    yield
    clear_dns_cache()


async def _public_resolver(*_args, **_kwargs):
    return ["93.184.216.34"]


async def _private_resolver(*_args, **_kwargs):
    return ["10.1.2.3"]


async def _metadata_resolver(*_args, **_kwargs):
    return ["169.254.169.254"]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/api/jobs",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://192.168.1.10/router",
        "http://172.16.9.9/",
        "http://100.64.7.7/",
        "http://redis.local/",
        "http://api.internal/",
        "http://example.com:6379/",
        "http://example.com:5432/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "http://user:password@example.com/",
        "http:///missing-host",
    ],
)
@pytest.mark.asyncio
async def test_dangerous_urls_are_refused(url):
    with pytest.raises(OutboundURLBlocked):
        await check_url(url)


@pytest.mark.asyncio
async def test_public_host_is_allowed(monkeypatch):
    monkeypatch.setattr(net_guard, "_resolve", _public_resolver)
    await check_url("https://boards-api.greenhouse.io/v1/boards/acme/jobs")


@pytest.mark.asyncio
async def test_private_dns_answer_is_refused(monkeypatch):
    monkeypatch.setattr(net_guard, "_resolve", _private_resolver)
    with pytest.raises(OutboundURLBlocked) as excinfo:
        await check_url("https://jobs.example.com/listing")
    assert "private address" in str(excinfo.value)


@pytest.mark.asyncio
async def test_metadata_ip_in_dns_answer_is_refused(monkeypatch):
    monkeypatch.setattr(net_guard, "_resolve", _metadata_resolver)
    with pytest.raises(OutboundURLBlocked):
        await check_url("https://sneaky.example.com/")


@pytest.mark.asyncio
async def test_dns_failure_defers_to_the_http_client(monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise socket.gaierror("name resolution unavailable")

    monkeypatch.setattr(net_guard, "_resolve", _boom)
    # No exception: an offline/mocked environment must not look like an attack.
    await check_url("https://still.example.com/")


@pytest.mark.asyncio
async def test_allowlist_bypasses_the_private_address_check(monkeypatch):
    monkeypatch.setattr(net_guard, "_resolve", _private_resolver)
    monkeypatch.setattr(settings, "outbound_allowed_hosts", ["jobs.example.com"], raising=False)
    await check_url("https://jobs.example.com/listing")


@pytest.mark.asyncio
async def test_allowlist_matches_subdomains(monkeypatch):
    monkeypatch.setattr(net_guard, "_resolve", _private_resolver)
    monkeypatch.setattr(settings, "outbound_allowed_hosts", ["corp.example.com"], raising=False)
    await check_url("http://intranet.corp.example.com/dashboard")
    with pytest.raises(OutboundURLBlocked):
        await check_url("http://notcorp.example.com/dashboard")


@pytest.mark.asyncio
async def test_allow_private_flag_accepts_internal_targets(monkeypatch):
    monkeypatch.setattr(settings, "outbound_allow_private", True, raising=False)
    await check_url("http://10.0.0.5/internal-jobs")
    # Schemes and credentials are still policed.
    with pytest.raises(OutboundURLBlocked):
        await check_url("file:///etc/passwd")
    with pytest.raises(OutboundURLBlocked):
        await check_url("http://user:pw@10.0.0.5/internal-jobs")


@pytest.mark.asyncio
async def test_dns_verdicts_are_cached(monkeypatch):
    calls = {"n": 0}

    async def _counting(*_args, **_kwargs):
        calls["n"] += 1
        return ["93.184.216.34"]

    monkeypatch.setattr(net_guard, "_resolve", _counting)
    await check_url("https://cached.example.com/")
    await check_url("https://cached.example.com/other")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_http_layer_enforces_the_policy():
    from app.services import http

    with pytest.raises(OutboundURLBlocked):
        await http.request("GET", "http://127.0.0.1:9/internal")


@pytest.mark.asyncio
async def test_transport_guard_checks_redirect_hops():
    seen = []

    class FakeTransport:
        async def handle_async_request(self, request):
            seen.append(str(request.url))
            return "response"

    transport = net_guard.install_transport_guard(FakeTransport())
    with pytest.raises(OutboundURLBlocked):
        await transport.handle_async_request(type("R", (), {"url": "http://169.254.169.254/"})())
    assert seen == []

    allowed = await transport.handle_async_request(
        type("R", (), {"url": "https://93.184.216.34/board"})(),
    )
    assert allowed == "response"
    assert seen == ["https://93.184.216.34/board"]
