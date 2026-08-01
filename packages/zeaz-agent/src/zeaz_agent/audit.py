"""Bounded append-only audit ledger with correlated, hash-chained events."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, StringConstraints, field_validator

from zeaz_agent.permissions import Actor
from zeaz_agent.schemas import Identifier, StrictModel, utc_now

if TYPE_CHECKING:
    from zeaz_agent.permissions import PermissionDecision
    from zeaz_agent.plan import PlanApproval

ZERO_HASH = "0" * 64
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
EventType = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,7}$", max_length=128),
]
_SENSITIVE_KEY = re.compile(
    r"^(?:api[_-]?key|client[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"authorization|credential|password|secret|prompt|messages?|content|request_body)$",
    re.IGNORECASE,
)


class AuditEvent(StrictModel):
    schema_version: Literal["1"] = "1"
    id: UUID = Field(default_factory=uuid4)
    sequence: int = Field(ge=0)
    session_id: UUID
    correlation_id: UUID
    event_type: EventType
    actor: Actor
    subject_id: Identifier | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def timestamp_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a UTC offset")
        return value


class AuditEntry(StrictModel):
    event: AuditEvent
    previous_sha256: Sha256
    sha256: Sha256


class AuditIntegrityError(RuntimeError):
    """The audit file is malformed, altered, or exceeds configured bounds."""


class AuditSink(Protocol):
    def append(
        self,
        *,
        session_id: UUID,
        correlation_id: UUID,
        event_type: str,
        actor: str,
        subject_id: str | None = None,
        details: dict[str, JsonValue] | None = None,
    ) -> AuditEntry: ...


class JsonlAuditLog:
    def __init__(
        self,
        path: Path,
        *,
        max_log_bytes: int = 67_108_864,
        max_event_bytes: int = 65_536,
        fsync: bool = True,
    ) -> None:
        if not 1024 <= max_event_bytes <= 1_048_576:
            raise ValueError("max_event_bytes must be between 1 KiB and 1 MiB")
        if not max_event_bytes <= max_log_bytes <= 1_073_741_824:
            raise ValueError("max_log_bytes must be between one event and 1 GiB")
        self._path = path
        self._max_log_bytes = max_log_bytes
        self._max_event_bytes = max_event_bytes
        self._fsync = fsync

    def append(
        self,
        *,
        session_id: UUID,
        correlation_id: UUID,
        event_type: str,
        actor: str,
        subject_id: str | None = None,
        details: dict[str, JsonValue] | None = None,
    ) -> AuditEntry:
        fd = self._open()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            size = os.fstat(fd).st_size
            if size > self._max_log_bytes:
                raise AuditIntegrityError("audit log exceeds its configured size")
            previous = self._last_entry(fd, size)
            event = AuditEvent(
                sequence=0 if previous is None else previous.event.sequence + 1,
                session_id=session_id,
                correlation_id=correlation_id,
                event_type=event_type,
                actor=actor,
                subject_id=subject_id,
                details=_sanitize(details or {}),
            )
            previous_hash = ZERO_HASH if previous is None else previous.sha256
            digest = _entry_digest(event, previous_hash)
            entry = AuditEntry(event=event, previous_sha256=previous_hash, sha256=digest)
            encoded = _canonical(entry.model_dump(mode="json")) + b"\n"
            if len(encoded) > self._max_event_bytes:
                raise ValueError("audit event exceeds its configured size")
            if size + len(encoded) > self._max_log_bytes:
                raise ValueError("audit log has reached its configured size")
            _write_all(fd, encoded)
            if self._fsync:
                os.fsync(fd)
            return entry
        finally:
            os.close(fd)

    def verify(self) -> tuple[AuditEntry, ...]:
        if not self._path.exists():
            return ()
        fd = self._open(create=False)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            size = os.fstat(fd).st_size
            if size > self._max_log_bytes:
                raise AuditIntegrityError("audit log exceeds its configured size")
            os.lseek(fd, 0, os.SEEK_SET)
            data = _read_exact(fd, size)
        finally:
            os.close(fd)
        if data and not data.endswith(b"\n"):
            raise AuditIntegrityError("audit log ends with a partial record")
        entries: list[AuditEntry] = []
        previous_hash = ZERO_HASH
        for sequence, line in enumerate(data.splitlines()):
            if len(line) + 1 > self._max_event_bytes:
                raise AuditIntegrityError("audit record exceeds its configured size")
            entry = _decode_entry(line)
            if entry.event.sequence != sequence:
                raise AuditIntegrityError("audit sequence is not contiguous")
            if entry.previous_sha256 != previous_hash:
                raise AuditIntegrityError("audit hash chain is broken")
            if entry.sha256 != _entry_digest(entry.event, previous_hash):
                raise AuditIntegrityError("audit record digest is invalid")
            entries.append(entry)
            previous_hash = entry.sha256
        return tuple(entries)

    def _open(self, *, create: bool = True) -> int:
        flags = os.O_RDWR | os.O_APPEND
        if create:
            flags |= os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self._path, flags, 0o600)
        except OSError as exc:
            raise AuditIntegrityError("unable to open audit log safely") from exc
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            os.close(fd)
            raise AuditIntegrityError("audit log must be an owner-only regular file")
        return fd

    def _last_entry(self, fd: int, size: int) -> AuditEntry | None:
        if size == 0:
            return None
        os.lseek(fd, -1, os.SEEK_END)
        if os.read(fd, 1) != b"\n":
            raise AuditIntegrityError("audit log ends with a partial record")
        start = max(0, size - self._max_event_bytes)
        os.lseek(fd, start, os.SEEK_SET)
        tail = _read_exact(fd, size - start)
        lines = tail.splitlines()
        if start and len(lines) < 2:
            raise AuditIntegrityError("last audit record exceeds its configured size")
        return _decode_entry(lines[-1])


def _entry_digest(event: AuditEvent, previous_hash: str) -> str:
    payload = {"event": event.model_dump(mode="json"), "previous_sha256": previous_hash}
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode()


def _decode_entry(line: bytes) -> AuditEntry:
    try:
        return AuditEntry.model_validate_json(line)
    except Exception as exc:
        raise AuditIntegrityError("audit record failed schema validation") from exc


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("audit write made no progress")
        view = view[written:]


def _read_exact(fd: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(fd, min(remaining, 1_048_576))
        if not chunk:
            raise AuditIntegrityError("audit log changed during read")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _sanitize(value: JsonValue, *, depth: int = 0) -> JsonValue:
    if depth > 16:
        raise ValueError("audit details exceed the nesting limit")
    if isinstance(value, dict):
        if len(value) > 256:
            raise ValueError("audit details contain too many object fields")
        return {
            str(key)[:128]: (
                "[REDACTED]" if _SENSITIVE_KEY.fullmatch(str(key)) else _sanitize(item, depth=depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        if len(value) > 256:
            raise ValueError("audit details contain too many array items")
        return [_sanitize(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return value[:4096]
    return value


def record_permission_decision(log: AuditSink, decision: PermissionDecision) -> AuditEntry:
    return log.append(
        session_id=decision.session_id,
        correlation_id=decision.correlation_id,
        event_type="tool.permission_decided",
        actor=decision.decided_by,
        subject_id=decision.tool_call_id,
        details={
            "decision_id": str(decision.id),
            "tool_name": decision.tool_name,
            "outcome": decision.outcome.value,
            "source": decision.source.value,
            "rule_id": decision.rule_id,
            "resolved_from": str(decision.resolved_from) if decision.resolved_from else None,
        },
    )


def record_plan_approval(
    log: AuditSink,
    approval: PlanApproval,
    *,
    correlation_id: UUID,
) -> AuditEntry:
    return log.append(
        session_id=approval.session_id,
        correlation_id=correlation_id,
        event_type="plan.approved",
        actor=approval.approved_by,
        subject_id=str(approval.plan_id),
        details={
            "approval_id": str(approval.id),
            "plan_sha256": approval.plan_sha256,
        },
    )
