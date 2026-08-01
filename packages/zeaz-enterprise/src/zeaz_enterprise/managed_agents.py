"""Bounded Anthropic Managed Agents environment and deployment adapter."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
)

MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"
Identifier = Annotated[
    str, StringConstraints(pattern=r"^[a-z]+_[A-Za-z0-9_-]{1,248}$")
]
Name = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Description = Annotated[str, StringConstraints(max_length=4096)]
Hostname = Annotated[
    str,
    StringConstraints(
        pattern=r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
    ),
]
Metadata = Annotated[
    dict[
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")],
        Annotated[str, StringConstraints(max_length=512)],
    ],
    Field(max_length=16),
]


class ManagedAgentAdapterError(RuntimeError):
    """A sanitized provider or policy failure."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ManagedAgentCredential(StrictModel):
    role: Literal["regular"] = "regular"
    secret: SecretStr

    @field_validator("secret")
    @classmethod
    def secret_is_present(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("managed-agent credential cannot be empty")
        return value


class LimitedNetwork(StrictModel):
    type: Literal["limited"] = "limited"
    allowed_hosts: tuple[Hostname, ...] = Field(default=(), max_length=100)
    allow_mcp_servers: bool = False
    allow_package_managers: bool = False

    @field_validator("allowed_hosts")
    @classmethod
    def hosts_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("allowed hosts must be unique")
        return value


class PackageSet(StrictModel):
    type: Literal["packages"] = "packages"
    apt: tuple[Name, ...] = Field(default=(), max_length=100)
    cargo: tuple[Name, ...] = Field(default=(), max_length=100)
    gem: tuple[Name, ...] = Field(default=(), max_length=100)
    go: tuple[Name, ...] = Field(default=(), max_length=100)
    npm: tuple[Name, ...] = Field(default=(), max_length=100)
    pip: tuple[Name, ...] = Field(default=(), max_length=100)


class CloudEnvironmentConfig(StrictModel):
    type: Literal["cloud"] = "cloud"
    networking: LimitedNetwork
    packages: PackageSet = PackageSet()


class SelfHostedEnvironmentConfig(StrictModel):
    type: Literal["self_hosted"] = "self_hosted"


EnvironmentConfig = Annotated[
    CloudEnvironmentConfig | SelfHostedEnvironmentConfig,
    Field(discriminator="type"),
]


class EnvironmentSpec(StrictModel):
    name: Name
    description: Description = ""
    config: EnvironmentConfig
    metadata: Metadata = {}
    scope: Literal["organization", "account"] | None = None

    @field_validator("scope")
    @classmethod
    def account_scope_only_for_self_hosted(
        cls, value: str | None, info: Any
    ) -> str | None:
        config = info.data.get("config")
        if (
            value is not None
            and isinstance(config, CloudEnvironmentConfig)
        ):
            raise ValueError("scope is only supported for self-hosted environments")
        return value


class Environment(StrictModel):
    id: Identifier
    type: Literal["environment"]
    name: Name
    description: Description
    config: EnvironmentConfig
    metadata: Metadata
    scope: Literal["organization", "account"] | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class AgentPin(StrictModel):
    type: Literal["agent"] = "agent"
    id: Identifier
    version: Annotated[int, Field(ge=1)]


class TextBlock(StrictModel):
    type: Literal["text"] = "text"
    text: Annotated[str, StringConstraints(min_length=1, max_length=65_536)]


class UserMessage(StrictModel):
    type: Literal["user.message"] = "user.message"
    content: tuple[TextBlock, ...] = Field(min_length=1, max_length=100)


class CronSchedule(StrictModel):
    type: Literal["cron"] = "cron"
    expression: Annotated[str, StringConstraints(min_length=9, max_length=128)]
    timezone: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_+./-]{1,128}$")]

    @field_validator("expression")
    @classmethod
    def five_fields(cls, value: str) -> str:
        if len(value.split()) != 5 or any(ord(char) < 32 for char in value):
            raise ValueError("schedule must be a five-field POSIX cron expression")
        return value


