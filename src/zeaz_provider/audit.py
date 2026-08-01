from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

# Uvicorn configures this logger at INFO for both CLI and programmatic startup.
# Using its message channel avoids silently dropping audit records at root's
# default WARNING level.
_LOGGER = logging.getLogger("uvicorn.error")
_KNOWN_PATHS = frozenset(
    {
        "/health/live",
        "/health/ready",
        "/metrics",
        "/v1/chat/completions",
        "/v1/messages",
        "/v1/models",
        "/v1/models/refresh",
        "/v1/responses",
    }
)
_KNOWN_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})


def audit_path(path: str) -> str:
    """Return only a public route name, never an arbitrary user-supplied path."""
    return path if path in _KNOWN_PATHS else "unmatched"


def audit_method(method: str) -> str:
    normalized = method.upper()
    return normalized if normalized in _KNOWN_METHODS else "OTHER"


def emit_request_audit(
    *,
    request_id: str,
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    client_id: str,
    rate_limited: bool,
) -> None:
    """Emit a JSON event made exclusively from explicitly allowed metadata."""
    event = {
        "schema_version": 1,
        "timestamp": datetime.now(UTC).isoformat(),
        "event": "http_request",
        "request_id": hashlib.sha256(request_id.encode()).hexdigest(),
        "method": audit_method(method),
        "path": audit_path(path),
        "status_code": status_code,
        "duration_ms": round(max(0.0, duration_ms), 3),
        "client_id": client_id,
        "rate_limited": rate_limited,
    }
    _LOGGER.info(json.dumps(event, separators=(",", ":"), sort_keys=True))
