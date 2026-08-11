from __future__ import annotations

import httpx
import pytest

from z_prov.config import ProviderConfig
from z_prov.errors import ErrorKind, ProviderError, classify_http_error
from z_prov.providers import ProviderClient


def test_http_error_classification_separates_retry_and_fallback():
    missing = classify_http_error(404, "missing")
    assert not missing.retryable
    assert missing.fallback_allowed
    assert not missing.circuit_failure

    invalid = classify_http_error(400, "invalid")
    assert invalid.kind == ErrorKind.BAD_REQUEST
    assert not invalid.retryable
    assert not invalid.fallback_allowed

    limited = classify_http_error(429, "limited", retry_after=2)
    assert limited.kind == ErrorKind.RATE_LIMIT
    assert limited.retryable
    assert limited.fallback_allowed
    assert limited.retry_after == 2


@pytest.mark.asyncio
async def test_provider_forwards_extra_headers_to_backend():
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    config = ProviderConfig(
        name="anthropic-test",
        api="anthropic",
        base_url="https://provider.example/v1",
        api_key="sk-test",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ProviderClient(config, http)
        await client.messages(
            {"model": "test"},
            extra_headers={"anthropic-beta": "mid-conversation-tool-changes-2026-07-01"},
        )
    assert seen["anthropic-beta"] == "mid-conversation-tool-changes-2026-07-01"


@pytest.mark.asyncio
async def test_provider_retries_network_timeout_then_succeeds():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"ok": True})

    config = ProviderConfig(
        name="test",
        api="openai",
        base_url="https://provider.example/v1",
        max_attempts=2,
        retry_base_seconds=0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ProviderClient(config, http)
        assert await client.chat({"model": "test"}) == {"ok": True}
    assert calls == 2


@pytest.mark.asyncio
async def test_provider_does_not_retry_bad_request():
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": "invalid"})

    config = ProviderConfig(
        name="test",
        api="openai",
        base_url="https://provider.example/v1",
        max_attempts=3,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ProviderClient(config, http)
        with pytest.raises(ProviderError) as error:
            await client.chat({"model": "test"})
    assert error.value.kind == ErrorKind.BAD_REQUEST
    assert calls == 1


@pytest.mark.asyncio
async def test_stream_failures_trip_the_same_circuit_breaker_as_non_streaming_calls():
    # Before this fix, ProviderClient.stream() never touched
    # self.resilience.breaker at all, so a provider that was persistently
    # down for streaming traffic never tripped its circuit: every single
    # streaming request kept retrying the dead provider first instead of
    # the router learning to prefer a healthy fallback.
    async def always_fails(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    config = ProviderConfig(
        name="test",
        api="openai",
        base_url="https://provider.example/v1",
        circuit_failure_threshold=2,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(always_fails)) as http:
        client = ProviderClient(config, http)
        assert client.resilience.breaker.state == "closed"

        for _ in range(2):
            with pytest.raises(ProviderError):
                async for _chunk in client.stream("chat/completions", {"model": "test"}):
                    pass

        assert client.resilience.breaker.state == "open"

        # With the breaker open, a further stream attempt should fail fast
        # with CIRCUIT_OPEN rather than hitting the (still-dead) transport.
        with pytest.raises(ProviderError) as error:
            async for _chunk in client.stream("chat/completions", {"model": "test"}):
                pass
        assert error.value.kind == ErrorKind.CIRCUIT_OPEN


@pytest.mark.asyncio
async def test_successful_stream_closes_the_circuit_breaker():
    async def ok_stream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data: {}\n\n")

    config = ProviderConfig(
        name="test", api="openai", base_url="https://provider.example/v1",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(ok_stream)) as http:
        client = ProviderClient(config, http)
        client.resilience.breaker.failure()  # simulate a prior failure
        chunks = [chunk async for chunk in client.stream("chat/completions", {"model": "test"})]
    assert chunks
    assert client.resilience.breaker.state == "closed"
