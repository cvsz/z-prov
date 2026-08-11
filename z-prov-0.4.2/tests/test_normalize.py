from z_prov.normalize import (
    anthropic_to_openai,
    chat_to_responses,
    openai_request_to_anthropic,
    openai_to_anthropic,
    responses_to_chat,
)


def test_anthropic_text_and_tool_conversion():
    payload = {
        "model": "alias",
        "max_tokens": 100,
        "system": "Be concise",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [{
            "name": "weather",
            "description": "Weather",
            "input_schema": {"type": "object", "properties": {}},
        }],
    }
    result = anthropic_to_openai(payload, "backend-model")
    assert result["model"] == "backend-model"
    assert result["messages"][0] == {"role": "system", "content": "Be concise"}
    assert result["tools"][0]["function"]["name"] == "weather"


def test_openai_content_filter_maps_to_anthropic_refusal():
    response = {
        "id": "chat-1",
        "choices": [{"message": {"content": None}, "finish_reason": "content_filter"}],
    }
    result = openai_to_anthropic(response, "alias")
    assert result["stop_reason"] == "refusal"


def test_openai_tool_response_conversion():
    response = {
        "id": "chat-1",
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "function": {"name": "weather", "arguments": '{"city":"BKK"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3},
    }
    result = openai_to_anthropic(response, "alias")
    assert result["stop_reason"] == "tool_use"
    assert result["content"][0]["input"] == {"city": "BKK"}
    assert result["usage"] == {"input_tokens": 2, "output_tokens": 3}


def test_responses_round_trip_shape():
    chat = responses_to_chat({"input": "hello", "max_output_tokens": 99}, "model")
    assert chat["messages"] == [{"role": "user", "content": "hello"}]
    response = chat_to_responses(
        {"choices": [{"message": {"content": "hi"}}], "usage": {}},
        "alias",
    )
    assert response["object"] == "response"
    assert response["output"][0]["content"][0]["text"] == "hi"


def test_openai_request_preserves_developer_tools_results_images_and_schema():
    result = openai_request_to_anthropic({
        "messages": [
            {"role": "developer", "content": "Use JSON"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "inspect", "arguments": '{"x":1}'},
                }],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "done"},
        ],
        "tools": [{
            "type": "function",
            "function": {
                "name": "inspect",
                "description": "Inspect",
                "parameters": {"type": "object"},
            },
        }],
        "tool_choice": "required",
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": {"type": "object"}},
        },
    }, "claude-model")
    assert result["system"] == "Use JSON"
    assert result["messages"][0]["content"][1]["source"]["media_type"] == "image/png"
    assert result["messages"][1]["content"][0]["input"] == {"x": 1}
    assert result["messages"][2]["content"][0]["type"] == "tool_result"
    assert result["tool_choice"] == {"type": "any"}
    assert result["output_config"]["format"]["schema"] == {"type": "object"}


def test_openai_parallel_tool_results_merge_into_one_anthropic_user_turn():
    # Parallel tool calls produce consecutive `role: tool` messages, one per
    # call. The Anthropic Messages API requires strictly alternating
    # user/assistant turns, so these must collapse into a single user turn
    # with multiple tool_result blocks -- two consecutive user messages is
    # an invalid request that a native Anthropic backend rejects outright.
    result = openai_request_to_anthropic({
        "model": "alias",
        "messages": [
            {"role": "user", "content": "weather in two cities?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call-1", "type": "function",
                     "function": {"name": "weather", "arguments": '{"city":"NYC"}'}},
                    {"id": "call-2", "type": "function",
                     "function": {"name": "weather", "arguments": '{"city":"LA"}'}},
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "sunny"},
            {"role": "tool", "tool_call_id": "call-2", "content": "cloudy"},
        ],
    }, "backend-model")
    roles = [message["role"] for message in result["messages"]]
    assert roles == ["user", "assistant", "user"]
    tool_turn = result["messages"][2]["content"]
    assert [block["type"] for block in tool_turn] == ["tool_result", "tool_result"]
    assert [block["tool_use_id"] for block in tool_turn] == ["call-1", "call-2"]


def test_anthropic_top_level_effort_takes_priority_over_thinking_budget():
    payload = {
        "model": "alias",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hello"}],
        "effort": "xhigh",
        "thinking": {"type": "enabled", "budget_tokens": 2_000},
    }
    result = anthropic_to_openai(payload, "backend-model")
    assert result["reasoning_effort"] == "xhigh"


def test_openai_reasoning_effort_maps_to_anthropic_effort():
    result = openai_request_to_anthropic({
        "model": "alias",
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning_effort": "high",
    }, "backend-model")
    assert result["effort"] == "high"


def test_responses_reasoning_effort_maps_to_chat_reasoning_effort():
    result = responses_to_chat({
        "input": "hello",
        "reasoning": {"effort": "medium"},
    }, "model")
    assert result["reasoning_effort"] == "medium"


def test_openai_reasoning_content_maps_to_anthropic_thinking_block():
    response = {
        "id": "chat-1",
        "choices": [{
            "message": {
                "reasoning_content": "let me think",
                "content": "the answer",
            },
            "finish_reason": "stop",
        }],
    }
    result = openai_to_anthropic(response, "alias")
    assert result["content"][0] == {
        "type": "thinking", "thinking": "let me think", "signature": "",
    }
    assert result["content"][1] == {"type": "text", "text": "the answer"}


def test_anthropic_thinking_maps_to_reasoning_effort():
    payload = {
        "model": "alias",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hello"}],
        "thinking": {"type": "enabled", "budget_tokens": 20_000},
    }
    result = anthropic_to_openai(payload, "backend-model")
    assert result["reasoning_effort"] == "xhigh"


def test_anthropic_thinking_disabled_is_not_forwarded():
    payload = {
        "model": "alias",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hello"}],
        "thinking": {"type": "disabled"},
    }
    result = anthropic_to_openai(payload, "backend-model")
    assert "reasoning_effort" not in result


def test_anthropic_tool_choice_maps_to_openai_tool_choice():
    forced = anthropic_to_openai({
        "model": "alias",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hello"}],
        "tool_choice": {"type": "tool", "name": "weather"},
    }, "backend-model")
    assert forced["tool_choice"] == {"type": "function", "function": {"name": "weather"}}

    required = anthropic_to_openai({
        "model": "alias",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hello"}],
        "tool_choice": {"type": "any"},
    }, "backend-model")
    assert required["tool_choice"] == "required"
    result = responses_to_chat({
        "instructions": "Be concise",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "sunny",
            },
        ],
        "max_output_tokens": 20,
    }, "model")
    assert result["messages"][0] == {"role": "developer", "content": "Be concise"}
    assert result["messages"][1]["content"][0] == {"type": "text", "text": "hello"}
    assert result["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "sunny",
    }


def test_openai_cached_tokens_surface_as_anthropic_cache_read():
    result = openai_to_anthropic({
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 80},
        },
    }, "alias")
    assert result["usage"]["cache_read_input_tokens"] == 80
    assert result["usage"]["input_tokens"] == 100


def test_openai_usage_without_cache_details_omits_cache_field():
    result = openai_to_anthropic({
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }, "alias")
    assert "cache_read_input_tokens" not in result["usage"]