class DeploymentSpec(StrictModel):
    name: Name
    agent: Identifier | AgentPin
    environment_id: Identifier
    initial_events: tuple[UserMessage, ...] = Field(min_length=1, max_length=50)
    schedule: CronSchedule
    description: Description = ""
    metadata: Metadata = {}
    vault_ids: tuple[Identifier, ...] = Field(default=(), max_length=50)

    @field_validator("vault_ids")
    @classmethod
    def vaults_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("vault IDs must be unique")
        return value


class Deployment(StrictModel):
    id: Identifier
    name: Name
    status: Literal["active", "paused"]
    agent: AgentPin
    environment_id: Identifier
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class DeploymentRun(StrictModel):
    id: Identifier
    type: Literal["deployment_run"]
    deployment_id: Identifier
    session_id: Identifier | None
    created_at: datetime


class AnthropicManagedAgentsAdapter:
    def __init__(
        self,
        credential: ManagedAgentCredential,
        *,
        base_url: str = "https://api.anthropic.com",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30,
        max_response_bytes: int = 8_388_608,
    ) -> None:
        if not isinstance(credential, ManagedAgentCredential):
            raise TypeError("a regular-role managed-agent credential is required")
        _validate_base_url(base_url)
        if not 0 < timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        if not 1024 <= max_response_bytes <= 67_108_864:
            raise ValueError("max_response_bytes must be between 1 KiB and 64 MiB")
        self._credential = credential
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=timeout_seconds
        )
        self._owns_client = client is None
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def create_environment(
        self, spec: EnvironmentSpec, *, idempotency_key: str
    ) -> Environment:
        _idempotency_key(idempotency_key)
        value = await self._request(
            "POST",
            "/v1/environments",
            json_body=spec.model_dump(mode="json", exclude_none=True),
            idempotency_key=idempotency_key,
        )
        return _environment(value)

    async def get_environment(self, environment_id: str) -> Environment:
        value = await self._request(
            "GET", f"/v1/environments/{_identifier(environment_id, 'env_')}"
        )
        return _environment(value)

    async def list_environments(
        self, *, page_size: int = 100, max_pages: int = 1000, max_items: int = 100_000
    ) -> tuple[Environment, ...]:
        raw = await self._list_pages(
            "/v1/environments", page_size, max_pages, max_items
        )
        return tuple(_environment(item) for item in raw)

    async def archive_environment(
        self, environment_id: str, *, idempotency_key: str
    ) -> Environment:
        _idempotency_key(idempotency_key)
        value = await self._request(
            "POST",
            f"/v1/environments/{_identifier(environment_id, 'env_')}/archive",
            json_body={},
            idempotency_key=idempotency_key,
        )
        return _environment(value)

    async def create_deployment(
        self, spec: DeploymentSpec, *, idempotency_key: str
    ) -> Deployment:
        _idempotency_key(idempotency_key)
        value = await self._request(
            "POST",
            "/v1/deployments",
            json_body=spec.model_dump(mode="json"),
            idempotency_key=idempotency_key,
        )
        return _deployment(value)

    async def get_deployment(self, deployment_id: str) -> Deployment:
        value = await self._request(
            "GET", f"/v1/deployments/{_identifier(deployment_id, 'depl_')}"
        )
        return _deployment(value)

    async def list_deployments(
        self, *, page_size: int = 100, max_pages: int = 1000, max_items: int = 100_000
    ) -> tuple[Deployment, ...]:
        raw = await self._list_pages(
            "/v1/deployments", page_size, max_pages, max_items
        )
        return tuple(_deployment(item) for item in raw)

    async def deployment_action(
        self,
        deployment_id: str,
        action: Literal["pause", "unpause", "archive"],
        *,
        idempotency_key: str,
    ) -> Deployment:
        _idempotency_key(idempotency_key)
        value = await self._request(
            "POST",
            f"/v1/deployments/{_identifier(deployment_id, 'depl_')}/{action}",
            json_body={},
            idempotency_key=idempotency_key,
        )
        return _deployment(value)

    async def run_deployment(
        self, deployment_id: str, *, idempotency_key: str
    ) -> DeploymentRun:
        _idempotency_key(idempotency_key)
        value = await self._request(
            "POST",
            f"/v1/deployments/{_identifier(deployment_id, 'depl_')}/run",
            json_body={},
            idempotency_key=idempotency_key,
        )
        return _model(DeploymentRun, value, "deployment run")

    async def _list_pages(
        self, path: str, page_size: int, max_pages: int, max_items: int
    ) -> tuple[dict[str, Any], ...]:
        if not 1 <= page_size <= 100 or not 1 <= max_pages <= 10_000:
            raise ValueError("managed-agent pagination limits are invalid")
        if not page_size <= max_items <= 1_000_000:
            raise ValueError("managed-agent max_items is invalid")
        page: str | None = None
        tokens: set[str] = set()
        ids: set[str] = set()
        items: list[dict[str, Any]] = []
        for _ in range(max_pages):
            params: list[tuple[str, str | int]] = [("limit", page_size)]
            if page is not None:
                params.append(("page", page))
            value = await self._request("GET", path, params=params)
            if set(value) != {"data", "next_page"}:
                raise ManagedAgentAdapterError("managed-agent API returned an invalid page")
            data, next_page = value["data"], value["next_page"]
            if not isinstance(data, list) or len(data) > page_size:
                raise ManagedAgentAdapterError("managed-agent API returned an invalid page")
            for item in data:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise ManagedAgentAdapterError("managed-agent API returned an invalid item")
                if item["id"] in ids:
                    raise ManagedAgentAdapterError("managed-agent API returned a duplicate ID")
                ids.add(item["id"])
                items.append(item)
                if len(items) > max_items:
                    raise ManagedAgentAdapterError("managed-agent API exceeded its item limit")
            if next_page is None:
                return tuple(items)
            if (
                not isinstance(next_page, str)
                or not next_page
                or len(next_page) > 4096
                or next_page in tokens
                or not data
            ):
                raise ManagedAgentAdapterError("managed-agent API returned an invalid page token")
            tokens.add(next_page)
            page = next_page
        raise ManagedAgentAdapterError("managed-agent API exceeded its page limit")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str | int]] | None = None,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "x-api-key": self._credential.secret.get_secret_value(),
            "anthropic-version": "2023-06-01",
            "anthropic-beta": MANAGED_AGENTS_BETA,
            "accept": "application/json",
        }
        if idempotency_key is not None:
            headers["idempotency-key"] = idempotency_key
        try:
            async with self._client.stream(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                params=params,
                json=json_body,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    raise ManagedAgentAdapterError("managed-agent API redirects are forbidden")
                if not 200 <= response.status_code < 300:
                    raise ManagedAgentAdapterError(
                        f"managed-agent API failed with HTTP {response.status_code}"
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise ManagedAgentAdapterError(
                            "managed-agent API response exceeded its byte limit"
                        )
        except ManagedAgentAdapterError:
            raise
        except httpx.HTTPError as exc:
            raise ManagedAgentAdapterError("managed-agent API request failed") from exc
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManagedAgentAdapterError("managed-agent API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ManagedAgentAdapterError("managed-agent API returned a non-object response")
        return value


def _environment(value: dict[str, Any]) -> Environment:
    return _model(Environment, value, "environment")


def _deployment(value: dict[str, Any]) -> Deployment:
    selected = {
        key: value.get(key)
        for key in (
            "id",
            "name",
            "status",
            "agent",
            "environment_id",
            "created_at",
            "updated_at",
            "archived_at",
        )
    }
    return _model(Deployment, selected, "deployment")


def _model(model: type[BaseModel], value: object, label: str) -> Any:
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise ManagedAgentAdapterError(
            f"managed-agent API returned an invalid {label}"
        ) from exc


def _identifier(value: str, prefix: str) -> str:
    try:
        parsed = TypeAdapter(Identifier).validate_python(value)
    except ValidationError as exc:
        raise ValueError("managed-agent identifier is invalid") from exc
    if not parsed.startswith(prefix):
        raise ValueError("managed-agent identifier has the wrong resource type")
    return parsed


def _idempotency_key(value: str) -> None:
    if not 1 <= len(value) <= 255 or any(
        not (character.isalnum() or character in "._:-") for character in value
    ):
        raise ValueError("idempotency key is invalid")


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
