"""Approval-bound sandbox orchestration, receipts, cancellation, and cleanup."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from zeaz_sandbox.backend import (
    ContainerExecutionResult,
    ContainerStopReason,
    NetworkAttachment,
    SandboxBackendError,
)
from zeaz_sandbox.schemas import (
    ExecutionReceipt,
    ExecutionState,
    JobRequest,
    policy_digest,
)
from zeaz_sandbox.streaming import (
    BoundedOutputStreamer,
    NullOutputSink,
    OutputSink,
)


class SandboxServiceError(RuntimeError):
    """A sanitized sandbox orchestration failure."""


class ActiveExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    container_id: str | None = None
    attachment: NetworkAttachment


class ReconciliationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    inspected: int = Field(ge=0)
    cleaned: int = Field(ge=0)
    failed: int = Field(ge=0)


class SandboxBackend(Protocol):
    async def probe(self) -> None: ...

    async def prepare_network(self, job) -> NetworkAttachment: ...

    async def create(
        self,
        job,
        *,
        container_name: str,
        attachment: NetworkAttachment,
    ) -> str: ...

    async def execute(
        self,
        container_id: str,
        streamer: BoundedOutputStreamer,
        *,
        timeout_seconds: int,
        cancel_event: asyncio.Event | None = None,
    ) -> ContainerExecutionResult: ...

    async def remove(self, container_id: str) -> None: ...

    async def managed_containers(self) -> tuple[str, ...]: ...

    async def cleanup_network(self, attachment: NetworkAttachment) -> None: ...


class SQLiteSandboxStore:
    def __init__(self, path: Path) -> None:
        _ensure_private_database(path)
        self._path = path
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    job_id TEXT PRIMARY KEY,
                    receipt_json BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS active_executions (
                    job_id TEXT PRIMARY KEY,
                    container_id TEXT UNIQUE,
                    attachment_json BLOB NOT NULL
                );
                """
            )
        os.chmod(path, 0o600)

    def record_active(self, execution: ActiveExecution) -> None:
        encoded = execution.attachment.model_dump_json().encode()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO active_executions (job_id, container_id, attachment_json) "
                "VALUES (?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET "
                "container_id = excluded.container_id, "
                "attachment_json = excluded.attachment_json",
                (str(execution.job_id), execution.container_id, encoded),
            )
            connection.commit()

    def finalize(self, receipt: ExecutionReceipt) -> None:
        encoded = receipt.model_dump_json().encode()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO receipts (job_id, receipt_json) VALUES (?, ?)",
                (str(receipt.job_id), encoded),
            )
            if receipt.cleanup_complete:
                connection.execute(
                    "DELETE FROM active_executions WHERE job_id = ?",
                    (str(receipt.job_id),),
                )
            connection.commit()

    def receipt(self, job_id: UUID) -> ExecutionReceipt | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT receipt_json FROM receipts WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            return ExecutionReceipt.model_validate_json(row[0])
        except Exception as exc:
            raise SandboxServiceError("stored execution receipt is invalid") from exc

    def active(self) -> tuple[ActiveExecution, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id, container_id, attachment_json "
                "FROM active_executions ORDER BY job_id"
            ).fetchall()
        results: list[ActiveExecution] = []
        for row in rows:
            try:
                results.append(
                    ActiveExecution(
                        job_id=UUID(row[0]),
                        container_id=row[1],
                        attachment=NetworkAttachment.model_validate_json(row[2]),
                    )
                )
            except Exception as exc:
                raise SandboxServiceError("stored active execution is invalid") from exc
        return tuple(results)

    def clear_active(self, job_id: UUID) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM active_executions WHERE job_id = ?",
                (str(job_id),),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection


