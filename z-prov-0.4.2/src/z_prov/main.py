from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import __version__
from .config import Settings, load_settings
from .errors import classify_http_error
from .normalize import (
    anthropic_to_openai,
    chat_to_responses,
    openai_request_to_anthropic,
    openai_to_anthropic,
    responses_to_chat,
)
from .providers import ProviderClient, ProviderError
from .router import ProviderRouter
from .security import InMemoryRateLimiter, client_bucket, redact, request_id
from .streaming import (
    anthropic_to_chat_stream,
    chat_to_anthropic_stream,
    chat_to_responses_stream,
)
from .usage import log_usage_event


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        follow_redirects=False,
        trust_env=False,
    )
    app.state.settings = settings
    app.state.client = client
    app.state.router = ProviderRouter(settings, client)
    app.state.rate_limiter = InMemoryRateLimiter(settings.rate_limit_per_minute)
    yield
    await client.aclose()


app = FastAPI(title="Z-Prov", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def request_boundary(request: Request, call_next):
    correlation_id = request_id(request.headers.get("x-request-id"))
    request.state.request_id = correlation_id
    peer = request.client.host if request.client else "unknown"
    bucket = client_bucket(
        request.headers.get("authorization"),
        request.headers.get("x-api-key"),
        peer,
    )
    allowed, remaining = request.app.state.rate_limiter.allow(bucket)
    if not allowed:
        response = JSONResponse(
            status_code=429,
            content={"error": {"type": "rate_limit_error", "message": "Rate limit exceeded"}},
            headers={"Retry-After": "60"},
        )
    else:
        response = await call_next(request)
    response.headers["X-Request-ID"] = correlation_id
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def settings(request: Request) -> Settings:
    return request.app.state.settings


def router(request: Request) -> ProviderRouter:
    return request.app.state.router


RouterDependency = Annotated[ProviderRouter, Depends(router)]


async def authenticate(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    configured = settings(request).client_keys
    if not configured and os.getenv("Z_PROV_ALLOW_UNAUTHENTICATED", "false").lower() == "true":
        return
    bearer = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not configured or (x_api_key not in configured and bearer not in configured):
        raise HTTPException(status_code=401, detail="Invalid API key")


async def body(request: Request) -> dict[str, Any]:
    maximum = settings(request).max_request_bytes
    raw = await request.body()
    if len(raw) > maximum:
        raise HTTPException(status_code=413, detail="Request body too large")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    tokens = value.get("max_tokens", value.get("max_output_tokens", 4096))
    if not isinstance(tokens, int) or tokens < 1 or tokens > settings(request).max_output_tokens:
        raise HTTPException(status_code=400, detail="Invalid output token limit")
    return value


@app.exception_handler(ProviderError)
async def provider_error(_: Request, exc: ProviderError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "type": exc.kind,
                "message": redact(str(exc)),
            }
        },
    )


@app.get("/health/live")
async def live():
    return {"status": "ok", "version": __version__}


@app.get("/health/ready")
async def ready(request: Request):
    return {
        "status": "ready",
        "providers": len(settings(request).providers),
        "models": len(settings(request).models),
    }


@app.get("/v1/models", dependencies=[Depends(authenticate)])
async def models(provider_router: RouterDependency):
    return {"object": "list", "data": provider_router.model_list()}


@app.post("/v1/models/refresh", dependencies=[Depends(authenticate)])
async def refresh_models(provider_router: RouterDependency):
    refreshed: dict[str, int] = {}
    errors: dict[str, str] = {}
    for name, client in provider_router.clients.items():
        try:
            refreshed[name] = await provider_router.capabilities.refresh_provider(name, client)
        except ProviderError as exc:
            errors[name] = f"{exc.kind}: {exc}"
    return {"refreshed": refreshed, "errors": errors}


def _batches_endpoint(client: ProviderClient) -> str:
    # Anthropic's native batch API lives under /v1/messages/batches; every
    # OpenAI-compatible surface (including the Responses providers) uses
    # /v1/batches. This is intentionally the only piece of protocol-specific
    # knowledge in the proxy path -- everything else about the request and
    # response bodies is passed through unmodified, per the audit's
    # "provider extension namespace before cross-provider normalization"
    # guidance: Z-Prov does not attempt to normalize batch or file payload
    # shapes across providers.
    return "messages/batches" if client.config.api == "anthropic" else "batches"


