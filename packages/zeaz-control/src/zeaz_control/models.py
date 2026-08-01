"""Transactional provider model discovery and lifecycle reconciliation."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$"),
]
ProviderName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$"),
]
Cursor = Annotated[str, StringConstraints(min_length=1, max_length=2048)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ExtensionField = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,63}$"),
]
ProviderExtensions = dict[
    ProviderName,
    dict[ExtensionField, JsonValue],
]


class ProviderExtendedModel(StrictModel):
    provider: ProviderName
    extensions: ProviderExtensions = Field(default_factory=dict, max_length=8)

    @model_validator(mode="after")
    def extensions_match_provider_namespace(self) -> ProviderExtendedModel:
        if any(namespace != self.provider for namespace in self.extensions):
            raise ValueError("extensions must use the record provider namespace")
        return self


class ModelLifecycle(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETIRED = "retired"
    UNKNOWN = "unknown"


class DiscoveredModel(ProviderExtendedModel):
    schema_version: Literal["1"] = "1"
    account: Identifier = "default"
    region: Identifier = "global"
    model: Identifier
    lifecycle: ModelLifecycle = ModelLifecycle.UNKNOWN
    capabilities: dict[str, JsonValue] = Field(default_factory=dict)
    source: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def observed_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a UTC offset")
        return value


class ModelPage(StrictModel):
    items: tuple[DiscoveredModel, ...] = Field(max_length=1000)
    next_cursor: Cursor | None = None


class ProviderModelAdapter(Protocol):
    provider: str
    account: str
    region: str

    async def list_models(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> ModelPage: ...


class ModelRecord(DiscoveredModel):
    revision: int = Field(ge=1)
    missing_observations: int = Field(ge=0)
    reconciled_at: datetime

    @field_validator("reconciled_at")
    @classmethod
    def reconciled_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reconciled_at must include a UTC offset")
        return value


class ModelReconciliation(StrictModel):
    id: UUID
    provider: ProviderName
    account: Identifier
    region: Identifier
    discovered: int = Field(ge=0)
    created: int = Field(ge=0)
    updated: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    missing: int = Field(ge=0)
    retired: int = Field(ge=0)
    observed_at: datetime


class ControlAuditEvent(StrictModel):
    id: UUID
    sequence: int = Field(ge=0)
    event_type: Annotated[
        str,
        StringConstraints(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"),
    ]
    subject_id: str
    details: dict[str, JsonValue]
    created_at: datetime


class ControlStore:
    """Private SQLite state with model and audit writes in one transaction."""

    def __init__(self, path: Path) -> None:
        _ensure_private_database(path)
        self._path = path
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS models (
                    provider TEXT NOT NULL,
                    account TEXT NOT NULL,
                    region TEXT NOT NULL,
                    model TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    PRIMARY KEY (provider, account, region, model)
                );
                CREATE TABLE IF NOT EXISTS control_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    details_json BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
        os.chmod(path, 0o600)

    def models(
        self,
        *,
        provider: str | None = None,
        account: str | None = None,
        region: str | None = None,
    ) -> tuple[ModelRecord, ...]:
        clauses: list[str] = []
        values: list[str] = []
        for column, value in (
            ("provider", provider),
            ("account", account),
            ("region", region),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        query = "SELECT payload FROM models"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY provider, account, region, model"
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        try:
            return tuple(ModelRecord.model_validate_json(row[0]) for row in rows)
        except Exception as exc:
            raise RuntimeError("stored model record is invalid") from exc

    def audit(self) -> tuple[ControlAuditEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, id, event_type, subject_id, details_json, created_at "
                "FROM control_audit ORDER BY sequence"
            ).fetchall()
        try:
            return tuple(
                ControlAuditEvent(
                    sequence=row[0] - 1,
                    id=UUID(row[1]),
                    event_type=row[2],
                    subject_id=row[3],
                    details=json.loads(row[4]),
                    created_at=datetime.fromisoformat(row[5]),
                )
                for row in rows
            )
        except Exception as exc:
            raise RuntimeError("stored control audit event is invalid") from exc

    def reconcile_models(
        self,
        discovered: Sequence[DiscoveredModel],
        *,
        provider: str,
        account: str,
        region: str,
        retire_after_missing: int,
        reconciled_at: datetime,
    ) -> ModelReconciliation:
        if not 1 <= retire_after_missing <= 100:
            raise ValueError("retire_after_missing must be between 1 and 100")
        if reconciled_at.tzinfo is None or reconciled_at.utcoffset() is None:
            raise ValueError("reconciled_at must include a UTC offset")
        incoming = {item.model: item for item in discovered}
        if len(incoming) != len(discovered):
            raise ValueError("discovery contains duplicate model IDs")
        if any(
            (item.provider, item.account, item.region)
            != (provider, account, region)
            for item in discovered
        ):
            raise ValueError("discovery scope does not match its adapter")

        created = updated = unchanged = missing = retired = 0
        reconciliation_id = uuid4()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT model, payload FROM models "
                "WHERE provider = ? AND account = ? AND region = ?",
                (provider, account, region),
            ).fetchall()
            try:
                existing = {
                    row[0]: ModelRecord.model_validate_json(row[1])
                    for row in rows
                }
            except Exception as exc:
                connection.rollback()
                raise RuntimeError("stored model record is invalid") from exc

            for model, item in incoming.items():
                previous = existing.get(model)
                revision = 1 if previous is None else previous.revision
                comparable = item.model_dump(mode="json")
                if previous is None:
                    created += 1
                else:
                    previous_comparable = previous.model_dump(
                        mode="json",
                        exclude={
                            "revision",
                            "missing_observations",
                            "reconciled_at",
                            "observed_at",
                        },
                    )
                    comparable.pop("observed_at", None)
                    if (
                        previous_comparable == comparable
                        and previous.missing_observations == 0
                    ):
                        unchanged += 1
                    else:
                        updated += 1
                        revision += 1
                record = ModelRecord(
                    **item.model_dump(),
                    revision=revision,
                    missing_observations=0,
                    reconciled_at=reconciled_at,
                )
                connection.execute(
                    "INSERT INTO models (provider, account, region, model, payload) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(provider, account, region, model) DO UPDATE SET "
                    "payload = excluded.payload",
                    (
                        provider,
                        account,
                        region,
                        model,
                        record.model_dump_json().encode(),
                    ),
                )

            for model, previous in existing.items():
                if model in incoming:
                    continue
                missing += 1
                missing_count = previous.missing_observations + 1
                lifecycle = previous.lifecycle
                revision = previous.revision + 1
                if (
                    missing_count >= retire_after_missing
                    and lifecycle is not ModelLifecycle.RETIRED
                ):
                    lifecycle = ModelLifecycle.RETIRED
                    retired += 1
                record = previous.model_copy(
                    update={
                        "lifecycle": lifecycle,
                        "missing_observations": missing_count,
                        "revision": revision,
                        "reconciled_at": reconciled_at,
                    }
                )
                connection.execute(
                    "UPDATE models SET payload = ? "
                    "WHERE provider = ? AND account = ? AND region = ? AND model = ?",
                    (
                        record.model_dump_json().encode(),
                        provider,
                        account,
                        region,
                        model,
                    ),
                )

            details = {
                "discovered": len(incoming),
                "created": created,
                "updated": updated,
                "unchanged": unchanged,
                "missing": missing,
                "retired": retired,
            }
            _append_audit(
                connection,
                event_id=reconciliation_id,
                event_type="control.models.reconciled",
                subject_id=f"{provider}:{account}:{region}",
                details=details,
                created_at=reconciled_at,
            )
            connection.commit()
        return ModelReconciliation(
            id=reconciliation_id,
            provider=provider,
            account=account,
            region=region,
            observed_at=reconciled_at,
            **details,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection


class ModelReconciler:
    def __init__(
        self,
        store: ControlStore,
        *,
        page_size: int = 100,
        max_pages: int = 1000,
        max_models: int = 100_000,
        retire_after_missing: int = 2,
    ) -> None:
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be between 1 and 1000")
        if not 1 <= max_pages <= 10_000:
            raise ValueError("max_pages must be between 1 and 10000")
        if not page_size <= max_models <= 1_000_000:
            raise ValueError("max_models must be between one page and 1000000")
        if not 1 <= retire_after_missing <= 100:
            raise ValueError("retire_after_missing must be between 1 and 100")
        self._store = store
        self._page_size = page_size
        self._max_pages = max_pages
        self._max_models = max_models
        self._retire_after_missing = retire_after_missing

    async def refresh(
        self,
        adapter: ProviderModelAdapter,
        *,
        now: datetime | None = None,
    ) -> ModelReconciliation:
        observed = now or datetime.now(UTC)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        models: list[DiscoveredModel] = []
        seen_models: set[str] = set()
        for _ in range(self._max_pages):
            page = await adapter.list_models(cursor=cursor, limit=self._page_size)
            for item in page.items:
                if item.model in seen_models:
                    raise RuntimeError("provider model pagination returned a duplicate model")
                seen_models.add(item.model)
                models.append(item)
                if len(models) > self._max_models:
                    raise RuntimeError("provider model discovery exceeded its item limit")
            cursor = page.next_cursor
            if cursor is None:
                break
            if cursor in seen_cursors:
                raise RuntimeError("provider model pagination repeated a cursor")
            seen_cursors.add(cursor)
        else:
            raise RuntimeError("provider model discovery exceeded its page limit")
        return self._store.reconcile_models(
            models,
            provider=adapter.provider,
            account=adapter.account,
            region=adapter.region,
            retire_after_missing=self._retire_after_missing,
            reconciled_at=observed,
        )


def _append_audit(
    connection: sqlite3.Connection,
    *,
    event_id: UUID,
    event_type: str,
    subject_id: str,
    details: dict[str, JsonValue],
    created_at: datetime,
) -> None:
    connection.execute(
        "INSERT INTO control_audit "
        "(id, event_type, subject_id, details_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            str(event_id),
            event_type,
            subject_id,
            json.dumps(details, separators=(",", ":"), sort_keys=True),
            created_at.isoformat(),
        ),
    )


def _ensure_private_database(path: Path) -> None:
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ValueError("control state parent must be a real directory")
    parent = path.parent.lstat()
    if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
        raise ValueError("control state parent must be caller-owned and private")
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
        raise ValueError("control database must be a private regular file")
