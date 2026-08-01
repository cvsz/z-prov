"""Bounded client for the ZeaZ Provider Responses-compatible endpoint."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
from pydantic import SecretStr, ValidationError

from zeaz_agent.schemas import (
    ImageBlock,
    ModelOutput,
    TextBlock,
    TokenUsage,
    ToolCall,
    ToolCallBlock,
    ToolDefinition,
    ToolResultBlock,
    Turn,
)


class GatewayError(RuntimeError):
    """A sanitized gateway transport or response failure."""


class GatewayProtocolError(GatewayError):
    """The gateway returned an invalid or excessive response."""


class ZeazProviderClient:
    """Translate agent schemas to the provider gateway's Responses API."""

    def __init__(
        self,
        base_url: str,
        client_key: SecretStr | str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 120,
        max_response_bytes: int = 16_777_216,
    ) -> None:
        _validate_gateway_url(base_url)
        if not 0 < timeout_seconds <= 600:
            raise ValueError("timeout_seconds must be between 0 and 600")
        if not 1024 <= max_response_bytes <= 67_108_864:
            raise ValueError("max_response_bytes must be between 1 KiB and 64 MiB")
        self._base_url = base_url.rstrip("/")
        self._client_key = client_key if isinstance(client_key, SecretStr) else SecretStr(client_key)
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._client = client or httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=timeout_seconds,
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def respond(
        self,
        turns: Sequence[Turn],
        tools: Sequence[ToolDefinition],
        *,
        model: str,
        max_output_tokens: int,
        correlation_id: UUID,
    ) -> ModelOutput:
        payload = {
            "model": model,
            "input": _turns_to_input(turns),
            "tools": [_tool_to_wire(tool) for tool in tools],
            "max_output_tokens": max_output_tokens,
            "stream": False,
            "store": False,
        }
        headers = {
            "x-api-key": self._client_key.get_secret_value(),
            "x-request-id": str(correlation_id),
            "content-type": "application/json",
        }
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/v1/responses",
                json=payload,
                headers=headers,
                timeout=self._timeout,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise GatewayError(f"gateway request failed with HTTP {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise GatewayProtocolError("gateway response exceeded the byte limit")
        except GatewayError:
            raise
        except httpx.HTTPError as exc:
            raise GatewayError("gateway request failed") from exc
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayProtocolError("gateway returned invalid JSON") from exc
        return _parse_output(value)


def _validate_gateway_url(value: str) -> None:
    parsed = urlparse(value)
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("gateway URL must use HTTPS unless it is loopback")
    if parsed.username or parsed.password or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("gateway URL must be an origin without credentials, query, or fragment")


def _turns_to_input(turns: Sequence[Turn]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for turn in turns:
        message_content: list[dict[str, Any]] = []
        for block in turn.content:
            if isinstance(block, TextBlock):
                content_type = "output_text" if turn.role.value == "assistant" else "input_text"
                message_content.append({"type": content_type, "text": block.text})
            elif isinstance(block, ImageBlock):
                if block.source.type == "base64":
                    image_url = f"data:{block.source.media_type};base64,{block.source.data}"
                else:
                    image_url = block.source.data
                message_content.append({"type": "input_image", "image_url": image_url})
            elif isinstance(block, ToolCallBlock):
                items.append({
                    "type": "function_call",
                    "call_id": block.call.id,
                    "name": block.call.name,
                    "arguments": json.dumps(block.call.arguments, separators=(",", ":")),
                })
            elif isinstance(block, ToolResultBlock):
                items.append({
                    "type": "function_call_output",
                    "call_id": block.result.tool_call_id,
                    "output": json.dumps(block.result.output, separators=(",", ":")),
                })
        if message_content:
            items.append({"role": turn.role.value, "content": message_content})
    return items


def _tool_to_wire(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
    }


def _parse_output(value: Any) -> ModelOutput:
    if not isinstance(value, dict) or not isinstance(value.get("output"), list):
        raise GatewayProtocolError("gateway response has no output array")
    if value.get("status", "completed") != "completed":
        raise GatewayProtocolError("gateway response is not complete")
    output = value["output"]
    if len(output) > 1024:
        raise GatewayProtocolError("gateway response has too many output items")
    blocks: list[TextBlock | ToolCallBlock] = []
    try:
        for item in output:
            if not isinstance(item, dict):
                raise GatewayProtocolError("gateway output item is not an object")
            if item.get("type") == "message":
                content = item.get("content")
                if not isinstance(content, list):
                    raise GatewayProtocolError("gateway message content is not an array")
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        blocks.append(TextBlock(text=part.get("text")))
            elif item.get("type") == "function_call":
                raw_arguments = item.get("arguments", "{}")
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise GatewayProtocolError("tool arguments must be a JSON object")
                blocks.append(
                    ToolCallBlock(
                        call=ToolCall(
                            id=item.get("call_id", item.get("id")),
                            name=item.get("name"),
                            arguments=arguments,
                        )
                    )
                )
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise GatewayProtocolError("gateway output failed schema validation") from exc
    if not blocks:
        raise GatewayProtocolError("gateway response contained no supported output")
    usage_value = value.get("usage") or {}
    if not isinstance(usage_value, dict):
        raise GatewayProtocolError("gateway usage is not an object")
    try:
        usage = TokenUsage(
            input_tokens=usage_value.get("input_tokens", 0),
            output_tokens=usage_value.get("output_tokens", 0),
        )
    except ValidationError as exc:
        raise GatewayProtocolError("gateway usage failed schema validation") from exc
    reported_total = usage_value.get("total_tokens")
    if reported_total is not None and reported_total != usage.total_tokens:
        raise GatewayProtocolError("gateway usage total is inconsistent")
    return ModelOutput(blocks=tuple(blocks), usage=usage)
