from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def anthropic_to_openai(payload: dict[str, Any], model: str) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    system = payload.get("system")
    if system:
        messages.append({"role": "system", "content": _blocks_to_text(system)})
    for raw_message in _array(payload.get("messages", []), "messages"):
        message = _object(raw_message, "message")
        role = message.get("role")
        if role not in {"user", "assistant"}:
            raise ValueError("Anthropic message role is invalid")
        content = message.get("content", "")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        blocks = _array(content, "message content")
        text_parts: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        for raw_block in blocks:
            block = _object(raw_block, "content block")
            kind = block.get("type")
            if kind == "text":
                text_parts.append({"type": "text", "text": str(block.get("text", ""))})
            elif kind == "image":
                source = _object(block.get("source", {}), "image source")
                source_type = source.get("type")
                data = source.get("data") if source_type == "base64" else source.get("url")
                if isinstance(data, str) and data:
                    if source_type == "base64":
                        data = (
                            f"data:{source.get('media_type', 'image/png')};base64,{data}"
                        )
                    text_parts.append({"type": "image_url", "image_url": {"url": data}})
            elif kind == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input", {}), separators=(",", ":")),
                    },
                })
            elif kind == "tool_result":
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id"),
                    "content": _blocks_to_text(block.get("content", "")),
                })
        if text_parts or tool_calls:
            converted: dict[str, Any] = {
                "role": role,
                "content": text_parts or None,
            }
            if tool_calls:
                converted["tool_calls"] = tool_calls
            messages.append(converted)
        messages.extend(tool_results)

    tools = []
    for raw_tool in _array(payload.get("tools", []), "tools"):
        tool = _object(raw_tool, "tool")
        if not isinstance(tool.get("name"), str) or not tool["name"]:
            raise ValueError("Anthropic tool name is invalid")
        tools.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object"}),
            },
        })
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
        elif choice_type == "tool":
            result["tool_choice"] = {
                "type": "function",
                "function": {"name": tool_choice.get("name")},
            }
    return result


def openai_to_anthropic(response: dict[str, Any], requested_model: str) -> dict[str, Any]:
    choice = _first_choice(response)
    message = _object(choice.get("message") or {}, "provider message")
    content: list[dict[str, Any]] = []
    for text in _chat_text_parts(message.get("content")):
        content.append({"type": "text", "text": text})
    for raw_call in _array(message.get("tool_calls", []), "tool calls"):
        call = _object(raw_call, "tool call")
        function = _object(call.get("function") or {}, "tool function")
        content.append({
            "type": "tool_use",
            "id": call.get("id"),
            "name": function.get("name"),
            "input": _json_object(function.get("arguments", "{}")),
        })
    finish = choice.get("finish_reason")
    stop_reason = {
        "tool_calls": "tool_use",
        "length": "max_tokens",
    }.get(finish, "end_turn")
    usage = _object(response.get("usage") or {}, "usage")
    return {
        "id": response.get("id", "msg_zeaz"),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": _nonnegative_int(usage.get("prompt_tokens", 0)),
            "output_tokens": _nonnegative_int(usage.get("completion_tokens", 0)),
        },
    }


def openai_request_to_anthropic(
    payload: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    systems: list[Any] = []
    messages: list[dict[str, Any]] = []
    for raw_message in _array(payload.get("messages", []), "messages"):
        message = _object(raw_message, "message")
        role = message.get("role")
        if role in {"system", "developer"}:
            systems.append(message.get("content", ""))
            continue
        if role == "tool":
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id"),
                    "content": _openai_content_to_anthropic(message.get("content", "")),
                }],
            })
            continue
        if role not in {"user", "assistant"}:
            raise ValueError("OpenAI message role is invalid")
        content = _openai_content_to_anthropic(message.get("content", ""))
        if role == "assistant":
            for raw_call in _array(message.get("tool_calls", []), "tool calls"):
                call = _object(raw_call, "tool call")
                function = _object(call.get("function") or {}, "tool function")
                content.append({
                    "type": "tool_use",
                    "id": call.get("id"),
                    "name": function.get("name"),
                    "input": _json_object(function.get("arguments", "{}")),
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
    tools = []
    for raw_tool in _array(payload.get("tools", []), "tools"):
        item = _object(raw_tool, "tool")
        if item.get("type") != "function":
            continue
        function = _object(item.get("function") or {}, "tool function")
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
            "name": (_object(tool_choice.get("function") or {}, "tool choice")).get("name"),
        }
    response_format = payload.get("response_format") or {}
    if isinstance(response_format, dict) and response_format.get("type") == "json_schema":
        schema = (_object(response_format.get("json_schema") or {}, "JSON schema")).get("schema")
        if isinstance(schema, dict):
            result["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    return result


def responses_to_chat(payload: dict[str, Any], model: str) -> dict[str, Any]:
    input_value = payload.get("input", "")
    if isinstance(input_value, str):
        messages = [{"role": "user", "content": input_value}]
    else:
        messages = []
        for raw_item in _array(input_value, "input"):
            item = _object(raw_item, "Responses input item")
            if item.get("type") == "function_call_output":
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id"),
                    "content": _json_text(item.get("output", "")),
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
                            "arguments": _json_text(item.get("arguments", "{}")),
                        },
                    }],
                })
            elif item.get("role") in {"system", "developer", "user", "assistant"}:
                messages.append({
                    "role": item["role"],
                    "content": _responses_content_to_chat(item.get("content", "")),
                })
            else:
                raise ValueError("Responses input item is unsupported")
    tools = []
    for raw_tool in _array(payload.get("tools", []), "tools"):
        tool = _object(raw_tool, "tool")
        if tool.get("type") != "function":
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        function = _object(function, "tool function")
        tools.append({
            "type": "function",
            "function": {
                "name": function.get("name"),
                "description": function.get("description", ""),
                "parameters": function.get("parameters", {"type": "object"}),
            },
        })
    result: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "stream": payload.get("stream", False),
        "max_tokens": payload.get(
            "max_output_tokens",
            payload.get("max_completion_tokens", 4096),
        ),
    }
    if "instructions" in payload:
        result["messages"].insert(
            0,
            {"role": "developer", "content": _responses_content_to_chat(payload["instructions"])},
        )
    for key in (
        "temperature",
        "top_p",
        "stop",
        "presence_penalty",
        "frequency_penalty",
        "parallel_tool_calls",
        "tool_choice",
        "response_format",
    ):
        if key in payload:
            result[key] = payload[key]
    return result


