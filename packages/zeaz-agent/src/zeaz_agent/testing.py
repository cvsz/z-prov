"""Deterministic fixtures for agent-runtime contract tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, StringConstraints, model_validator

from zeaz_agent.schemas import (
    ModelOutput,
    StrictModel,
    ToolDefinition,
    Turn,
)

FixtureErrorCode = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$"),
]


class ExpectedModelRequest(StrictModel):
    model: str | None = None
    roles: tuple[str, ...] | None = None
    tool_names: tuple[str, ...] | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)
    context_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class ScriptedModelStep(StrictModel):
    output: ModelOutput | None = None
    error_code: FixtureErrorCode | None = None
    expected: ExpectedModelRequest = Field(default_factory=ExpectedModelRequest)

    @model_validator(mode="after")
    def exactly_one_outcome(self) -> ScriptedModelStep:
        if (self.output is None) == (self.error_code is None):
            raise ValueError("scripted step requires exactly one output or error")
        return self


class RecordedModelRequest(StrictModel):
    turns: tuple[Turn, ...]
    tools: tuple[ToolDefinition, ...]
    model: str
    max_output_tokens: int
    correlation_id: UUID
    context_sha256: str


class ScriptedProviderError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(f"scripted provider error: {code}")
        self.code = code


class DeterministicModelClient:
    def __init__(self, steps: Sequence[ScriptedModelStep]) -> None:
        if not steps:
            raise ValueError("script must contain at least one step")
        self._steps = tuple(steps)
        self._index = 0
        self._requests: list[RecordedModelRequest] = []
        self._lock = asyncio.Lock()

    @property
    def requests(self) -> tuple[RecordedModelRequest, ...]:
        return tuple(self._requests)

    async def respond(
        self,
        turns: Sequence[Turn],
        tools: Sequence[ToolDefinition],
        *,
        model: str,
        max_output_tokens: int,
        correlation_id: UUID,
    ) -> ModelOutput:
        async with self._lock:
            if self._index >= len(self._steps):
                raise AssertionError("deterministic provider script is exhausted")
            digest = context_digest(turns, tools)
            request = RecordedModelRequest(
                turns=tuple(turns),
                tools=tuple(tools),
                model=model,
                max_output_tokens=max_output_tokens,
                correlation_id=correlation_id,
                context_sha256=digest,
            )
            step = self._steps[self._index]
            self._index += 1
            self._requests.append(request)
            _assert_expected(step.expected, request)
            if step.error_code is not None:
                raise ScriptedProviderError(step.error_code)
            if step.output is None:
                raise AssertionError("validated scripted step has no output")
            return step.output

    def assert_exhausted(self) -> None:
        if self._index != len(self._steps):
            raise AssertionError(
                f"{len(self._steps) - self._index} scripted provider steps remain"
            )


class DeterministicUUIDFactory:
    def __init__(self, seed: str) -> None:
        if not seed:
            raise ValueError("deterministic UUID seed cannot be empty")
        self._namespace = uuid5(NAMESPACE_URL, seed)
        self._counter = 0

    def __call__(self) -> UUID:
        value = uuid5(self._namespace, str(self._counter))
        self._counter += 1
        return value


class FixedClock:
    def __init__(self, value: datetime | None = None) -> None:
        self._value = value or datetime(2100, 1, 1, tzinfo=UTC)
        if self._value.tzinfo is None or self._value.utcoffset() is None:
            raise ValueError("fixed clock requires a timezone-aware datetime")

    def __call__(self) -> datetime:
        return self._value


def context_digest(
    turns: Sequence[Turn],
    tools: Sequence[ToolDefinition],
) -> str:
    canonical = json.dumps(
        {
            "turns": [turn.model_dump(mode="json") for turn in turns],
            "tools": [tool.model_dump(mode="json") for tool in tools],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _assert_expected(
    expected: ExpectedModelRequest,
    actual: RecordedModelRequest,
) -> None:
    comparisons = {
        "model": actual.model,
        "roles": tuple(turn.role.value for turn in actual.turns),
        "tool_names": tuple(tool.name for tool in actual.tools),
        "max_output_tokens": actual.max_output_tokens,
        "context_sha256": actual.context_sha256,
    }
    for field, value in comparisons.items():
        wanted = getattr(expected, field)
        if wanted is not None and wanted != value:
            raise AssertionError(
                f"deterministic provider expected {field}={wanted!r}, got {value!r}"
            )
