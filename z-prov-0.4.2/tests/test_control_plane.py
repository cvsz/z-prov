from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from z_prov.main import app
from z_prov.providers import ProviderClient


def _install_mock_provider(client: TestClient, provider: str, handler) -> None:
    router = client.app.state.router
    config = router.settings.providers[provider]
    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    router.clients[provider] = ProviderClient(config, mock_http)


def test_files_proxy_forwards_upload_to_native_provider(monkeypatch):
    monkeypatch.setenv("Z_PROV_CONFIG", "config/providers.example.yaml")
    monkeypatch.setenv("Z_PROV_CLIENT_KEYS", "test-client-key")
    seen: dict[str, httpx.Request] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"id": "file_123", "purpose": "user_data"})

    with TestClient(app) as client:
        _install_mock_provider(client, "anthropic", handler)
        response = client.post(
            "/v1/providers/anthropic/files",
            content=b"--boundary\r\ncontent\r\n--boundary--",
            headers={
                "content-type": "multipart/form-data; boundary=boundary",
                "x-api-key": "test-client-key",
            },
        )
    assert response.status_code == 200
    assert response.json()["id"] == "file_123"
    assert seen["request"].url.path.endswith("/files")
    assert seen["request"].headers["content-type"].startswith("multipart/form-data")


def test_files_proxy_rejects_unknown_provider(monkeypatch):
    monkeypatch.setenv("Z_PROV_CONFIG", "config/providers.example.yaml")
    monkeypatch.setenv("Z_PROV_CLIENT_KEYS", "test-client-key")
    with TestClient(app) as client:
        response = client.get(
            "/v1/providers/does-not-exist/files",
            headers={"x-api-key": "test-client-key"},
        )
    assert response.status_code == 404


def test_files_proxy_requires_authentication(monkeypatch):
    monkeypatch.setenv("Z_PROV_CONFIG", "config/providers.example.yaml")
    monkeypatch.setenv("Z_PROV_CLIENT_KEYS", "test-client-key")
    with TestClient(app) as client:
        response = client.get("/v1/providers/anthropic/files")
    assert response.status_code == 401


def test_files_proxy_enforces_max_file_bytes(monkeypatch):
    monkeypatch.setenv("Z_PROV_CONFIG", "config/providers.example.yaml")
    monkeypatch.setenv("Z_PROV_CLIENT_KEYS", "test-client-key")
    monkeypatch.setenv("Z_PROV_MAX_FILE_BYTES", "10")
    with TestClient(app) as client:
        response = client.post(
            "/v1/providers/anthropic/files",
            content=b"x" * 100,
            headers={"x-api-key": "test-client-key", "content-type": "application/octet-stream"},
        )
    assert response.status_code == 413


def test_batches_proxy_uses_native_anthropic_path(monkeypatch):
    monkeypatch.setenv("Z_PROV_CONFIG", "config/providers.example.yaml")
    monkeypatch.setenv("Z_PROV_CLIENT_KEYS", "test-client-key")
    seen: dict[str, httpx.Request] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"id": "batch_1", "processing_status": "in_progress"})

    with TestClient(app) as client:
        _install_mock_provider(client, "anthropic", handler)
        response = client.post(
            "/v1/providers/anthropic/batches",
            json={"requests": []},
            headers={"x-api-key": "test-client-key"},
        )
    assert response.status_code == 200
    assert "/messages/batches" in seen["request"].url.path


def test_batches_proxy_uses_openai_path_for_openai_compatible_provider(monkeypatch):
    monkeypatch.setenv("Z_PROV_CONFIG", "config/providers.example.yaml")
    monkeypatch.setenv("Z_PROV_CLIENT_KEYS", "test-client-key")
    seen: dict[str, httpx.Request] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"id": "batch_1"})

    with TestClient(app) as client:
        _install_mock_provider(client, "groq", handler)
        response = client.get(
            "/v1/providers/groq/batches/batch_1",
            headers={"x-api-key": "test-client-key"},
        )
    assert response.status_code == 200
    assert seen["request"].url.path.endswith("/batches/batch_1")


def test_upstream_error_is_classified_and_surfaced(monkeypatch):
    monkeypatch.setenv("Z_PROV_CONFIG", "config/providers.example.yaml")
    monkeypatch.setenv("Z_PROV_CLIENT_KEYS", "test-client-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    with TestClient(app) as client:
        _install_mock_provider(client, "anthropic", handler)
        response = client.get(
            "/v1/providers/anthropic/files/missing",
            headers={"x-api-key": "test-client-key"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found"