def _forwarded_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    content_type = request.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    beta = request.headers.get("anthropic-beta")
    if beta:
        headers["anthropic-beta"] = beta
    return headers


async def _provider_or_404(provider_router: ProviderRouter, provider: str) -> ProviderClient:
    client = provider_router.clients.get(provider)
    if client is None:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")
    return client


async def _proxy(
    request: Request,
    provider_router: RouterDependency,
    provider: str,
    endpoint: str,
    *,
    params: dict[str, str] | None = None,
) -> Response:
    client = await _provider_or_404(provider_router, provider)
    raw = await request.body()
    if len(raw) > settings(request).max_file_bytes:
        raise HTTPException(status_code=413, detail="Request body too large")
    upstream = await client.raw(
        request.method,
        endpoint,
        content=raw or None,
        headers=_forwarded_headers(request),
        params=params,
    )
    if upstream.status_code >= 400:
        raise classify_http_error(upstream.status_code, upstream.text[:8192])
    response_content_type = upstream.headers.get("content-type", "application/json")
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=response_content_type,
    )


@app.api_route(
    "/v1/providers/{provider}/files",
    methods=["GET", "POST"],
    dependencies=[Depends(authenticate)],
)
async def provider_files(request: Request, provider_router: RouterDependency, provider: str):
    # Files proxying (P1 in the upgrade audit): strict size limits via
    # max_file_bytes, no cross-provider normalization, and no content
    # inspection here -- malware/content policy is the operator's and the
    # upstream provider's responsibility, same as any other proxied upload.
    return await _proxy(request, provider_router, provider, "files")


@app.api_route(
    "/v1/providers/{provider}/files/{file_id}",
    methods=["GET", "DELETE"],
    dependencies=[Depends(authenticate)],
)
async def provider_file(request: Request, provider_router: RouterDependency, provider: str, file_id: str):
    return await _proxy(request, provider_router, provider, f"files/{file_id}")


@app.api_route(
    "/v1/providers/{provider}/batches",
    methods=["GET", "POST"],
    dependencies=[Depends(authenticate)],
)
async def provider_batches(request: Request, provider_router: RouterDependency, provider: str):
    client = await _provider_or_404(provider_router, provider)
    return await _proxy(request, provider_router, provider, _batches_endpoint(client))


@app.api_route(
    "/v1/providers/{provider}/batches/{batch_id}",
    methods=["GET"],
    dependencies=[Depends(authenticate)],
)
async def provider_batch(request: Request, provider_router: RouterDependency, provider: str, batch_id: str):
    client = await _provider_or_404(provider_router, provider)
    return await _proxy(request, provider_router, provider, f"{_batches_endpoint(client)}/{batch_id}")


@app.post(
    "/v1/providers/{provider}/batches/{batch_id}/cancel",
    dependencies=[Depends(authenticate)],
)
async def provider_batch_cancel(
    request: Request, provider_router: RouterDependency, provider: str, batch_id: str
):
    client = await _provider_or_404(provider_router, provider)
    return await _proxy(
        request, provider_router, provider, f"{_batches_endpoint(client)}/{batch_id}/cancel"
    )


@app.post("/v1/chat/completions", dependencies=[Depends(authenticate)])
async def chat_completions(request: Request, provider_router: RouterDependency):
    payload = await body(request)
    route = provider_router.route(payload.get("model"))
    beta_header = request.headers.get("anthropic-beta")
    if payload.get("stream"):
        return StreamingResponse(
            _stream_with_usage_logging(
                request, route, "chat.completions",
                lambda served: _stream_openai(
                    provider_router, route, payload, "chat/completions", beta_header, served
                ),
            ),
            media_type="text/event-stream",
        )

    async def call(client: ProviderClient, model: str):
        started = time.monotonic()
        outbound = {**payload, "model": model}
        if client.config.api == "anthropic":
            anthropic_payload = openai_request_to_anthropic(outbound, model)
            extra = {"anthropic-beta": beta_header} if beta_header else None
            result = await client.messages(anthropic_payload, extra_headers=extra)
            converted = _anthropic_to_openai_response(result, route.alias)
        else:
            converted = await client.chat(outbound)
        _log_usage(
            request, route.alias, client.config.name, model, "chat.completions",
            converted.get("usage"), started,
        )
        return converted

    return await provider_router.execute(route, call)


