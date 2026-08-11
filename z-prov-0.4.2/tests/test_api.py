from fastapi.testclient import TestClient

from z_prov.main import _anthropic_to_openai_response, _anthropic_usage_to_openai, app


def test_anthropic_response_thinking_and_refusal_map_correctly():
    payload = {
        "id": "msg_1",
        "content": [
            {"type": "thinking", "thinking": "reasoning trace"},
            {"type": "text", "text": "final answer"},
        ],
        "stop_reason": "refusal",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    result = _anthropic_to_openai_response(payload, "alias")
    assert result["choices"][0]["message"]["reasoning_content"] == "reasoning trace"
    assert result["choices"][0]["message"]["content"] == "final answer"
    assert result["choices"][0]["finish_reason"] == "content_filter"


def test_anthropic_response_max_tokens_maps_to_length():
    payload = {
        "content": [{"type": "text", "text": "partial"}],
        "stop_reason": "max_tokens",
        "usage": {},
    }
    result = _anthropic_to_openai_response(payload, "alias")
    assert result["choices"][0]["finish_reason"] == "length"


def test_anthropic_cache_read_tokens_surface_as_openai_cached_tokens():
    usage = _anthropic_usage_to_openai({
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_input_tokens": 90,
        "cache_creation_input_tokens": 5,
    })
    assert usage["prompt_tokens_details"] == {"cached_tokens": 90}
    assert usage["cache_creation_input_tokens"] == 5
    assert usage["prompt_tokens"] == 100


def test_usage_without_cache_fields_has_no_cache_keys():
    usage = _anthropic_usage_to_openai({"input_tokens": 3, "output_tokens": 1})
    assert "prompt_tokens_details" not in usage
    assert "cache_creation_input_tokens" not in usage


def test_health_and_authenticated_models(monkeypatch):
    monkeypatch.setenv("Z_PROV_CONFIG", "config/providers.example.yaml")
    monkeypatch.setenv("Z_PROV_CLIENT_KEYS", "test-client-key")
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").json()["status"] == "ready"
        assert client.get("/v1/models").status_code == 401
        response = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer test-client-key"},
        )
        assert response.status_code == 200
        assert "z-prov-codex" in {item["id"] for item in response.json()["data"]}


def test_invalid_json_is_rejected_before_provider_call(monkeypatch):
    monkeypatch.setenv("Z_PROV_CONFIG", "config/providers.example.yaml")
    monkeypatch.setenv("Z_PROV_CLIENT_KEYS", "test-client-key")
    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            content=b"{",
            headers={
                "content-type": "application/json",
                "x-api-key": "test-client-key",
            },
        )
        assert response.status_code == 400


def test_request_boundary_adds_safe_headers_and_preserves_valid_request_id(monkeypatch):
    monkeypatch.setenv("Z_PROV_CONFIG", "config/providers.example.yaml")
    monkeypatch.setenv("Z_PROV_CLIENT_KEYS", "test-client-key")
    with TestClient(app) as client:
        response = client.get(
            "/health/live",
            headers={"X-Request-ID": "trace-123"},
        )
    assert response.headers["x-request-id"] == "trace-123"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
