"""Bounded pre/post-tool hooks over immutable input snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, JsonValue, StringConstraints

from zeaz_agent.schemas import StrictModel, ToolCall

HookName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"),
]


class HookPhase(StrEnum):
    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"


class HookFailurePolicy(StrEnum):
    FAIL_CLOSED = "fail_closed"
    FAIL_OPEN = "fail_open"


class HookVerdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class HookContext(StrictModel):
    """Immutable bytes preserve the exact view presented to every hook."""

    schema_version: Literal["1"] = "1"
    phase: HookPhase
    session_id: UUID
    correlation_id: UUID
    tool_call_id: str
    tool_name: str
    input_json: bytes = Field(min_length=2, max_length=16_777_216)
    input_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    output_json: bytes | None = Field(default=None, max_length=16_777_216)
    output_sha256: Annotated[
        str | None,
        StringConstraints(pattern=r"^[0-9a-f]{64}$"),
    ] = None

    def input_value(self) -> dict[str, JsonValue]:
        """Return a new mutable decoding; the stored snapshot remains unchanged."""
        value = json.loads(self.input_json)
        if not isinstance(value, dict):
            raise RuntimeError("invalid internal hook input snapshot")
        return value

    def output_value(self) -> JsonValue | None:
        """Return a new decoding of the optional output snapshot."""
        return None if self.output_json is None else json.loads(self.output_json)


class HookDecision(StrictModel):
    verdict: HookVerdict
    reason_code: Annotated[
        str,
        StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$"),
    ] = "policy"


class HookOutcome(StrictModel):
    name: HookName
    phase: HookPhase
    failure_policy: HookFailurePolicy
    verdict: HookVerdict
    reason_code: str
    failed: bool = False
    timed_out: bool = False


HookCallback = Callable[[HookContext], Awaitable[HookDecision]]


class ToolHook(StrictModel):
    name: HookName
    phase: HookPhase
    timeout_seconds: float = Field(gt=0, le=60)
    failure_policy: HookFailurePolicy = HookFailurePolicy.FAIL_CLOSED
    callback: HookCallback


class HookDenied(RuntimeError):
    def __init__(self, outcome: HookOutcome) -> None:
        super().__init__(f"tool hook {outcome.name} denied execution ({outcome.reason_code})")
        self.outcome = outcome


class ToolHookRunner:
    def __init__(
        self,
        hooks: Sequence[ToolHook],
        *,
        max_hooks: int = 64,
        max_snapshot_bytes: int = 1_048_576,
    ) -> None:
        immutable = tuple(hooks)
        if not 1 <= max_hooks <= 1024:
            raise ValueError("max_hooks must be between 1 and 1024")
        if len(immutable) > max_hooks:
            raise ValueError("hook count exceeds the configured limit")
        if len({hook.name for hook in immutable}) != len(immutable):
            raise ValueError("hook names must be unique")
        if not 1024 <= max_snapshot_bytes <= 16_777_216:
            raise ValueError("max_snapshot_bytes must be between 1 KiB and 16 MiB")
        self._hooks = immutable
        self._max_snapshot_bytes = max_snapshot_bytes

    async def run_pre(
        self,
        call: ToolCall,
        *,
        session_id: UUID,
        correlation_id: UUID,
    ) -> tuple[HookOutcome, ...]:
        return await self._run(
            HookPhase.PRE_TOOL,
            call,
            session_id=session_id,
            correlation_id=correlation_id,
            output_marker=None,
        )

    async def run_post(
        self,
        call: ToolCall,
        output: JsonValue,
        *,
        session_id: UUID,
        correlation_id: UUID,
    ) -> tuple[HookOutcome, ...]:
        return await self._run(
            HookPhase.POST_TOOL,
            call,
            session_id=session_id,
            correlation_id=correlation_id,
            output_marker=(output,),
        )

    async def _run(
        self,
        phase: HookPhase,
        call: ToolCall,
        *,
        session_id: UUID,
        correlation_id: UUID,
        output_marker: tuple[JsonValue] | None,
    ) -> tuple[HookOutcome, ...]:
        input_json = _snapshot_json(call.arguments, self._max_snapshot_bytes)
        output_json = (
            None
            if output_marker is None
            else _snapshot_json(output_marker[0], self._max_snapshot_bytes)
        )
        context = HookContext(
            phase=phase,
            session_id=session_id,
            correlation_id=correlation_id,
            tool_call_id=call.id,
            tool_name=call.name,
            input_json=input_json,
            input_sha256=hashlib.sha256(input_json).hexdigest(),
            output_json=output_json,
            output_sha256=(
                hashlib.sha256(output_json).hexdigest()
                if output_json is not None
                else None
            ),
        )
        outcomes: list[HookOutcome] = []
        for hook in self._hooks:
            if hook.phase is not phase:
                continue
            outcome = await _invoke_hook(hook, context)
            outcomes.append(outcome)
            if outcome.verdict is HookVerdict.DENY:
                raise HookDenied(outcome)
        return tuple(outcomes)


async def _invoke_hook(hook: ToolHook, context: HookContext) -> HookOutcome:
    try:
        decision = await asyncio.wait_for(
            hook.callback(context),
            timeout=hook.timeout_seconds,
        )
        if not isinstance(decision, HookDecision):
            raise TypeError("hook returned an invalid decision")
        return HookOutcome(
            name=hook.name,
            phase=hook.phase,
            failure_policy=hook.failure_policy,
            verdict=decision.verdict,
            reason_code=decision.reason_code,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return _failure_outcome(hook, reason_code="hook_timeout", timed_out=True)
    except Exception:
        return _failure_outcome(hook, reason_code="hook_failure")


def _failure_outcome(
    hook: ToolHook,
    *,
    reason_code: str,
    timed_out: bool = False,
) -> HookOutcome:
    return HookOutcome(
        name=hook.name,
        phase=hook.phase,
        failure_policy=hook.failure_policy,
        verdict=(
            HookVerdict.DENY
            if hook.failure_policy is HookFailurePolicy.FAIL_CLOSED
            else HookVerdict.ALLOW
        ),
        reason_code=reason_code,
        failed=True,
        timed_out=timed_out,
    )


def _snapshot_json(value: Mapping[str, JsonValue] | JsonValue, maximum: int) -> bytes:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("hook snapshot value must be JSON") from exc
    if len(encoded) > maximum:
        raise ValueError("hook snapshot exceeds the byte limit")
    return encoded
