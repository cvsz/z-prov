from __future__ import annotations

import json
from typing import Any


def anthropic_to_openai(payload: dict[str, Any], model: str) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    system = payload.get("system")
    if system:
        text = system if isinstance(system, str) else _blocks_to_text(system)
        messages.append({"role": "system", "content": text})
    for message in payload.get("messages", []):
        content = message.get("content", "")
        if isinstance(content, str):
            messages.append({"role": message["role"], "content": content})
            continue
        text_parts: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        for block in content:
            kind = block.get("type")
            if kind == "text":
                text_parts.append({"type": "text", "text": block.get("text", "")})
            elif kind == "image":
                source = block.get("source", {})
                if source.get("type") == "base64":
                    url = f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
                    text_parts.append({"type": "image_url", "image_url": {"url": url}})
            elif kind == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })
            elif kind == "tool_result":
                messages.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id"),
                    "content": _blocks_to_text(block.get("content", "")),
                })
        if text_parts or tool_calls:
            converted: dict[str, Any] = {"role": message["role"], "content": text_parts or None}
            if tool_calls:
                converted["tool_calls"] = tool_calls
            messages.append(converted)
    tools = [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object"}),
            },
        }
        for tool in payload.get("tools", [])
    ]
    result: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": payload.get("max_tokens", 4096),
        "stream": payload.get("stream", False),
    }
    for key in ("temperature", "top_p", "stop_sequences"):
        if key in payload:
            result["stop" if key == "stop_sequences" else key] = payload[key]
    if tools:
        result["tools"] = tools
    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type")
        if choice_type == "auto":
            result["tool_choice"] = "auto"
        elif choice_type == "any":
            result["tool_choice"] = "required"
        elif choice_type == "none":
            result["tool_choice"] = "none"
        elif choice_type == "tool":
            result["tool_choice"] = {
                "type": "function",
                "function": {"name": tool_choice.get("name")},
            }
    thinking = payload.get("thinking")
    effort = payload.get("effort")
    if effort is not None:
        # Top-level `effort` (GA since Feb 5, 2026, and "the primary control
        # for steering Claude Opus 5" per the July 24, 2026 release note) is
        # already a string on the same low/medium/high/xhigh/max ladder as
        # OpenAI-compatible `reasoning_effort`, so it's forwarded directly —
        # no bucketing needed, unlike the legacy budget_tokens path below.
        result["reasoning_effort"] = effort
    elif isinstance(thinking, dict) and thinking.get("type") == "enabled":
        # Legacy manual mode: only consulted when the modern top-level
        # `effort` field is absent, matching effort's role as the
        # replacement for budget_tokens on current models.
        result["reasoning_effort"] = _budget_tokens_to_effort(thinking.get("budget_tokens"))
    return result


# Bucket boundaries mirror zcoder's claude_thinking.py EFFORT_BUDGETS ladder
# (low=2_000, medium=8_000, high=16_000, xhigh=24_000, max=32_000) so a
# given budget_tokens value maps to the same named rung across both
# projects. This is a best-effort bucketing, not a guarantee — not every
# OpenAI-compatible backend recognizes "xhigh" as a reasoning_effort value
# (some only accept low/medium/high), but forwarding the closest rung is
# strictly better than the pre-fix behavior of dropping `thinking` and
# silently running the request with no reasoning effort at all.
def _budget_tokens_to_effort(budget_tokens: Any) -> str:
    if not isinstance(budget_tokens, int):
        return "medium"
    if budget_tokens <= 2_000:
        return "low"
    if budget_tokens <= 8_000:
        return "medium"
    if budget_tokens <= 16_000:
        return "high"
    if budget_tokens <= 24_000:
        return "xhigh"
    return "max"


