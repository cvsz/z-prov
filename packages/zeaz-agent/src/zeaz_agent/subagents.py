"""Bounded subagent scheduling with hierarchical cancellation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Protocol
from uuid import UUID, uuid4

from pydantic import Field, StringConstraints

from zeaz_agent.audit import AuditSink
from zeaz_agent.schemas import StrictModel, TokenUsage

TaskText = Annotated[str, StringConstraints(min_length=1, max_length=100_000)]
OutputText = Annotated[str, StringConstraints(max_length=1_000_000)]


class SubagentStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class SubagentLimits(StrictModel):
    max_concurrent: int = Field(default=4, ge=1, le=128)
    max_total: int = Field(default=32, ge=1, le=10_000)
    max_depth: int = Field(default=3, ge=1, le=32)
    max_tokens_per_agent: int = Field(default=32_768, ge=1, le=10_000_000)
    max_total_tokens: int = Field(default=262_144, ge=1, le=1_000_000_000)
    timeout_seconds: float = Field(default=300, gt=0, le=86_400)


class SubagentRequest(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    correlation_id: UUID = Field(default_factory=uuid4)
    parent_id: UUID | None = None
    depth: int = Field(ge=1)
    task: TaskText
    token_budget: int = Field(ge=1)


class SubagentCompletion(StrictModel):
    output: OutputText = ""
    usage: TokenUsage = Field(default_factory=TokenUsage)


class SubagentResult(StrictModel):
    request: SubagentRequest
    status: SubagentStatus
    output: OutputText = ""
    usage: TokenUsage = Field(default_factory=TokenUsage)
    error_code: LiteralErrorCode | None = None


LiteralErrorCode = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$"),
]


class CancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError

    def _cancel(self) -> None:
        self._event.set()


class SubagentWorker(Protocol):
    async def run(
        self,
        request: SubagentRequest,
        cancellation: CancellationToken,
    ) -> SubagentCompletion: ...


class SubagentLimitError(RuntimeError):
    pass


@dataclass
class _State:
    request: SubagentRequest
    cancellation: CancellationToken
    task: asyncio.Task[SubagentResult] | None
    reservation: int
    running: bool = False
    result: SubagentResult | None = None


class SubagentHandle:
    def __init__(self, manager: SubagentManager, request: SubagentRequest) -> None:
        self._manager = manager
        self.request = request

    async def wait(self) -> SubagentResult:
        return await self._manager.wait(self.request.id)

    async def cancel(self) -> None:
        await self._manager.cancel(self.request.id)


class SubagentManager:
    def __init__(
        self,
        limits: SubagentLimits | None = None,
        *,
        audit: AuditSink | None = None,
    ) -> None:
        self._limits = limits or SubagentLimits()
        self._audit = audit
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(self._limits.max_concurrent)
        self._states: dict[UUID, _State] = {}
        self._total_started = 0
        self._consumed_tokens = 0
        self._active_reservations = 0

    async def spawn(
        self,
        *,
        session_id: UUID,
        task: str,
        token_budget: int,
        worker: SubagentWorker,
        parent_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> SubagentHandle:
        async with self._lock:
            if self._total_started >= self._limits.max_total:
                raise SubagentLimitError("subagent lifetime count limit exceeded")
            if not 1 <= token_budget <= self._limits.max_tokens_per_agent:
                raise SubagentLimitError("subagent token budget exceeds the per-agent limit")
            depth = 1
            if parent_id is not None:
                parent = self._states.get(parent_id)
                if parent is None or not parent.running or parent.result is not None:
                    raise SubagentLimitError("subagent parent is not actively running")
                if parent.request.session_id != session_id:
                    raise SubagentLimitError("subagent parent belongs to another session")
                depth = parent.request.depth + 1
            if depth > self._limits.max_depth:
                raise SubagentLimitError("subagent depth limit exceeded")
            if (
                self._consumed_tokens + self._active_reservations + token_budget
                > self._limits.max_total_tokens
            ):
                raise SubagentLimitError("subagent aggregate token budget exceeded")
            request = SubagentRequest(
                session_id=session_id,
                correlation_id=correlation_id or uuid4(),
                parent_id=parent_id,
                depth=depth,
                task=task,
                token_budget=token_budget,
            )
            self._emit(
                request,
                "subagent.spawned",
                {"depth": depth, "token_budget": token_budget},
            )
            cancellation = CancellationToken()
            state = _State(
                request=request,
                cancellation=cancellation,
                task=None,
                reservation=token_budget,
            )
            self._states[request.id] = state
            self._total_started += 1
            self._active_reservations += token_budget
            state.task = asyncio.create_task(self._execute(state, worker))
        await asyncio.sleep(0)
        return SubagentHandle(self, request)

    async def wait(self, subagent_id: UUID) -> SubagentResult:
        async with self._lock:
            state = self._states.get(subagent_id)
            if state is None:
                raise KeyError("unknown subagent")
            task = state.task
            if task is None:
                raise RuntimeError("subagent task was not initialized")
        return await asyncio.shield(task)

    async def cancel(self, subagent_id: UUID) -> None:
        async with self._lock:
            if subagent_id not in self._states:
                raise KeyError("unknown subagent")
            targets = self._descendants_including(subagent_id)
            for target in targets:
                state = self._states[target]
                if state.result is None:
                    state.cancellation._cancel()
                    if state.task is not None:
                        state.task.cancel()

    async def usage(self) -> tuple[int, int, int]:
        """Return lifetime count, charged tokens, and active reservations."""
        async with self._lock:
            return self._total_started, self._consumed_tokens, self._active_reservations

    async def _execute(
        self,
        state: _State,
        worker: SubagentWorker,
    ) -> SubagentResult:
        request = state.request
        result: SubagentResult
        try:
            async with self._semaphore:
                async with self._lock:
                    state.running = True
                state.cancellation.raise_if_cancelled()
                self._emit(request, "subagent.started")
                completion = await asyncio.wait_for(
                    worker.run(request, state.cancellation),
                    timeout=self._limits.timeout_seconds,
                )
                if completion.usage.total_tokens > request.token_budget:
                    result = SubagentResult(
                        request=request,
                        status=SubagentStatus.FAILED,
                        error_code="token_budget_exceeded",
                    )
                else:
                    result = SubagentResult(
                        request=request,
                        status=SubagentStatus.COMPLETED,
                        output=completion.output,
                        usage=completion.usage,
                    )
        except TimeoutError:
            state.cancellation._cancel()
            result = SubagentResult(
                request=request,
                status=SubagentStatus.TIMED_OUT,
                error_code="timeout",
            )
        except asyncio.CancelledError:
            state.cancellation._cancel()
            result = SubagentResult(
                request=request,
                status=SubagentStatus.CANCELLED,
                error_code="cancelled",
            )
        except Exception:
            result = SubagentResult(
                request=request,
                status=SubagentStatus.FAILED,
                error_code="worker_failed",
            )
        await self._finalize(state, result)
        return result

    async def _finalize(self, state: _State, result: SubagentResult) -> None:
        async with self._lock:
            state.running = False
            state.result = result
            self._active_reservations -= state.reservation
            charged = (
                result.usage.total_tokens
                if result.status is SubagentStatus.COMPLETED
                else state.reservation
            )
            self._consumed_tokens += charged
        self._emit(
            state.request,
            f"subagent.{result.status.value}",
            {"charged_tokens": charged, "reported_tokens": result.usage.total_tokens},
        )

    def _descendants_including(self, root: UUID) -> set[UUID]:
        targets = {root}
        changed = True
        while changed:
            changed = False
            for identifier, state in self._states.items():
                if state.request.parent_id in targets and identifier not in targets:
                    targets.add(identifier)
                    changed = True
        return targets

    def _emit(
        self,
        request: SubagentRequest,
        event_type: str,
        details: dict | None = None,
    ) -> None:
        if self._audit is not None:
            self._audit.append(
                session_id=request.session_id,
                correlation_id=request.correlation_id,
                event_type=event_type,
                actor="agent",
                subject_id=str(request.id),
                details=details,
            )
