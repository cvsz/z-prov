"""Explicit, immutable tool permission decisions."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, StringConstraints, field_validator, model_validator

from zeaz_agent.schemas import Identifier, StrictModel, ToolCall, ToolName, utc_now

RulePattern = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9*?_.-]+$",
    ),
]
Reason = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
Actor = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$",
    ),
]


class PermissionOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class DecisionSource(StrEnum):
    POLICY = "policy"
    DEFAULT = "default"
    USER = "user"


class PermissionRule(StrictModel):
    schema_version: Literal["1"] = "1"
    id: Identifier
    effect: PermissionOutcome
    tool_pattern: RulePattern
    argument_equals: dict[str, JsonValue] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-10_000, le=10_000)
    reason: Reason

    def matches(self, call: ToolCall) -> bool:
        return fnmatchcase(call.name, self.tool_pattern) and all(
            key in call.arguments and call.arguments[key] == expected
            for key, expected in self.argument_equals.items()
        )


class PermissionDecision(StrictModel):
    schema_version: Literal["1"] = "1"
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    correlation_id: UUID
    tool_call_id: Identifier
    tool_name: ToolName
    arguments_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    outcome: PermissionOutcome
    source: DecisionSource
    rule_id: Identifier | None = None
    decided_by: Actor
    reason: Reason
    resolved_from: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def timestamp_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a UTC offset")
        return value

    @model_validator(mode="after")
    def validate_provenance(self) -> PermissionDecision:
        if self.source is DecisionSource.POLICY and self.rule_id is None:
            raise ValueError("policy decisions require a rule_id")
        if self.source is DecisionSource.USER and self.resolved_from is None:
            raise ValueError("user decisions must resolve an ask decision")
        if self.source is not DecisionSource.USER and self.resolved_from is not None:
            raise ValueError("only user decisions may have resolved_from")
        return self


class PermissionPolicy:
    """Deterministic rule evaluation with secure equal-priority precedence."""

    def __init__(
        self,
        rules: tuple[PermissionRule, ...] = (),
        *,
        default: PermissionOutcome = PermissionOutcome.ASK,
    ) -> None:
        if len(rules) > 10_000:
            raise ValueError("permission policy exceeds the rule limit")
        ids = [rule.id for rule in rules]
        if len(ids) != len(set(ids)):
            raise ValueError("permission rule IDs must be unique")
        self._rules = rules
        self._default = PermissionOutcome(default)

    def decide(
        self,
        call: ToolCall,
        *,
        session_id: UUID,
        correlation_id: UUID,
    ) -> PermissionDecision:
        matching = [rule for rule in self._rules if rule.matches(call)]
        if matching:
            precedence = {
                PermissionOutcome.DENY: 2,
                PermissionOutcome.ASK: 1,
                PermissionOutcome.ALLOW: 0,
            }
            rule = min(
                matching,
                key=lambda item: (-item.priority, -precedence[item.effect], item.id),
            )
            return PermissionDecision(
                session_id=session_id,
                correlation_id=correlation_id,
                tool_call_id=call.id,
                tool_name=call.name,
                arguments_sha256=tool_arguments_digest(call),
                outcome=rule.effect,
                source=DecisionSource.POLICY,
                rule_id=rule.id,
                decided_by="policy",
                reason=rule.reason,
            )
        return PermissionDecision(
            session_id=session_id,
            correlation_id=correlation_id,
            tool_call_id=call.id,
            tool_name=call.name,
            arguments_sha256=tool_arguments_digest(call),
            outcome=self._default,
            source=DecisionSource.DEFAULT,
            decided_by="default-policy",
            reason=f"no matching rule; default is {self._default.value}",
        )

    @staticmethod
    def resolve_ask(
        decision: PermissionDecision,
        outcome: PermissionOutcome,
        *,
        decided_by: str,
        reason: str,
    ) -> PermissionDecision:
        if decision.outcome is not PermissionOutcome.ASK:
            raise ValueError("only ask decisions can be resolved")
        if outcome not in {PermissionOutcome.ALLOW, PermissionOutcome.DENY}:
            raise ValueError("an ask decision must resolve to allow or deny")
        return PermissionDecision(
            session_id=decision.session_id,
            correlation_id=decision.correlation_id,
            tool_call_id=decision.tool_call_id,
            tool_name=decision.tool_name,
            arguments_sha256=decision.arguments_sha256,
            outcome=outcome,
            source=DecisionSource.USER,
            rule_id=decision.rule_id,
            decided_by=decided_by,
            reason=reason,
            resolved_from=decision.id,
        )


def require_allowed(
    call: ToolCall,
    decision: PermissionDecision,
    *,
    session_id: UUID,
    correlation_id: UUID,
) -> None:
    """Reject execution unless a recorded allow is bound to this exact request."""

    if decision.outcome is not PermissionOutcome.ALLOW:
        raise PermissionError("tool call does not have an allow decision")
    expected = (
        session_id,
        correlation_id,
        call.id,
        call.name,
        tool_arguments_digest(call),
    )
    actual = (
        decision.session_id,
        decision.correlation_id,
        decision.tool_call_id,
        decision.tool_name,
        decision.arguments_sha256,
    )
    if actual != expected:
        raise PermissionError("allow decision does not match the tool call context")


def tool_arguments_digest(call: ToolCall) -> str:
    canonical = json.dumps(
        call.arguments,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()
