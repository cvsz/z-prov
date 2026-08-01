"""Digest-bound plan approval and mutation gating."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, StringConstraints, field_validator, model_validator

from zeaz_agent.permissions import (
    Actor,
    PermissionDecision,
    Reason,
    require_allowed,
    tool_arguments_digest,
)
from zeaz_agent.schemas import (
    ExecutionMode,
    Identifier,
    NonEmptyText,
    Session,
    SessionStatus,
    StrictModel,
    ToolCall,
    ToolDefinition,
    ToolName,
    utc_now,
)

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class PlannedAction(StrictModel):
    tool_call_id: Identifier
    tool_name: ToolName
    arguments_sha256: Sha256

    @classmethod
    def from_call(cls, call: ToolCall) -> PlannedAction:
        return cls(
            tool_call_id=call.id,
            tool_name=call.name,
            arguments_sha256=tool_arguments_digest(call),
        )

    def matches(self, call: ToolCall) -> bool:
        return (
            self.tool_call_id == call.id
            and self.tool_name == call.name
            and self.arguments_sha256 == tool_arguments_digest(call)
        )


class PlanStep(StrictModel):
    id: Identifier
    description: NonEmptyText
    action: PlannedAction | None = None


class Plan(StrictModel):
    schema_version: Literal["1"] = "1"
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    summary: NonEmptyText
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=256)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def timestamp_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a UTC offset")
        return value

    @model_validator(mode="after")
    def step_ids_are_unique(self) -> Plan:
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("plan step IDs must be unique")
        return self


class PlanApproval(StrictModel):
    schema_version: Literal["1"] = "1"
    id: UUID = Field(default_factory=uuid4)
    plan_id: UUID
    session_id: UUID
    plan_sha256: Sha256
    approved_by: Actor
    reason: Reason
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def timestamp_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a UTC offset")
        return value


def set_plan_mode(session: Session, *, enabled: bool) -> Session:
    if session.status is not SessionStatus.ACTIVE:
        raise ValueError("only active sessions can change execution mode")
    mode = ExecutionMode.PLAN if enabled else ExecutionMode.NORMAL
    if session.execution_mode is mode:
        return session
    return Session(
        id=session.id,
        revision=session.revision + 1,
        status=session.status,
        execution_mode=mode,
        token_budget=session.token_budget,
        token_usage=session.token_usage,
        turns=session.turns,
        created_at=session.created_at,
        updated_at=utc_now(),
    )


def approve_plan(plan: Plan, *, approved_by: str, reason: str) -> PlanApproval:
    return PlanApproval(
        plan_id=plan.id,
        session_id=plan.session_id,
        plan_sha256=plan_digest(plan),
        approved_by=approved_by,
        reason=reason,
    )


def require_plan_allows(
    session: Session,
    call: ToolCall,
    definitions: tuple[ToolDefinition, ...],
    *,
    plan: Plan | None = None,
    approval: PlanApproval | None = None,
) -> None:
    if session.execution_mode is not ExecutionMode.PLAN:
        return
    mutating = _is_mutating(call, definitions)
    if not mutating:
        return
    if plan is None or approval is None:
        raise PermissionError("plan mode blocks mutation until a plan is approved")
    expected_approval = (plan.id, session.id, plan_digest(plan))
    actual_approval = (approval.plan_id, approval.session_id, approval.plan_sha256)
    if plan.session_id != session.id or actual_approval != expected_approval:
        raise PermissionError("plan approval does not match the session and exact plan")
    if not any(step.action is not None and step.action.matches(call) for step in plan.steps):
        raise PermissionError("mutating tool call is not an exact action in the approved plan")


def require_execution_allowed(
    session: Session,
    call: ToolCall,
    decision: PermissionDecision,
    definitions: tuple[ToolDefinition, ...],
    *,
    correlation_id: UUID,
    plan: Plan | None = None,
    approval: PlanApproval | None = None,
) -> None:
    """Require independent permission and plan-mode authorization."""

    require_allowed(
        call,
        decision,
        session_id=session.id,
        correlation_id=correlation_id,
    )
    require_plan_allows(
        session,
        call,
        definitions,
        plan=plan,
        approval=approval,
    )


def plan_digest(plan: Plan) -> str:
    canonical = json.dumps(
        plan.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _is_mutating(call: ToolCall, definitions: tuple[ToolDefinition, ...]) -> bool:
    matching = [definition for definition in definitions if definition.name == call.name]
    if len(matching) > 1:
        raise ValueError("tool definition names must be unique")
    return matching[0].mutating if matching else True