def openai_to_anthropic(response: dict[str, Any], requested_model: str) -> dict[str, Any]:
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content: list[dict[str, Any]] = []
    reasoning = message.get("reasoning_content")
    if reasoning:
        # Mirrors the thinking_delta handling in streaming.py's
        # anthropic_to_chat_stream, but for the non-streaming response path:
        # several OpenAI-compatible reasoning backends (e.g. DeepSeek) put
        # the full reasoning trace in `message.reasoning_content` on the
        # final response, not just as streamed deltas. Without this, that
        # reasoning is silently dropped instead of reaching the client as a
        # `thinking` block, same failure mode, just on the non-streaming
        # path. `signature` is left empty: it's an opaque, cryptographically
        # verified field Anthropic itself issues for its own thinking
        # blocks, and there's no equivalent to carry over from a foreign
        # backend's plain-text reasoning trace.
        content.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {"raw": function.get("arguments", "")}
        content.append({
            "type": "tool_use",
            "id": call.get("id"),
            "name": function.get("name"),
            "input": arguments,
        })
    finish = choice.get("finish_reason")
    stop_reason = (
        "tool_use" if finish == "tool_calls"
        else "max_tokens" if finish == "length"
        # A backend's own safety block surfaces as finish_reason:
        # "content_filter"; mapping it to "refusal" (rather than falling
        # through to "end_turn" as if the model completed normally) lets an
        # Anthropic client apply its usual refusal handling — e.g. not
        # billing/retrying a blocked request the way it would a completed
        # one. See "Handling stop reasons" in the July 24, 2026-era release
        # notes for why this distinction matters to clients.
        else "refusal" if finish == "content_filter"
        else "end_turn"
    )
    usage = response.get("usage") or {}
    return {
        "id": response.get("id", "msg_z_prov"),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": _openai_usage_to_anthropic(usage),
    }


def _openai_usage_to_anthropic(usage: dict[str, Any]) -> dict[str, Any]:
    # OpenAI-compatible backends that support prompt caching (OpenAI itself,
    # DeepSeek, and others) report it under `prompt_tokens_details.cached_tokens`
    # rather than Anthropic's separate cache_creation/cache_read counters.
    # Surfacing it as `cache_read_input_tokens` lets an Anthropic-shaped client
    # see cache hits instead of the field silently disappearing on this
    # cross-protocol path. There's no equivalent for `cache_creation_input_tokens`
    # on the OpenAI side, so that counter is only ever non-zero for native
    # Anthropic responses.
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens")
    result = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
    }
    if cached:
        result["cache_read_input_tokens"] = cached
    return result


