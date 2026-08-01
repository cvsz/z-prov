"""Bounded file upload, catalog, download, and deletion orchestration."""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from pydantic import Field, StringConstraints, field_validator

from zeaz_control.models import (
    ControlStore,
    Identifier,
    ProviderExtendedModel,
    StrictModel,
    _append_audit,
)

MediaType = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$",
        max_length=127,
    ),
]


class ControlFileError(RuntimeError):
    """A sanitized local or provider file operation failure."""


class FilePurpose(StrEnum):
    BATCH = "batch"
    FINE_TUNE = "fine_tune"
    VISION = "vision"
    ASSISTANTS = "assistants"
    USER_DATA = "user_data"
    EVALS = "evals"


class FileRecord(ProviderExtendedModel):
    schema_version: Literal["1"] = "1"
    account: Identifier = "default"
    id: Identifier
    filename: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    bytes: int = Field(ge=0, le=1_073_741_824)
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    media_type: MediaType
    purpose: FilePurpose
    created_at: datetime

    @field_validator("filename")
    @classmethod
    def filename_is_basename(cls, value: str) -> str:
        return validate_filename(value)

    @field_validator("created_at")
    @classmethod
    def created_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a UTC offset")
        return value


class FilePage(StrictModel):
    items: tuple[FileRecord, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=256)


class ProviderFileAdapter(Protocol):
    provider: str
    account: str

    async def upload_file(
        self,
        path: Path,
        *,
        filename: str,
        media_type: str,
        purpose: FilePurpose,
        sha256: str,
    ) -> FileRecord: ...

    def download_file(self, file_id: str) -> AsyncIterator[bytes]: ...

    async def delete_file(self, file_id: str) -> None: ...


class FilePolicy(StrictModel):
    max_upload_bytes: int = Field(default=209_715_200, ge=1024, le=1_073_741_824)
    max_download_bytes: int = Field(default=536_870_912, ge=1024, le=1_073_741_824)
    max_chunk_bytes: int = Field(default=1_048_576, ge=1024, le=4_194_304)
    max_jsonl_line_bytes: int = Field(default=1_048_576, ge=128, le=16_777_216)
    max_jsonl_lines: int = Field(default=50_000, ge=1, le=1_000_000)


_PURPOSE_MEDIA: Mapping[FilePurpose, frozenset[str]] = {
    FilePurpose.BATCH: frozenset({"application/jsonl"}),
    FilePurpose.FINE_TUNE: frozenset({"application/jsonl"}),
    FilePurpose.EVALS: frozenset({"application/jsonl"}),
    FilePurpose.VISION: frozenset(
        {"image/jpeg", "image/png", "image/gif", "image/webp"}
    ),
    FilePurpose.ASSISTANTS: frozenset(
        {
            "application/pdf",
            "application/jsonl",
            "text/plain",
            "image/jpeg",
            "image/png",
            "image/gif",
            "image/webp",
        }
    ),
    FilePurpose.USER_DATA: frozenset(
        {
            "application/octet-stream",
            "application/pdf",
            "application/jsonl",
            "text/plain",
            "image/jpeg",
            "image/png",
            "image/gif",
            "image/webp",
        }
    ),
}


