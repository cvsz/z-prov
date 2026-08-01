"""SQLite-backed resumable sessions with optimistic concurrency."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from zeaz_agent.schemas import Session


class SessionStoreError(RuntimeError):
    """Base class for safe session repository failures."""


class SessionNotFound(SessionStoreError):
    pass


class SessionConflict(SessionStoreError):
    pass


class SessionIntegrityError(SessionStoreError):
    pass


class SessionStore(Protocol):
    def create(self, session: Session) -> None: ...

    def load(self, session_id: UUID) -> Session: ...

    def save(self, session: Session, *, expected_revision: int) -> None: ...


class SQLiteSessionStore:
    def __init__(
        self,
        path: Path,
        *,
        max_session_bytes: int = 16_777_216,
        timeout_seconds: float = 5,
    ) -> None:
        if not 1024 <= max_session_bytes <= 67_108_864:
            raise ValueError("max_session_bytes must be between 1 KiB and 64 MiB")
        if not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 0 and 60")
        if not path.parent.is_dir():
            raise ValueError("session database parent directory must exist")
        self._path = path
        self._max_session_bytes = max_session_bytes
        self._timeout = timeout_seconds
        self._prepare_file()
        self._initialize()

    def create(self, session: Session) -> None:
        document = self._encode(session)
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO sessions(session_id, revision, document) VALUES (?, ?, ?)",
                    (str(session.id), session.revision, document),
                )
        except sqlite3.IntegrityError as exc:
            raise SessionConflict("session already exists") from exc

    def load(self, session_id: UUID) -> Session:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT revision, document FROM sessions WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
        if row is None:
            raise SessionNotFound("session does not exist")
        revision, document = row
        if len(document.encode()) > self._max_session_bytes:
            raise SessionIntegrityError("stored session exceeds the size limit")
        try:
            session = Session.model_validate_json(document)
        except ValidationError as exc:
            raise SessionIntegrityError("stored session failed schema validation") from exc
        if session.id != session_id or session.revision != revision:
            raise SessionIntegrityError("stored session identity or revision is inconsistent")
        return session

    def save(self, session: Session, *, expected_revision: int) -> None:
        if expected_revision < 0:
            raise ValueError("expected_revision cannot be negative")
        if session.revision <= expected_revision:
            raise ValueError("new session revision must exceed expected_revision")
        document = self._encode(session)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE sessions
                SET revision = ?, document = ?
                WHERE session_id = ? AND revision = ?
                """,
                (session.revision, document, str(session.id), expected_revision),
            )
            if cursor.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ?",
                    (str(session.id),),
                ).fetchone()
                connection.rollback()
                if exists is None:
                    raise SessionNotFound("session does not exist")
                raise SessionConflict("session revision changed concurrently")
            connection.commit()

    def _encode(self, session: Session) -> str:
        document = session.model_dump_json()
        if len(document.encode()) > self._max_session_bytes:
            raise ValueError("session exceeds the configured size limit")
        return document

    def _prepare_file(self) -> None:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self._path, flags, 0o600)
        except OSError as exc:
            raise SessionIntegrityError("unable to open session database safely") from exc
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
            ):
                raise SessionIntegrityError(
                    "session database must be an owner-only regular file"
                )
        finally:
            os.close(fd)

    def _initialize(self) -> None:
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise SessionIntegrityError("unsupported session database schema version")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 0),
                    document TEXT NOT NULL
                ) WITHOUT ROWID
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
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        return connection

    def _validate_file(self) -> None:
        try:
            info = self._path.lstat()
        except OSError as exc:
            raise SessionIntegrityError("session database is unavailable") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise SessionIntegrityError("session database must be an owner-only regular file")
