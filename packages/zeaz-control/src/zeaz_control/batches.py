"""Transactional Message Batch lifecycle with durable idempotency."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from pydantic import (
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from zeaz_control.files import FilePurpose, FileRecord
from zeaz_control.models import (
    ControlStore,
    Identifier,
    ProviderExtendedModel,
    StrictModel,
    _append_audit,
)

IdempotencyKey = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_IDEMPOTENCY_KEY_ADAPTER = TypeAdapter(IdempotencyKey)


class ControlBatchError(RuntimeError):
    """A sanitized batch operation or provider failure."""


class IdempotencyConflict(ControlBatchError):
    """An idempotency key was reused for a different operation payload."""


class BatchStatus(StrEnum):
    VALIDATING = "validating"
    IN_PROGRESS = "in_progress"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


_TERMINAL_BATCH_STATUSES = frozenset(
    {
        BatchStatus.COMPLETED,
        BatchStatus.FAILED,
        BatchStatus.EXPIRED,
        BatchStatus.CANCELLED,
    }
)


class BatchCounts(StrictModel):
    total: int = Field(ge=0, le=1_000_000)
    completed: int = Field(ge=0, le=1_000_000)
    failed: int = Field(ge=0, le=1_000_000)

    @model_validator(mode="after")
    def counts_are_coherent(self) -> BatchCounts:
        if self.completed + self.failed > self.total:
            raise ValueError("completed plus failed cannot exceed total")
        return self


class BatchSubmission(ProviderExtendedModel):
    schema_version: Literal["1"] = "1"
    account: Identifier = "default"
    input_file_id: Identifier
    endpoint: Literal[
        "/v1/responses",
        "/v1/chat/completions",
        "/v1/messages",
    ]
    completion_window: Literal["24h"] = "24h"
    metadata: dict[
        Annotated[str, StringConstraints(min_length=1, max_length=64)],
        Annotated[str, StringConstraints(max_length=512)],
    ] = Field(default_factory=dict, max_length=16)


class BatchRecord(ProviderExtendedModel):
    schema_version: Literal["1"] = "1"
    account: Identifier = "default"
    id: Identifier
    input_file_id: Identifier
    endpoint: str = Field(min_length=1, max_length=128)
    status: BatchStatus
    counts: BatchCounts
    output_file_id: Identifier | None = None
    error_file_id: Identifier | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("batch timestamps must include a UTC offset")
        return value

    @model_validator(mode="after")
    def timestamps_are_ordered(self) -> BatchRecord:
        if self.updated_at < self.created_at:
            raise ValueError("batch update cannot precede creation")
        return self


class BatchPage(StrictModel):
    items: tuple[BatchRecord, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=2048)


class BatchResult(StrictModel):
    schema_version: Literal["1"] = "1"
    batch_id: Identifier
    custom_id: Identifier
    status_code: int | None = Field(default=None, ge=100, le=599)
    response: JsonValue | None = None
    error_code: Annotated[
        str | None,
        StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"),
    ] = None

    @model_validator(mode="after")
    def response_or_error_is_exclusive(self) -> BatchResult:
        if (self.response is None) == (self.error_code is None):
            raise ValueError("batch result must contain exactly one response or error")
        if self.response is not None and self.status_code is None:
            raise ValueError("batch response requires an HTTP status")
        return self


class ProviderBatchAdapter(Protocol):
    provider: str
    account: str

    async def submit_batch(
        self,
        submission: BatchSubmission,
        *,
        idempotency_key: str,
    ) -> BatchRecord: ...

    async def list_batches(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> BatchPage: ...

    async def get_batch(self, batch_id: str) -> BatchRecord: ...

    async def cancel_batch(
        self,
        batch_id: str,
        *,
        idempotency_key: str,
    ) -> BatchRecord: ...

    def batch_results(self, batch: BatchRecord) -> AsyncIterator[BatchResult]: ...


class BatchService:
    def __init__(
        self,
        store: ControlStore,
        adapters: Mapping[str, ProviderBatchAdapter],
        *,
        max_result_count: int = 1_000_000,
        max_result_bytes: int = 1_073_741_824,
    ) -> None:
        self._store = store
        self._adapters = dict(adapters)
        if not self._adapters:
            raise ValueError("at least one batch adapter is required")
        if any(name != adapter.provider for name, adapter in self._adapters.items()):
            raise ValueError("batch adapter mapping does not match provider names")
        if not 1 <= max_result_count <= 10_000_000:
            raise ValueError("max_result_count is invalid")
        if not 1024 <= max_result_bytes <= 10_737_418_240:
            raise ValueError("max_result_bytes is invalid")
        self._max_result_count = max_result_count
        self._max_result_bytes = max_result_bytes
        with store._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS control_batches (
                    provider TEXT NOT NULL,
                    account TEXT NOT NULL,
                    id TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    PRIMARY KEY (provider, account, id)
                );
                CREATE TABLE IF NOT EXISTS control_idempotency (
                    operation TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    account TEXT NOT NULL,
                    key TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'completed')),
                    response_json BLOB,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY (operation, provider, account, key)
                );
                """
            )
            connection.commit()

    async def submit(
        self,
        submission: BatchSubmission,
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> BatchRecord:
        adapter = self._adapter(submission.provider)
        if adapter.account != submission.account:
            raise ControlBatchError("batch adapter account does not match")
        self._require_batch_file(submission)
        request_sha = _digest(submission.model_dump(mode="json"))
        cached = self._reserve(
            "batch.create",
            submission.provider,
            submission.account,
            idempotency_key,
            request_sha,
            now=now,
        )
        if cached is not None:
            return cached
        record = await adapter.submit_batch(
            submission,
            idempotency_key=idempotency_key,
        )
        _validate_record_scope(record, submission.provider, submission.account)
        if (
            record.input_file_id != submission.input_file_id
            or record.endpoint != submission.endpoint
        ):
            raise ControlBatchError("provider batch does not match its submission")
        self._complete(
            operation="batch.create",
            key=idempotency_key,
            request_sha=request_sha,
            record=record,
            event_type="control.batch.created",
            now=now,
        )
        return record

    async def cancel(
        self,
        provider: str,
        account: str,
        batch_id: str,
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> BatchRecord:
        previous = self.get(provider, account, batch_id)
        adapter = self._adapter(provider)
        if adapter.account != account:
            raise ControlBatchError("batch adapter account does not match")
        request = {"batch_id": batch_id}
        request_sha = _digest(request)
        cached = self._reserve(
            "batch.cancel",
            provider,
            account,
            idempotency_key,
            request_sha,
            now=now,
        )
        if cached is not None:
            return cached
        if previous.status in {
            BatchStatus.COMPLETED,
            BatchStatus.FAILED,
            BatchStatus.EXPIRED,
            BatchStatus.CANCELLED,
        }:
            record = previous
        else:
            record = await adapter.cancel_batch(
                batch_id,
                idempotency_key=idempotency_key,
            )
            _validate_record_scope(record, provider, account)
            if record.id != batch_id:
                raise ControlBatchError("provider cancelled a different batch")
        self._complete(
            operation="batch.cancel",
            key=idempotency_key,
            request_sha=request_sha,
            record=record,
            event_type="control.batch.cancelled",
            now=now,
        )
        return record

    async def refresh(
        self,
        provider: str,
        *,
        page_size: int = 100,
        max_pages: int = 1000,
        max_batches: int = 100_000,
        now: datetime | None = None,
    ) -> int:
        adapter = self._adapter(provider)
        if not 1 <= page_size <= 100 or not 1 <= max_pages <= 10_000:
            raise ValueError("batch pagination limits are invalid")
        if not page_size <= max_batches <= 1_000_000:
            raise ValueError("max_batches is invalid")
        cursor: str | None = None
        cursors: set[str] = set()
        records: list[BatchRecord] = []
        identifiers: set[str] = set()
        for _ in range(max_pages):
            page = await adapter.list_batches(cursor=cursor, limit=page_size)
            for record in page.items:
                _validate_record_scope(record, provider, adapter.account)
                if record.id in identifiers:
                    raise ControlBatchError(
                        "provider batch pagination returned a duplicate batch"
                    )
                identifiers.add(record.id)
                records.append(record)
                if len(records) > max_batches:
                    raise ControlBatchError("provider batch list exceeded its item limit")
            cursor = page.next_cursor
            if cursor is None:
                break
            if cursor in cursors:
                raise ControlBatchError("provider batch pagination repeated a cursor")
            cursors.add(cursor)
        else:
            raise ControlBatchError("provider batch list exceeded its page limit")
        created = now or datetime.now(UTC)
        with self._store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for record in records:
                _upsert_batch(connection, record)
            _append_audit(
                connection,
                event_id=uuid4(),
                event_type="control.batches.reconciled",
                subject_id=f"{provider}:{adapter.account}",
                details={"count": len(records)},
                created_at=created,
            )
            connection.commit()
        return len(records)

    async def retrieve(
        self,
        provider: str,
        account: str,
        batch_id: str,
        *,
        now: datetime | None = None,
    ) -> BatchRecord:
        adapter = self._adapter(provider)
        if adapter.account != account:
            raise ControlBatchError("batch adapter account does not match")
        record = await adapter.get_batch(batch_id)
        _validate_record_scope(record, provider, account)
        if record.id != batch_id:
            raise ControlBatchError("provider returned a different batch")
        created = now or datetime.now(UTC)
        with self._store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _upsert_batch(connection, record)
            _append_audit(
                connection,
                event_id=uuid4(),
                event_type="control.batch.refreshed",
                subject_id=f"{provider}:{account}:{batch_id}",
                details={"status": record.status.value},
                created_at=created,
            )
            connection.commit()
        return record

    def list(
        self,
        provider: str,
        account: str,
        *,
        after: str | None = None,
        limit: int = 20,
    ) -> BatchPage:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        query = (
            "SELECT id, payload FROM control_batches "
            "WHERE provider = ? AND account = ?"
        )
        values: list[str | int] = [provider, account]
        if after is not None:
            query += " AND id > ?"
            values.append(after)
        query += " ORDER BY id LIMIT ?"
        values.append(limit + 1)
        with self._store._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        try:
            items = tuple(BatchRecord.model_validate_json(row[1]) for row in rows)
        except Exception as exc:
            raise ControlBatchError("stored batch record is invalid") from exc
        return BatchPage(
            items=items,
            next_cursor=rows[-1][0] if more and rows else None,
        )

    def get(self, provider: str, account: str, batch_id: str) -> BatchRecord:
        with self._store._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM control_batches "
                "WHERE provider = ? AND account = ? AND id = ?",
                (provider, account, batch_id),
            ).fetchone()
        if row is None:
            raise ControlBatchError("batch was not found")
        try:
            return BatchRecord.model_validate_json(row[0])
        except Exception as exc:
            raise ControlBatchError("stored batch record is invalid") from exc

    async def results(
        self,
        provider: str,
        account: str,
        batch_id: str,
    ) -> AsyncIterator[BatchResult]:
        record = self.get(provider, account, batch_id)
        adapter = self._adapter(provider)
        if adapter.account != account:
            raise ControlBatchError("batch adapter account does not match")
        count = total_bytes = 0
        seen: set[str] = set()
        async for result in adapter.batch_results(record):
            if result.batch_id != batch_id:
                raise ControlBatchError("provider returned a result for another batch")
            if result.custom_id in seen:
                raise ControlBatchError("provider returned a duplicate batch result")
            seen.add(result.custom_id)
            count += 1
            total_bytes += len(result.model_dump_json().encode())
            if count > self._max_result_count or total_bytes > self._max_result_bytes:
                raise ControlBatchError("batch results exceeded their configured limit")
            yield result

    def _reserve(
        self,
        operation: str,
        provider: str,
        account: str,
        key: str,
        request_sha: str,
        *,
        now: datetime | None,
    ) -> BatchRecord | None:
        try:
            key = _IDEMPOTENCY_KEY_ADAPTER.validate_python(key)
        except ValidationError as exc:
            raise ControlBatchError("idempotency key is invalid") from exc
        created = now or datetime.now(UTC)
        with self._store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_sha256, state, response_json "
                "FROM control_idempotency "
                "WHERE operation = ? AND provider = ? AND account = ? AND key = ?",
                (operation, provider, account, key),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO control_idempotency "
                    "(operation, provider, account, key, request_sha256, state, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (
                        operation,
                        provider,
                        account,
                        key,
                        request_sha,
                        created.isoformat(),
                    ),
                )
                connection.commit()
                return None
            connection.rollback()
        if row[0] != request_sha:
            raise IdempotencyConflict("idempotency key payload does not match")
        if row[1] == "completed":
            try:
                return BatchRecord.model_validate_json(row[2])
            except Exception as exc:
                raise ControlBatchError("stored idempotency response is invalid") from exc
        return None

    def _complete(
        self,
        *,
        operation: str,
        key: str,
        request_sha: str,
        record: BatchRecord,
        event_type: str,
        now: datetime | None,
    ) -> None:
        completed = now or datetime.now(UTC)
        with self._store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_sha256, state FROM control_idempotency "
                "WHERE operation = ? AND provider = ? AND account = ? AND key = ?",
                (operation, record.provider, record.account, key),
            ).fetchone()
            if row is None or row[0] != request_sha:
                connection.rollback()
                raise IdempotencyConflict("idempotency reservation changed")
            if row[1] == "completed":
                connection.rollback()
                return
            _upsert_batch(connection, record)
            connection.execute(
                "UPDATE control_idempotency SET state = 'completed', "
                "response_json = ?, completed_at = ? "
                "WHERE operation = ? AND provider = ? AND account = ? AND key = ?",
                (
                    record.model_dump_json().encode(),
                    completed.isoformat(),
                    operation,
                    record.provider,
                    record.account,
                    key,
                ),
            )
            _append_audit(
                connection,
                event_id=uuid4(),
                event_type=event_type,
                subject_id=f"{record.provider}:{record.account}:{record.id}",
                details={"idempotency_key_sha256": hashlib.sha256(key.encode()).hexdigest()},
                created_at=completed,
            )
            connection.commit()

    def _require_batch_file(self, submission: BatchSubmission) -> FileRecord:
        with self._store._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM control_files "
                "WHERE provider = ? AND account = ? AND id = ?",
                (
                    submission.provider,
                    submission.account,
                    submission.input_file_id,
                ),
            ).fetchone()
        if row is None:
            raise ControlBatchError("batch input file was not found")
        try:
            record = FileRecord.model_validate_json(row[0])
        except Exception as exc:
            raise ControlBatchError("stored batch input file is invalid") from exc
        if record.purpose is not FilePurpose.BATCH:
            raise ControlBatchError("batch input file has the wrong purpose")
        return record

    def _adapter(self, provider: str) -> ProviderBatchAdapter:
        try:
            return self._adapters[provider]
        except KeyError as exc:
            raise ControlBatchError("batch provider is not configured") from exc


