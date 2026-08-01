"""Managed-agent memory stores, outcome events, and dream jobs."""

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
    StringConstraints,
    ValidationError,
    field_validator,
)

from zeaz_enterprise.managed_agents import (
    MANAGED_AGENTS_BETA,
    ManagedAgentCredential,
)

MEMORY_BETA = "agent-memory-2026-07-22"
DREAMING_BETA = "dreaming-2026-04-21"
Identifier = Annotated[
    str, StringConstraints(pattern=r"^[a-z]+_[A-Za-z0-9_-]{1,248}$")
]
Name = Annotated[str, StringConstraints(min_length=1, max_length=255)]
Metadata = Annotated[
    dict[
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")],
        Annotated[str, StringConstraints(max_length=512)],
    ],
    Field(max_length=16),
]


class AgentMemoryAdapterError(RuntimeError):
    """A sanitized memory, outcome, or dream API failure."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryStoreSpec(StrictModel):
    name: Name
    description: Annotated[str, StringConstraints(max_length=1024)] = ""
    metadata: Metadata = {}


class MemoryStore(StrictModel):
    id: Identifier
    type: Literal["memory_store"]
    name: Name
    description: Annotated[str, StringConstraints(max_length=1024)] = ""
    metadata: Metadata = {}
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class TextRubric(StrictModel):
    type: Literal["text"] = "text"
    content: Annotated[str, StringConstraints(min_length=1, max_length=262_144)]


class FileRubric(StrictModel):
    type: Literal["file"] = "file"
    file_id: Identifier


class OutcomeSpec(StrictModel):
    type: Literal["user.define_outcome"] = "user.define_outcome"
    description: Annotated[str, StringConstraints(min_length=1, max_length=65_536)]
    rubric: TextRubric | FileRubric = Field(discriminator="type")
    max_iterations: Annotated[int, Field(ge=1, le=20)] = 3


class Outcome(StrictModel):
    id: Identifier
    type: Literal["user.define_outcome"]
    outcome_id: Identifier
    description: Annotated[str, StringConstraints(min_length=1, max_length=65_536)]
    rubric: TextRubric | FileRubric = Field(discriminator="type")
    max_iterations: Annotated[int, Field(ge=1, le=20)] | None
    processed_at: datetime


class DreamSpec(StrictModel):
    memory_store_id: Identifier
    session_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=100)
    model: Annotated[str, StringConstraints(pattern=r"^claude-[a-z0-9-]{1,120}$")]
    instructions: Annotated[str, StringConstraints(max_length=65_536)] = ""

    @field_validator("session_ids")
    @classmethod
    def _ids_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("dream session IDs must be unique")
        return value


class Dream(StrictModel):
    id: Identifier
    type: Literal["dream"]
    status: Literal["pending", "running", "completed", "failed", "canceled"]
    memory_store_id: Identifier
    session_ids: tuple[Identifier, ...]
    output_memory_store_ids: tuple[Identifier, ...]
    model: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    instructions: Annotated[str, StringConstraints(max_length=65_536)]
    session_id: Identifier | None
    created_at: datetime
    ended_at: datetime | None
    archived_at: datetime | None
    usage: dict[
        Literal[
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ],
        Annotated[int, Field(ge=0)],
    ] = Field(max_length=4)


class AnthropicAgentMemoryAdapter:
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

    async def create_memory_store(
        self, spec: MemoryStoreSpec, *, idempotency_key: str
    ) -> MemoryStore:
        return _model(
            MemoryStore,
            await self._request(
                "POST",
                "/v1/memory_stores",
                beta=MEMORY_BETA,
                body=spec.model_dump(mode="json"),
                idempotency_key=_key(idempotency_key),
            ),
            "memory store",
        )

    async def get_memory_store(self, memory_store_id: str) -> MemoryStore:
        return _model(
            MemoryStore,
            await self._request(
                "GET",
                f"/v1/memory_stores/{_id(memory_store_id, 'memstore_')}",
                beta=MEMORY_BETA,
            ),
            "memory store",
        )

    async def list_memory_stores(
        self, *, page_size: int = 100, max_pages: int = 1000, max_items: int = 100_000
    ) -> tuple[MemoryStore, ...]:
        raw = await self._pages(
            "/v1/memory_stores", MEMORY_BETA, page_size, max_pages, max_items
        )
        return tuple(_model(MemoryStore, item, "memory store") for item in raw)

    async def archive_memory_store(
        self, memory_store_id: str, *, idempotency_key: str
    ) -> MemoryStore:
        return _model(
            MemoryStore,
            await self._request(
                "POST",
                f"/v1/memory_stores/{_id(memory_store_id, 'memstore_')}/archive",
                beta=MEMORY_BETA,
                body={},
                idempotency_key=_key(idempotency_key),
            ),
            "memory store",
        )

    async def define_outcome(
        self, session_id: str, spec: OutcomeSpec, *, idempotency_key: str
    ) -> Outcome:
        value = await self._request(
            "POST",
            f"/v1/sessions/{_id(session_id, 'session_')}/events",
            beta=MANAGED_AGENTS_BETA,
            body={"events": [spec.model_dump(mode="json")]},
            idempotency_key=_key(idempotency_key),
        )
        if set(value) != {"data"} or not isinstance(value["data"], list) or len(value["data"]) != 1:
            raise AgentMemoryAdapterError("outcome API returned an invalid response")
        return _model(Outcome, value["data"][0], "outcome")

    async def create_dream(
        self, spec: DreamSpec, *, idempotency_key: str
    ) -> Dream:
        body = {
            "inputs": [
                {"type": "memory_store", "memory_store_id": _id(spec.memory_store_id, "memstore_")},
                {"type": "sessions", "session_ids": [_id(item, "session_") for item in spec.session_ids]},
            ],
            "model": spec.model,
            "instructions": spec.instructions,
        }
        return _dream(
            await self._request(
                "POST",
                "/v1/dreams",
                beta=f"{MANAGED_AGENTS_BETA},{DREAMING_BETA}",
                body=body,
                idempotency_key=_key(idempotency_key),
            )
        )

    async def get_dream(self, dream_id: str) -> Dream:
        return _dream(
            await self._request(
                "GET",
                f"/v1/dreams/{_id(dream_id, 'drm_')}",
                beta=f"{MANAGED_AGENTS_BETA},{DREAMING_BETA}",
            )
        )

    async def list_dreams(
        self, *, page_size: int = 100, max_pages: int = 1000, max_items: int = 100_000
    ) -> tuple[Dream, ...]:
        raw = await self._pages(
            "/v1/dreams",
            f"{MANAGED_AGENTS_BETA},{DREAMING_BETA}",
            page_size,
            max_pages,
            max_items,
        )
        return tuple(_dream(item) for item in raw)

    async def dream_action(
        self,
        dream_id: str,
        action: Literal["cancel", "archive"],
        *,
        idempotency_key: str,
    ) -> Dream:
        return _dream(
            await self._request(
                "POST",
                f"/v1/dreams/{_id(dream_id, 'drm_')}/{action}",
                beta=f"{MANAGED_AGENTS_BETA},{DREAMING_BETA}",
                body={},
                idempotency_key=_key(idempotency_key),
            )
        )

    async def _pages(
        self,
        path: str,
        beta: str,
        page_size: int,
        max_pages: int,
        max_items: int,
    ) -> tuple[dict[str, Any], ...]:
        if not 1 <= page_size <= 100 or not 1 <= max_pages <= 10_000:
            raise ValueError("pagination limits are invalid")
        if not page_size <= max_items <= 1_000_000:
            raise ValueError("max_items is invalid")
        page: str | None = None
        tokens: set[str] = set()
        ids: set[str] = set()
        items: list[dict[str, Any]] = []
        for _ in range(max_pages):
            params: list[tuple[str, str | int]] = [("limit", page_size)]
            if page is not None:
                params.append(("page", page))
            value = await self._request("GET", path, beta=beta, params=params)
            if set(value) != {"data", "next_page"}:
                raise AgentMemoryAdapterError("API returned an invalid page")
            data, next_page = value["data"], value["next_page"]
            if not isinstance(data, list) or len(data) > page_size:
                raise AgentMemoryAdapterError("API returned an invalid page")
            for item in data:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise AgentMemoryAdapterError("API returned an invalid item")
                if item["id"] in ids:
                    raise AgentMemoryAdapterError("API returned a duplicate ID")
                ids.add(item["id"])
                items.append(item)
                if len(items) > max_items:
                    raise AgentMemoryAdapterError("API exceeded its item limit")
            if next_page is None:
                return tuple(items)
            if (
                not isinstance(next_page, str)
                or not next_page
                or len(next_page) > 4096
                or next_page in tokens
                or not data
            ):
                raise AgentMemoryAdapterError("API returned an invalid page token")
            tokens.add(next_page)
            page = next_page
        raise AgentMemoryAdapterError("API exceeded its page limit")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        beta: str,
        params: list[tuple[str, str | int]] | None = None,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "x-api-key": self._credential.secret.get_secret_value(),
            "anthropic-version": "2023-06-01",
            "anthropic-beta": beta,
            "accept": "application/json",
        }
        if idempotency_key:
            headers["idempotency-key"] = idempotency_key
        try:
            async with self._client.stream(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                params=params,
                json=body,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    raise AgentMemoryAdapterError("API redirects are forbidden")
                if not 200 <= response.status_code < 300:
                    raise AgentMemoryAdapterError(f"API failed with HTTP {response.status_code}")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > self._max_response_bytes:
                        raise AgentMemoryAdapterError("API response exceeded its byte limit")
        except AgentMemoryAdapterError:
            raise
        except httpx.HTTPError as exc:
            raise AgentMemoryAdapterError("API request failed") from exc
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentMemoryAdapterError("API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise AgentMemoryAdapterError("API returned a non-object response")
        return value


def _dream(value: dict[str, Any]) -> Dream:
    try:
        inputs = value["inputs"]
        outputs = value["outputs"]
        memory = next(item for item in inputs if item.get("type") == "memory_store")
        sessions = next(item for item in inputs if item.get("type") == "sessions")
        output_ids = tuple(
            item["memory_store_id"]
            for item in outputs
            if item.get("type") == "memory_store"
        )
        selected = {
            key: value.get(key)
            for key in (
                "id", "type", "status", "instructions", "session_id",
                "created_at", "ended_at", "archived_at", "usage",
            )
        }
        selected.update(
            memory_store_id=memory["memory_store_id"],
            session_ids=tuple(sessions["session_ids"]),
            output_memory_store_ids=output_ids,
            model=value["model"]["id"],
        )
        return Dream.model_validate(selected)
    except (KeyError, StopIteration, TypeError, ValidationError) as exc:
        raise AgentMemoryAdapterError("API returned an invalid dream") from exc


def _model(model: type[BaseModel], value: object, label: str) -> Any:
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise AgentMemoryAdapterError(f"API returned an invalid {label}") from exc


def _id(value: str, prefix: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or len(value) > 255
        or not value[len(prefix):]
        or not all(char.isalnum() or char in "_-" for char in value[len(prefix):])
    ):
        raise ValueError("identifier is invalid")
    return value


def _key(value: str) -> str:
    if not 1 <= len(value) <= 255 or any(
        not (char.isalnum() or char in "._:-") for char in value
    ):
        raise ValueError("idempotency key is invalid")
    return value


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
