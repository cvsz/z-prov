from __future__ import annotations

import math
from enum import StrEnum
from typing import Any


class ErrorKind(StrEnum):
    AUTHENTICATION = "authentication"
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    NETWORK = "network"
    UPSTREAM = "upstream"
    PROTOCOL = "protocol"
    CIRCUIT_OPEN = "circuit_open"
    CONFIGURATION = "configuration"


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int = 502,
        retryable: bool = False,
        *,
        kind: ErrorKind = ErrorKind.UPSTREAM,
        fallback_allowed: bool | None = None,
        circuit_failure: bool | None = None,
        retry_after: float | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.kind = kind
        self.fallback_allowed = retryable if fallback_allowed is None else fallback_allowed
        self.circuit_failure = retryable if circuit_failure is None else circuit_failure
        self.retry_after = retry_after
        self.details = details or {}


def classify_http_error(
    status_code: int,
    message: str,
    *,
    retry_after: float | None = None,
) -> ProviderError:
    safe_retry_after = (
        retry_after
        if isinstance(retry_after, (int, float))
        and not isinstance(retry_after, bool)
        and math.isfinite(retry_after)
        and retry_after >= 0
        else None
    )
    if status_code in {401, 403}:
        return ProviderError(
            "Provider authentication failed",
            status_code,
            kind=ErrorKind.AUTHENTICATION,
            fallback_allowed=False,
            circuit_failure=False,
        )
    if status_code == 404:
        return ProviderError(
            "Provider model or endpoint was not found",
            status_code,
            kind=ErrorKind.NOT_FOUND,
            fallback_allowed=True,
            circuit_failure=False,
        )
    if status_code == 429:
        return ProviderError(
            "Provider rate limit exceeded",
            status_code,
            retryable=True,
            kind=ErrorKind.RATE_LIMIT,
            fallback_allowed=True,
            circuit_failure=True,
            retry_after=safe_retry_after,
        )
    if status_code in {408, 409, 425}:
        return ProviderError(
            "Provider request was rejected temporarily",
            status_code,
            retryable=True,
            kind=ErrorKind.UPSTREAM,
            fallback_allowed=True,
            circuit_failure=True,
        )
    if status_code >= 500:
        return ProviderError(
            "Provider service failure",
            status_code,
            retryable=True,
            kind=ErrorKind.UPSTREAM,
            fallback_allowed=True,
            circuit_failure=True,
        )
    return ProviderError(
        "Provider rejected the request",
        status_code,
        kind=ErrorKind.BAD_REQUEST,
        fallback_allowed=False,
        circuit_failure=False,
    )