def chat_to_responses(response: dict[str, Any], requested_model: str) -> dict[str, Any]:
    choice = _first_choice(response)
    message = _object(choice.get("message") or {}, "provider message")
    output: list[dict[str, Any]] = []
    text_parts = _chat_text_parts(message.get("content"))
    if text_parts:
        output.append({
            "id": "msg_zeaz",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": text, "annotations": []}
                for text in text_parts
            ],
        })
    for raw_call in _array(message.get("tool_calls", []), "tool calls"):
        call = _object(raw_call, "tool call")
        function = _object(call.get("function") or {}, "tool function")
        arguments = function.get("arguments", "{}")
        output.append({
            "id": call.get("id"),
            "type": "function_call",
            "call_id": call.get("id"),
            "name": function.get("name"),
            "arguments": _json_text(arguments),
            "status": "completed",
        })
    usage = _object(response.get("usage") or {}, "usage")
    input_tokens = _nonnegative_int(usage.get("prompt_tokens", 0))
    output_tokens = _nonnegative_int(usage.get("completion_tokens", 0))
    total_tokens = _nonnegative_int(usage.get("total_tokens", input_tokens + output_tokens))
    return {
        "id": response.get("id", "resp_zeaz"),
        "object": "response",
        "created_at": _nonnegative_int(response.get("created", 0)),
        "status": "completed",
        "model": requested_model,
        "output": output,
        "output_text": "".join(text_parts),
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        },
    }


def _openai_content_to_anthropic(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    blocks: list[dict[str, Any]] = []
    for raw_item in _array(value, "content"):
        item = _object(raw_item, "content item")
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
            elif isinstance(url, str) and url:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
    return blocks


def _responses_content_to_chat(value: Any) -> Any:
    if isinstance(value, str):
        return value
    converted = []
    for raw_item in _array(value, "Responses content"):
        item = _object(raw_item, "Responses content item")
        kind = item.get("type")
        if kind in {"input_text", "output_text", "text"}:
            converted.append({"type": "text", "text": item.get("text", "")})
        elif kind in {"input_image", "image_url"}:
            image = item.get("image_url", item.get("url", ""))
            image_url = image.get("url", "") if isinstance(image, dict) else image
            converted.append({"type": "image_url", "image_url": {"url": image_url}})
    return converted


def _blocks_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return str(value)
    parts: list[str] = []
    for raw_item in value:
        if isinstance(raw_item, str):
            parts.append(raw_item)
            continue
        item = _object(raw_item, "text block")
        nested = item.get("text", item.get("content", ""))
        parts.append(nested if isinstance(nested, str) else str(nested))
    return "\n".join(parts)


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _first_choice(response: dict[str, Any]) -> dict[str, Any]:
    choices = _array(response.get("choices"), "provider choices")
    if not choices:
        raise ValueError("provider returned no choices")
    return _object(choices[0], "provider choice")


def _chat_text_parts(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    parts: list[str] = []
    for raw_item in _array(value, "provider message content"):
        item = _object(raw_item, "provider content item")
        if item.get("type") in {"text", "input_text", "output_text"}:
            text = item.get("text", "")
            if isinstance(text, str) and text:
                parts.append(text)
    return parts


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {"raw": str(value)}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    return parsed if isinstance(parsed, dict) else {"raw": value}


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


def _nonnegative_int(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0