class SandboxService:
    def __init__(
        self,
        backend: SandboxBackend,
        store: SQLiteSandboxStore,
    ) -> None:
        self._backend = backend
        self._store = store
        self._cancel_events: dict[UUID, asyncio.Event] = {}
        self._active_lock = asyncio.Lock()
        self._ready = False

    async def start(self) -> None:
        await self._backend.probe()
        self._ready = True

    async def execute(
        self,
        request: JobRequest,
        *,
        sink: OutputSink | None = None,
        redaction_secrets: Sequence[bytes] = (),
        now: datetime | None = None,
    ) -> ExecutionReceipt:
        if not self._ready:
            raise SandboxServiceError("sandbox service has not passed its runtime probe")
        current = now or datetime.now(UTC)
        try:
            request.require_current_approval(current)
        except (PermissionError, ValueError):
            receipt = _receipt(
                request,
                state=ExecutionState.REJECTED,
                finished_at=current,
                failure_code="approval_invalid",
                cleanup_complete=True,
            )
            self._store.finalize(receipt)
            return receipt

        async with self._active_lock:
            if request.spec.id in self._cancel_events or self._store.receipt(request.spec.id):
                raise SandboxServiceError("job has already been submitted")
            cancel_event = asyncio.Event()
            self._cancel_events[request.spec.id] = cancel_event

        attachment: NetworkAttachment | None = None
        container_id: str | None = None
        started_at: datetime | None = None
        execution: ContainerExecutionResult | None = None
        failure_code: str | None = None
        cleanup_complete = True
        was_cancelled = False
        try:
            attachment = await self._backend.prepare_network(request.spec)
            self._store.record_active(
                ActiveExecution(
                    job_id=request.spec.id,
                    attachment=attachment,
                )
            )
            container_id = await self._backend.create(
                request.spec,
                container_name=f"zeaz-job-{request.spec.id.hex}",
                attachment=attachment,
            )
            self._store.record_active(
                ActiveExecution(
                    job_id=request.spec.id,
                    container_id=container_id,
                    attachment=attachment,
                )
            )
            started_at = datetime.now(UTC)
            streamer = BoundedOutputStreamer(
                sink or NullOutputSink(),
                max_bytes=request.spec.policy.limits.output_bytes,
                secrets=redaction_secrets,
            )
            execution = await self._backend.execute(
                container_id,
                streamer,
                timeout_seconds=request.spec.policy.limits.timeout_seconds,
                cancel_event=cancel_event,
            )
        except SandboxBackendError:
            failure_code = "backend_failure"
        except asyncio.CancelledError:
            cancel_event.set()
            was_cancelled = True
            failure_code = "cancelled"
        except Exception:
            failure_code = "execution_failure"
        finally:
            if container_id is not None:
                try:
                    await self._backend.remove(container_id)
                except Exception:
                    cleanup_complete = False
            if attachment is not None:
                try:
                    await self._backend.cleanup_network(attachment)
                except Exception:
                    cleanup_complete = False

        state, exit_code, mapped_failure = _map_execution(execution, failure_code)
        if was_cancelled:
            state = ExecutionState.CANCELLED
            exit_code = None
            mapped_failure = "cancelled"
        if not cleanup_complete and state is ExecutionState.COMPLETED:
            state = ExecutionState.FAILED
            exit_code = None
            mapped_failure = "cleanup_failure"
        receipt = _receipt(
            request,
            state=state,
            exit_code=exit_code,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            stdout_bytes=execution.stdout_bytes if execution else 0,
            stderr_bytes=execution.stderr_bytes if execution else 0,
            output_truncated=execution.output_truncated if execution else False,
            cleanup_complete=cleanup_complete,
            failure_code=mapped_failure,
        )
        try:
            self._store.finalize(receipt)
            return receipt
        finally:
            async with self._active_lock:
                self._cancel_events.pop(request.spec.id, None)

    async def cancel(self, job_id: UUID) -> bool:
        async with self._active_lock:
            event = self._cancel_events.get(job_id)
            if event is None:
                return False
            event.set()
            return True

    async def reconcile(self) -> ReconciliationResult:
        if not self._ready:
            raise SandboxServiceError("sandbox service has not passed its runtime probe")
        managed = set(await self._backend.managed_containers())
        journal = self._store.active()
        async with self._active_lock:
            live_jobs = set(self._cancel_events)
        cleaned = 0
        failed = 0
        inspected = 0
        journaled_containers = {
            active.container_id
            for active in journal
            if active.container_id is not None
        }
        for active in journal:
            if active.job_id in live_jobs:
                continue
            inspected += 1
            try:
                if active.container_id is not None and active.container_id in managed:
                    await self._backend.remove(active.container_id)
                await self._backend.cleanup_network(active.attachment)
                self._store.clear_active(active.job_id)
                cleaned += 1
            except Exception:
                failed += 1
        for container_id in sorted(managed - journaled_containers):
            inspected += 1
            try:
                await self._backend.remove(container_id)
                cleaned += 1
            except Exception:
                failed += 1
        return ReconciliationResult(
            inspected=inspected,
            cleaned=cleaned,
            failed=failed,
        )


def _map_execution(
    execution: ContainerExecutionResult | None,
    failure_code: str | None,
) -> tuple[ExecutionState, int | None, str | None]:
    if execution is None:
        return ExecutionState.FAILED, None, failure_code or "execution_failure"
    if execution.reason is ContainerStopReason.CANCELLED:
        return ExecutionState.CANCELLED, None, "cancelled"
    if execution.reason is ContainerStopReason.TIMED_OUT:
        return ExecutionState.FAILED, None, "timeout"
    if execution.reason is ContainerStopReason.OUTPUT_LIMIT:
        return ExecutionState.FAILED, None, "output_limit"
    if execution.reason is ContainerStopReason.OUTPUT_FAILURE:
        return ExecutionState.FAILED, None, "output_failure"
    if execution.reason is ContainerStopReason.RUNTIME_FAILURE:
        return ExecutionState.FAILED, None, "runtime_failure"
    if execution.exit_code == 0:
        return ExecutionState.COMPLETED, 0, None
    return ExecutionState.FAILED, execution.exit_code, "nonzero_exit"


def _receipt(
    request: JobRequest,
    *,
    state: ExecutionState,
    finished_at: datetime,
    exit_code: int | None = None,
    started_at: datetime | None = None,
    stdout_bytes: int = 0,
    stderr_bytes: int = 0,
    output_truncated: bool = False,
    cleanup_complete: bool,
    failure_code: str | None = None,
) -> ExecutionReceipt:
    return ExecutionReceipt(
        job_id=request.spec.id,
        session_id=request.spec.session_id,
        correlation_id=request.spec.correlation_id,
        approval_id=request.approval.id,
        image_digest="sha256:" + request.spec.image.rsplit("@sha256:", 1)[1],
        policy_sha256=policy_digest(request.spec.policy),
        state=state,
        exit_code=exit_code,
        started_at=started_at,
        finished_at=finished_at,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        output_truncated=output_truncated,
        cleanup_complete=cleanup_complete,
        failure_code=failure_code,
    )


def _ensure_private_database(path: Path) -> None:
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ValueError("sandbox state parent must be a real directory")
    parent_info = path.parent.lstat()
    if parent_info.st_uid != os.geteuid() or parent_info.st_mode & 0o077:
        raise ValueError("sandbox state parent must be caller-owned and private")
    if not path.exists():
        fd = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.close(fd)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or path.is_symlink()
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("sandbox state database must be a private regular file")
