"""Deterministic bounded context projection for provider requests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from uuid import uuid5

from pydantic import Field

from zeaz_agent.schemas import (
    ImageBlock,
    StrictModel,
    TextBlock,
    ToolCallBlock,
    ToolDefinition,
    ToolResultBlock,
    Turn,
)


class ContextLimitExceeded(RuntimeError):
    pass


class CompactionResult(StrictModel):
    turns: tuple[Turn, ...]
    estimated_tokens: int = Field(ge=0)
    omitted_turns: int = Field(ge=0)


class ConservativeTokenCounter:
    """Count canonical UTF-8 bytes as a conservative tokenizer-independent unit."""

    def count(self, turns: Sequence[Turn], tools: Sequence[ToolDefinition]) -> int:
        payload = {
            "turns": [turn.model_dump(mode="json") for turn in turns],
            "tools": [tool.model_dump(mode="json") for tool in tools],
        }
        return len(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode()
        )


class ContextCompactor:
    def __init__(
        self,
        *,
        counter: ConservativeTokenCounter | None = None,
        min_recent_turns: int = 2,
        max_summary_chars: int = 8192,
    ) -> None:
        if not 1 <= min_recent_turns <= 100:
            raise ValueError("min_recent_turns must be between 1 and 100")
        if not 256 <= max_summary_chars <= 65_536:
            raise ValueError("max_summary_chars must be between 256 and 65536")
        self._counter = counter or ConservativeTokenCounter()
        self._min_recent_turns = min_recent_turns
        self._max_summary_chars = max_summary_chars

    def compact(
        self,
        turns: Sequence[Turn],
        tools: Sequence[ToolDefinition],
        *,
        token_limit: int,
    ) -> CompactionResult:
        if token_limit < 1:
            raise ContextLimitExceeded("no input-token budget remains")
        original = tuple(turns)
        estimate = self._counter.count(original, tools)
        if estimate <= token_limit:
            return CompactionResult(turns=original, estimated_tokens=estimate, omitted_turns=0)

        leading_system: list[Turn] = []
        for turn in original:
            if turn.role.value != "system":
                break
            leading_system.append(turn)
        removable = list(original[len(leading_system) :])
        minimum = min(self._min_recent_turns, len(removable))
        omitted: list[Turn] = []
        while len(removable) > minimum:
            omitted.append(removable.pop(0))
            while (
                removable
                and removable[0].role.value == "tool"
                and len(removable) > minimum
            ):
                omitted.append(removable.pop(0))
            projected = self._projection(leading_system, omitted, removable)
            estimate = self._counter.count(projected, tools)
            if estimate <= token_limit:
                return CompactionResult(
                    turns=projected,
                    estimated_tokens=estimate,
                    omitted_turns=len(omitted),
                )
        raise ContextLimitExceeded(
            "system instructions, tools, summary, and recent turns exceed the context budget"
        )

    def _projection(
        self,
        leading_system: Sequence[Turn],
        omitted: Sequence[Turn],
        recent: Sequence[Turn],
    ) -> tuple[Turn, ...]:
        summary = self._summary_turn(omitted)
        return (*leading_system, summary, *recent)

    def _summary_turn(self, omitted: Sequence[Turn]) -> Turn:
        digest = hashlib.sha256(
            b"\n".join(turn.model_dump_json().encode() for turn in omitted)
        ).hexdigest()
        lines = [
            f"[Deterministic context summary: {len(omitted)} earlier turns; sha256={digest}]"
        ]
        remaining = self._max_summary_chars - len(lines[0]) - 1
        for turn in omitted:
            if remaining <= 0:
                break
            excerpt = _turn_excerpt(turn)
            line = f"{turn.role.value}: {excerpt}"
            line = line[:remaining]
            lines.append(line)
            remaining -= len(line) + 1
        namespace = omitted[0].session_id
        return Turn(
            id=uuid5(namespace, f"context-summary:{digest}"),
            session_id=namespace,
            correlation_id=uuid5(namespace, f"context-correlation:{digest}"),
            sequence=omitted[-1].sequence,
            role="system",
            content=(TextBlock(text="\n".join(lines)),),
            created_at=omitted[-1].created_at,
        )


def _turn_excerpt(turn: Turn) -> str:
    parts: list[str] = []
    for block in turn.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ImageBlock):
            parts.append(f"[image {block.source.media_type}]")
        elif isinstance(block, ToolCallBlock):
            parts.append(f"[tool call {block.call.name} id={block.call.id}]")
        elif isinstance(block, ToolResultBlock):
            state = "error" if block.result.is_error else "completed"
            parts.append(f"[tool result id={block.result.tool_call_id} {state}]")
    return " ".join(parts)
