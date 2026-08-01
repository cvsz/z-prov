"""Bounded Anthropic Workload Identity Federation token exchange."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    SecretStr,
    StringConstraints,
    ValidationError,
)

TaggedRuleId = Annotated[
    str, StringConstraints(pattern=r"^fdrl_[A-Za-z0-9_-]{1,248}$")
]
TaggedServiceAccountId = Annotated[
    str, StringConstraints(pattern=r"^svac_[A-Za-z0-9_-]{1,248}$")
]
TaggedWorkspaceId = Annotated[
    str, StringConstraints(pattern=r"^wrkspc_[A-Za-z0-9_-]{1,246}$")
]
OAuthScope = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*:[a-z][a-z0-9_-]*$")
]
IdentityTokenProvider = Callable[[], Awaitable[SecretStr]]

JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class WIFExchangeError(RuntimeError):
    """A sanitized workload identity exchange failure."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AnthropicWIFConfig(StrictModel):
    federation_rule_id: TaggedRuleId
    organization_id: UUID
    service_account_id: TaggedServiceAccountId
    workspace_id: TaggedWorkspaceId | Literal["default"] | None = None


class FederatedCredential(StrictModel):
    """A short-lived bearer credential; SecretStr prevents incidental display."""

    role: Literal["federated"] = "federated"
    access_token: SecretStr
    token_type: Literal["Bearer"]
    scope: OAuthScope
    issued_at: datetime
    expires_at: datetime

    def authorization_header(self) -> str:
        return f"Bearer {self.access_token.get_secret_value()}"


class AnthropicWIFExchange:
    """Exchange fresh upstream JWTs and cache only bounded short-lived tokens."""

    def __init__(
        self,
        config: AnthropicWIFConfig,
        identity_token_provider: IdentityTokenProvider,
        *,
        base_url: str = "https://api.anthropic.com",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30,
        max_response_bytes: int = 65_536,
        max_assertion_bytes: int = 32_768,
        refresh_margin_seconds: int = 30,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(config, AnthropicWIFConfig):
            raise TypeError("an Anthropic WIF configuration is required")
        if not callable(identity_token_provider):
            raise TypeError("an async identity token provider is required")
        _validate_base_url(base_url)
        if not 0 < timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        if not 1024 <= max_response_bytes <= 1_048_576:
            raise ValueError("max_response_bytes must be between 1 KiB and 1 MiB")
        if not 256 <= max_assertion_bytes <= 1_048_576:
            raise ValueError("max_assertion_bytes must be between 256 B and 1 MiB")
        if not 0 <= refresh_margin_seconds <= 3600:
            raise ValueError("refresh_margin_seconds must be between 0 and 3600")
        self._config = config
        self._identity_token_provider = identity_token_provider
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=timeout_seconds,
        )
        self._owns_client = client is None
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_assertion_bytes = max_assertion_bytes
        self._refresh_margin = refresh_margin_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._cached: FederatedCredential | None = None
        self._refresh_at = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        self._cached = None
        self._refresh_at = 0
        if self._owns_client:
            await self._client.aclose()

    async def credential(self) -> FederatedCredential:
        if self._cached is not None and self._monotonic() < self._refresh_at:
            return self._cached
        async with self._lock:
            if self._cached is not None and self._monotonic() < self._refresh_at:
                return self._cached
            credential, usable_seconds = await self._exchange()
            self._cached = credential
            self._refresh_at = self._monotonic() + usable_seconds
            return credential

    async def _exchange(self) -> tuple[FederatedCredential, float]:
        try:
            provided = await self._identity_token_provider()
        except Exception as exc:
            raise WIFExchangeError("identity token provider failed") from exc
        if not isinstance(provided, SecretStr):
            raise WIFExchangeError("identity token provider returned an invalid token")
        assertion = provided.get_secret_value()
        _validate_assertion(assertion, self._max_assertion_bytes)
        payload: dict[str, str] = {
            "grant_type": JWT_BEARER_GRANT,
            "assertion": assertion,
            "federation_rule_id": self._config.federation_rule_id,
            "organization_id": str(self._config.organization_id),
            "service_account_id": self._config.service_account_id,
        }
        if self._config.workspace_id is not None:
            payload["workspace_id"] = self._config.workspace_id
        value = await self._request_json(payload)
        try:
            if set(value) != {"access_token", "token_type", "expires_in", "scope"}:
                raise ValueError
            token = value["access_token"]
            expires_in = value["expires_in"]
            if (
                not isinstance(token, str)
                or not token.startswith("sk-ant-oat01-")
                or len(token) > 8192
                or not isinstance(expires_in, int)
                or isinstance(expires_in, bool)
                or not 1 <= expires_in <= 86_400
            ):
                raise ValueError
            issued_at = self._clock()
            if issued_at.tzinfo is None or issued_at.utcoffset() is None:
                raise ValueError
            credential = FederatedCredential(
                access_token=SecretStr(token),
                token_type=value["token_type"],
                scope=value["scope"],
                issued_at=issued_at,
                expires_at=issued_at + timedelta(seconds=expires_in),
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise WIFExchangeError(
                "token exchange returned an invalid response"
            ) from exc
        return credential, max(0.0, expires_in - self._refresh_margin)

    async def _request_json(self, payload: dict[str, str]) -> dict[str, Any]:
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/v1/oauth/token",
                headers={
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                json=payload,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    raise WIFExchangeError("token exchange redirects are forbidden")
                if not 200 <= response.status_code < 300:
                    raise WIFExchangeError(
                        f"token exchange failed with HTTP {response.status_code}"
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise WIFExchangeError(
                            "token exchange response exceeded its byte limit"
                        )
        except WIFExchangeError:
            raise
        except httpx.HTTPError as exc:
            raise WIFExchangeError("token exchange request failed") from exc
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WIFExchangeError("token exchange returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise WIFExchangeError("token exchange returned a non-object response")
        return value


def _validate_assertion(value: str, max_bytes: int) -> None:
    if (
        not value
        or len(value.encode()) > max_bytes
        or value.count(".") != 2
        or any(character.isspace() for character in value)
    ):
        raise WIFExchangeError("identity token provider returned an invalid token")


def _validate_base_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url must be a credential-free HTTPS origin")
