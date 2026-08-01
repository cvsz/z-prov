"""Bounded, credential-isolated provider administration adapters."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    SecretStr,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$"),
]
DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=256)]
IdempotencyKey = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"),
]
_IDEMPOTENCY_KEY = TypeAdapter(IdempotencyKey)


class AdminAdapterError(RuntimeError):
    """A sanitized enterprise administration failure."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdminCredential(StrictModel):
    role: Literal["admin"] = "admin"
    provider: Literal["openai", "anthropic"]
    secret: SecretStr

    @field_validator("secret")
    @classmethod
    def secret_is_present(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("admin credential cannot be empty")
        return value


class UserRole(StrEnum):
    OWNER = "owner"
    READER = "reader"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class AdminUser(StrictModel):
    schema_version: Literal["1"] = "1"
    provider: Literal["openai"] = "openai"
    organization: Identifier
    id: Identifier
    name: DisplayName
    email: Annotated[
        str,
        StringConstraints(
            min_length=3,
            max_length=320,
            pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
        ),
    ]
    role: UserRole
    added_at: datetime

    @field_validator("added_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("added_at must include a UTC offset")
        return value


class AdminProject(StrictModel):
    schema_version: Literal["1"] = "1"
    provider: Literal["openai"] = "openai"
    organization: Identifier
    id: Identifier
    name: DisplayName
    status: ProjectStatus
    created_at: datetime
    archived_at: datetime | None = None

    @field_validator("created_at", "archived_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("project timestamps must include a UTC offset")
        return value

    @model_validator(mode="after")
    def status_matches_archive_time(self) -> AdminProject:
        if (self.status is ProjectStatus.ARCHIVED) != (self.archived_at is not None):
            raise ValueError("project status and archive timestamp do not match")
        return self


class OpenAIAdminAdapter:
    """OpenAI organization administration using a dedicated admin credential."""

    def __init__(
        self,
        credential: AdminCredential,
        *,
        organization: str,
        base_url: str = "https://api.openai.com",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30,
        max_response_bytes: int = 4_194_304,
    ) -> None:
        if not isinstance(credential, AdminCredential) or credential.provider != "openai":
            raise TypeError("an OpenAI admin-role credential is required")
        _validate_base_url(base_url)
        if not 0 < timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        if not 1024 <= max_response_bytes <= 16_777_216:
            raise ValueError("max_response_bytes must be between 1 KiB and 16 MiB")
        self.organization = TypeAdapter(Identifier).validate_python(organization)
        self._credential = credential
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=timeout_seconds,
        )
        self._owns_client = client is None
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def list_users(
        self,
        *,
        page_size: int = 100,
        max_pages: int = 1000,
        max_items: int = 100_000,
    ) -> tuple[AdminUser, ...]:
        raw = await self._list_all(
            "/v1/organization/users",
            page_size=page_size,
            max_pages=max_pages,
            max_items=max_items,
        )
        return tuple(self._user(item) for item in raw)

    async def get_user(self, user_id: str) -> AdminUser:
        identifier = _identifier(user_id)
        return self._user(
            await self._request_json(
                "GET",
                f"/v1/organization/users/{quote(identifier, safe='')}",
            )
        )

    async def update_user_role(
        self,
        user_id: str,
        role: UserRole,
        *,
        idempotency_key: str,
    ) -> AdminUser:
        identifier = _identifier(user_id)
        try:
            normalized_role = UserRole(role)
        except ValueError as exc:
            raise AdminAdapterError("user role is invalid") from exc
        return self._user(
            await self._request_json(
                "POST",
                f"/v1/organization/users/{quote(identifier, safe='')}",
                json_body={"role": normalized_role.value},
                idempotency_key=idempotency_key,
            )
        )

    async def list_projects(
        self,
        *,
        include_archived: bool = False,
        page_size: int = 100,
        max_pages: int = 1000,
        max_items: int = 100_000,
    ) -> tuple[AdminProject, ...]:
        raw = await self._list_all(
            "/v1/organization/projects",
            page_size=page_size,
            max_pages=max_pages,
            max_items=max_items,
            extra_params={"include_archived": str(include_archived).lower()},
        )
        return tuple(self._project(item) for item in raw)

    async def get_project(self, project_id: str) -> AdminProject:
        identifier = _identifier(project_id)
        return self._project(
            await self._request_json(
                "GET",
                f"/v1/organization/projects/{quote(identifier, safe='')}",
            )
        )

    async def create_project(
        self,
        name: str,
        *,
        idempotency_key: str,
        geography: str | None = None,
    ) -> AdminProject:
        body: dict[str, str] = {"name": _display_name(name)}
        if geography is not None:
            body["geography"] = _identifier(geography)
        return self._project(
            await self._request_json(
                "POST",
                "/v1/organization/projects",
                json_body=body,
                idempotency_key=idempotency_key,
            )
        )

    async def update_project(
        self,
        project_id: str,
        name: str,
        *,
        idempotency_key: str,
    ) -> AdminProject:
        identifier = _identifier(project_id)
        return self._project(
            await self._request_json(
                "POST",
                f"/v1/organization/projects/{quote(identifier, safe='')}",
                json_body={"name": _display_name(name)},
                idempotency_key=idempotency_key,
            )
        )

    async def archive_project(
        self,
        project_id: str,
        *,
        idempotency_key: str,
    ) -> AdminProject:
        identifier = _identifier(project_id)
        return self._project(
            await self._request_json(
                "POST",
                f"/v1/organization/projects/{quote(identifier, safe='')}/archive",
                json_body={},
                idempotency_key=idempotency_key,
            )
        )

    async def _list_all(
        self,
        path: str,
        *,
        page_size: int,
        max_pages: int,
        max_items: int,
        extra_params: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        if not 1 <= page_size <= 100 or not 1 <= max_pages <= 10_000:
            raise ValueError("admin pagination limits are invalid")
        if not page_size <= max_items <= 1_000_000:
            raise ValueError("max_items is invalid")
        cursor: str | None = None
        cursors: set[str] = set()
        identifiers: set[str] = set()
        items: list[dict[str, Any]] = []
        for _ in range(max_pages):
            params = {"limit": page_size, **(extra_params or {})}
            if cursor is not None:
                params["after"] = cursor
            value = await self._request_json("GET", path, params=params)
            raw_items = value.get("data")
            has_more = value.get("has_more")
            last_id = value.get("last_id")
            if (
                value.get("object") != "list"
                or set(value)
                != {"object", "data", "first_id", "last_id", "has_more"}
                or not isinstance(raw_items, list)
                or len(raw_items) > page_size
                or not isinstance(has_more, bool)
            ):
                raise AdminAdapterError("admin API returned an invalid page")
            for raw in raw_items:
                if not isinstance(raw, dict):
                    raise AdminAdapterError("admin API returned an invalid item")
                identifier = raw.get("id")
                if not isinstance(identifier, str) or identifier in identifiers:
                    raise AdminAdapterError("admin API returned a duplicate or invalid ID")
                identifiers.add(identifier)
                items.append(raw)
                if len(items) > max_items:
                    raise AdminAdapterError("admin API exceeded its item limit")
            if not has_more:
                return tuple(items)
            if (
                not isinstance(last_id, str)
                or not last_id
                or last_id in cursors
                or not raw_items
                or raw_items[-1].get("id") != last_id
            ):
                raise AdminAdapterError("admin API returned an invalid pagination cursor")
            cursors.add(last_id)
            cursor = last_id
        raise AdminAdapterError("admin API exceeded its page limit")

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json_body: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "authorization": f"Bearer {self._credential.secret.get_secret_value()}",
            "accept": "application/json",
        }
        if idempotency_key is not None:
            try:
                headers["idempotency-key"] = _IDEMPOTENCY_KEY.validate_python(
                    idempotency_key
                )
            except ValidationError as exc:
                raise AdminAdapterError("idempotency key is invalid") from exc
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
                    raise AdminAdapterError("admin API redirects are forbidden")
                if not 200 <= response.status_code < 300:
                    raise AdminAdapterError(
                        f"admin API failed with HTTP {response.status_code}"
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise AdminAdapterError(
                            "admin API response exceeded its byte limit"
                        )
        except AdminAdapterError:
            raise
        except httpx.HTTPError as exc:
            raise AdminAdapterError("admin API request failed") from exc
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdminAdapterError("admin API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise AdminAdapterError("admin API returned a non-object response")
        return value

    def _user(self, value: dict[str, Any]) -> AdminUser:
        try:
            if (
                value.get("object") != "organization.user"
                or set(value)
                != {"object", "id", "name", "email", "role", "added_at"}
            ):
                raise ValueError
            return AdminUser(
                organization=self.organization,
                id=value["id"],
                name=value["name"],
                email=value["email"],
                role=value["role"],
                added_at=_timestamp(value["added_at"]),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise AdminAdapterError("admin API returned an invalid user") from exc

    def _project(self, value: dict[str, Any]) -> AdminProject:
        try:
            if (
                value.get("object") != "organization.project"
                or set(value)
                != {
                    "object",
                    "id",
                    "name",
                    "status",
                    "created_at",
                    "archived_at",
                }
            ):
                raise ValueError
            archived = value["archived_at"]
            return AdminProject(
                organization=self.organization,
                id=value["id"],
                name=value["name"],
                status=value["status"],
                created_at=_timestamp(value["created_at"]),
                archived_at=None if archived is None else _timestamp(archived),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise AdminAdapterError("admin API returned an invalid project") from exc


def _timestamp(value: object) -> datetime:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("invalid timestamp")
    return datetime.fromtimestamp(value, tz=UTC)


def _identifier(value: str) -> str:
    try:
        return TypeAdapter(Identifier).validate_python(value)
    except ValidationError as exc:
        raise AdminAdapterError("resource identifier is invalid") from exc


def _display_name(value: str) -> str:
    try:
        return TypeAdapter(DisplayName).validate_python(value)
    except ValidationError as exc:
        raise AdminAdapterError("display name is invalid") from exc


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


class AnthropicUserRole(StrEnum):
    ADMIN = "admin"
    BILLING = "billing"
    CLAUDE_CODE_USER = "claude_code_user"
    DEVELOPER = "developer"
    MANAGED = "managed"
    MEMBERSHIP_ADMIN = "membership_admin"
    OWNER = "owner"
    PRIMARY_OWNER = "primary_owner"
    USER = "user"


class AnthropicAssignableRole(StrEnum):
    BILLING = "billing"
    CLAUDE_CODE_USER = "claude_code_user"
    DEVELOPER = "developer"
    MANAGED = "managed"
    USER = "user"


class AnthropicAdminUser(StrictModel):
    schema_version: Literal["1"] = "1"
    provider: Literal["anthropic"] = "anthropic"
    organization: Identifier
    id: Identifier
    name: DisplayName
    email: Annotated[
        str,
        StringConstraints(
            min_length=3,
            max_length=320,
            pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
        ),
    ]
    role: AnthropicUserRole
    added_at: datetime

    @field_validator("added_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("added_at must include a UTC offset")
        return value


class AnthropicWorkspace(StrictModel):
    schema_version: Literal["1"] = "1"
    provider: Literal["anthropic"] = "anthropic"
    organization: Identifier
    id: Identifier
    name: DisplayName
    display_color: Annotated[
        str,
        StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$"),
    ]
    compartment_id: Identifier
    external_key_id: Identifier | None
    created_at: datetime
    archived_at: datetime | None
    data_residency: dict[str, JsonValue]
    tags: dict[
        Annotated[str, StringConstraints(min_length=1, max_length=128)],
        Annotated[str, StringConstraints(max_length=512)],
    ]

    @field_validator("created_at", "archived_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("workspace timestamps must include a UTC offset")
        return value

    @field_validator("tags")
    @classmethod
    def tags_are_bounded(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 64 or any(key.lower().startswith("anthropic") for key in value):
            raise ValueError("workspace tags are invalid")
        return value


class AnthropicAdminAdapter:
    """Anthropic Admin API using a dedicated admin key or OAuth bearer."""

    def __init__(
        self,
        credential: AdminCredential,
        *,
        organization: str,
        auth_kind: Literal["api_key", "oauth"] = "api_key",
        base_url: str = "https://api.anthropic.com",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30,
        max_response_bytes: int = 4_194_304,
    ) -> None:
        if (
            not isinstance(credential, AdminCredential)
            or credential.provider != "anthropic"
        ):
            raise TypeError("an Anthropic admin-role credential is required")
        _validate_base_url(base_url)
        if not 0 < timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        if not 1024 <= max_response_bytes <= 16_777_216:
            raise ValueError("max_response_bytes must be between 1 KiB and 16 MiB")
        self.organization = TypeAdapter(Identifier).validate_python(organization)
        self._credential = credential
        self._auth_kind = auth_kind
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=timeout_seconds,
        )
        self._owns_client = client is None
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def list_users(
        self,
        *,
        page_size: int = 1000,
        max_pages: int = 1000,
        max_items: int = 100_000,
    ) -> tuple[AnthropicAdminUser, ...]:
        return tuple(
            self._user(item)
            for item in await self._list_all(
                "/v1/organizations/users",
                page_size=page_size,
                max_pages=max_pages,
                max_items=max_items,
            )
        )

    async def get_user(self, user_id: str) -> AnthropicAdminUser:
        identifier = _identifier(user_id)
        return self._user(
            await self._request_json(
                "GET",
                f"/v1/organizations/users/{quote(identifier, safe='')}",
            )
        )

    async def update_user_role(
        self,
        user_id: str,
        role: AnthropicAssignableRole,
        *,
        idempotency_key: str,
    ) -> AnthropicAdminUser:
        identifier = _identifier(user_id)
        try:
            normalized_role = AnthropicAssignableRole(role)
        except ValueError as exc:
            raise AdminAdapterError("Anthropic user role is not assignable") from exc
        return self._user(
            await self._request_json(
                "POST",
                f"/v1/organizations/users/{quote(identifier, safe='')}",
                json_body={"role": normalized_role.value},
                idempotency_key=idempotency_key,
            )
        )

    async def list_workspaces(
        self,
        *,
        include_archived: bool = False,
        page_size: int = 1000,
        max_pages: int = 1000,
        max_items: int = 100_000,
    ) -> tuple[AnthropicWorkspace, ...]:
        return tuple(
            self._workspace(item)
            for item in await self._list_all(
                "/v1/organizations/workspaces",
                page_size=page_size,
                max_pages=max_pages,
                max_items=max_items,
                extra_params={"include_archived": str(include_archived).lower()},
            )
        )

    async def get_workspace(self, workspace_id: str) -> AnthropicWorkspace:
        identifier = _identifier(workspace_id)
        return self._workspace(
            await self._request_json(
                "GET",
                f"/v1/organizations/workspaces/{quote(identifier, safe='')}",
            )
        )

    async def create_workspace(
        self,
        name: str,
        *,
        idempotency_key: str,
    ) -> AnthropicWorkspace:
        return self._workspace(
            await self._request_json(
                "POST",
                "/v1/organizations/workspaces",
                json_body={"name": _display_name(name)},
                idempotency_key=idempotency_key,
            )
        )

    async def update_workspace(
        self,
        workspace_id: str,
        name: str,
        *,
        idempotency_key: str,
    ) -> AnthropicWorkspace:
        identifier = _identifier(workspace_id)
        return self._workspace(
            await self._request_json(
                "POST",
                f"/v1/organizations/workspaces/{quote(identifier, safe='')}",
                json_body={"name": _display_name(name)},
                idempotency_key=idempotency_key,
            )
        )

    async def archive_workspace(
        self,
        workspace_id: str,
        *,
        idempotency_key: str,
    ) -> AnthropicWorkspace:
        identifier = _identifier(workspace_id)
        return self._workspace(
            await self._request_json(
                "POST",
                f"/v1/organizations/workspaces/{quote(identifier, safe='')}/archive",
                json_body={},
                idempotency_key=idempotency_key,
            )
        )

    async def _list_all(
        self,
        path: str,
        *,
        page_size: int,
        max_pages: int,
        max_items: int,
        extra_params: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        if not 1 <= page_size <= 1000 or not 1 <= max_pages <= 10_000:
            raise ValueError("admin pagination limits are invalid")
        if not page_size <= max_items <= 1_000_000:
            raise ValueError("max_items is invalid")
        cursor: str | None = None
        cursors: set[str] = set()
        identifiers: set[str] = set()
        items: list[dict[str, Any]] = []
        for _ in range(max_pages):
            params = {"limit": page_size, **(extra_params or {})}
            if cursor is not None:
                params["after_id"] = cursor
            value = await self._request_json("GET", path, params=params)
            if set(value) != {"data", "first_id", "last_id", "has_more"}:
                raise AdminAdapterError("Anthropic Admin API returned an invalid page")
            raw_items = value["data"]
            has_more = value["has_more"]
            last_id = value["last_id"]
            if (
                not isinstance(raw_items, list)
                or len(raw_items) > page_size
                or not isinstance(has_more, bool)
            ):
                raise AdminAdapterError("Anthropic Admin API returned an invalid page")
            for raw in raw_items:
                if not isinstance(raw, dict):
                    raise AdminAdapterError("Anthropic Admin API returned an invalid item")
                identifier = raw.get("id")
                if not isinstance(identifier, str) or identifier in identifiers:
                    raise AdminAdapterError(
                        "Anthropic Admin API returned a duplicate or invalid ID"
                    )
                identifiers.add(identifier)
                items.append(raw)
                if len(items) > max_items:
                    raise AdminAdapterError("Anthropic Admin API exceeded its item limit")
            if not has_more:
                return tuple(items)
            if (
                not isinstance(last_id, str)
                or not last_id
                or last_id in cursors
                or not raw_items
                or raw_items[-1].get("id") != last_id
            ):
                raise AdminAdapterError(
                    "Anthropic Admin API returned an invalid pagination cursor"
                )
            cursors.add(last_id)
            cursor = last_id
        raise AdminAdapterError("Anthropic Admin API exceeded its page limit")

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json_body: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        secret = self._credential.secret.get_secret_value()
        headers = {
            "anthropic-version": "2023-06-01",
            "accept": "application/json",
        }
        headers[
            "x-api-key" if self._auth_kind == "api_key" else "authorization"
        ] = secret if self._auth_kind == "api_key" else f"Bearer {secret}"
        if idempotency_key is not None:
            try:
                headers["idempotency-key"] = _IDEMPOTENCY_KEY.validate_python(
                    idempotency_key
                )
            except ValidationError as exc:
                raise AdminAdapterError("idempotency key is invalid") from exc
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
                    raise AdminAdapterError("Anthropic Admin API redirects are forbidden")
                if not 200 <= response.status_code < 300:
                    raise AdminAdapterError(
                        f"Anthropic Admin API failed with HTTP {response.status_code}"
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise AdminAdapterError(
                            "Anthropic Admin API response exceeded its byte limit"
                        )
        except AdminAdapterError:
            raise
        except httpx.HTTPError as exc:
            raise AdminAdapterError("Anthropic Admin API request failed") from exc
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdminAdapterError(
                "Anthropic Admin API returned invalid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise AdminAdapterError(
                "Anthropic Admin API returned a non-object response"
            )
        return value

    def _user(self, value: dict[str, Any]) -> AnthropicAdminUser:
        try:
            if (
                value.get("type") != "user"
                or set(value)
                != {"type", "id", "name", "email", "role", "added_at"}
            ):
                raise ValueError
            return AnthropicAdminUser(
                organization=self.organization,
                id=value["id"],
                name=value["name"],
                email=value["email"],
                role=value["role"],
                added_at=_rfc3339(value["added_at"]),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise AdminAdapterError(
                "Anthropic Admin API returned an invalid user"
            ) from exc

    def _workspace(self, value: dict[str, Any]) -> AnthropicWorkspace:
        try:
            if (
                value.get("type") != "workspace"
                or set(value)
                != {
                    "type",
                    "id",
                    "name",
                    "display_color",
                    "compartment_id",
                    "external_key_id",
                    "created_at",
                    "archived_at",
                    "data_residency",
                    "tags",
                }
            ):
                raise ValueError
            return AnthropicWorkspace(
                organization=self.organization,
                id=value["id"],
                name=value["name"],
                display_color=value["display_color"],
                compartment_id=value["compartment_id"],
                external_key_id=value["external_key_id"],
                created_at=_rfc3339(value["created_at"]),
                archived_at=(
                    None
                    if value["archived_at"] is None
                    else _rfc3339(value["archived_at"])
                ),
                data_residency=value["data_residency"],
                tags=value["tags"],
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise AdminAdapterError(
                "Anthropic Admin API returned an invalid workspace"
            ) from exc


def _rfc3339(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid RFC3339 timestamp")
    parsed = TypeAdapter(datetime).validate_python(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed
