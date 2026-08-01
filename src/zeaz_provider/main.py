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
from redis.asyncio import Redis

from . import __version__
from .audit import audit_method, audit_path, emit_request_audit
from .config import Settings, load_settings
from .errors import ErrorKind
from .limits import RequestConcurrencyLimiter, ResponseLimitExceeded, bounded_stream
from .normalize import (
    anthropic_to_openai,
    chat_to_responses,
    openai_request_to_anthropic,
    openai_to_anthropic,
    responses_to_chat,
)
from .observability import Observability
from .providers import ProviderClient, ProviderError
from .router import ProviderRouter
from .security import (
    InMemoryRateLimiter,
    RateLimitBackend,
    RateLimitBackendError,
    RedisRateLimiter,
    TrustedProxyPolicy,
    client_bucket,
    request_id,
    verify_client_key,
)
from .streaming import (
    anthropic_to_chat_stream,
    chat_to_anthropic_stream,
    chat_to_responses_stream,
    rewrite_sse_model,
)


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
    app.state.rate_limiter = rate_limiter(settings)
    app.state.concurrency_limiter = RequestConcurrencyLimiter(settings.max_concurrent_requests)
    app.state.trusted_proxy_policy = TrustedProxyPolicy.from_cidrs(settings.trusted_proxy_cidrs)
    app.state.observability = Observability(otlp_enabled=settings.otlp_metrics_enabled)
    yield
    app.state.observability.shutdown()
    await app.state.rate_limiter.close()
    await client.aclose()


app = FastAPI(title="ZeaZ Provider", version=__version__, lifespan=lifespan)


def rate_limiter(config: Settings) -> RateLimitBackend:
    if config.rate_limit_backend == "redis":
        client = Redis.from_url(
            config.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        return RedisRateLimiter(
            client,
            config.rate_limit_per_minute,
            key_prefix=config.rate_limit_key_prefix,
        )
    return InMemoryRateLimiter(config.rate_limit_per_minute)


@app.middleware("http")
async def request_boundary(request: Request, call_next):
    started = time.monotonic()
    request.app.state.observability.start_request()
    correlation_id = request_id(request.headers.get("x-request-id"))
    request.state.request_id = correlation_id
    direct_peer = request.client.host if request.client else "unknown"
    peer = request.app.state.trusted_proxy_policy.client_ip(
        direct_peer,
        request.headers.get("cf-connecting-ip"),
    )
    bucket = client_bucket(
        request.headers.get("authorization"),
        request.headers.get("x-api-key"),
        peer,
    )
    acquired = await request.app.state.concurrency_limiter.try_acquire()
    finalized = False
    backend_failed = False
    allowed = True
    remaining = 0
    rate_limited = False
    status_code = 500

    async def finalize(final_status: int) -> None:
        nonlocal finalized
        if finalized:
            return
        finalized = True
        duration = time.monotonic() - started
        request.app.state.observability.finish_request(
            method=audit_method(request.method),
            path=audit_path(request.url.path),
            status_code=final_status,
            duration_seconds=duration,
        )
        emit_request_audit(
            request_id=correlation_id,
            method=request.method,
            path=request.url.path,
            status_code=final_status,
            duration_ms=duration * 1000,
            client_id=bucket,
            rate_limited=rate_limited,
        )
        if acquired:
            await request.app.state.concurrency_limiter.release()

    try:
        if not acquired:
            response = JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "type": "service_unavailable",
                        "message": "Request concurrency limit exceeded",
                    }
                },
                headers={"Retry-After": "1"},
            )
        else:
            try:
                allowed, remaining = await request.app.state.rate_limiter.allow(bucket)
            except RateLimitBackendError:
                allowed, remaining = False, 0
                backend_failed = True
            rate_limited = not allowed and not backend_failed
            if backend_failed:
                response = JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "type": "service_unavailable",
                            "message": "Rate-limit service unavailable",
                        }
                    },
                )
            elif not allowed:
                response = JSONResponse(
                    status_code=429,
                    content={"error": {"type": "rate_limit_error", "message": "Rate limit exceeded"}},
                    headers={"Retry-After": "60"},
                )
            else:
                response = await call_next(request)
        status_code = response.status_code
        _security_headers(response, correlation_id, remaining)
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            source = response.body_iterator
            stream_status = status_code

            async def limited_body():
                nonlocal stream_status
                try:
                    async for chunk in bounded_stream(_bytes(source), settings(request).max_response_bytes):
                        yield chunk
                except ResponseLimitExceeded:
                    stream_status = 502
                    raise
                finally:
                    await finalize(stream_status)

            response.body_iterator = limited_body()
            return response
        response = await _bounded_response(response, settings(request).max_response_bytes)
        status_code = response.status_code
        _security_headers(response, correlation_id, remaining)
        await finalize(status_code)
        return response
    except BaseException:
        await finalize(status_code)
        raise


