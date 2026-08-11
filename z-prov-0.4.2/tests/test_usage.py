from __future__ import annotations

import json
import logging

from z_prov.usage import log_usage_event


def test_usage_event_reports_tokens_and_never_a_cost_field(caplog):
    with caplog.at_level(logging.INFO, logger="z_prov.usage"):
        log_usage_event(
            request_id="req-1",
            alias="z-prov-claude",
            provider="anthropic",
            model="claude-sonnet-5",
            surface="messages",
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 60,
            },
            duration_seconds=0.25,
        )
    record = json.loads(caplog.records[0].message)
    assert record["input_tokens"] == 100
    assert record["output_tokens"] == 20
    assert record["cache_read_input_tokens"] == 60
    assert record["provider"] == "anthropic"
    assert record["request_id"] == "req-1"
    assert "cost" not in record
    assert "price" not in record


def test_usage_event_falls_back_to_openai_field_names():
    import io

    handler_stream = io.StringIO()
    handler = logging.StreamHandler(handler_stream)
    logger = logging.getLogger("z_prov.usage")
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        log_usage_event(
            request_id="req-2",
            alias="z-prov-groq",
            provider="groq",
            model="openai/gpt-oss-120b",
            surface="chat.completions",
            usage={"prompt_tokens": 12, "completion_tokens": 4},
            duration_seconds=0.1,
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    record = json.loads(handler_stream.getvalue().strip())
    assert record["input_tokens"] == 12
    assert record["output_tokens"] == 4
    assert "cache_read_input_tokens" not in record


def test_usage_event_handles_missing_usage_gracefully():
    import io

    handler_stream = io.StringIO()
    handler = logging.StreamHandler(handler_stream)
    logger = logging.getLogger("z_prov.usage")
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        log_usage_event(
            request_id="req-3",
            alias="alias",
            provider="provider",
            model="model",
            surface="messages",
            usage=None,
            duration_seconds=0.01,
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    record = json.loads(handler_stream.getvalue().strip())
    assert record["input_tokens"] == 0
    assert record["output_tokens"] == 0
