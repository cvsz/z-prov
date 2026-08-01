"""Scoped memory interface and bounded local SQLite implementation."""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Protocol
from uuid import UUID, uuid4

from pydantic import (
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from zeaz_agent.schemas import Identifier, StrictModel, utc_now

MemoryContent = Annotated[str, StringConstraints(min_length=1, max_length=65_536)]
MemoryTag = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"),
]


class MemoryKind(StrEnum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PREFERENCE = "preference"


class MemoryRecord(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    namespace: Identifier
    key: Identifier
    kind: MemoryKind
    content: MemoryContent
    tags: tuple[MemoryTag, ...] = Field(default_factory=tuple, max_length=64)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("memory timestamps must include a UTC offset")
        return value

    @model_validator(mode="after")
    def validate_record(self) -> MemoryRecord:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("memory tags must be unique")
        return self


class MemoryStoreError(RuntimeError):
    pass


class MemoryNotFound(MemoryStoreError):
    pass


class MemoryConflict(MemoryStoreError):
    pass


class MemoryIntegrityError(MemoryStoreError):
    pass


class MemoryStore(Protocol):
    def put(self, record: MemoryRecord, *, expected_revision: int | None = None) -> None: ...

    def get(self, namespace: str, memory_id: UUID) -> MemoryRecord: ...

    def search(self, namespace: str, query: str, *, limit: int = 20) -> tuple[MemoryRecord, ...]: ...


class SQLiteMemoryStore:
    def __init__(
        self,
        path: Path,
        *,
        max_record_bytes: int = 131_072,
        timeout_seconds: float = 5,
    ) -> None:
        if not 1024 <= max_record_bytes <= 1_048_576:
            raise ValueError("max_record_bytes must be between 1 KiB and 1 MiB")
        if not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 0 and 60")
        if not path.parent.is_dir():
            raise ValueError("memory database parent directory must exist")
        self._path = path
        self._max_record_bytes = max_record_bytes
        self._timeout = timeout_seconds
        self._prepare_file()
        self._initialize()

    def put(self, record: MemoryRecord, *, expected_revision: int | None = None) -> None:
        document = self._encode(record)
        if expected_revision is None:
            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO memories(
                            namespace, memory_id, memory_key, revision,
                            content, document, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.namespace,
                            str(record.id),
                            record.key,
                            record.revision,
                            record.content,
                            document,
                            record.updated_at.isoformat(),
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise MemoryConflict("memory already exists") from exc
            return
        if expected_revision < 0:
            raise ValueError("expected_revision cannot be negative")
        if record.revision <= expected_revision:
            raise ValueError("new memory revision must exceed expected_revision")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE memories
                SET memory_key = ?, revision = ?, content = ?, document = ?, updated_at = ?
                WHERE namespace = ? AND memory_id = ? AND revision = ?
                """,
                (
                    record.key,
                    record.revision,
                    record.content,
                    document,
                    record.updated_at.isoformat(),
                    record.namespace,
                    str(record.id),
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM memories WHERE namespace = ? AND memory_id = ?",
                    (record.namespace, str(record.id)),
                ).fetchone()
                connection.rollback()
                if exists is None:
                    raise MemoryNotFound("memory does not exist in this namespace")
                raise MemoryConflict("memory revision changed concurrently")
            connection.commit()

    def get(self, namespace: str, memory_id: UUID) -> MemoryRecord:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT revision, content, document
                FROM memories
                WHERE namespace = ? AND memory_id = ?
                """,
                (namespace, str(memory_id)),
            ).fetchone()
        if row is None:
            raise MemoryNotFound("memory does not exist in this namespace")
        return self._decode(namespace, memory_id, *row)

    def search(
        self,
        namespace: str,
        query: str,
        *,
        limit: int = 20,
    ) -> tuple[MemoryRecord, ...]:
        if not 1 <= len(query) <= 256:
            raise ValueError("memory query must contain between 1 and 256 characters")
        if not 1 <= limit <= 100:
            raise ValueError("memory search limit must be between 1 and 100")
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT memory_id, revision, content, document
                FROM memories
                WHERE namespace = ?
                  AND (content LIKE ? ESCAPE '\\' OR memory_key LIKE ? ESCAPE '\\')
                ORDER BY updated_at DESC, memory_id ASC
                LIMIT ?
                """,
                (namespace, pattern, pattern, limit),
            ).fetchall()
        return tuple(
            self._decode(namespace, UUID(memory_id), revision, content, document)
            for memory_id, revision, content, document in rows
        )

    def _encode(self, record: MemoryRecord) -> str:
        document = record.model_dump_json()
        if len(document.encode()) > self._max_record_bytes:
            raise ValueError("memory record exceeds the configured size limit")
        return document

    def _decode(
        self,
        namespace: str,
        memory_id: UUID,
        revision: int,
        content: str,
        document: str,
    ) -> MemoryRecord:
        if len(document.encode()) > self._max_record_bytes:
            raise MemoryIntegrityError("stored memory exceeds the size limit")
        try:
            record = MemoryRecord.model_validate_json(document)
        except Exception as exc:
            raise MemoryIntegrityError("stored memory failed schema validation") from exc
        if (
            record.namespace != namespace
            or record.id != memory_id
            or record.revision != revision
            or record.content != content
        ):
            raise MemoryIntegrityError("stored memory indexes are inconsistent")
        return record

    def _prepare_file(self) -> None:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self._path, flags, 0o600)
        except OSError as exc:
            raise MemoryIntegrityError("unable to open memory database safely") from exc
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
            ):
                raise MemoryIntegrityError("memory database must be an owner-only regular file")
        finally:
            os.close(fd)

    def _initialize(self) -> None:
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise MemoryIntegrityError("unsupported memory database schema version")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    namespace TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    memory_key TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 0),
                    content TEXT NOT NULL,
                    document TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (namespace, memory_id)
                ) WITHOUT ROWID
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS memories_search_order
                ON memories(namespace, updated_at DESC, memory_id)
                """
            )
            connection.execute("PRAGMA user_version=1")

    def _connect(self) -> sqlite3.Connection:
        self._validate_file()
        connection = sqlite3.connect(
            self._path,
            timeout=self._timeout,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout={int(self._timeout * 1000)}")
        connection.execute("PRAGMA trusted_schema=OFF")
        return connection

    def _validate_file(self) -> None:
        try:
            info = self._path.lstat()
        except OSError as exc:
            raise MemoryIntegrityError("memory database is unavailable") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise MemoryIntegrityError("memory database must be an owner-only regular file")
