"""Strict immutable job, approval, policy, and receipt schemas."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePath
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"),
]
Actor = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$"),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ImageReference = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*"
            r"[a-z0-9]+(?:[._-][a-z0-9]+)*@sha256:[0-9a-f]{64}$"
        ),
        max_length=512,
    ),
]
CommandArgument = Annotated[str, StringConstraints(max_length=16_384)]
_HOSTNAME = re.compile(
    r"^(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkspaceAccess(StrEnum):
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class NetworkMode(StrEnum):
    DISABLED = "disabled"
    ALLOW_LIST = "allow_list"


class ExecutionState(StrEnum):
    REJECTED = "rejected"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EgressDestination(StrictModel):
    host: Annotated[str, StringConstraints(min_length=1, max_length=253)]
    ports: tuple[int, ...] = Field(min_length=1, max_length=64)

    @field_validator("host")
    @classmethod
    def host_is_exact_and_not_metadata(cls, value: str) -> str:
        normalized = value.lower().rstrip(".")
        if value.startswith("[") or "*" in value or not normalized:
            raise ValueError("egress host must be exact")
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            if not _HOSTNAME.fullmatch(value):
                raise ValueError("egress host is invalid") from None
            if normalized in {"localhost", "metadata.google.internal"}:
                raise ValueError(
                    "loopback and metadata destinations are forbidden"
                ) from None
        else:
            if (
                address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_unspecified
                or str(address) == "169.254.169.254"
            ):
                raise ValueError("loopback, link-local, and metadata addresses are forbidden")
        return normalized

    @field_validator("ports")
    @classmethod
    def ports_are_unique_and_valid(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(port < 1 or port > 65535 for port in value) or len(set(value)) != len(value):
            raise ValueError("egress ports must be unique values from 1 through 65535")
        return tuple(sorted(value))


class SandboxLimits(StrictModel):
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    cpu_cores: float = Field(default=1, gt=0, le=32)
    memory_bytes: int = Field(default=536_870_912, ge=16_777_216, le=68_719_476_736)
    process_count: int = Field(default=64, ge=1, le=4096)
    file_bytes: int = Field(default=67_108_864, ge=4096, le=10_737_418_240)
    temporary_bytes: int = Field(default=268_435_456, ge=1_048_576, le=10_737_418_240)
    output_bytes: int = Field(default=1_048_576, ge=1024, le=67_108_864)


class SandboxPolicy(StrictModel):
    schema_version: Literal["1"] = "1"
    limits: SandboxLimits = Field(default_factory=SandboxLimits)
    workspace_access: WorkspaceAccess = WorkspaceAccess.READ_ONLY
    network_mode: NetworkMode = NetworkMode.DISABLED
    allowed_destinations: tuple[EgressDestination, ...] = Field(
        default=(),
        max_length=128,
    )
    seccomp_profile: Literal["builtin"] = "builtin"
    apparmor_profile: Annotated[
        str,
        StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"),
    ] = "docker-default"

    @model_validator(mode="after")
    def network_configuration_is_coherent(self) -> SandboxPolicy:
        if self.network_mode is NetworkMode.DISABLED and self.allowed_destinations:
            raise ValueError("disabled networking cannot have allowed destinations")
        if self.network_mode is NetworkMode.ALLOW_LIST and not self.allowed_destinations:
            raise ValueError("allow-list networking requires at least one destination")
        keys = [
            (destination.host, destination.ports)
            for destination in self.allowed_destinations
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("egress destinations must be unique")
        return self


class JobSpec(StrictModel):
    schema_version: Literal["1"] = "1"
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    correlation_id: UUID
    image: ImageReference
    command: tuple[CommandArgument, ...] = Field(min_length=1, max_length=128)
    workspace: Path
    policy: SandboxPolicy = Field(default_factory=SandboxPolicy)

    @field_validator("command")
    @classmethod
    def argv_is_safe_and_bounded(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any("\x00" in argument for argument in value):
            raise ValueError("command argv cannot contain NUL bytes")
        if sum(len(argument.encode("utf-8")) for argument in value) > 65_536:
            raise ValueError("command argv exceeds the total byte limit")
        return value

    @field_validator("workspace")
    @classmethod
    def workspace_is_absolute_and_normalized(cls, value: Path) -> Path:
        if (
            not value.is_absolute()
            or value != Path(PurePath(value))
            or any(part in {".", ".."} for part in value.parts)
        ):
            raise ValueError("workspace path must be absolute and normalized")
        if "\x00" in str(value):
            raise ValueError("workspace path cannot contain NUL bytes")
        return value


class ExecutionApproval(StrictModel):
    schema_version: Literal["1"] = "1"
    id: UUID = Field(default_factory=uuid4)
    job_id: UUID
    session_id: UUID
    spec_sha256: Sha256
    approved_by: Actor
    permission_decision_id: UUID
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def timestamps_are_valid(self) -> ExecutionApproval:
        for value in (self.created_at, self.expires_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("approval timestamps must include a UTC offset")
        if self.expires_at <= self.created_at:
            raise ValueError("approval must expire after it is created")
        if self.expires_at - self.created_at > timedelta(hours=1):
            raise ValueError("approval lifetime cannot exceed one hour")
        return self


class JobRequest(StrictModel):
    schema_version: Literal["1"] = "1"
    spec: JobSpec
    approval: ExecutionApproval

    @model_validator(mode="after")
    def approval_is_bound_to_exact_job(self) -> JobRequest:
        if (
            self.approval.job_id != self.spec.id
            or self.approval.session_id != self.spec.session_id
            or self.approval.spec_sha256 != job_spec_digest(self.spec)
        ):
            raise ValueError("execution approval does not match the exact job")
        return self

    def require_current_approval(self, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("current time must include a UTC offset")
        if current < self.approval.created_at or current >= self.approval.expires_at:
            raise PermissionError("execution approval is not currently valid")


class ExecutionReceipt(StrictModel):
    schema_version: Literal["1"] = "1"
    id: UUID = Field(default_factory=uuid4)
    job_id: UUID
    session_id: UUID
    correlation_id: UUID
    approval_id: UUID
    image_digest: Annotated[
        str,
        StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
    ]
    policy_sha256: Sha256
    state: ExecutionState
    exit_code: int | None = Field(default=None, ge=0, le=255)
    started_at: datetime | None = None
    finished_at: datetime
    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)
    output_truncated: bool = False
    cleanup_complete: bool
    failure_code: Annotated[
        str | None,
        StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$"),
    ] = None

    @model_validator(mode="after")
    def receipt_state_is_coherent(self) -> ExecutionReceipt:
        timestamps = (self.started_at, self.finished_at)
        if any(
            value is not None and (value.tzinfo is None or value.utcoffset() is None)
            for value in timestamps
        ):
            raise ValueError("receipt timestamps must include a UTC offset")
        if self.started_at is not None and self.finished_at < self.started_at:
            raise ValueError("receipt cannot finish before it starts")
        if self.state is ExecutionState.REJECTED and self.started_at is not None:
            raise ValueError("a rejected job cannot have a start time")
        if self.state is ExecutionState.COMPLETED and self.exit_code != 0:
            raise ValueError("a completed job requires exit code zero")
        if self.state is ExecutionState.COMPLETED and not self.cleanup_complete:
            raise ValueError("a completed job requires completed cleanup")
        if self.state in {ExecutionState.REJECTED, ExecutionState.CANCELLED} and self.exit_code is not None:
            raise ValueError("rejected and cancelled jobs cannot have an exit code")
        if self.state is not ExecutionState.COMPLETED and not self.failure_code:
            raise ValueError("non-completed receipts require a failure code")
        return self


def job_spec_digest(spec: JobSpec) -> str:
    return hashlib.sha256(_canonical(spec.model_dump(mode="json"))).hexdigest()


def policy_digest(policy: SandboxPolicy) -> str:
    return hashlib.sha256(_canonical(policy.model_dump(mode="json"))).hexdigest()


def approve_job(
    spec: JobSpec,
    *,
    approved_by: str,
    permission_decision_id: UUID,
    now: datetime | None = None,
    lifetime_seconds: int = 300,
) -> ExecutionApproval:
    if not 1 <= lifetime_seconds <= 3600:
        raise ValueError("approval lifetime must be between 1 and 3600 seconds")
    created = now or datetime.now(UTC)
    if created.tzinfo is None or created.utcoffset() is None:
        raise ValueError("approval time must include a UTC offset")
    return ExecutionApproval(
        job_id=spec.id,
        session_id=spec.session_id,
        spec_sha256=job_spec_digest(spec),
        approved_by=approved_by,
        permission_decision_id=permission_decision_id,
        created_at=created,
        expires_at=created + timedelta(seconds=lifetime_seconds),
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode()
