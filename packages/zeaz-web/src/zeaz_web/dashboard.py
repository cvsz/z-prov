"""Server-side dashboard aggregation with no browser-held credentials."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    model_validator,
)

from zeaz_web.views import StateReader, StateSnapshot

WEB_ROOT = Path(__file__).parent / "static"
HostLabel = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class DashboardSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gateway_url: str = "http://127.0.0.1:8080"
    control_url: str = "http://127.0.0.1:8090"
    enterprise_url: str = "http://127.0.0.1:8091"
    gateway_key: SecretStr | None = None
    request_timeout_seconds: float = Field(default=5, gt=0, le=30)
    max_response_bytes: int = Field(default=1_048_576, ge=1024, le=8_388_608)
    session_db_path: Path | None = None
    audit_log_path: Path | None = None
    receipts_dir: Path | None = None
    admin_key_hashes: frozenset[bytes] = frozenset()

    @model_validator(mode="after")
    def validate_upstreams(self) -> DashboardSettings:
        for field in ("gateway_url", "control_url", "enterprise_url"):
            _validate_upstream(getattr(self, field))
        return self

    @classmethod
    def from_env(cls) -> DashboardSettings:
        key = os.getenv("ZEAZ_WEB_GATEWAY_KEY")
        return cls(
            gateway_url=os.getenv("ZEAZ_WEB_GATEWAY_URL", "http://127.0.0.1:8080"),
            control_url=os.getenv("ZEAZ_WEB_CONTROL_URL", "http://127.0.0.1:8090"),
            enterprise_url=os.getenv(
                "ZEAZ_WEB_ENTERPRISE_URL", "http://127.0.0.1:8091"
            ),
            gateway_key=SecretStr(key) if key else None,
            session_db_path=_env_path("ZEAZ_WEB_SESSION_DB"),
            audit_log_path=_env_path("ZEAZ_WEB_AUDIT_LOG"),
            receipts_dir=_env_path("ZEAZ_WEB_RECEIPTS_DIR"),
            admin_key_hashes=_admin_hashes(os.getenv("ZEAZ_WEB_ADMIN_KEY_HASHES", "")),
        )


class ServiceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: HostLabel
    url: str
    status: str
    latency_ms: int | None
    detail: str | None = None


class ModelSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    owned_by: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    route: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class RouteSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    provider: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    status: Literal["healthy", "unknown"]
    model_count: int = Field(ge=0, le=1000)


class DashboardSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: str
    services: tuple[ServiceSummary, ...]
    models: tuple[ModelSummary, ...]
    routes: tuple[RouteSummary, ...]


async def _fetch(
    client: httpx.AsyncClient,
    name: str,
    base_url: str,
    path: str,
    settings: DashboardSettings,
    *,
    auth: bool = False,
) -> tuple[ServiceSummary, dict[str, Any] | None]:
    import time

    started = time.monotonic()
    headers = {"accept": "application/json"}
    if auth and settings.gateway_key is not None:
        headers["authorization"] = f"Bearer {settings.gateway_key.get_secret_value()}"
    try:
        async with client.stream(
            "GET",
            f"{base_url.rstrip('/')}{path}",
            headers=headers,
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,
        ) as response:
            if response.is_redirect:
                raise RuntimeError("redirects are forbidden")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > settings.max_response_bytes:
                    raise RuntimeError("response exceeded byte limit")
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f"HTTP {response.status_code}")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("response is not an object")
        latency = max(0, int((time.monotonic() - started) * 1000))
        return ServiceSummary(name=name, url=base_url, status="healthy", latency_ms=latency), value
    except Exception as exc:
        latency = max(0, int((time.monotonic() - started) * 1000))
        return ServiceSummary(
            name=name,
            url=base_url,
            status="unavailable",
            latency_ms=latency,
            detail=str(exc)[:128],
        ), None


def _model_summaries(value: dict[str, Any] | None) -> tuple[ModelSummary, ...]:
    if value is None or not isinstance(value.get("data"), list):
        return ()
    summaries: list[ModelSummary] = []
    for item in value["data"][:1000]:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        owner = item.get("owned_by", "unknown")
        if not isinstance(model_id, str) or not isinstance(owner, str):
            continue
        summaries.append(ModelSummary(id=model_id[:256], owned_by=owner[:256], route=model_id[:256]))
    return tuple(summaries)


def _route_summaries(
    models: tuple[ModelSummary, ...], gateway_healthy: bool
) -> tuple[RouteSummary, ...]:
    grouped: dict[str, int] = {}
    for model in models:
        grouped[model.owned_by] = grouped.get(model.owned_by, 0) + 1
    return tuple(
        RouteSummary(
            name=provider,
            provider=provider,
            status="healthy" if gateway_healthy else "unknown",
            model_count=count,
        )
        for provider, count in sorted(grouped.items())
    )


def _validate_upstream(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("dashboard upstream must be a credential-free origin")
    if parsed.scheme == "http" and "." in parsed.hostname and parsed.hostname not in {
        "localhost",
    }:
        raise ValueError("non-local dashboard upstreams must use HTTPS")


def create_app(settings: DashboardSettings | None = None) -> FastAPI:
    resolved = settings or DashboardSettings.from_env()
    state_reader = StateReader(
        session_db_path=resolved.session_db_path,
        audit_log_path=resolved.audit_log_path,
        receipts_dir=resolved.receipts_dir,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.dashboard_settings = resolved
        yield

    app = FastAPI(
        title="ZeaZ Console",
        version="0.1.0a1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/api/dashboard", response_model=DashboardSnapshot)
    async def dashboard() -> DashboardSnapshot:
        async with httpx.AsyncClient(trust_env=False) as client:
            results = await asyncio.gather(
                _fetch(client, "gateway", resolved.gateway_url, "/health/ready", resolved),
                _fetch(client, "control", resolved.control_url, "/health/ready", resolved),
                _fetch(client, "enterprise", resolved.enterprise_url, "/health/ready", resolved),
                _fetch(
                    client,
                    "gateway-models",
                    resolved.gateway_url,
                    "/v1/models",
                    resolved,
                    auth=True,
                ),
            )
        services = tuple(item[0] for item in results[:3])
        models = _model_summaries(results[3][1])
        from datetime import UTC, datetime

        return DashboardSnapshot(
            generated_at=datetime.now(UTC).isoformat(),
            services=services,
            models=models,
            routes=_route_summaries(models, services[0].status == "healthy"),
        )

    @app.get("/api/state", response_model=StateSnapshot)
    async def state() -> StateSnapshot:
        return state_reader.snapshot()

    @app.get("/api/admin/state", response_model=StateSnapshot)
    async def admin_state(request: Request) -> StateSnapshot:
        _require_admin(request, resolved)
        return state_reader.snapshot()

    @app.post("/api/chat/{protocol}")
    async def chat(protocol: str, request: Request) -> StreamingResponse:
        if protocol not in {"anthropic", "chat", "responses"}:
            raise HTTPException(status_code=404, detail="Unsupported protocol")
        if resolved.gateway_key is None:
            raise HTTPException(
                status_code=503, detail="Gateway credential is not configured"
            )
        payload = await _request_json(request, 262_144)
        payload["stream"] = True
        paths = {
            "anthropic": "/v1/messages",
            "chat": "/v1/chat/completions",
            "responses": "/v1/responses",
        }
        return StreamingResponse(
            _stream_chat(paths[protocol], payload, resolved),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-store",
                "x-content-type-options": "nosniff",
            },
        )

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_ROOT / "index.html")

    @app.get("/chat", include_in_schema=False)
    async def chat_page() -> FileResponse:
        return FileResponse(WEB_ROOT / "chat.html")

    @app.get("/sessions", include_in_schema=False)
    async def sessions_page() -> FileResponse:
        return FileResponse(WEB_ROOT / "sessions.html")

    @app.get("/admin", include_in_schema=False)
    async def admin_page() -> FileResponse:
        return FileResponse(WEB_ROOT / "admin.html")

    @app.get("/assets/{asset}", include_in_schema=False)
    async def asset(asset: str) -> FileResponse:
        if "/" in asset or asset not in {
            "app.js",
            "chat.css",
            "chat.js",
            "styles.css",
            "sessions.css",
            "sessions.js",
            "admin.js",
            "admin.css",
        }:
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(WEB_ROOT / asset)

    return app


def _env_path(name: str) -> Path | None:
    value = os.getenv(name)
    return Path(value) if value else None


def _admin_hashes(value: str) -> frozenset[bytes]:
    hashes: set[bytes] = set()
    for item in value.split(","):
        item = item.strip().lower()
        if not item:
            continue
        if len(item) != 64:
            raise ValueError("admin key hashes must be SHA-256 hex digests")
        try:
            hashes.add(bytes.fromhex(item))
        except ValueError as exc:
            raise ValueError("admin key hashes must be SHA-256 hex digests") from exc
    return frozenset(hashes)


def _require_admin(request: Request, settings: DashboardSettings) -> None:
    candidate = request.headers.get("x-zeaz-admin-key", "")
    if not candidate or not settings.admin_key_hashes:
        raise HTTPException(status_code=404, detail="Not found")
    digest = hashlib.sha256(candidate.encode("utf-8")).digest()
    if not any(hmac.compare_digest(digest, expected) for expected in settings.admin_key_hashes):
        raise HTTPException(status_code=404, detail="Not found")


async def _request_json(request: Request, max_bytes: int) -> dict[str, Any]:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise HTTPException(status_code=413, detail="Request is too large")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400, detail="Request must be valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=400, detail="Request must be a JSON object"
        )
    return value


async def _stream_chat(
    path: str, payload: dict[str, Any], settings: DashboardSettings
):
    headers = {
        "accept": "text/event-stream",
        "content-type": "application/json",
        "authorization": (
            f"Bearer {settings.gateway_key.get_secret_value()}"  # type: ignore[union-attr]
        ),
    }
    try:
        total = 0
        async with httpx.AsyncClient(trust_env=False) as client:
            async with client.stream(
                "POST",
                f"{settings.gateway_url.rstrip('/')}{path}",
                headers=headers,
                json=payload,
                timeout=settings.request_timeout_seconds,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    yield 'event: error\ndata: {"message":"Upstream redirect rejected"}\n\n'
                    return
                if not 200 <= response.status_code < 300:
                    yield 'event: error\ndata: {"message":"Gateway request failed"}\n\n'
                    return
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > settings.max_response_bytes:
                        yield 'event: error\ndata: {"message":"Gateway stream exceeded its byte limit"}\n\n'
                        return
                    yield chunk
    except Exception:
        yield 'event: error\ndata: {"message":"Gateway stream unavailable"}\n\n'
