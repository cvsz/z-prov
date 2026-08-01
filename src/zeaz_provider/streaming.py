from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any


async def sse_events(chunks: AsyncIterator[bytes]) -> AsyncIterator[tuple[str, str]]:
    buffer = ""
    async for chunk in chunks:
        buffer += chunk.decode(errors="replace").replace("\r\n", "\n")
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            event = "message"
            data = []
            for line in frame.splitlines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
            if data:
                yield event, "\n".join(data)
    if buffer.strip():
        event = "message"
        data = []
        for line in buffer.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if data:
            yield event, "\n".join(data)


async def rewrite_sse_model(
    chunks: AsyncIterator[bytes],
    model: str,
    protocol: str,
) -> AsyncIterator[bytes]:
    """Rewrite only provider model fields while preserving the SSE contract."""
    if protocol not in {"anthropic", "chat", "responses"}:
        raise ValueError("unsupported SSE protocol")
    async for event, raw in sse_events(chunks):
        if raw == "[DONE]":
            yield _sse_frame(event, raw)
            continue
        data = _json(raw)
        if data is None:
            yield _data({
                "error": {
                    "type": "protocol_error",
                    "message": "Provider returned invalid SSE data",
                }
            })
            return
        if protocol == "anthropic":
            message = data.get("message")
            if isinstance(message, dict) and "model" in message:
                message["model"] = model
            elif "model" in data:
                data["model"] = model
        elif protocol == "responses":
            response = data.get("response")
            if isinstance(response, dict) and "model" in response:
                response["model"] = model
            elif "model" in data:
                data["model"] = model
        elif "model" in data:
            data["model"] = model
        yield _sse_frame(event, json.dumps(data, separators=(",", ":")))


async def anthropic_to_chat_stream(
    chunks: AsyncIterator[bytes],
    model: str,
) -> AsyncIterator[bytes]:
    stream_id = "chatcmpl_zeaz"
    created = int(time.time())
    tool_indexes: dict[int, int] = {}
    async for event, raw in sse_events(chunks):
        if raw == "[DONE]":
            continue
        data = _json(raw)
        if data is None:
            yield _data(_protocol_error())
            return
        kind = data.get("type", event)
        if kind == "message_start":
            message = _mapping_or_empty(data.get("message"))
            stream_id = _safe_string(message.get("id"), stream_id)
            yield _data(_chat_chunk(stream_id, created, model, {"role": "assistant"}))
        elif kind == "content_block_start":
            block_index = _safe_index(data.get("index"))
            block = _mapping_or_empty(data.get("content_block"))
            if block.get("type") == "tool_use":
                tool_index = len(tool_indexes)
                tool_indexes[block_index] = tool_index
                yield _data(_chat_chunk(stream_id, created, model, {
                    "tool_calls": [{
                        "index": tool_index,
                        "id": _safe_string(block.get("id"), f"tool_{tool_index}"),
                        "type": "function",
                        "function": {
                            "name": _safe_string(block.get("name"), "function"),
                            "arguments": "",
                        },
                    }]
                }))
        elif kind == "content_block_delta":
            delta = _mapping_or_empty(data.get("delta"))
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                if not isinstance(text, str):
                    yield _data(_protocol_error())
                    return
                yield _data(_chat_chunk(
                    stream_id,
                    created,
                    model,
                    {"content": text},
                ))
            elif delta.get("type") == "input_json_delta":
                block_index = _safe_index(data.get("index"))
                partial_json = delta.get("partial_json", "")
                if not isinstance(partial_json, str):
                    yield _data(_protocol_error())
                    return
                yield _data(_chat_chunk(stream_id, created, model, {
                    "tool_calls": [{
                        "index": tool_indexes.get(block_index, block_index),
                        "function": {"arguments": partial_json},
                    }]
                }))
        elif kind == "message_delta":
            stop = _mapping_or_empty(data.get("delta")).get("stop_reason")
            finish = "tool_calls" if stop == "tool_use" else "length" if stop == "max_tokens" else "stop"
            usage = _mapping_or_empty(data.get("usage"))
            chunk = _chat_chunk(stream_id, created, model, {}, finish)
            if usage:
                chunk["usage"] = {
                    "prompt_tokens": 0,
                    "completion_tokens": _safe_count(usage.get("output_tokens")),
                    "total_tokens": _safe_count(usage.get("output_tokens")),
                }
            yield _data(chunk)
        elif kind == "error":
            yield _data({"error": _protocol_error()})
    yield b"data: [DONE]\n\n"