def _validate_record_scope(record: BatchRecord, provider: str, account: str) -> None:
    if (record.provider, record.account) != (provider, account):
        raise ControlBatchError("provider batch scope does not match")


def _upsert_batch(connection, record: BatchRecord) -> None:
    row = connection.execute(
        "SELECT payload FROM control_batches "
        "WHERE provider = ? AND account = ? AND id = ?",
        (record.provider, record.account, record.id),
    ).fetchone()
    if row is not None:
        try:
            previous = BatchRecord.model_validate_json(row[0])
        except Exception as exc:
            raise ControlBatchError("stored batch record is invalid") from exc
        if record.updated_at < previous.updated_at:
            raise ControlBatchError("provider returned a stale batch update")
        if (
            previous.status in _TERMINAL_BATCH_STATUSES
            and record.status is not previous.status
        ):
            raise ControlBatchError("provider batch regressed from a terminal status")
        if (
            record.counts.completed < previous.counts.completed
            or record.counts.failed < previous.counts.failed
        ):
            raise ControlBatchError("provider batch counts regressed")
    connection.execute(
        "INSERT INTO control_batches (provider, account, id, payload) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(provider, account, id) DO UPDATE SET payload = excluded.payload",
        (
            record.provider,
            record.account,
            record.id,
            record.model_dump_json().encode(),
        ),
    )


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
