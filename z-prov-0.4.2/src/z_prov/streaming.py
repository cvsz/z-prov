from __future__ import annotations

import json
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


async def anthropic_to_chat_stream(
    chunks: AsyncIterator[bytes],
    model: str,
) -> AsyncIterator[bytes]:
    stream_id = "chatcmpl_z_prov"
    created = int(time.time())
    tool_indexes: dict[int, int] = {}
    async for event, raw in sse_events(chunks):
        if raw == "[DONE]":
            continue
        data = _json(raw)
        kind = data.get("type", event)
        if kind == "message_start":
            message = data.get("message") or {}
            stream_id = message.get("id", stream_id)
            yield _data(_chat_chunk(stream_id, created, model, {"role": "assistant"}))
        elif kind == "content_block_start":
            block_index = int(data.get("index", 0))
            block = data.get("content_block") or {}
            if block.get("type") == "tool_use":
                tool_index = len(tool_indexes)
                tool_indexes[block_index] = tool_index
                yield _data(_chat_chunk(stream_id, created, model, {
                    "tool_calls": [{
                        "index": tool_index,
                        "id": block.get("id"),
                        "type": "function",
                        "function": {"name": block.get("name"), "arguments": ""},
                    }]
                }))
        elif kind == "content_block_delta":
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta":
                yield _data(_chat_chunk(
                    stream_id,
                    created,
                    model,
                    {"content": delta.get("text", "")},
                ))
            elif delta.get("type") == "thinking_delta":
                # Surfaced as `reasoning_content`, the de facto convention
                # several OpenAI-compatible reasoning backends already use
                # (e.g. DeepSeek's API) for streamed reasoning tokens, so a
                # client reading `delta.reasoning_content` sees the same
                # thinking text an Anthropic-native stream would have sent
                # as a `thinking_delta` block, instead of it vanishing.
                yield _data(_chat_chunk(
                    stream_id,
                    created,
                    model,
                    {"reasoning_content": delta.get("thinking", "")},
                ))
            elif delta.get("type") == "input_json_delta":
                block_index = int(data.get("index", 0))
                yield _data(_chat_chunk(stream_id, created, model, {
                    "tool_calls": [{
                        "index": tool_indexes.get(block_index, block_index),
                        "function": {"arguments": delta.get("partial_json", "")},
                    }]
                }))
        elif kind == "message_delta":
            stop = (data.get("delta") or {}).get("stop_reason")
            finish = (
                "tool_calls" if stop == "tool_use"
                else "length" if stop == "max_tokens"
                # Symmetric with openai_to_anthropic's content_filter ->
                # refusal mapping: a native Anthropic refusal shouldn't
                # look like a normal "stop" completion to an OpenAI-shaped
                # client, or it loses the signal to skip billing/retry.
                else "content_filter" if stop == "refusal"
                else "stop"
            )
            usage = data.get("usage") or {}
            chunk = _chat_chunk(stream_id, created, model, {}, finish)
            if usage:
                chunk["usage"] = {
                    "prompt_tokens": 0,
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("output_tokens", 0),
                }
            yield _data(chunk)
        elif kind == "error":
            yield _data({"error": data.get("error", data)})
    yield b"data: [DONE]\n\n"


async def chat_to_anthropic_stream(
    chunks: AsyncIterator[bytes],
    model: str,
) -> AsyncIterator[bytes]:
    message_id = "msg_z_prov"
    started = False
    reasoning_started = False
    reasoning_block_index = 0
    text_started = False
    text_block_index = 0
    tool_blocks: dict[int, int] = {}
    next_block = 0
    output_tokens = 0
    async for _event, raw in sse_events(chunks):
        if raw == "[DONE]":
            break
        data = _json(raw)
        if "error" in data:
            yield _event_data("error", {"type": "error", "error": data["error"]})
            return
        message_id = data.get("id", message_id)
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
        choice = (data.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        reasoning_text = delta.get("reasoning_content")
        if reasoning_text is not None:
            # Symmetric with anthropic_to_chat_stream's thinking_delta ->
            # reasoning_content mapping above: an OpenAI-compatible backend's
            # streamed reasoning trace becomes an Anthropic `thinking` block
            # instead of being dropped (there's no "reasoning_content" case
            # in the Anthropic event vocabulary to silently fall through to).
            if not reasoning_started:
                reasoning_started = True
                reasoning_block_index = next_block
                next_block += 1
                yield _event_data("content_block_start", {
                    "type": "content_block_start",
                    "index": reasoning_block_index,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                })
            yield _event_data("content_block_delta", {
                "type": "content_block_delta",
                "index": reasoning_block_index,
                "delta": {"type": "thinking_delta", "thinking": reasoning_text},
            })
        text = delta.get("content")
        if text is not None:
            if not text_started:
                text_started = True
                text_block_index = next_block
                next_block += 1
                yield _event_data("content_block_start", {
                    "type": "content_block_start",
                    "index": text_block_index,
                    "content_block": {"type": "text", "text": ""},
                })
            yield _event_data("content_block_delta", {
                "type": "content_block_delta",
                "index": text_block_index,
                "delta": {"type": "text_delta", "text": text},
            })
        for call in delta.get("tool_calls") or []:
            chat_index = int(call.get("index", 0))
            if chat_index not in tool_blocks:
                block_index = next_block
                next_block += 1
                tool_blocks[chat_index] = block_index
                function = call.get("function") or {}
                yield _event_data("content_block_start", {
                    "type": "content_block_start",
                    "index": block_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": call.get("id"),
                        "name": function.get("name"),
                        "input": {},
                    },
                })
            arguments = (call.get("function") or {}).get("arguments")
            if arguments:
                yield _event_data("content_block_delta", {
                    "type": "content_block_delta",
                    "index": tool_blocks[chat_index],
                    "delta": {"type": "input_json_delta", "partial_json": arguments},
                })
        usage = data.get("usage") or {}
        output_tokens = usage.get("completion_tokens", output_tokens)
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
                else "refusal"
                if finish == "content_filter"
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
    response_id = "resp_z_prov"
    item_id = "msg_z_prov"
    created = False
    async for _event, raw in sse_events(chunks):
        if raw == "[DONE]":
            break
        data = _json(raw)
        if "error" in data:
            yield _data({"type": "error", "error": data["error"]})
            return
        response_id = data.get("id", response_id)
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
        choice = (data.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if delta.get("content") is not None:
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
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens", input_tokens + output_tokens),
    }


def _json(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _data(value: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(value, separators=(',', ':'))}\n\n".encode()


def _event_data(event: str, value: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(value, separators=(',', ':'))}\n\n".encode()
