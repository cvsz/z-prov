"""Provider-neutral, non-executing agent state machine."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from zeaz_agent.audit import AuditSink
from zeaz_agent.context import ContextCompactor, ContextLimitExceeded
from zeaz_agent.schemas import (
    ContentBlock,
    ModelOutput,
    Session,
    SessionStatus,
    TokenUsage,
    ToolCall,
    ToolCallBlock,
    ToolDefinition,
    ToolResult,
    ToolResultBlock,
    Turn,
    utc_now,
)


class ModelClient(Protocol):
    async def respond(
        self,
        turns: Sequence[Turn],
        tools: Sequence[ToolDefinition],
        *,
        model: str,
        max_output_tokens: int,
        correlation_id: UUID,
    ) -> ModelOutput: ...


class RunStatus(StrEnum):
    COMPLETED = "completed"
    REQUIRES_ACTION = "requires_action"


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RunStatus
    session: Session
    pending_tool_calls: tuple[ToolCall, ...] = ()


class TokenBudgetExceeded(RuntimeError):
    pass


class AgentLoop:
    """Advance immutable sessions; tool execution remains outside this class."""

    def __init__(
        self,
        client: ModelClient,
        *,
        model: str = "zeaz-auto",
        max_output_tokens: int = 4096,
        max_turns: int = 1000,
        audit: AuditSink | None = None,
        compactor: ContextCompactor | None = None,
        uuid_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not model.startswith("zeaz-"):
            raise ValueError("agent model must use a stable zeaz-* alias")
        if not 1 <= max_output_tokens <= 131_072:
            raise ValueError("max_output_tokens is outside the supported range")
        if not 2 <= max_turns <= 100_000:
            raise ValueError("max_turns is outside the supported range")
        self._client = client
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._max_turns = max_turns
        self._audit = audit
        self._compactor = compactor or ContextCompactor()
        self._uuid_factory = uuid_factory
        self._clock = clock

    async def start(
        self,
        session: Session,
        content: Sequence[ContentBlock],
        *,
        tools: Sequence[ToolDefinition] = (),
        correlation_id: UUID | None = None,
    ) -> RunResult:
        if session.status is not SessionStatus.ACTIVE:
            raise ValueError("only active sessions can be advanced")
        if not content:
            raise ValueError("user content cannot be empty")
        if any(isinstance(block, (ToolCallBlock, ToolResultBlock)) for block in content):
            raise ValueError("user input cannot inject tool calls or tool results")
        self._require_capacity(session, 2)
        turn = Turn(
            session_id=session.id,
            id=self._uuid_factory(),
            correlation_id=correlation_id or self._uuid_factory(),
            sequence=_next_sequence(session),
            role="user",
            content=tuple(content),
            created_at=self._clock(),
        )
        updated = _append(session, turn, self._max_turns, clock=self._clock)
        self._emit(
            updated,
            turn.correlation_id,
            "turn.user_appended",
            subject_id=str(turn.id),
            details={"sequence": turn.sequence, "block_count": len(turn.content)},
        )
        return await self._infer(updated, tools)

    async def resume(
        self,
        session: Session,
        results: Sequence[ToolResult],
        *,
        tools: Sequence[ToolDefinition] = (),
        correlation_id: UUID | None = None,
    ) -> RunResult:
        expected = _pending_calls(session)
        if not expected:
            raise ValueError("session has no pending tool calls")
        expected_ids = {call.id for call in expected}
        result_ids = [result.tool_call_id for result in results]
        if len(result_ids) != len(set(result_ids)) or set(result_ids) != expected_ids:
            raise ValueError("tool results must match every pending call exactly once")
        self._require_capacity(session, 2)
        turn = Turn(
            session_id=session.id,
            id=self._uuid_factory(),
            correlation_id=correlation_id or self._uuid_factory(),
            sequence=_next_sequence(session),
            role="tool",
            content=tuple(ToolResultBlock(result=result) for result in results),
            created_at=self._clock(),
        )
        updated = _append(session, turn, self._max_turns, clock=self._clock)
        self._emit(
            updated,
            turn.correlation_id,
            "turn.tool_results_appended",
            subject_id=str(turn.id),
            details={"sequence": turn.sequence, "result_count": len(results)},
        )
        return await self._infer(updated, tools)

    async def _infer(
        self,
        session: Session,
        tools: Sequence[ToolDefinition],
    ) -> RunResult:
        correlation_id = self._uuid_factory()
        remaining = session.token_budget.max_total_tokens - session.token_usage.total_tokens
        if remaining <= 1:
            raise TokenBudgetExceeded("session token budget is exhausted")
        input_limit = min(session.token_budget.max_context_tokens, remaining - 1)
        try:
            context = self._compactor.compact(
                session.turns,
                tools,
                token_limit=input_limit,
            )
        except ContextLimitExceeded as exc:
            raise TokenBudgetExceeded("session context cannot fit the remaining budget") from exc
        output_limit = min(
            self._max_output_tokens,
            session.token_budget.max_output_tokens,
            remaining - context.estimated_tokens,
        )
        if output_limit < 1:
            raise TokenBudgetExceeded("no output-token budget remains")
        if context.omitted_turns:
            self._emit(
                session,
                correlation_id,
                "context.compacted",
                details={
                    "omitted_turns": context.omitted_turns,
                    "projected_turns": len(context.turns),
                    "estimated_input_tokens": context.estimated_tokens,
                },
            )
        self._emit(
            session,
            correlation_id,
            "model.requested",
            details={
                "model": self._model,
                "turn_count": len(context.turns),
                "max_output_tokens": output_limit,
            },
        )
        try:
            output = await self._client.respond(
                context.turns,
                tools,
                model=self._model,
                max_output_tokens=output_limit,
                correlation_id=correlation_id,
            )
        except Exception:
            self._emit(session, correlation_id, "model.failed")
            raise
        try:
            _validate_usage(output.usage, session, output_limit)
        except TokenBudgetExceeded:
            self._emit(session, correlation_id, "model.failed")
            raise
        blocks = output.blocks
        assistant = Turn(
            id=self._uuid_factory(),
            session_id=session.id,
            correlation_id=correlation_id,
            sequence=_next_sequence(session),
            role="assistant",
            content=blocks,
            created_at=self._clock(),
        )
        updated = _append(
            session,
            assistant,
            self._max_turns,
            added_usage=output.usage,
            clock=self._clock,
        )
        calls = tuple(block.call for block in blocks if isinstance(block, ToolCallBlock))
        self._emit(
            updated,
            correlation_id,
            "model.completed",
            subject_id=str(assistant.id),
            details={
                "block_count": len(blocks),
                "tool_call_count": len(calls),
                "input_tokens": output.usage.input_tokens,
                "output_tokens": output.usage.output_tokens,
            },
        )
        return RunResult(
            status=RunStatus.REQUIRES_ACTION if calls else RunStatus.COMPLETED,
            session=updated,
            pending_tool_calls=calls,
        )

    def _require_capacity(self, session: Session, additional_turns: int) -> None:
        if len(session.turns) + additional_turns > self._max_turns:
            raise RuntimeError("session turn limit exceeded")

    def _emit(
        self,
        session: Session,
        correlation_id: UUID,
        event_type: str,
        *,
        subject_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        if self._audit is not None:
            self._audit.append(
                session_id=session.id,
                correlation_id=correlation_id,
                event_type=event_type,
                actor="agent",
                subject_id=subject_id,
                details=details,
            )


def _next_sequence(session: Session) -> int:
    return session.turns[-1].sequence + 1 if session.turns else 0


def _append(
    session: Session,
    turn: Turn,
    maximum: int,
    *,
    added_usage: TokenUsage | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> Session:
    if len(session.turns) >= maximum:
        raise RuntimeError("session turn limit exceeded")
    return Session(
        id=session.id,
        revision=session.revision + 1,
        status=session.status,
        execution_mode=session.execution_mode,
        token_budget=session.token_budget,
        token_usage=session.token_usage.plus(added_usage or TokenUsage()),
        turns=(*session.turns, turn),
        created_at=session.created_at,
        updated_at=clock(),
    )


def _pending_calls(session: Session) -> tuple[ToolCall, ...]:
    if not session.turns or session.turns[-1].role.value != "assistant":
        return ()
    return tuple(
        block.call for block in session.turns[-1].content if isinstance(block, ToolCallBlock)
    )


def _validate_usage(usage: TokenUsage, session: Session, output_limit: int) -> None:
    remaining = session.token_budget.max_total_tokens - session.token_usage.total_tokens
    if usage.input_tokens > session.token_budget.max_context_tokens:
        raise TokenBudgetExceeded("provider reported input usage above the context limit")
    if usage.output_tokens > output_limit:
        raise TokenBudgetExceeded("provider reported output usage above the requested limit")
    if usage.total_tokens > remaining:
        raise TokenBudgetExceeded("provider reported usage above the remaining session budget")
