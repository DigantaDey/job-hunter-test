"""Async singletons must not be reused across event loops."""
from __future__ import annotations

import asyncio

import pytest

from app.services import ai_client, http


@pytest.fixture(autouse=True)
def _reset_singletons():
    yield
    http._client = None
    http._client_loop = None
    http._semaphore = None
    http._semaphore_loop = None
    ai_client._semaphore = None
    ai_client._semaphore_loop = None


def _grab():
    async def _inner():
        return await http.get_client(), http._sem(), ai_client._sem()
    return asyncio.run(_inner())


def test_singletons_are_rebuilt_for_each_event_loop():
    first = _grab()
    second = _grab()
    assert first[0] is not second[0], "http client reused across a closed loop"
    assert first[1] is not second[1], "http semaphore reused across a closed loop"
    assert first[2] is not second[2], "ai semaphore reused across a closed loop"


def test_a_closed_loop_client_is_never_handed_out():
    stale = _grab()
    _grab()

    async def _check():
        client = await http.get_client()
        assert client is not stale[0]
        assert http._client_loop is asyncio.get_running_loop()
        assert not http._client_loop.is_closed()
    asyncio.run(_check())


def test_same_loop_keeps_the_same_client():
    async def _inner():
        return (await http.get_client()) is (await http.get_client())
    assert asyncio.run(_inner())