class ControlFileService:
    def __init__(
        self,
        store: ControlStore,
        adapters: Mapping[str, ProviderFileAdapter],
        *,
        policy: FilePolicy | None = None,
    ) -> None:
        self._store = store
        self._adapters = dict(adapters)
        if not self._adapters:
            raise ValueError("at least one file adapter is required")
        if any(name != adapter.provider for name, adapter in self._adapters.items()):
            raise ValueError("file adapter mapping does not match provider names")
        self._policy = policy or FilePolicy()
        self._staging = store._path.parent / "file-staging"
        _ensure_private_directory(self._staging)
        with store._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS control_files (
                    provider TEXT NOT NULL,
                    account TEXT NOT NULL,
                    id TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    PRIMARY KEY (provider, account, id)
                )
                """
            )
            connection.commit()

    async def upload(
        self,
        provider: str,
        source: AsyncIterable[bytes],
        *,
        filename: str,
        media_type: str,
        purpose: FilePurpose,
        now: datetime | None = None,
    ) -> FileRecord:
        adapter = self._adapter(provider)
        normalized_filename = validate_filename(filename)
        _require_media_type(purpose, media_type)
        staging_path: Path | None = None
        try:
            staging_path, size, digest = await _spool_upload(
                source,
                self._staging,
                maximum=self._policy.max_upload_bytes,
                max_chunk=self._policy.max_chunk_bytes,
            )
            _validate_content(
                staging_path,
                filename=normalized_filename,
                media_type=media_type,
                purpose=purpose,
                policy=self._policy,
            )
            record = await adapter.upload_file(
                staging_path,
                filename=normalized_filename,
                media_type=media_type,
                purpose=purpose,
                sha256=digest,
            )
            if (
                record.provider != provider
                or record.account != adapter.account
                or record.filename != normalized_filename
                or record.bytes != size
                or record.sha256 != digest
                or record.media_type != media_type
                or record.purpose is not purpose
            ):
                raise ControlFileError(
                    "provider file metadata does not match the validated upload"
                )
            self._put_record(record, now=now)
            return record
        finally:
            if staging_path is not None:
                try:
                    staging_path.unlink()
                except FileNotFoundError:
                    pass

    def list(
        self,
        *,
        provider: str,
        account: str,
        after: str | None = None,
        limit: int = 20,
    ) -> FilePage:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        query = (
            "SELECT id, payload FROM control_files "
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
        has_more = len(rows) > limit
        rows = rows[:limit]
        try:
            items = tuple(FileRecord.model_validate_json(row[1]) for row in rows)
        except Exception as exc:
            raise ControlFileError("stored file metadata is invalid") from exc
        return FilePage(
            items=items,
            next_cursor=rows[-1][0] if has_more and rows else None,
        )

    def get(self, provider: str, account: str, file_id: str) -> FileRecord:
        with self._store._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM control_files "
                "WHERE provider = ? AND account = ? AND id = ?",
                (provider, account, file_id),
            ).fetchone()
        if row is None:
            raise ControlFileError("file was not found")
        try:
            return FileRecord.model_validate_json(row[0])
        except Exception as exc:
            raise ControlFileError("stored file metadata is invalid") from exc

    async def download(
        self,
        provider: str,
        account: str,
        file_id: str,
    ) -> AsyncIterator[bytes]:
        record = self.get(provider, account, file_id)
        adapter = self._adapter(provider)
        if adapter.account != account:
            raise ControlFileError("file adapter account does not match")
        total = 0
        digest = hashlib.sha256()
        async for chunk in adapter.download_file(file_id):
            if not isinstance(chunk, bytes) or not chunk:
                raise ControlFileError("provider file download yielded an invalid chunk")
            if len(chunk) > self._policy.max_chunk_bytes:
                raise ControlFileError("provider file download chunk is excessive")
            total += len(chunk)
            if total > min(record.bytes, self._policy.max_download_bytes):
                raise ControlFileError("provider file download exceeded its byte limit")
            digest.update(chunk)
            yield chunk
        if total != record.bytes or digest.hexdigest() != record.sha256:
            raise ControlFileError("provider file download failed its integrity check")

    async def delete(
        self,
        provider: str,
        account: str,
        file_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        self.get(provider, account, file_id)
        adapter = self._adapter(provider)
        if adapter.account != account:
            raise ControlFileError("file adapter account does not match")
        await adapter.delete_file(file_id)
        created = now or datetime.now(UTC)
        with self._store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM control_files "
                "WHERE provider = ? AND account = ? AND id = ?",
                (provider, account, file_id),
            )
            _append_audit(
                connection,
                event_id=uuid4(),
                event_type="control.file.deleted",
                subject_id=f"{provider}:{account}:{file_id}",
                details={},
                created_at=created,
            )
            connection.commit()

    def _put_record(self, record: FileRecord, *, now: datetime | None) -> None:
        created = now or datetime.now(UTC)
        with self._store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO control_files (provider, account, id, payload) "
                "VALUES (?, ?, ?, ?)",
                (
                    record.provider,
                    record.account,
                    record.id,
                    record.model_dump_json().encode(),
                ),
            )
            _append_audit(
                connection,
                event_id=uuid4(),
                event_type="control.file.created",
                subject_id=f"{record.provider}:{record.account}:{record.id}",
                details={
                    "bytes": record.bytes,
                    "media_type": record.media_type,
                    "purpose": record.purpose.value,
                    "sha256": record.sha256,
                },
                created_at=created,
            )
            connection.commit()

    def _adapter(self, provider: str) -> ProviderFileAdapter:
        try:
            return self._adapters[provider]
        except KeyError as exc:
            raise ControlFileError("file provider is not configured") from exc


def validate_filename(value: str) -> str:
    if (
        value in {".", ".."}
        or value.startswith(".")
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or len(value.encode("utf-8")) > 255
    ):
        raise ValueError("filename must be a portable visible basename")
    return value


def _require_media_type(purpose: FilePurpose, media_type: str) -> None:
    if media_type not in _PURPOSE_MEDIA[purpose]:
        raise ValueError("media type is not allowed for this file purpose")


async def _spool_upload(
    source: AsyncIterable[bytes],
    directory: Path,
    *,
    maximum: int,
    max_chunk: int,
) -> tuple[Path, int, str]:
    fd, raw_path = tempfile.mkstemp(prefix=".upload-", dir=directory)
    path = Path(raw_path)
    os.chmod(path, 0o600)
    total = 0
    digest = hashlib.sha256()
    try:
        async for chunk in source:
            if not isinstance(chunk, bytes) or not chunk:
                raise ControlFileError("upload yielded an invalid chunk")
            if len(chunk) > max_chunk:
                raise ControlFileError("upload chunk exceeds its byte limit")
            total += len(chunk)
            if total > maximum:
                raise ControlFileError("upload exceeds its byte limit")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("upload write made no progress")
                view = view[written:]
        if total == 0:
            raise ControlFileError("upload cannot be empty")
        os.fsync(fd)
    except Exception:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    os.close(fd)
    return path, total, digest.hexdigest()


def _validate_content(
    path: Path,
    *,
    filename: str,
    media_type: str,
    purpose: FilePurpose,
    policy: FilePolicy,
) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ControlFileError("staged upload is not a regular file")
    with path.open("rb") as stream:
        prefix = stream.read(32)
    if media_type == "application/pdf":
        if not prefix.startswith(b"%PDF-"):
            raise ControlFileError("declared PDF content has an invalid signature")
    elif media_type == "image/png":
        if not prefix.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ControlFileError("declared PNG content has an invalid signature")
    elif media_type == "image/jpeg":
        if not prefix.startswith(b"\xff\xd8\xff"):
            raise ControlFileError("declared JPEG content has an invalid signature")
    elif media_type == "image/gif":
        if not (prefix.startswith(b"GIF87a") or prefix.startswith(b"GIF89a")):
            raise ControlFileError("declared GIF content has an invalid signature")
    elif media_type == "image/webp":
        if not (prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"):
            raise ControlFileError("declared WebP content has an invalid signature")
    elif media_type == "application/jsonl":
        if not filename.lower().endswith(".jsonl"):
            raise ControlFileError("JSONL uploads require a .jsonl filename")
        _validate_jsonl(path, purpose=purpose, policy=policy)
    elif media_type == "text/plain":
        _validate_utf8(path)


def _validate_jsonl(path: Path, *, purpose: FilePurpose, policy: FilePolicy) -> None:
    count = 0
    with path.open("rb") as stream:
        while True:
            line = stream.readline(policy.max_jsonl_line_bytes + 1)
            if not line:
                break
            if len(line) > policy.max_jsonl_line_bytes:
                raise ControlFileError("JSONL line exceeds its byte limit")
            count += 1
            if count > policy.max_jsonl_lines:
                raise ControlFileError("JSONL file has too many lines")
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ControlFileError("JSONL file contains an invalid line") from exc
            if not isinstance(value, dict):
                raise ControlFileError("JSONL lines must be JSON objects")
            if purpose is FilePurpose.BATCH:
                required = {"custom_id", "method", "url", "body"}
                if set(value) != required or value.get("method") != "POST":
                    raise ControlFileError("batch JSONL line has an invalid request shape")
    if count == 0:
        raise ControlFileError("JSONL file cannot be empty")


def _validate_utf8(path: Path) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1_048_576):
                decoder.decode(chunk)
            decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise ControlFileError("text upload must be valid UTF-8") from exc


def _ensure_private_directory(path: Path) -> None:
    if not path.exists():
        path.mkdir(mode=0o700)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("file staging directory must be private and real")
