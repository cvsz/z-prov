from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import ProviderConfig
from .errors import ErrorKind, ProviderError, classify_http_error
from .resilience import ResilienceExecutor, ResiliencePolicy


class ProviderClient:
    def __init__(
        self,
        config: ProviderConfig,
        client: httpx.AsyncClient,
        *,
        max_response_bytes: int = 16 * 1024 * 1024,
    ):
        self.config = config
        self.client = client
        self.max_response_bytes = max_response_bytes
        self.resilience = ResilienceExecutor(ResiliencePolicy(
            max_attempts=config.max_attempts,
            base_delay_seconds=config.retry_base_seconds,
            max_delay_seconds=config.retry_max_seconds,
            failure_threshold=config.circuit_failure_threshold,
            reset_timeout_seconds=config.circuit_reset_seconds,
            total_timeout_seconds=config.total_timeout_seconds,
        ))

    def headers(self) -> dict[str, str]:
        headers = dict(self.config.headers)
        if self.config.api == "anthropic":
            headers.update({
                "x-api-key": self.config.api_key,
                "anthropic-version": self.config.api_version or "2023-06-01",
            })
        elif self.config.api == "azure":
            headers["api-key"] = self.config.api_key
        elif self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def url(self, endpoint: str) -> str:
        url = f"{self.config.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        if self.config.api == "azure" and self.config.api_version:
            return f"{url}?api-version={self.config.api_version}"
        return url

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.url("chat/completions")
        return await self._json("POST", url, payload)

    async def messages(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.url("messages")
        return await self._json("POST", url, payload)

    async def responses(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.url("responses")
        return await self._json("POST", url, payload)

    async def models(self) -> dict[str, Any]:
        return await self._json("GET", self.url("models"), None)

    async def stream(self, endpoint: str, payload: dict[str, Any]) -> AsyncIterator[bytes]:
        url = self.url(endpoint)
        emitted_bytes = 0
        try:
            async with self.client.stream(
                "POST",
                url,
                headers=self.headers(),
                json=payload,
                timeout=self.config.timeout_seconds,
            ) as response:
                if response.status_code >= 400:
                    body = (await _read_at_most(response, 8192)).decode(errors="replace")
                    raise classify_http_error(
                        response.status_code,
                        body,
                        retry_after=_retry_after(response),
                    )
                async for chunk in response.aiter_bytes():
                    emitted_bytes += len(chunk)
                    if emitted_bytes > self.max_response_bytes:
                        raise _oversized_response()
                    yield chunk
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "Provider stream timed out",
                504,
                kind=ErrorKind.TIMEOUT,
                fallback_allowed=True,
                circuit_failure=True,
            ) from exc
        except httpx.NetworkError as exc:
            raise ProviderError(
                "Provider stream network failure",
                502,
                kind=ErrorKind.NETWORK,
                fallback_allowed=True,
                circuit_failure=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "Provider stream protocol failure",
                502,
                kind=ErrorKind.PROTOCOL,
                fallback_allowed=True,
                circuit_failure=True,
            ) from exc

    async def _json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        async def request() -> dict[str, Any]:
            try:
                request_args: dict[str, Any] = {
                    "headers": self.headers(),
                    "timeout": self.config.timeout_seconds,
                }
                if payload is not None:
                    request_args["json"] = payload
                response_context = self.client.stream(method, url, **request_args)
            except httpx.TimeoutException as exc:
                raise ProviderError(
                    "Provider request timed out",
                    504,
                    retryable=True,
                    kind=ErrorKind.TIMEOUT,
                    fallback_allowed=True,
                    circuit_failure=True,
                ) from exc
            except httpx.NetworkError as exc:
                raise ProviderError(
                    "Provider network failure",
                    502,
                    retryable=True,
                    kind=ErrorKind.NETWORK,
                    fallback_allowed=True,
                    circuit_failure=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderError(
                    "Provider request protocol failure",
                    502,
                    retryable=True,
                    kind=ErrorKind.PROTOCOL,
                    fallback_allowed=True,
                    circuit_failure=True,
                ) from exc
            try:
                async with response_context as response:
                    if response.status_code >= 400:
                        error_body = (await _read_at_most(response, 8192)).decode(errors="replace")
                        raise classify_http_error(
                            response.status_code,
                            error_body,
                            retry_after=_retry_after(response),
                        )
                    raw = await _read_bounded(response, self.max_response_bytes)
            except httpx.TimeoutException as exc:
                raise ProviderError(
                    "Provider request timed out",
                    504,
                    retryable=True,
                    kind=ErrorKind.TIMEOUT,
                    fallback_allowed=True,
                    circuit_failure=True,
                ) from exc
            except httpx.NetworkError as exc:
                raise ProviderError(
                    "Provider network failure",
                    502,
                    retryable=True,
                    kind=ErrorKind.NETWORK,
                    fallback_allowed=True,
                    circuit_failure=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderError(
                    "Provider request protocol failure",
                    502,
                    retryable=True,
                    kind=ErrorKind.PROTOCOL,
                    fallback_allowed=True,
                    circuit_failure=True,
                ) from exc
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProviderError(
                    "Provider returned invalid JSON",
                    kind=ErrorKind.PROTOCOL,
                    fallback_allowed=True,
                    circuit_failure=True,
                ) from exc
            if not isinstance(value, dict):
                raise ProviderError(
                    "Provider returned a non-object JSON response",
                    kind=ErrorKind.PROTOCOL,
                    fallback_allowed=True,
                    circuit_failure=True,
                )
            return value

        return await self.resilience.call(request)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _oversized_response() -> ProviderError:
    return ProviderError(
        "Provider response exceeded configured byte limit",
        502,
        kind=ErrorKind.PROTOCOL,
        fallback_allowed=True,
        circuit_failure=True,
    )


async def _read_bounded(response: httpx.Response, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > maximum:
            raise _oversized_response()
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_at_most(response: httpx.Response, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        remaining = maximum - total
        if remaining <= 0:
            break
        chunks.append(chunk[:remaining])
        total += min(len(chunk), remaining)
        if len(chunk) >= remaining:
            break
    return b"".join(chunks)
