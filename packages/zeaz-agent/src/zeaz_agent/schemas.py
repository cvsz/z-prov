"""Strict, versioned persistence and transport schemas for the agent runtime."""

from __future__ import annotations

import base64
import binascii
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

SchemaVersion = Literal["1"]
Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"),
]
ToolName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"),
]
NonEmptyText = Annotated[str, StringConstraints(min_length=1, max_length=1_000_000)]
_CREDENTIAL_FIELD = re.compile(
    r"^(?:api[_-]?key|provider[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"authorization|credential|password|secret)$",
    re.IGNORECASE,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    """Immutable model that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class TurnStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionMode(StrEnum):
    NORMAL = "normal"
    PLAN = "plan"


class TokenBudget(StrictModel):
    max_context_tokens: int = Field(default=131_072, ge=128, le=10_000_000)
    max_output_tokens: int = Field(default=4096, ge=1, le=1_000_000)
    max_total_tokens: int = Field(default=1_000_000, ge=2, le=1_000_000_000)

    @model_validator(mode="after")
    def limits_are_coherent(self) -> TokenBudget:
        if self.max_output_tokens >= self.max_total_tokens:
            raise ValueError("max_output_tokens must be lower than max_total_tokens")
        return self


class TokenUsage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def plus(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class TextBlock(StrictModel):
    type: Literal["text"] = "text"
    text: NonEmptyText


class ImageSource(StrictModel):
    type: Literal["url", "base64"]
    media_type: Literal["image/jpeg", "image/png", "image/gif", "image/webp"]
    data: Annotated[str, StringConstraints(min_length=1, max_length=16_777_216)]

    @model_validator(mode="after")
    def validate_source(self) -> ImageSource:
        if self.type == "base64":
            try:
                base64.b64decode(self.data, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("image data must be valid base64") from exc
        else:
            parsed = urlparse(self.data)
            loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
                raise ValueError("image URL must use HTTPS unless it is loopback")
            if parsed.username or parsed.password or not parsed.netloc or parsed.fragment:
                raise ValueError("image URL must not contain credentials or a fragment")
        return self


class ImageBlock(StrictModel):
    type: Literal["image"] = "image"
    source: ImageSource
    alt_text: Annotated[str, StringConstraints(max_length=4096)] = ""


class ToolCall(StrictModel):
    schema_version: SchemaVersion = "1"
    id: Identifier
    name: ToolName
    arguments: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("arguments")
    @classmethod
    def arguments_exclude_credentials(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        def inspect(item: JsonValue, depth: int = 0) -> None:
            if depth > 32:
                raise ValueError("tool arguments exceed the nesting limit")
            if isinstance(item, dict):
                for key, nested in item.items():
                    if _CREDENTIAL_FIELD.fullmatch(key):
                        raise ValueError("provider credentials are forbidden in tool arguments")
                    inspect(nested, depth + 1)
            elif isinstance(item, list):
                for nested in item:
                    inspect(nested, depth + 1)

        inspect(value)
        return value


class ToolResult(StrictModel):
    schema_version: SchemaVersion = "1"
    tool_call_id: Identifier
    output: JsonValue
    is_error: bool = False


class ToolDefinition(StrictModel):
    name: ToolName
    description: Annotated[str, StringConstraints(max_length=16_384)] = ""
    parameters: dict[str, JsonValue] = Field(default_factory=lambda: {"type": "object"})
    mutating: bool = True


class ToolCallBlock(StrictModel):
    type: Literal["tool_call"] = "tool_call"
    call: ToolCall


class ToolResultBlock(StrictModel):
    type: Literal["tool_result"] = "tool_result"
    result: ToolResult


ContentBlock = Annotated[
    TextBlock | ImageBlock | ToolCallBlock | ToolResultBlock,
    Field(discriminator="type"),
]


class ModelOutput(StrictModel):
    blocks: tuple[TextBlock | ToolCallBlock, ...] = Field(min_length=1, max_length=1024)
    usage: TokenUsage = Field(default_factory=TokenUsage)


class Turn(StrictModel):
    schema_version: SchemaVersion = "1"
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    correlation_id: UUID = Field(default_factory=uuid4)
    sequence: int = Field(ge=0)
    role: Role
    status: TurnStatus = TurnStatus.COMPLETED
    content: tuple[ContentBlock, ...] = Field(min_length=1, max_length=1024)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def timestamp_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a UTC offset")
        return value

    @model_validator(mode="after")
    def role_matches_tool_blocks(self) -> Turn:
        has_call = any(isinstance(block, ToolCallBlock) for block in self.content)
        has_result = any(isinstance(block, ToolResultBlock) for block in self.content)
        if has_call and self.role is not Role.ASSISTANT:
            raise ValueError("tool calls are only valid on assistant turns")
        if has_result and self.role is not Role.TOOL:
            raise ValueError("tool results are only valid on tool turns")
        if self.role is Role.TOOL and not has_result:
            raise ValueError("tool turns must contain a tool result")
        return self


class Session(StrictModel):
    schema_version: SchemaVersion = "1"
    id: UUID = Field(default_factory=uuid4)
    revision: int = Field(default=0, ge=0)
    status: SessionStatus = SessionStatus.ACTIVE
    execution_mode: ExecutionMode = ExecutionMode.NORMAL
    token_budget: TokenBudget = Field(default_factory=TokenBudget)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    turns: tuple[Turn, ...] = Field(default_factory=tuple, max_length=100_000)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a UTC offset")
        return value

    @model_validator(mode="after")
    def validate_turn_history(self) -> Session:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.token_usage.total_tokens > self.token_budget.max_total_tokens:
            raise ValueError("token usage exceeds the session budget")
        sequences: list[int] = []
        turn_ids: set[UUID] = set()
        for turn in self.turns:
            if turn.session_id != self.id:
                raise ValueError("every turn must belong to this session")
            if turn.id in turn_ids:
                raise ValueError("turn IDs must be unique")
            turn_ids.add(turn.id)
            sequences.append(turn.sequence)
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("turn sequences must be strictly increasing")
        return self