@app.post("/v1/messages", dependencies=[Depends(authenticate)])
async def messages(request: Request, provider_router: RouterDependency):
    payload = await body(request)
    route = provider_router.route(payload.get("model"))
    beta_header = request.headers.get("anthropic-beta")
    if payload.get("stream"):
        return StreamingResponse(
            _stream_with_usage_logging(
                request, route, "messages",
                lambda served: _stream_anthropic(provider_router, route, payload, beta_header, served),
            ),
            media_type="text/event-stream",
        )

    async def call(client: ProviderClient, model: str):
        started = time.monotonic()
        outbound = {**payload, "model": model}
        if client.config.api == "anthropic":
            # The client's own `anthropic-beta` header was previously
            # dropped entirely on this pass-through path — there's no
            # other place a beta header reaches the backend from, so every
            # beta-gated feature (Managed Agents, cache diagnostics,
            # structured outputs beta, memory headers, and so on) silently
            # stopped working the moment a request went through this
            # gateway to a native Anthropic backend, regardless of the
            # feature. Forwarding it here fixes all of them at once,
            # rather than needing a per-feature patch each time.
            extra = {"anthropic-beta": beta_header} if beta_header else None
            result = await client.messages(outbound, extra_headers=extra)
        else:
            chat_result = await client.chat(anthropic_to_openai(outbound, model))
            result = openai_to_anthropic(chat_result, route.alias)
        _log_usage(
            request, route.alias, client.config.name, model, "messages",
            result.get("usage"), started,
        )
        return result

    return await provider_router.execute(route, call)


@app.post("/v1/responses", dependencies=[Depends(authenticate)])
async def responses(request: Request, provider_router: RouterDependency):
    payload = await body(request)
    route = provider_router.route(payload.get("model"))
    beta_header = request.headers.get("anthropic-beta")
    if payload.get("stream"):
        return StreamingResponse(
            _stream_with_usage_logging(
                request, route, "responses",
                lambda served: _stream_responses(provider_router, route, payload, beta_header, served),
            ),
            media_type="text/event-stream",
        )

    async def call(client: ProviderClient, model: str):
        started = time.monotonic()
        outbound = {**payload, "model": model}
        if client.config.api == "responses":
            converted = await client.responses(outbound)
        else:
            chat_payload = responses_to_chat(outbound, model)
            if client.config.api == "anthropic":
                extra = {"anthropic-beta": beta_header} if beta_header else None
                result = await client.messages(
                    openai_request_to_anthropic(chat_payload, model), extra_headers=extra
                )
                chat = _anthropic_to_openai_response(result, route.alias)
            else:
                chat = await client.chat(chat_payload)
            converted = chat_to_responses(chat, route.alias)
        _log_usage(
            request, route.alias, client.config.name, model, "responses",
            converted.get("usage"), started,
        )
        return converted

    return await provider_router.execute(route, call)


async def _stream_openai(provider_router, route, payload, endpoint, beta_header=None, served=None):
    async def operation(client, model):
        if served is not None:
            served["provider"] = client.config.name
            served["model"] = model
        if client.config.api == "anthropic":
            outbound = openai_request_to_anthropic({**payload, "stream": True}, model)
            extra = {"anthropic-beta": beta_header} if beta_header else None
            source = client.stream("messages", outbound, extra_headers=extra)
            async for chunk in anthropic_to_chat_stream(source, route.alias):
                yield chunk
            return
        outbound = {**payload, "model": model, "stream": True}
        async for chunk in client.stream(endpoint, outbound):
            yield chunk

    async for chunk in provider_router.stream(route, operation):
        yield chunk


