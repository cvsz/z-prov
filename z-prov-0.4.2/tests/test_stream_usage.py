from __future__ import annotations

import json
import logging

import httpx
from fastapi.testclient import TestClient

from z_prov.main import app
from z_prov.providers import ProviderClient


def _install_mock_provider(client: TestClient, provider: str, handler) -> None:
    router = client.app.state.router
    config = router.settings.providers[provider]
    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    router.clients[provider] = ProviderClient(config, mock_http)


def _sse(*frames: str) -> bytes:
    return "".join(frames).encode()


def test_streaming_chat_completions_logs_a_usage_event(monkeypatch, caplog):
    # Regression test: before this fix, log_usage_event() was only ever
    # called from the non-streaming branch of each endpoint. A client
    # that set "stream": true never produced a usage log line at all,
    # contradicting the README's "every completed ... call emits one
    # structured JSON line" claim.
    monkeypatch.setenv("Z_PROV_CONFIG", "config/providers.example.yaml")
    monkeypatch.setenv("Z_PROV_CLIENT_KEYS", "test-client-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        body = _sse(
            'data: {"id":"c1","choices":[{"delta":{"content":"hi"},'
            '"finish_reason":null}]}\n\n',
            'data: {"id":"c1","choices":[{"delta":{},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":11,"completion_tokens":3,"total_tokens":14}}\n\n',
            "data: [DONE]\n\n",
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    with caplog.at_level(logging.INFO, logger="z_prov.usage"):
        with TestClient(app) as client:
            _install_mock_provider(client, "groq", handler)
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "z-prov-groq",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"x-api-key": "test-client-key"},
            )
            list(response.iter_bytes())  # drain the stream so the generator's finally runs

    events = [json.loads(r.message) for r in caplog.records if r.name == "z_prov.usage"]
    assert len(events) == 1
    assert events[0]["surface"] == "chat.completions"
    assert events[0]["provider"] == "groq"
    assert events[0]["stream"] is True
    assert events[0]["input_tokens"] == 11
    assert events[0]["output_tokens"] == 3


def test_streaming_anthropic_messages_logs_a_usage_event(monkeypatch, caplog):
    monkeypatch.setenv("Z_PROV_CONFIG", "config/providers.example.yaml")
    monkeypatch.setenv("Z_PROV_CLIENT_KEYS", "test-client-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        body = _sse(
            'event: message_start\ndata: {"type":"message_start","message":'
            '{"id":"m1","usage":{"input_tokens":20,"output_tokens":0}}}\n\n',
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"hi"}}\n\n',
            'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
            '"usage":{"output_tokens":5}}\n\n',
            'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    with caplog.at_level(logging.INFO, logger="z_prov.usage"):
        with TestClient(app) as client:
            _install_mock_provider(client, "anthropic", handler)
            response = client.post(
                "/v1/messages",
                json={
                    "model": "z-prov-claude",
                    "stream": True,
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"x-api-key": "test-client-key"},
            )
            list(response.iter_bytes())

    events = [json.loads(r.message) for r in caplog.records if r.name == "z_prov.usage"]
    assert len(events) == 1
    assert events[0]["surface"] == "messages"
    assert events[0]["provider"] == "anthropic"
    assert events[0]["input_tokens"] == 20
    assert events[0]["output_tokens"] == 5


def test_streaming_responses_logs_a_usage_event(monkeypatch, caplog):
    monkeypatch.setenv("Z_PROV_CONFIG", "config/providers.example.yaml")
    monkeypatch.setenv("Z_PROV_CLIENT_KEYS", "test-client-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        body = _sse(
            'data: {"type":"response.created","response":{"id":"r1","status":"in_progress"}}\n\n',
            'data: {"type":"response.completed","response":{"id":"r1","status":"completed",'
            '"usage":{"input_tokens":9,"output_tokens":2,"total_tokens":11}}}\n\n',
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    with caplog.at_level(logging.INFO, logger="z_prov.usage"):
        with TestClient(app) as client:
            _install_mock_provider(client, "openai", handler)
            response = client.post(
                "/v1/responses",
                json={"model": "z-prov-codex", "stream": True, "input": "hi"},
                headers={"x-api-key": "test-client-key"},
            )
            list(response.iter_bytes())

    events = [json.loads(r.message) for r in caplog.records if r.name == "z_prov.usage"]
    assert len(events) == 1
    assert events[0]["surface"] == "responses"
    assert events[0]["provider"] == "openai"
    assert events[0]["input_tokens"] == 9
    assert events[0]["output_tokens"] == 2


def test_streaming_usage_event_logs_even_when_backend_never_sends_usage(monkeypatch, caplog):
    # No usage field anywhere in the stream (e.g. a backend that omits
    # stream_options.include_usage support). A usage event must still be
    # emitted -- with zero counts -- rather than silently skipped, exactly
    # like the existing non-streaming "missing usage" behavior.
    monkeypatch.setenv("Z_PROV_CONFIG", "config/providers.example.yaml")
    monkeypatch.setenv("Z_PROV_CLIENT_KEYS", "test-client-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        body = _sse(
            'data: {"id":"c1","choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n',
            'data: {"id":"c1","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            "data: [DONE]\n\n",
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    with caplog.at_level(logging.INFO, logger="z_prov.usage"):
        with TestClient(app) as client:
            _install_mock_provider(client, "groq", handler)
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "z-prov-groq",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"x-api-key": "test-client-key"},
            )
            list(response.iter_bytes())

    events = [json.loads(r.message) for r in caplog.records if r.name == "z_prov.usage"]
    assert len(events) == 1
    assert events[0]["input_tokens"] == 0
    assert events[0]["output_tokens"] == 0
