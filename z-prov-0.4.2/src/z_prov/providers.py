from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import ProviderConfig
from .errors import ErrorKind, ProviderError, classify_http_error
from .resilience import ResilienceExecutor, ResiliencePolicy


class ProviderClient:
    def __init__(self, config: ProviderConfig, client: httpx.AsyncClient):
        self.config = config
        self.client = client
        self.resilience = ResilienceExecutor(ResiliencePolicy(
            max_attempts=config.max_attempts,
            base_delay_seconds=config.retry_base_seconds,
            max_delay_seconds=config.retry_max_seconds,
            failure_threshold=config.circuit_failure_threshold,
            reset_timeout_seconds=config.circuit_reset_seconds,
            total_timeout_seconds=config.total_timeout_seconds,
        ))

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
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
        if extra:
            headers.update(extra)
        return headers

    def url(self, endpoint: str) -> str:
        url = f"{self.config.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        if self.config.api == "azure" and self.config.api_version:
            return f"{url}?api-version={self.config.api_version}"
        return url

    async def chat(
        self, payload: dict[str, Any], extra_headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        url = self.url("chat/completions")
        return await self._json("POST", url, payload, extra_headers)

    async def messages(
        self, payload: dict[str, Any], extra_headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        url = self.url("messages")
        return await self._json("POST", url, payload, extra_headers)

    async def responses(
        self, payload: dict[str, Any], extra_headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        url = self.url("responses")
        return await self._json("POST", url, payload, extra_headers)

    async def models(self) -> dict[str, Any]:
        return await self._json("GET", self.url("models"), None)

    async def raw(
        self,
        method: str,
        endpoint: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        # Used by the /v1/providers/{provider}/{files,batches} control-plane
        # proxy. Unlike chat()/messages()/responses(), this deliberately
        # returns the raw httpx.Response rather than parsed JSON: file
        # uploads are multipart, list/retrieve responses have provider-
        # specific shapes we do not normalize (see docs/DEEP_UPGRADE_AUDIT_
        # ZCODER_1_36.md: "Provider extension namespace before cross-provider
        # normalization"), and callers may need the status code or binary
        # body verbatim. No resilience/circuit-breaker wrapping here either:
        # file uploads and batch submissions are not safe to retry blindly
        # since they are not always idempotent.
        try:
            return await self.client.request(
                method,
                self.url(endpoint),
                content=content,
                headers=self.headers(headers),
                params=params,
                timeout=self.config.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "Provider request timed out",
                504,
                kind=ErrorKind.TIMEOUT,
                fallback_allowed=False,
                circuit_failure=True,
            ) from exc
        except httpx.NetworkError as exc:
            raise ProviderError(
                "Provider network failure",
                502,
                kind=ErrorKind.NETWORK,
                fallback_allowed=False,
                circuit_failure=True,
            ) from exc

    async def stream(
        self,
        endpoint: str,
        payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> AsyncIterator[bytes]:
        # Unlike _json()/call(), a stream is not safe to retry once bytes
        # have reached the client, so this deliberately does not go through
        # ResilienceExecutor.call(). It does still drive the same
        # CircuitBreaker instance that non-streaming calls use: before this
        # fix, a provider that was down for streaming traffic would never
        # accumulate failures and the breaker would stay permanently
        # "closed" for that provider, so the router kept retrying a dead
        # backend first on every single streaming request instead of
        # learning to prefer a healthy fallback the way it already does for
        # non-streaming calls.
        breaker = self.resilience.breaker
        url = self.url(endpoint)
        breaker.before_call()
        try:
            async with self.client.stream(
                "POST",
                url,
                headers=self.headers(extra_headers),
                json=payload,
                timeout=self.config.timeout_seconds,
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread())[:8192].decode(errors="replace")
                    error = classify_http_error(
                        response.status_code,
                        body,
                        retry_after=_retry_after(response),
                    )
                    if error.circuit_failure:
                        breaker.failure()
                    elif breaker.state == "half_open":
                        breaker.success()
                    raise error
                async for chunk in response.aiter_bytes():
                    yield chunk
            breaker.success()
        except httpx.TimeoutException as exc:
            breaker.failure()
            raise ProviderError(
                "Provider stream timed out",
                504,
                kind=ErrorKind.TIMEOUT,
                fallback_allowed=True,
                circuit_failure=True,
            ) from exc
        except httpx.NetworkError as exc:
            breaker.failure()
            raise ProviderError(
                "Provider stream network failure",
                502,
                kind=ErrorKind.NETWORK,
                fallback_allowed=True,
                circuit_failure=True,
            ) from exc

    async def _json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        async def request() -> dict[str, Any]:
            try:
                request_args: dict[str, Any] = {
                    "headers": self.headers(extra_headers),
                    "timeout": self.config.timeout_seconds,
                }
                if payload is not None:
                    request_args["json"] = payload
                response = await self.client.request(method, url, **request_args)
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
            if response.status_code >= 400:
                raise classify_http_error(
                    response.status_code,
                    response.text[:8192],
                    retry_after=_retry_after(response),
                )
            try:
                return response.json()
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    "Provider returned invalid JSON",
                    kind=ErrorKind.PROTOCOL,
                    fallback_allowed=True,
                    circuit_failure=True,
                ) from exc

        return await self.resilience.call(request)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
