"""Bounded MCP JSON-RPC transports for the agent process."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue, TypeAdapter, ValidationError

from zeaz_agent.schemas import StrictModel

_PARAMS_ADAPTER = TypeAdapter(dict[str, JsonValue])


class MCPError(RuntimeError):
    """Sanitized MCP failure."""


class MCPPolicyError(MCPError):
    """An MCP destination or method was denied by policy."""


class MCPProtocolError(MCPError):
    """An MCP peer returned malformed or excessive protocol data."""


class MCPRemoteError(MCPError):
    """An MCP server returned a JSON-RPC error."""


class MCPErrorObject(StrictModel):
    code: int
    message: str
    data: JsonValue | None = None


class MCPResponse(StrictModel):
    jsonrpc: Literal["2.0"]
    id: int | str
    result: JsonValue | None = None
    error: MCPErrorObject | None = None


class MCPMethodPolicy:
    """Exact, immutable method allow-list."""

    def __init__(self, allowed_methods: Sequence[str]) -> None:
        methods = frozenset(allowed_methods)
        if not methods or len(methods) > 256:
            raise ValueError("allowed_methods must contain between 1 and 256 methods")
        if any(
            not method
            or len(method) > 128
            or any(
                character
                not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./-"
                for character in method
            )
            for method in methods
        ):
            raise ValueError("allowed_methods contains an invalid method")
        self._methods = methods

    @property
    def methods(self) -> frozenset[str]:
        return self._methods

    def require(self, method: str) -> None:
        if method not in self._methods:
            raise MCPPolicyError("MCP method is not allowed")


class StreamableHTTPTransport:
    """MCP Streamable HTTP POST transport with exact host and method policy."""

    def __init__(
        self,
        endpoint: str,
        *,
        allowed_hosts: Sequence[str],
        allowed_methods: Sequence[str],
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30,
        max_response_bytes: int = 4_194_304,
        max_sse_events: int = 1024,
    ) -> None:
        parsed = urlsplit(endpoint)
        hosts = frozenset(host.lower().rstrip(".") for host in allowed_hosts)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.hostname.lower().rstrip(".") not in hosts
        ):
            raise MCPPolicyError("MCP endpoint is not an allowed HTTPS host")
        if not hosts or len(hosts) > 256 or any(not host for host in hosts):
            raise ValueError("allowed_hosts must contain between 1 and 256 hosts")
        _validate_limits(timeout_seconds, max_response_bytes)
        if not 1 <= max_sse_events <= 10_000:
            raise ValueError("max_sse_events must be between 1 and 10000")
        self._endpoint = endpoint
        self._policy = MCPMethodPolicy(allowed_methods)
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_sse_events = max_sse_events
        self._client = client or httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=timeout_seconds,
        )
        self._owns_client = client is None
        self._next_id = 1
        self._session_id: str | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
    ) -> JsonValue:
        self._policy.require(method)
        async with self._lock:
            request_id = self._next_id
            self._next_id += 1
            payload: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
            }
            if params is not None:
                payload["params"] = _validated_params(params)
            encoded = _encode_request(payload, self._max_response_bytes)
            headers = {
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            }
            if self._session_id is not None:
                headers["mcp-session-id"] = self._session_id
            try:
                async with self._client.stream(
                    "POST",
                    self._endpoint,
                    content=encoded,
                    headers=headers,
                    timeout=self._timeout,
                    follow_redirects=False,
                ) as response:
                    if response.is_redirect:
                        raise MCPPolicyError("MCP redirects are forbidden")
                    if response.status_code < 200 or response.status_code >= 300:
                        raise MCPError(f"MCP request failed with HTTP {response.status_code}")
                    session_id = response.headers.get("mcp-session-id")
                    if session_id is not None:
                        if not session_id.isascii() or not 1 <= len(session_id) <= 1024:
                            raise MCPProtocolError("MCP session identifier is invalid")
                        self._session_id = session_id
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
                    body = await _bounded_http_body(response, self._max_response_bytes)
            except MCPError:
                raise
            except httpx.HTTPError as exc:
                raise MCPError("MCP HTTPS request failed") from exc
            if content_type == "application/json":
                message = _decode_json_message(body)
            elif content_type == "text/event-stream":
                message = _response_from_sse(body, request_id, self._max_sse_events)
            else:
                raise MCPProtocolError("MCP response has an unsupported content type")
            return _result_for_request(message, request_id)


class StdioTransport:
    """Newline-delimited MCP stdio transport using configured immutable argv."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        allowed_methods: Sequence[str],
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout_seconds: float = 30,
        max_message_bytes: int = 4_194_304,
    ) -> None:
        immutable_argv = tuple(argv)
        if not immutable_argv or len(immutable_argv) > 128:
            raise ValueError("argv must contain between 1 and 128 entries")
        executable = Path(immutable_argv[0])
        if not executable.is_absolute():
            raise ValueError("MCP stdio executable must be an absolute path")
        try:
            info = executable.lstat()
        except OSError as exc:
            raise ValueError("MCP stdio executable is unavailable") from exc
        if executable.is_symlink() or not stat.S_ISREG(info.st_mode) or not os.access(executable, os.X_OK):
            raise ValueError("MCP stdio executable must be an executable regular file")
        if any("\x00" in item or len(item) > 16_384 for item in immutable_argv):
            raise ValueError("MCP stdio argv contains an invalid entry")
        if cwd is not None and (not cwd.is_dir() or cwd.is_symlink()):
            raise ValueError("MCP stdio cwd must be a real directory")
        clean_env = dict(env or {})
        if len(clean_env) > 256 or any(
            not key
            or "\x00" in key
            or "=" in key
            or "\x00" in value
            or len(key) > 256
            or len(value) > 16_384
            for key, value in clean_env.items()
        ):
            raise ValueError("MCP stdio environment is invalid")
        _validate_limits(timeout_seconds, max_message_bytes)
        self._argv = immutable_argv
        self._policy = MCPMethodPolicy(allowed_methods)
        self._env = clean_env
        self._cwd = cwd
        self._timeout = timeout_seconds
        self._max_message_bytes = max_message_bytes
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._process is not None:
            return
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._env,
                cwd=self._cwd,
                limit=self._max_message_bytes + 1,
            )
        except OSError as exc:
            raise MCPError("MCP stdio server failed to start") from exc

    async def aclose(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=min(self._timeout, 5))
        except TimeoutError:
            process.kill()
            await process.wait()

    async def request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None = None,
    ) -> JsonValue:
        self._policy.require(method)
        async with self._lock:
            await self.start()
            process = self._process
            if process is None or process.stdin is None or process.stdout is None:
                raise MCPError("MCP stdio server is unavailable")
            request_id = self._next_id
            self._next_id += 1
            payload: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
            }
            if params is not None:
                payload["params"] = _validated_params(params)
            encoded = _encode_request(payload, self._max_message_bytes) + b"\n"
            if len(encoded) > self._max_message_bytes:
                raise MCPProtocolError("MCP request exceeds the message-size limit")
            try:
                process.stdin.write(encoded)
                await asyncio.wait_for(process.stdin.drain(), timeout=self._timeout)
                line = await asyncio.wait_for(process.stdout.readline(), timeout=self._timeout)
            except (TimeoutError, asyncio.LimitOverrunError, ValueError) as exc:
                await self._abort()
                raise MCPProtocolError("MCP stdio response timed out or exceeded its limit") from exc
            except (BrokenPipeError, ConnectionError) as exc:
                await self._abort()
                raise MCPError("MCP stdio server disconnected") from exc
            if not line:
                await self._abort()
                raise MCPError("MCP stdio server disconnected")
            if len(line) > self._max_message_bytes or not line.endswith(b"\n"):
                await self._abort()
                raise MCPProtocolError("MCP stdio response exceeds the message-size limit")
            return _result_for_request(_decode_json_message(line[:-1]), request_id)

    async def _abort(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


def _validate_limits(timeout_seconds: float, max_bytes: int) -> None:
    if not 0 < timeout_seconds <= 600:
        raise ValueError("timeout_seconds must be between 0 and 600")
    if not 1024 <= max_bytes <= 67_108_864:
        raise ValueError("message byte limit must be between 1 KiB and 64 MiB")


def _validated_params(params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    try:
        return _PARAMS_ADAPTER.validate_python(dict(params), strict=True)
    except (TypeError, ValidationError) as exc:
        raise MCPProtocolError("MCP request params are not valid JSON") from exc


def _encode_request(payload: dict[str, Any], maximum: int) -> bytes:
    try:
        encoded = json.dumps(
            payload,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise MCPProtocolError("MCP request is not valid JSON") from exc
    if len(encoded) > maximum:
        raise MCPProtocolError("MCP request exceeds the message-size limit")
    return encoded


async def _bounded_http_body(response: httpx.Response, maximum: int) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > maximum:
            raise MCPProtocolError("MCP response exceeds the byte limit")
    return bytes(body)


def _decode_json_message(content: bytes) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MCPProtocolError("MCP peer returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise MCPProtocolError("MCP peer returned a non-object message")
    return value


def _response_from_sse(content: bytes, request_id: int, maximum_events: int) -> dict[str, Any]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MCPProtocolError("MCP SSE response is not UTF-8") from exc
    events = text.replace("\r\n", "\n").split("\n\n")
    if len(events) > maximum_events:
        raise MCPProtocolError("MCP SSE response has too many events")
    for event in events:
        data = "\n".join(
            line[5:].lstrip()
            for line in event.splitlines()
            if line.startswith("data:")
        )
        if not data:
            continue
        message = _decode_json_message(data.encode())
        if message.get("id") == request_id:
            return message
    raise MCPProtocolError("MCP SSE stream omitted the matching response")


def _result_for_request(message: dict[str, Any], request_id: int) -> JsonValue:
    if message.get("jsonrpc") != "2.0" or message.get("id") != request_id:
        raise MCPProtocolError("MCP response does not match the request")
    if ("result" in message) == ("error" in message):
        raise MCPProtocolError("MCP response must contain exactly one of result or error")
    try:
        response = MCPResponse.model_validate(message)
    except ValidationError as exc:
        raise MCPProtocolError("MCP response failed schema validation") from exc
    if response.error is not None:
        raise MCPRemoteError(
            f"MCP server returned JSON-RPC error {response.error.code}"
        )
    return response.result
