from __future__ import annotations

import json
import logging
import time
from typing import Any

# A dedicated logger name (rather than the root logger) lets an operator
# route usage events to their own sink -- e.g. a metering pipeline -- without
# picking up unrelated application logs, and without this module reaching
# into logging configuration it doesn't own.
logger = logging.getLogger("z_prov.usage")


def log_usage_event(
    *,
    request_id: str,
    alias: str,
    provider: str,
    model: str,
    surface: str,
    usage: dict[str, Any] | None,
    duration_seconds: float,
    stream: bool = False,
) -> None:
    """Emit one structured, machine-readable usage event per completed call.

    This intentionally reports token counts only -- never a dollar cost.
    Provider pricing changes independently of this gateway and per audit
    guidance (see docs/DEEP_UPGRADE_AUDIT_ZCODER_1_36.md, "Usage/cost
    events") Z-Prov must not assert a cost figure it cannot verify against
    the provider's own billing source. Prompt/response content is never
    included here; only counts and routing metadata.
    """
    usage = usage or {}
    event = {
        "event": "usage",
        "request_id": request_id,
        "alias": alias,
        "provider": provider,
        "model": model,
        "surface": surface,
        "stream": stream,
        "duration_ms": round(duration_seconds * 1000, 2),
        "input_tokens": _first_present(usage, "input_tokens", "prompt_tokens"),
        "output_tokens": _first_present(usage, "output_tokens", "completion_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "timestamp": time.time(),
    }
    logger.info(json.dumps({k: v for k, v in event.items() if v is not None}))


def _first_present(usage: dict[str, Any], *keys: str) -> int:
    for key in keys:
        if key in usage:
            return usage[key]
    return 0