async def _stream_anthropic(provider_router, route, payload, beta_header=None, served=None):
    async def operation(client, model):
        if served is not None:
            served["provider"] = client.config.name
            served["model"] = model
        outbound = {**payload, "model": model, "stream": True}
        if client.config.api == "anthropic":
            extra = {"anthropic-beta": beta_header} if beta_header else None
            source = client.stream("messages", outbound, extra_headers=extra)
        else:
            source = client.stream("chat/completions", anthropic_to_openai(outbound, model))
            source = chat_to_anthropic_stream(source, route.alias)
        async for chunk in source:
            yield chunk

    async for chunk in provider_router.stream(route, operation):
        yield chunk


async def _stream_responses(provider_router, route, payload, beta_header=None, served=None):
    async def operation(client, model):
        if served is not None:
            served["provider"] = client.config.name
            served["model"] = model
        outbound = {**payload, "model": model, "stream": True}
        if client.config.api == "responses":
            source = client.stream("responses", outbound)
        else:
            chat_payload = responses_to_chat(outbound, model)
            if client.config.api == "anthropic":
                anthropic_payload = openai_request_to_anthropic(chat_payload, model)
                extra = {"anthropic-beta": beta_header} if beta_header else None
                anthropic_source = client.stream("messages", anthropic_payload, extra_headers=extra)
                chat_source = anthropic_to_chat_stream(anthropic_source, route.alias)
            else:
                chat_source = client.stream("chat/completions", chat_payload)
            source = chat_to_responses_stream(chat_source, route.alias)
        async for chunk in source:
            yield chunk

    async for chunk in provider_router.stream(route, operation):
        yield chunk


