"""Read-only Anthropic Compliance API adapter with a distinct credential role."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
)

Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$"),
]
ActivityType = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,127}$"),
]


class ComplianceAdapterError(RuntimeError):
    """A sanitized compliance API or policy failure."""


class ComplianceScope(StrEnum):
    ACTIVITIES = "read:compliance_activities"
    ORGANIZATION_DATA = "read:compliance_org_data"
    USER_DATA = "read:compliance_user_data"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ComplianceCredential(StrictModel):
    """In-memory-only credential binding; never part of resource records."""

    role: Literal["compliance"] = "compliance"
    secret: SecretStr
    scopes: frozenset[ComplianceScope] = Field(min_length=1, max_length=3)

    @field_validator("secret")
    @classmethod
    def secret_is_present(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("compliance credential cannot be empty")
        return value


class ComplianceOrganization(StrictModel):
    schema_version: Literal["1"] = "1"
    provider: Literal["anthropic"] = "anthropic"
    uuid: UUID
    name: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a UTC offset")
        return value


class ComplianceActivity(StrictModel):
    schema_version: Literal["1"] = "1"
    provider: Literal["anthropic"] = "anthropic"
    id: Identifier
    type: ActivityType
    created_at: datetime
    organization_id: Identifier
    organization_uuid: UUID
    actor: dict[
        Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")],
        JsonValue,
    ] = Field(max_length=32)
    details: dict[
        Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,127}$")],
        JsonValue,
    ] = Field(max_length=64)

    @field_validator("created_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a UTC offset")
        return value


class AnthropicComplianceAdapter:
    """Bounded read-only Compliance API access with explicit scopes."""

    def __init__(
        self,
        credential: ComplianceCredential,
        *,
        base_url: str = "https://api.anthropic.com",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30,
        max_response_bytes: int = 8_388_608,
        max_record_bytes: int = 262_144,
    ) -> None:
        if not isinstance(credential, ComplianceCredential):
            raise TypeError("a compliance-role credential is required")
        _validate_base_url(base_url)
        if not 0 < timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        if not 1024 <= max_response_bytes <= 67_108_864:
            raise ValueError("max_response_bytes must be between 1 KiB and 64 MiB")
        if not 1024 <= max_record_bytes <= 1_048_576:
            raise ValueError("max_record_bytes must be between 1 KiB and 1 MiB")
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
        self._max_record_bytes = max_record_bytes

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def list_organizations(
        self,
        *,
        page_size: int = 1000,
        max_pages: int = 1000,
        max_items: int = 100_000,
    ) -> tuple[ComplianceOrganization, ...]:
        self._require_scope(ComplianceScope.ORGANIZATION_DATA)
        raw = await self._list_opaque_pages(
            "/v1/compliance/organizations",
            page_size=page_size,
            max_pages=max_pages,
            max_items=max_items,
        )
        return tuple(self._organization(item) for item in raw)

    async def list_activities(
        self,
        *,
        activity_types: tuple[str, ...] = (),
        organization_ids: tuple[str, ...] = (),
        created_at_gte: datetime | None = None,
        created_at_lt: datetime | None = None,
        page_size: int = 5000,
        max_pages: int = 1000,
        max_items: int = 1_000_000,
    ) -> tuple[ComplianceActivity, ...]:
        self._require_scope(ComplianceScope.ACTIVITIES)
        if len(activity_types) > 100 or len(organization_ids) > 100:
            raise ValueError("compliance filter count exceeded")
        normalized_types = tuple(_activity_type(value) for value in activity_types)
        normalized_orgs = tuple(_identifier(value) for value in organization_ids)
        for value in (created_at_gte, created_at_lt):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError("activity time filters must include a UTC offset")
        if (
            created_at_gte is not None
            and created_at_lt is not None
            and created_at_gte >= created_at_lt
        ):
            raise ValueError("activity time filter range is invalid")
        params: list[tuple[str, str | int]] = []
        params.extend(("activity_types[]", value) for value in normalized_types)
        params.extend(("organization_ids[]", value) for value in normalized_orgs)
        if created_at_gte is not None:
            params.append(("created_at.gte", created_at_gte.isoformat()))
        if created_at_lt is not None:
            params.append(("created_at.lt", created_at_lt.isoformat()))
        raw = await self._list_id_pages(
            "/v1/compliance/activities",
            page_size=page_size,
            max_pages=max_pages,
            max_items=max_items,
            base_params=params,
        )
        return tuple(self._activity(item) for item in raw)

    async def _list_opaque_pages(
        self,
        path: str,
        *,
        page_size: int,
        max_pages: int,
        max_items: int,
    ) -> tuple[dict[str, Any], ...]:
        _pagination_limits(page_size, max_pages, max_items, max_page_size=1000)
        page_token: str | None = None
        tokens: set[str] = set()
        identifiers: set[str] = set()
        items: list[dict[str, Any]] = []
        for _ in range(max_pages):
            params: list[tuple[str, str | int]] = [("limit", page_size)]
            if page_token is not None:
                params.append(("page", page_token))
            value = await self._request_json("GET", path, params=params)
            if set(value) != {"data", "has_more", "next_page"}:
                raise ComplianceAdapterError("compliance API returned an invalid page")
            raw_items = value["data"]
            has_more = value["has_more"]
            next_page = value["next_page"]
            _append_page_items(
                raw_items,
                items=items,
                identifiers=identifiers,
                page_size=page_size,
                max_items=max_items,
                identifier_field="uuid",
            )
            if not isinstance(has_more, bool):
                raise ComplianceAdapterError("compliance API returned an invalid page")
            if not has_more:
                if next_page is not None:
                    raise ComplianceAdapterError(
                        "compliance API returned inconsistent pagination"
                    )
                return tuple(items)
            if (
                not isinstance(next_page, str)
                or not next_page
                or len(next_page) > 4096
                or next_page in tokens
                or not raw_items
            ):
                raise ComplianceAdapterError(
                    "compliance API returned an invalid page token"
                )
            tokens.add(next_page)
            page_token = next_page
        raise ComplianceAdapterError("compliance API exceeded its page limit")

    async def _list_id_pages(
        self,
        path: str,
        *,
        page_size: int,
        max_pages: int,
        max_items: int,
        base_params: list[tuple[str, str | int]],
    ) -> tuple[dict[str, Any], ...]:
        _pagination_limits(page_size, max_pages, max_items, max_page_size=5000)
        cursor: str | None = None
        cursors: set[str] = set()
        identifiers: set[str] = set()
        items: list[dict[str, Any]] = []
        for _ in range(max_pages):
            params = [*base_params, ("limit", page_size)]
            if cursor is not None:
                params.append(("after_id", cursor))
            value = await self._request_json("GET", path, params=params)
            if set(value) != {"data", "has_more", "first_id", "last_id"}:
                raise ComplianceAdapterError("compliance API returned an invalid page")
            raw_items = value["data"]
            has_more = value["has_more"]
            last_id = value["last_id"]
            _append_page_items(
                raw_items,
                items=items,
                identifiers=identifiers,
                page_size=page_size,
                max_items=max_items,
                identifier_field="id",
            )
            if not isinstance(has_more, bool):
                raise ComplianceAdapterError("compliance API returned an invalid page")
            if not has_more:
                return tuple(items)
            if (
                not isinstance(last_id, str)
                or not last_id
                or last_id in cursors
                or not raw_items
                or raw_items[-1].get("id") != last_id
            ):
                raise ComplianceAdapterError(
                    "compliance API returned an invalid activity cursor"
                )
            cursors.add(last_id)
            cursor = last_id
        raise ComplianceAdapterError("compliance API exceeded its page limit")

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str | int]],
    ) -> dict[str, Any]:
        try:
            async with self._client.stream(
                method,
                f"{self._base_url}{path}",
                headers={
                    "x-api-key": self._credential.secret.get_secret_value(),
                    "accept": "application/json",
                },
                params=params,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    raise ComplianceAdapterError(
                        "compliance API redirects are forbidden"
                    )
                if not 200 <= response.status_code < 300:
                    raise ComplianceAdapterError(
                        f"compliance API failed with HTTP {response.status_code}"
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise ComplianceAdapterError(
                            "compliance API response exceeded its byte limit"
                        )
        except ComplianceAdapterError:
            raise
        except httpx.HTTPError as exc:
            raise ComplianceAdapterError("compliance API request failed") from exc
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComplianceAdapterError("compliance API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ComplianceAdapterError(
                "compliance API returned a non-object response"
            )
        return value

    def _organization(self, value: dict[str, Any]) -> ComplianceOrganization:
        try:
            if set(value) != {"uuid", "name", "created_at"}:
                raise ValueError
            return ComplianceOrganization(
                uuid=value["uuid"],
                name=value["name"],
                created_at=_rfc3339(value["created_at"]),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ComplianceAdapterError(
                "compliance API returned an invalid organization"
            ) from exc

    def _activity(self, value: dict[str, Any]) -> ComplianceActivity:
        required = {
            "id",
            "created_at",
            "organization_id",
            "organization_uuid",
            "actor",
            "type",
        }
        try:
            if not required.issubset(value) or len(value) > 70:
                raise ValueError
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
            if len(encoded) > self._max_record_bytes:
                raise ValueError
            return ComplianceActivity(
                id=value["id"],
                created_at=_rfc3339(value["created_at"]),
                organization_id=value["organization_id"],
                organization_uuid=value["organization_uuid"],
                actor=value["actor"],
                type=value["type"],
                details={
                    key: item for key, item in value.items() if key not in required
                },
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ComplianceAdapterError(
                "compliance API returned an invalid activity"
            ) from exc

    def _require_scope(self, scope: ComplianceScope) -> None:
        if scope not in self._credential.scopes:
            raise ComplianceAdapterError(
                "compliance credential lacks the required declared scope"
            )


def _append_page_items(
    raw_items: object,
    *,
    items: list[dict[str, Any]],
    identifiers: set[str],
    page_size: int,
    max_items: int,
    identifier_field: str,
) -> None:
    if not isinstance(raw_items, list) or len(raw_items) > page_size:
        raise ComplianceAdapterError("compliance API returned an invalid item list")
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ComplianceAdapterError("compliance API returned an invalid item")
        identifier = raw.get(identifier_field)
        if not isinstance(identifier, str) or identifier in identifiers:
            raise ComplianceAdapterError(
                "compliance API returned a duplicate or invalid ID"
            )
        identifiers.add(identifier)
        items.append(raw)
        if len(items) > max_items:
            raise ComplianceAdapterError("compliance API exceeded its item limit")


def _pagination_limits(
    page_size: int,
    max_pages: int,
    max_items: int,
    *,
    max_page_size: int,
) -> None:
    if not 1 <= page_size <= max_page_size or not 1 <= max_pages <= 10_000:
        raise ValueError("compliance pagination limits are invalid")
    if not page_size <= max_items <= 5_000_000:
        raise ValueError("compliance max_items is invalid")


def _rfc3339(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid RFC3339 timestamp")
    parsed = TypeAdapter(datetime).validate_python(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed


def _identifier(value: str) -> str:
    try:
        return TypeAdapter(Identifier).validate_python(value)
    except ValidationError as exc:
        raise ValueError("compliance identifier is invalid") from exc


def _activity_type(value: str) -> str:
    try:
        return TypeAdapter(ActivityType).validate_python(value)
    except ValidationError as exc:
        raise ValueError("compliance activity type is invalid") from exc


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