async def _bytes(source):
    async for chunk in source:
        yield chunk.encode() if isinstance(chunk, str) else bytes(chunk)


def _validate_stream_request(provider_router, route, payload: dict[str, Any], protocol: str) -> None:
    """Validate request conversion for every configured streaming candidate before headers are sent."""
    for target in (route.primary, *route.fallbacks):
        client = provider_router.clients.get(target.provider)
        if client is None:
            continue
        try:
            if protocol == "chat" and client.config.api == "anthropic":
                openai_request_to_anthropic({**payload, "stream": True}, target.model)
            elif protocol == "anthropic" and client.config.api != "anthropic":
                anthropic_to_openai({**payload, "stream": True}, target.model)
            elif protocol == "responses" and client.config.api != "responses":
                chat_payload = responses_to_chat({**payload, "stream": True}, target.model)
                if client.config.api == "anthropic":
                    openai_request_to_anthropic(chat_payload, target.model)
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise HTTPException(status_code=400, detail="Invalid request format") from exc


def _provider_response(converter, *args) -> dict[str, Any]:
    try:
        return converter(*args)
    except (TypeError, ValueError, KeyError, IndexError, OverflowError) as exc:
        raise ProviderError(
            "Provider returned an invalid response",
            kind=ErrorKind.PROTOCOL,
            fallback_allowed=True,
            circuit_failure=True,
        ) from exc


async def _bounded_response(response: Response, maximum: int) -> Response:
    body = getattr(response, "body", None)
    if body is None:
        chunks: list[bytes] = []
        total = 0
        async for chunk in _bytes(response.body_iterator):
            total += len(chunk)
            if total > maximum:
                return _response_limit_error()
            chunks.append(chunk)
        headers = {key: value for key, value in response.headers.items() if key.lower() != "content-length"}
        return Response(
            content=b"".join(chunks),
            status_code=response.status_code,
            headers=headers,
            background=response.background,
        )
    if len(body) > maximum:
        return _response_limit_error()
    return response


def _response_limit_error() -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={
            "error": {
                "type": "response_limit_error",
                "message": "Response exceeded configured byte limit",
            }
        },
    )


def _security_headers(response: Response, correlation_id: str, remaining: int) -> None:
    response.headers["X-Request-ID"] = correlation_id
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"


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
    configured = settings(request).client_key_hashes
    if not configured and os.getenv("ZEAZ_ALLOW_UNAUTHENTICATED", "false").lower() == "true":
        return
    bearer = _bearer_token(authorization)
    if not configured or not (
        verify_client_key(x_api_key or "", configured)
        | verify_client_key(bearer, configured)
    ):
        raise HTTPException(status_code=401, detail="Invalid API key")


async def body(request: Request) -> dict[str, Any]:
    maximum = settings(request).max_request_bytes
    raw_parts: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum:
            raise HTTPException(status_code=413, detail="Request body too large")
        raw_parts.append(bytes(chunk))
    raw = b"".join(raw_parts)
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    model = value.get("model")
    if model is not None and (
        not isinstance(model, str)
        or not 1 <= len(model) <= 256
        or any(character in model for character in "\r\n\x00")
    ):
        raise HTTPException(status_code=400, detail="Invalid model")
    stream = value.get("stream")
    if stream is not None and type(stream) is not bool:
        raise HTTPException(status_code=400, detail="Invalid stream flag")
    token_values = [
        value[name]
        for name in ("max_tokens", "max_output_tokens", "max_completion_tokens")
        if name in value
    ]
    if any(
        type(tokens) is not int
        or tokens < 1
        or tokens > settings(request).max_output_tokens
        for tokens in token_values
    ) or len(set(token_values)) > 1:
        raise HTTPException(status_code=400, detail="Invalid output token limit")
    for field_name in ("messages", "tools"):
        if field_name in value and not isinstance(value[field_name], list):
            raise HTTPException(status_code=400, detail=f"{field_name} must be an array")
    if "input" in value and not isinstance(value["input"], (str, list)):
        raise HTTPException(status_code=400, detail="input must be a string or array")
    return value


@app.exception_handler(ProviderError)
async def provider_error(_: Request, exc: ProviderError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "type": exc.kind,
                "message": _public_provider_error(exc),
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


@app.get("/metrics")
async def metrics(request: Request):
    if not settings(request).metrics_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(
        content=request.app.state.observability.prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


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


@app.post("/v1/chat/completions", dependencies=[Depends(authenticate)])
async def chat_completions(request: Request, provider_router: RouterDependency):
    payload = await body(request)
    route = provider_router.route(payload.get("model"))
    if payload.get("stream"):
        _validate_stream_request(provider_router, route, payload, "chat")
        return StreamingResponse(
            _stream_openai(provider_router, route, payload, "chat/completions"),
            media_type="text/event-stream",
        )

    async def call(client: ProviderClient, model: str):
        outbound = {**payload, "model": model}
        if client.config.api == "anthropic":
            try:
                anthropic_payload = openai_request_to_anthropic(outbound, model)
            except (TypeError, ValueError, KeyError, IndexError) as exc:
                raise HTTPException(status_code=400, detail="Invalid request format") from exc
            result = await client.messages(anthropic_payload)
            return _provider_response(_anthropic_to_openai_response, result, route.alias)
        return _public_model_response(await client.chat(outbound), route.alias)

    return await provider_router.execute(route, call)


@app.post("/v1/messages", dependencies=[Depends(authenticate)])
async def messages(request: Request, provider_router: RouterDependency):
    payload = await body(request)
    route = provider_router.route(payload.get("model"))
    if payload.get("stream"):
        _validate_stream_request(provider_router, route, payload, "anthropic")
        return StreamingResponse(
            _stream_anthropic(provider_router, route, payload),
            media_type="text/event-stream",
        )

    async def call(client: ProviderClient, model: str):
        outbound = {**payload, "model": model}
        if client.config.api == "anthropic":
            return _public_model_response(await client.messages(outbound), route.alias)
        try:
            converted = anthropic_to_openai(outbound, model)
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise HTTPException(status_code=400, detail="Invalid request format") from exc
        result = await client.chat(converted)
        return _provider_response(openai_to_anthropic, result, route.alias)

    return await provider_router.execute(route, call)


@app.post("/v1/responses", dependencies=[Depends(authenticate)])
async def responses(request: Request, provider_router: RouterDependency):
    payload = await body(request)
    route = provider_router.route(payload.get("model"))
    if payload.get("stream"):
        _validate_stream_request(provider_router, route, payload, "responses")
        return StreamingResponse(
            _stream_responses(provider_router, route, payload),
            media_type="text/event-stream",
        )

    async def call(client: ProviderClient, model: str):
        outbound = {**payload, "model": model}
        if client.config.api == "responses":
            return _public_model_response(await client.responses(outbound), route.alias)
        try:
            chat_payload = responses_to_chat(outbound, model)
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise HTTPException(status_code=400, detail="Invalid request format") from exc
        if client.config.api == "anthropic":
            try:
                anthropic_payload = openai_request_to_anthropic(chat_payload, model)
            except (TypeError, ValueError, KeyError, IndexError) as exc:
                raise HTTPException(status_code=400, detail="Invalid request format") from exc
            result = await client.messages(anthropic_payload)
            chat = _provider_response(_anthropic_to_openai_response, result, route.alias)
        else:
            chat = await client.chat(chat_payload)
        return _provider_response(chat_to_responses, chat, route.alias)

    return await provider_router.execute(route, call)


async def _stream_openai(provider_router, route, payload, endpoint):
    async def operation(client, model):
        if client.config.api == "anthropic":
            outbound = openai_request_to_anthropic({**payload, "stream": True}, model)
            source = client.stream("messages", outbound)
            async for chunk in anthropic_to_chat_stream(source, route.alias):
                yield chunk
            return
        outbound = {**payload, "model": model, "stream": True}
        source = client.stream(endpoint, outbound)
        async for chunk in rewrite_sse_model(source, route.alias, "chat"):
            yield chunk

    async for chunk in provider_router.stream(route, operation):
        yield chunk


async def _stream_anthropic(provider_router, route, payload):
    async def operation(client, model):
        outbound = {**payload, "model": model, "stream": True}
        if client.config.api == "anthropic":
            source = rewrite_sse_model(
                client.stream("messages", outbound),
                route.alias,
                "anthropic",
            )
        else:
            source = client.stream("chat/completions", anthropic_to_openai(outbound, model))
            source = chat_to_anthropic_stream(source, route.alias)
        async for chunk in source:
            yield chunk

    async for chunk in provider_router.stream(route, operation):
        yield chunk


async def _stream_responses(provider_router, route, payload):
    async def operation(client, model):
        outbound = {**payload, "model": model, "stream": True}
        if client.config.api == "responses":
            source = rewrite_sse_model(
                client.stream("responses", outbound),
                route.alias,
                "responses",
            )
        else:
            chat_payload = responses_to_chat(outbound, model)
            if client.config.api == "anthropic":
                anthropic_payload = openai_request_to_anthropic(chat_payload, model)
                anthropic_source = client.stream("messages", anthropic_payload)
                chat_source = anthropic_to_chat_stream(anthropic_source, route.alias)
            else:
                chat_source = client.stream("chat/completions", chat_payload)
            source = chat_to_responses_stream(chat_source, route.alias)
        async for chunk in source:
            yield chunk

    async for chunk in provider_router.stream(route, operation):
        yield chunk


def _anthropic_to_openai_response(payload: dict[str, Any], model: str) -> dict[str, Any]:
    content = payload.get("content")
    if not isinstance(content, list):
        raise ValueError("Anthropic response content is invalid")
    text_parts: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("Anthropic response content block is invalid")
        kind = block.get("type")
        if kind == "text":
            text = block.get("text", "")
            if not isinstance(text, str):
                raise ValueError("Anthropic response text is invalid")
            text_parts.append(text)
        elif kind == "tool_use":
            call_id = block.get("id")
            name = block.get("name")
            tool_input = block.get("input", {})
            if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                raise ValueError("Anthropic response tool call is invalid")
            if not isinstance(tool_input, dict):
                raise ValueError("Anthropic response tool input is invalid")
            calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(tool_input, separators=(",", ":")),
                },
            })
    text = "".join(text_parts)
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = calls
    usage = payload.get("usage", {})
    if not isinstance(usage, dict):
        raise ValueError("Anthropic response usage is invalid")
    input_tokens = _safe_count(usage.get("input_tokens", 0))
    output_tokens = _safe_count(usage.get("output_tokens", 0))
    stop_reason = payload.get("stop_reason")
    return {
        "id": payload.get("id") if isinstance(payload.get("id"), str) else "chatcmpl_zeaz",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": (
                "tool_calls"
                if calls
                else "length"
                if stop_reason == "max_tokens"
                else "stop"
            ),
        }],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def _bearer_token(value: str | None) -> str:
    if not value:
        return ""
    scheme, separator, token = value.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return token.strip()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _public_provider_error(exc: ProviderError) -> str:
    messages = {
        ErrorKind.AUTHENTICATION: "Provider authentication failed",
        ErrorKind.BAD_REQUEST: "Provider rejected the request",
        ErrorKind.NOT_FOUND: "Provider model or endpoint was not found",
        ErrorKind.RATE_LIMIT: "Provider rate limit exceeded",
        ErrorKind.TIMEOUT: "Provider request timed out",
        ErrorKind.NETWORK: "Provider network failure",
        ErrorKind.PROTOCOL: "Provider returned an invalid response",
        ErrorKind.CIRCUIT_OPEN: "Provider temporarily unavailable",
        ErrorKind.CONFIGURATION: "Provider configuration is invalid",
        ErrorKind.UPSTREAM: "Provider request failed",
    }
    return messages.get(exc.kind, "Provider request failed")


def _public_model_response(payload: dict[str, Any], model: str) -> dict[str, Any]:
    result = dict(payload)
    result["model"] = model
    return result


def _safe_count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def run() -> None:
    parser = argparse.ArgumentParser(
        prog="zeaz-provider",
        description="Anthropic- and OpenAI-compatible multi-provider AI gateway.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.parse_args()
    uvicorn.run(
        "zeaz_provider.main:app",
        host=os.getenv("ZEAZ_HOST", "127.0.0.1"),
        port=int(os.getenv("ZEAZ_PORT", "8080")),
        workers=int(os.getenv("ZEAZ_WORKERS", "1")),
    )