class _StreamUsageTracker:
    """Best-effort usage capture for the streaming usage-event log.

    Streaming responses previously never called `log_usage_event()` at
    all -- only the non-streaming branches of `/v1/messages`,
    `/v1/chat/completions`, and `/v1/responses` did, despite the README's
    "every completed ... call emits one structured JSON line" claim. This
    reconstructs a usage dict from the same bytes already being sent to
    the client (never buffering or delaying them) by watching for the
    `usage` field each surface's terminal SSE event carries: Anthropic's
    `message_start`/`message_delta`, an OpenAI-compatible backend's usage
    on its final chunk (when the client requested it), and a Responses
    `response.completed` event's `response.usage`. If a frame cannot be
    parsed, or a backend never sends usage at all (some OpenAI-compatible
    streams omit it unless `stream_options.include_usage` was set),
    tracking simply stays empty rather than raising -- this must never be
    able to break the response stream it is only observing.
    """

    def __init__(self, surface: str):
        self.surface = surface
        self._buffer = ""
        self.usage: dict[str, Any] = {}

    def feed(self, chunk: bytes) -> None:
        try:
            self._buffer += chunk.decode(errors="replace").replace("\r\n", "\n")
            while "\n\n" in self._buffer:
                frame, self._buffer = self._buffer.split("\n\n", 1)
                self._consume(frame)
        except Exception:
            pass

    def _consume(self, frame: str) -> None:
        data_lines = [line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")]
        if not data_lines:
            return
        raw = "\n".join(data_lines)
        if raw == "[DONE]":
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        if self.surface == "responses":
            usage = (payload.get("response") or {}).get("usage")
            if usage:
                self.usage = usage
            return
        message = payload.get("message")
        if isinstance(message, dict) and message.get("usage"):
            self.usage = {**self.usage, **message["usage"]}
        if payload.get("usage"):
            self.usage = {**self.usage, **payload["usage"]}


def _stream_with_usage_logging(request: Request, route, surface: str, generator_factory):
    # `generator_factory` is called with a mutable `served` dict that the
    # underlying `_stream_*` operation fills in with whichever
    # provider/model actually served the request (fallback can change this
    # from the route's primary target, but only before the first byte --
    # see ProviderRouter.stream). Logging happens in `finally` so a usage
    # event is emitted whether the stream finishes normally or the client
    # disconnects partway through.
    async def wrapped():
        served: dict[str, str] = {}
        tracker = _StreamUsageTracker(surface)
        started = time.monotonic()
        try:
            async for chunk in generator_factory(served):
                tracker.feed(chunk)
                yield chunk
        finally:
            _log_usage(
                request,
                route.alias,
                served.get("provider", route.primary.provider),
                served.get("model", route.primary.model),
                surface,
                tracker.usage or None,
                started,
                stream=True,
            )

    return wrapped()


def _log_usage(
    request: Request,
    alias: str,
    provider: str,
    model: str,
    surface: str,
    usage: dict[str, Any] | None,
    started: float,
    *,
    stream: bool = False,
) -> None:
    log_usage_event(
        request_id=getattr(request.state, "request_id", "unknown"),
        alias=alias,
        provider=provider,
        model=model,
        surface=surface,
        usage=usage,
        duration_seconds=time.monotonic() - started,
        stream=stream,
    )


def _anthropic_to_openai_response(payload: dict[str, Any], model: str) -> dict[str, Any]:
    text = "".join(
        block.get("text", "")
        for block in payload.get("content", [])
        if block.get("type") == "text"
    )
    # Same reasoning-content gap as normalize.py's openai_to_anthropic() and
    # chat_to_anthropic_stream(), just for the one path that goes the other
    # way (an OpenAI-shaped client, an Anthropic-native backend): thinking
    # blocks were dropped entirely instead of surfacing as reasoning_content.
    thinking = "".join(
        block.get("thinking", "")
        for block in payload.get("content", [])
        if block.get("type") == "thinking"
    )
    calls = [
        {
            "id": block.get("id"),
            "type": "function",
            "function": {"name": block.get("name"), "arguments": json.dumps(block.get("input", {}))},
        }
        for block in payload.get("content", [])
        if block.get("type") == "tool_use"
    ]
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if thinking:
        message["reasoning_content"] = thinking
    if calls:
        message["tool_calls"] = calls
    usage = payload.get("usage", {})
    stop_reason = payload.get("stop_reason")
    # Previously hardcoded to "tool_calls" if calls else "stop", which
    # ignored stop_reason entirely — a real "max_tokens" or "refusal"
    # response looked identical to a normal completion to an OpenAI-shaped
    # client. "refusal" maps to "content_filter", the closest OpenAI
    # equivalent, consistent with the mapping in normalize.py/streaming.py.
    finish_reason = (
        "tool_calls" if calls
        else "length" if stop_reason == "max_tokens"
        else "content_filter" if stop_reason == "refusal"
        else "stop"
    )
    return {
        "id": payload.get("id", "chatcmpl_z_prov"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": _anthropic_usage_to_openai(usage),
    }


def _anthropic_usage_to_openai(usage: dict[str, Any]) -> dict[str, Any]:
    # Mirrors normalize._openai_usage_to_anthropic in the opposite direction:
    # Anthropic's cache_read_input_tokens is the closest analogue to OpenAI's
    # prompt_tokens_details.cached_tokens, so it's surfaced there rather than
    # silently dropped for an OpenAI-shaped client talking to a native
    # Anthropic backend. cache_creation_input_tokens has no OpenAI field to
    # map onto; it is still forwarded verbatim as a namespaced extension so a
    # client that knows to look for it isn't left with no signal at all.
    result: dict[str, Any] = {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
    }
    cache_read = usage.get("cache_read_input_tokens")
    if cache_read:
        result["prompt_tokens_details"] = {"cached_tokens": cache_read}
    cache_creation = usage.get("cache_creation_input_tokens")
    if cache_creation:
        result["cache_creation_input_tokens"] = cache_creation
    return result


def run() -> None:
    parser = argparse.ArgumentParser(
        prog="z-prov",
        description="Anthropic- and OpenAI-compatible multi-provider AI gateway.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.parse_args()
    uvicorn.run(
        "z_prov.main:app",
        host=os.getenv("Z_PROV_HOST", "127.0.0.1"),
        port=int(os.getenv("Z_PROV_PORT", "8080")),
        workers=int(os.getenv("Z_PROV_WORKERS", "1")),
    )