def openai_request_to_anthropic(
    payload: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    systems: list[Any] = []
    messages: list[dict[str, Any]] = []
    for message in payload.get("messages", []):
        role = message.get("role")
        if role in {"system", "developer"}:
            systems.append(message.get("content", ""))
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id"),
                "content": _openai_content_to_anthropic(message.get("content", "")),
            }
            # Parallel tool calls produce one `role: tool` message per call,
            # back to back. The Anthropic Messages API requires strictly
            # alternating user/assistant turns, so those must land as
            # multiple tool_result blocks inside a single user turn, not as
            # consecutive user messages -- the latter is an invalid request
            # that a native Anthropic backend rejects with a 400, taking
            # down the whole parallel-tool-call flow.
            previous = messages[-1] if messages else None
            if (
                previous is not None
                and previous["role"] == "user"
                and isinstance(previous["content"], list)
                and previous["content"]
                and all(item.get("type") == "tool_result" for item in previous["content"])
            ):
                previous["content"].append(block)
            else:
                messages.append({"role": "user", "content": [block]})
            continue
        if role not in {"user", "assistant"}:
            continue
        content = _openai_content_to_anthropic(message.get("content", ""))
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {"raw": function.get("arguments", "")}
                content.append({
                    "type": "tool_use",
                    "id": call.get("id"),
                    "name": function.get("name"),
                    "input": arguments,
                })
        messages.append({"role": role, "content": content})

    result: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": payload.get("max_tokens", 4096),
        "stream": payload.get("stream", False),
    }
    if systems:
        result["system"] = "\n\n".join(_blocks_to_text(value) for value in systems)
    for source, target in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stop", "stop_sequences"),
    ):
        if source in payload:
            result[target] = payload[source]
    if "reasoning_effort" in payload:
        result["effort"] = payload["reasoning_effort"]
    tools = []
    for item in payload.get("tools", []):
        if item.get("type") != "function":
            continue
        function = item.get("function") or {}
        tools.append({
            "name": function.get("name"),
            "description": function.get("description", ""),
            "input_schema": function.get("parameters", {"type": "object"}),
        })
    if tools:
        result["tools"] = tools
    tool_choice = payload.get("tool_choice")
    if tool_choice == "auto":
        result["tool_choice"] = {"type": "auto"}
    elif tool_choice == "required":
        result["tool_choice"] = {"type": "any"}
    elif isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        result["tool_choice"] = {
            "type": "tool",
            "name": (tool_choice.get("function") or {}).get("name"),
        }
    response_format = payload.get("response_format") or {}
    if response_format.get("type") == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema")
        if isinstance(schema, dict):
            result["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    return result


def responses_to_chat(payload: dict[str, Any], model: str) -> dict[str, Any]:
    input_value = payload.get("input", "")
    if isinstance(input_value, str):
        messages = [{"role": "user", "content": input_value}]
    else:
        messages = []
        for item in input_value:
            if item.get("type") == "function_call_output":
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id"),
                    "content": item.get("output", ""),
                })
            elif item.get("type") == "function_call":
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": item.get("call_id", item.get("id")),
                        "type": "function",
                        "function": {
                            "name": item.get("name"),
                            "arguments": item.get("arguments", "{}"),
                        },
                    }],
                })
            elif item.get("role") in {"system", "developer", "user", "assistant"}:
                messages.append({
                    "role": item["role"],
                    "content": _responses_content_to_chat(item.get("content", "")),
                })
    result = {
        "model": model,
        "messages": messages,
        "tools": payload.get("tools", []),
        "stream": payload.get("stream", False),
        "max_tokens": payload.get("max_output_tokens", 4096),
    }
    if "instructions" in payload:
        result["messages"].insert(0, {"role": "developer", "content": payload["instructions"]})
    if "temperature" in payload:
        result["temperature"] = payload["temperature"]
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort") is not None:
        result["reasoning_effort"] = reasoning["effort"]
    return result


def chat_to_responses(response: dict[str, Any], requested_model: str) -> dict[str, Any]:
    message = ((response.get("choices") or [{}])[0].get("message") or {})
    output: list[dict[str, Any]] = []
    if message.get("content"):
        output.append({
            "id": "msg_z_prov",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": message["content"], "annotations": []}],
        })
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        output.append({
            "id": call.get("id"),
            "type": "function_call",
            "call_id": call.get("id"),
            "name": function.get("name"),
            "arguments": function.get("arguments", "{}"),
            "status": "completed",
        })
    usage = response.get("usage") or {}
    return {
        "id": response.get("id", "resp_z_prov"),
        "object": "response",
        "created_at": response.get("created", 0),
        "status": "completed",
        "model": requested_model,
        "output": output,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get(
                "total_tokens",
                usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
            ),
        },
    }


def _openai_content_to_anthropic(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    blocks: list[dict[str, Any]] = []
    for item in value:
        kind = item.get("type")
        if kind in {"text", "input_text"}:
            blocks.append({"type": "text", "text": item.get("text", "")})
        elif kind in {"image_url", "input_image"}:
            raw = item.get("image_url", item.get("image", ""))
            url = raw.get("url", "") if isinstance(raw, dict) else raw
            if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                metadata, data = url.split(";base64,", 1)
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": metadata.removeprefix("data:"),
                        "data": data,
                    },
                })
            elif url:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
    return blocks


def _responses_content_to_chat(value: Any) -> Any:
    if isinstance(value, str):
        return value
    converted = []
    for item in value:
        kind = item.get("type")
        if kind in {"input_text", "output_text", "text"}:
            converted.append({"type": "text", "text": item.get("text", "")})
        elif kind in {"input_image", "image_url"}:
            converted.append({
                "type": "image_url",
                "image_url": {"url": item.get("image_url", item.get("url", ""))},
            })
    return converted


def _blocks_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(item.get("text", item.get("content", ""))) for item in value)
    return str(value)