async def chat_to_anthropic_stream(
    chunks: AsyncIterator[bytes],
    model: str,
) -> AsyncIterator[bytes]:
    message_id = "msg_zeaz"
    started = False
    text_started = False
    tool_blocks: dict[int, int] = {}
    next_block = 0
    output_tokens = 0
    async for _event, raw in sse_events(chunks):
        if raw == "[DONE]":
            break
        data = _json(raw)
        if data is None:
            yield _event_data("error", {"type": "error", "error": _protocol_error()})
            return
        if "error" in data:
            yield _event_data("error", {"type": "error", "error": _protocol_error()})
            return
        message_id = _safe_string(data.get("id"), message_id)
        if not started:
            started = True
            yield _event_data("message_start", {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            })
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            yield _event_data("error", {"type": "error", "error": _protocol_error()})
            return
        choice = choices[0]
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            yield _event_data("error", {"type": "error", "error": _protocol_error()})
            return
        text = delta.get("content")
        if text is not None and not isinstance(text, str):
            yield _event_data("error", {"type": "error", "error": _protocol_error()})
            return
        if text is not None:
            if not text_started:
                text_started = True
                next_block += 1
                yield _event_data("content_block_start", {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                })
            yield _event_data("content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            })
        calls = delta.get("tool_calls") or []
        if not isinstance(calls, list):
            yield _event_data("error", {"type": "error", "error": _protocol_error()})
            return
        for call in calls:
            if not isinstance(call, dict):
                yield _event_data("error", {"type": "error", "error": _protocol_error()})
                return
            chat_index = _safe_index(call.get("index"))
            if chat_index not in tool_blocks:
                block_index = next_block
                next_block += 1
                tool_blocks[chat_index] = block_index
                function = _mapping_or_empty(call.get("function"))
                yield _event_data("content_block_start", {
                    "type": "content_block_start",
                    "index": block_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": _safe_string(call.get("id"), f"tool_{chat_index}"),
                        "name": _safe_string(function.get("name"), "function"),
                        "input": {},
                    },
                })
            arguments = _mapping_or_empty(call.get("function")).get("arguments")
            if arguments is not None and not isinstance(arguments, str):
                yield _event_data("error", {"type": "error", "error": _protocol_error()})
                return
            if arguments:
                yield _event_data("content_block_delta", {
                    "type": "content_block_delta",
                    "index": tool_blocks[chat_index],
                    "delta": {"type": "input_json_delta", "partial_json": arguments},
                })
        usage = _mapping_or_empty(data.get("usage"))
        output_tokens = _safe_count(usage.get("completion_tokens")) or output_tokens
        finish = choice.get("finish_reason")
        if finish:
            for index in range(next_block):
                yield _event_data("content_block_stop", {
                    "type": "content_block_stop",
                    "index": index,
                })
            stop = (
                "tool_use"
                if finish == "tool_calls"
                else "max_tokens"
                if finish == "length"
                else "end_turn"
            )
            yield _event_data("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            })
    if not started:
        yield _event_data("message_start", {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })
    yield _event_data("message_stop", {"type": "message_stop"})


async def chat_to_responses_stream(
    chunks: AsyncIterator[bytes],
    model: str,
) -> AsyncIterator[bytes]:
    response_id = "resp_zeaz"
    item_id = "msg_zeaz"
    created = False
    async for _event, raw in sse_events(chunks):
        if raw == "[DONE]":
            break
        data = _json(raw)
        if data is None:
            yield _data({"type": "error", "error": _protocol_error()})
            return
        if "error" in data:
            yield _data({"type": "error", "error": _protocol_error()})
            return
        response_id = _safe_string(data.get("id"), response_id)
        if not created:
            created = True
            yield _data({
                "type": "response.created",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "status": "in_progress",
                    "model": model,
                    "output": [],
                },
            })
            yield _data({
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"id": item_id, "type": "message", "role": "assistant", "content": []},
            })
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            yield _data({"type": "error", "error": _protocol_error()})
            return
        choice = choices[0]
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            yield _data({"type": "error", "error": _protocol_error()})
            return
        if delta.get("content") is not None:
            if not isinstance(delta["content"], str):
                yield _data({"type": "error", "error": _protocol_error()})
                return
            yield _data({
                "type": "response.output_text.delta",
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "delta": delta["content"],
            })
        if choice.get("finish_reason"):
            yield _data({
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "status": "completed",
                    "model": model,
                    "output": [],
                    "usage": _responses_usage(data.get("usage") or {}),
                },
            })
    yield b"data: [DONE]\n\n"


def _chat_chunk(
    stream_id: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": stream_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _responses_usage(usage: dict[str, Any]) -> dict[str, int]:
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = _safe_count(usage.get("prompt_tokens"))
    output_tokens = _safe_count(usage.get("completion_tokens"))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": _safe_count(usage.get("total_tokens")) or input_tokens + output_tokens,
    }


def _json(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _data(value: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(value, separators=(',', ':'))}\n\n".encode()


def _event_data(event: str, value: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(value, separators=(',', ':'))}\n\n".encode()


def _sse_frame(event: str, value: str) -> bytes:
    safe_event = event if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", event or "") else "message"
    prefix = f"event: {safe_event}\n" if safe_event != "message" else ""
    return f"{prefix}data: {value}\n\n".encode()


def _protocol_error() -> dict[str, Any]:
    return {
        "type": "protocol_error",
        "message": "Provider returned invalid SSE data",
    }


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_index(value: Any) -> int:
    return value if type(value) is int and 0 <= value < 100_000 else 0


def _safe_string(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _safe_count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0
