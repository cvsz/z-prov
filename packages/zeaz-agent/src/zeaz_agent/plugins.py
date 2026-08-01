"""Signed, bounded, atomic plugin package lifecycle for zeaz-agent."""

from __future__ import annotations

import fcntl
import hashlib
import io
import os
import sqlite3
import stat
import subprocess
import tempfile
import zipfile
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import Field, StringConstraints, model_validator

from zeaz_agent.audit import AuditSink
from zeaz_agent.permissions import Actor
from zeaz_agent.schemas import StrictModel
from zeaz_agent.skills import SemVer, Sha256, SkillName, validate_resource_path

PluginName = SkillName
KeyId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"),
]


class PluginPackageError(RuntimeError):
    """A plugin package failed trust, archive, or manifest validation."""


class PluginRegistryError(RuntimeError):
    """A plugin registry mutation could not be completed safely."""


class PluginFile(StrictModel):
    path: str
    size: int = Field(ge=0, le=16_777_216)
    sha256: Sha256

    @model_validator(mode="after")
    def path_is_safe(self) -> PluginFile:
        try:
            validate_resource_path(self.path)
        except ValueError as exc:
            raise ValueError("plugin file path is invalid") from exc
        if self.path == "plugin.json":
            raise ValueError("plugin.json cannot list itself")
        return self


class PluginManifest(StrictModel):
    schema_version: Literal["1"]
    name: PluginName
    version: SemVer
    description: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    files: tuple[PluginFile, ...] = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def paths_are_unique(self) -> PluginManifest:
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)) or len(paths) != len({path.casefold() for path in paths}):
            raise ValueError("plugin file paths must be portable and unique")
        return self


class PluginAuditContext(StrictModel):
    session_id: UUID
    correlation_id: UUID
    actor: Actor


class InstalledPlugin(StrictModel):
    name: PluginName
    version: SemVer
    enabled: bool
    archive_sha256: Sha256
    signing_key_id: KeyId
    install_path: Path


class RemovedPlugin(StrictModel):
    removal_id: UUID
    name: PluginName
    version: SemVer
    archive_sha256: Sha256
    signing_key_id: KeyId
    trash_path: Path


class SignatureVerifier(Protocol):
    def verify(self, payload: bytes, signature: bytes, key_id: str) -> None: ...


class OpenSSLEd25519Verifier:
    """Verify exact archive bytes with trusted Ed25519 public keys."""

    def __init__(
        self,
        public_keys: Mapping[str, bytes],
        *,
        openssl_path: Path = Path("/usr/bin/openssl"),
        timeout_seconds: float = 10,
    ) -> None:
        keys = dict(public_keys)
        if not keys or len(keys) > 256:
            raise ValueError("public_keys must contain between 1 and 256 keys")
        for key_id, content in keys.items():
            if (
                not key_id
                or len(key_id) > 128
                or any(
                    character
                    not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
                    for character in key_id
                )
                or not 1 <= len(content) <= 16_384
            ):
                raise ValueError("trusted public key configuration is invalid")
        if not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 0 and 60")
        try:
            info = openssl_path.lstat()
        except OSError as exc:
            raise ValueError("OpenSSL executable is unavailable") from exc
        if (
            not openssl_path.is_absolute()
            or openssl_path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or not os.access(openssl_path, os.X_OK)
        ):
            raise ValueError("OpenSSL must be an absolute executable regular file")
        self._keys = keys
        self._openssl_path = openssl_path
        self._timeout = timeout_seconds

    def verify(self, payload: bytes, signature: bytes, key_id: str) -> None:
        public_key = self._keys.get(key_id)
        if public_key is None:
            raise PluginPackageError("plugin signing key is not trusted")
        if len(signature) != 64:
            raise PluginPackageError("plugin signature has an invalid size")
        with tempfile.TemporaryDirectory(prefix="zeaz-plugin-verify-") as temporary:
            root = Path(temporary)
            payload_path = root / "archive"
            signature_path = root / "signature"
            key_path = root / "public.pem"
            payload_path.write_bytes(payload)
            signature_path.write_bytes(signature)
            key_path.write_bytes(public_key)
            try:
                result = subprocess.run(
                    (
                        str(self._openssl_path),
                        "pkeyutl",
                        "-verify",
                        "-pubin",
                        "-inkey",
                        str(key_path),
                        "-rawin",
                        "-in",
                        str(payload_path),
                        "-sigfile",
                        str(signature_path),
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={},
                    timeout=self._timeout,
                    check=False,
                    close_fds=True,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise PluginPackageError("plugin signature verification failed") from exc
        if result.returncode != 0:
            raise PluginPackageError("plugin signature verification failed")


class PluginArchiveReader:
    def __init__(
        self,
        verifier: SignatureVerifier,
        *,
        max_archive_bytes: int = 67_108_864,
        max_expanded_bytes: int = 268_435_456,
        max_file_bytes: int = 16_777_216,
        max_entries: int = 1024,
        max_expansion_ratio: int = 100,
    ) -> None:
        if not 1024 <= max_archive_bytes <= 1_073_741_824:
            raise ValueError("max_archive_bytes must be between 1 KiB and 1 GiB")
        if not max_archive_bytes <= max_expanded_bytes <= 2_147_483_648:
            raise ValueError("max_expanded_bytes must be between archive size and 2 GiB")
        if not 1024 <= max_file_bytes <= max_expanded_bytes:
            raise ValueError("max_file_bytes is invalid")
        if not 1 <= max_entries <= 10_000:
            raise ValueError("max_entries must be between 1 and 10000")
        if not 1 <= max_expansion_ratio <= 1000:
            raise ValueError("max_expansion_ratio must be between 1 and 1000")
        self._verifier = verifier
        self._max_archive_bytes = max_archive_bytes
        self._max_expanded_bytes = max_expanded_bytes
        self._max_file_bytes = max_file_bytes
        self._max_entries = max_entries
        self._max_expansion_ratio = max_expansion_ratio

    def read(
        self,
        archive_path: Path,
        *,
        signature: bytes,
        key_id: str,
    ) -> tuple[PluginManifest, dict[str, bytes], str]:
        archive = _read_regular_file(archive_path, self._max_archive_bytes)
        self._verifier.verify(archive, signature, key_id)
        digest = hashlib.sha256(archive).hexdigest()
        try:
            package = zipfile.ZipFile(io.BytesIO(archive))
        except (OSError, zipfile.BadZipFile) as exc:
            raise PluginPackageError("plugin archive is not a valid ZIP file") from exc
        with package:
            infos = package.infolist()
            if not 2 <= len(infos) <= self._max_entries + 1:
                raise PluginPackageError("plugin archive entry count is invalid")
            names: list[str] = []
            total = 0
            for info in infos:
                name = _validate_zip_entry(info)
                if name in names or name.casefold() in {item.casefold() for item in names}:
                    raise PluginPackageError("plugin archive contains duplicate paths")
                names.append(name)
                if info.file_size > self._max_file_bytes:
                    raise PluginPackageError("plugin archive entry exceeds its size limit")
                total += info.file_size
                if total > self._max_expanded_bytes:
                    raise PluginPackageError("plugin archive exceeds its expansion limit")
                if info.file_size > max(1, info.compress_size) * self._max_expansion_ratio:
                    raise PluginPackageError("plugin archive entry exceeds its expansion ratio")
            if "plugin.json" not in names:
                raise PluginPackageError("plugin archive has no manifest")
            try:
                manifest_bytes = _read_zip_member(
                    package,
                    package.getinfo("plugin.json"),
                    min(self._max_file_bytes, 65_536),
                )
                manifest = PluginManifest.model_validate_json(manifest_bytes)
            except PluginPackageError:
                raise
            except Exception as exc:
                raise PluginPackageError("plugin manifest failed schema validation") from exc
            declared = {item.path: item for item in manifest.files}
            if set(names) != set(declared) | {"plugin.json"}:
                raise PluginPackageError("plugin archive files do not exactly match its manifest")
            files: dict[str, bytes] = {"plugin.json": manifest_bytes}
            for path, declaration in declared.items():
                content = _read_zip_member(package, package.getinfo(path), self._max_file_bytes)
                if len(content) != declaration.size:
                    raise PluginPackageError("plugin file size does not match its manifest")
                if hashlib.sha256(content).hexdigest() != declaration.sha256:
                    raise PluginPackageError("plugin file checksum does not match its manifest")
                files[path] = content
        return manifest, files, digest


class PluginRegistry:
    """Persistent registry with atomic publication and recoverable removal."""

    def __init__(
        self,
        root: Path,
        archive_reader: PluginArchiveReader,
        audit: AuditSink,
    ) -> None:
        _ensure_private_root(root)
        self._root = root
        self._archive_reader = archive_reader
        self._audit = audit
        self._lock_path = root / "registry.lock"
        self._database_path = root / "registry.sqlite3"
        _ensure_private_directory(root / ".trash")
        _ensure_private_directory(root / "plugins")
        self._initialize()

    def install(
        self,
        archive_path: Path,
        *,
        signature: bytes,
        key_id: str,
        audit_context: PluginAuditContext,
    ) -> InstalledPlugin:
        manifest, files, archive_digest = self._archive_reader.read(
            archive_path,
            signature=signature,
            key_id=key_id,
        )
        with self._locked():
            plugin_root = self._root / "plugins" / manifest.name
            versions = plugin_root / "versions"
            _ensure_private_directory(plugin_root)
            _ensure_private_directory(versions)
            target = versions / manifest.version
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT enabled, archive_sha256, signing_key_id, install_path "
                    "FROM plugins WHERE name = ? AND version = ?",
                    (manifest.name, manifest.version),
                ).fetchone()
            if existing is not None:
                if existing[1] != archive_digest or not target.is_dir():
                    raise PluginRegistryError("installed plugin state conflicts with the archive")
                return InstalledPlugin(
                    name=manifest.name,
                    version=manifest.version,
                    enabled=bool(existing[0]),
                    archive_sha256=existing[1],
                    signing_key_id=existing[2],
                    install_path=Path(existing[3]),
                )
            if target.exists() or target.is_symlink():
                raise PluginRegistryError("plugin version directory already exists")
            staging = versions / f".staging-{uuid4()}"
            staging.mkdir(mode=0o700)
            published = False
            try:
                _write_package(staging, files)
                os.rename(staging, target)
                published = True
                _fsync_directory(versions)
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT INTO plugins "
                        "(name, version, enabled, archive_sha256, signing_key_id, install_path) "
                        "VALUES (?, ?, 0, ?, ?, ?)",
                        (
                            manifest.name,
                            manifest.version,
                            archive_digest,
                            key_id,
                            str(target),
                        ),
                    )
                    connection.commit()
            except Exception:
                if staging.exists():
                    _remove_tree(staging)
                if published and target.exists():
                    _remove_tree(target)
                raise
            record = InstalledPlugin(
                name=manifest.name,
                version=manifest.version,
                enabled=False,
                archive_sha256=archive_digest,
                signing_key_id=key_id,
                install_path=target,
            )
            try:
                self._record_audit(
                    audit_context,
                    "agent.plugin.installed",
                    record,
                    {"enabled": False},
                )
            except Exception:
                with self._connect() as connection:
                    connection.execute(
                        "DELETE FROM plugins WHERE name = ? AND version = ?",
                        (manifest.name, manifest.version),
                    )
                    connection.commit()
                _remove_tree(target)
                raise
            return record

    def list(self) -> tuple[InstalledPlugin, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT name, version, enabled, archive_sha256, signing_key_id, install_path "
                "FROM plugins ORDER BY name, version"
            ).fetchall()
        return tuple(
            InstalledPlugin(
                name=row[0],
                version=row[1],
                enabled=bool(row[2]),
                archive_sha256=row[3],
                signing_key_id=row[4],
                install_path=Path(row[5]),
            )
            for row in rows
        )

    def set_enabled(
        self,
        name: str,
        version: str,
        enabled: bool,
        *,
        audit_context: PluginAuditContext,
    ) -> InstalledPlugin:
        with self._locked(), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous_states = connection.execute(
                "SELECT version, enabled FROM plugins WHERE name = ?",
                (name,),
            ).fetchall()
            row = connection.execute(
                "SELECT archive_sha256, signing_key_id, install_path "
                "FROM plugins WHERE name = ? AND version = ?",
                (name, version),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise PluginRegistryError("plugin version is not installed")
            install_path = Path(row[2])
            expected_path = self._root / "plugins" / name / "versions" / version
            if (
                install_path != expected_path
                or not install_path.is_dir()
                or install_path.is_symlink()
            ):
                connection.rollback()
                raise PluginRegistryError("plugin installation is unavailable")
            if enabled:
                _validate_installed_package(install_path)
                connection.execute("UPDATE plugins SET enabled = 0 WHERE name = ?", (name,))
            connection.execute(
                "UPDATE plugins SET enabled = ? WHERE name = ? AND version = ?",
                (int(enabled), name, version),
            )
            connection.commit()
            record = InstalledPlugin(
                name=name,
                version=version,
                enabled=enabled,
                archive_sha256=row[0],
                signing_key_id=row[1],
                install_path=install_path,
            )
            try:
                self._record_audit(
                    audit_context,
                    "agent.plugin.state_changed",
                    record,
                    {"enabled": enabled},
                )
            except Exception:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE plugins SET enabled = 0 WHERE name = ?", (name,))
                for previous_version, previous_enabled in previous_states:
                    connection.execute(
                        "UPDATE plugins SET enabled = ? WHERE name = ? AND version = ?",
                        (previous_enabled, name, previous_version),
                    )
                connection.commit()
                raise
            return record

    def remove(
        self,
        name: str,
        version: str,
        *,
        audit_context: PluginAuditContext,
    ) -> RemovedPlugin:
        removal_id = uuid4()
        with self._locked(), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT enabled, archive_sha256, signing_key_id, install_path "
                "FROM plugins WHERE name = ? AND version = ?",
                (name, version),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise PluginRegistryError("plugin version is not installed")
            if row[0]:
                connection.rollback()
                raise PluginRegistryError("disable the plugin before removal")
            source = Path(row[3])
            if not source.is_dir() or source.is_symlink():
                connection.rollback()
                raise PluginRegistryError("plugin installation is unavailable")
            trash = self._root / ".trash" / str(removal_id)
            os.rename(source, trash)
            try:
                connection.execute(
                    "INSERT INTO removed_plugins "
                    "(removal_id, name, version, archive_sha256, signing_key_id, trash_path) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (str(removal_id), name, version, row[1], row[2], str(trash)),
                )
                connection.execute(
                    "DELETE FROM plugins WHERE name = ? AND version = ?",
                    (name, version),
                )
                connection.commit()
            except Exception:
                os.rename(trash, source)
                connection.rollback()
                raise
            removed = RemovedPlugin(
                removal_id=removal_id,
                name=name,
                version=version,
                archive_sha256=row[1],
                signing_key_id=row[2],
                trash_path=trash,
            )
            try:
                self._audit.append(
                    session_id=audit_context.session_id,
                    correlation_id=audit_context.correlation_id,
                    event_type="agent.plugin.removed",
                    actor=audit_context.actor,
                    subject_id=name,
                    details={
                        "version": version,
                        "removal_id": str(removal_id),
                        "recoverable": True,
                    },
                )
            except Exception:
                os.rename(trash, source)
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO plugins "
                    "(name, version, enabled, archive_sha256, signing_key_id, install_path) "
                    "VALUES (?, ?, 0, ?, ?, ?)",
                    (name, version, row[1], row[2], str(source)),
                )
                connection.execute(
                    "DELETE FROM removed_plugins WHERE removal_id = ?",
                    (str(removal_id),),
                )
                connection.commit()
                raise
            return removed

    def restore(
        self,
        removal_id: UUID,
        *,
        audit_context: PluginAuditContext,
    ) -> InstalledPlugin:
        with self._locked(), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT name, version, archive_sha256, signing_key_id, trash_path "
                "FROM removed_plugins WHERE removal_id = ?",
                (str(removal_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise PluginRegistryError("removed plugin is not recoverable")
            trash = Path(row[4])
            target = self._root / "plugins" / row[0] / "versions" / row[1]
            if not trash.is_dir() or trash.is_symlink() or target.exists():
                connection.rollback()
                raise PluginRegistryError("removed plugin files cannot be restored safely")
            _ensure_private_directory(target.parent.parent)
            _ensure_private_directory(target.parent)
            os.rename(trash, target)
            try:
                connection.execute(
                    "INSERT INTO plugins "
                    "(name, version, enabled, archive_sha256, signing_key_id, install_path) "
                    "VALUES (?, ?, 0, ?, ?, ?)",
                    (row[0], row[1], row[2], row[3], str(target)),
                )
                connection.execute(
                    "DELETE FROM removed_plugins WHERE removal_id = ?",
                    (str(removal_id),),
                )
                connection.commit()
            except Exception:
                os.rename(target, trash)
                connection.rollback()
                raise
            record = InstalledPlugin(
                name=row[0],
                version=row[1],
                enabled=False,
                archive_sha256=row[2],
                signing_key_id=row[3],
                install_path=target,
            )
            try:
                self._record_audit(
                    audit_context,
                    "agent.plugin.restored",
                    record,
                    {"removal_id": str(removal_id), "enabled": False},
                )
            except Exception:
                os.rename(target, trash)
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO removed_plugins "
                    "(removal_id, name, version, archive_sha256, signing_key_id, trash_path) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(removal_id),
                        row[0],
                        row[1],
                        row[2],
                        row[3],
                        str(trash),
                    ),
                )
                connection.execute(
                    "DELETE FROM plugins WHERE name = ? AND version = ?",
                    (row[0], row[1]),
                )
                connection.commit()
                raise
            return record

    def _initialize(self) -> None:
        _ensure_private_file(self._database_path)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS plugins (
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    archive_sha256 TEXT NOT NULL,
                    signing_key_id TEXT NOT NULL,
                    install_path TEXT NOT NULL,
                    PRIMARY KEY (name, version)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_enabled_plugin_version
                    ON plugins(name) WHERE enabled = 1;
                CREATE TABLE IF NOT EXISTS removed_plugins (
                    removal_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    archive_sha256 TEXT NOT NULL,
                    signing_key_id TEXT NOT NULL,
                    trash_path TEXT NOT NULL
                );
                """
            )
        os.chmod(self._database_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _locked(self):
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise PluginRegistryError("plugin registry lock is not private")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _record_audit(
        self,
        context: PluginAuditContext,
        event_type: str,
        record: InstalledPlugin,
        details: dict[str, str | bool],
    ) -> None:
        self._audit.append(
            session_id=context.session_id,
            correlation_id=context.correlation_id,
            event_type=event_type,
            actor=context.actor,
            subject_id=record.name,
            details={"version": record.version, **details},
        )


def _read_regular_file(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PluginPackageError("unable to open plugin archive safely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise PluginPackageError("plugin archive is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 1_048_576))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        result = b"".join(chunks)
        if len(result) > maximum:
            raise PluginPackageError("plugin archive exceeds its size limit")
        return result
    finally:
        os.close(fd)


def _validate_zip_entry(info: zipfile.ZipInfo) -> str:
    original = getattr(info, "orig_filename", info.filename)
    if "\x00" in original or original != info.filename or info.is_dir():
        raise PluginPackageError("plugin archive contains an invalid entry")
    try:
        name = validate_resource_path(info.filename)
    except ValueError as exc:
        raise PluginPackageError("plugin archive contains an invalid path") from exc
    if info.flag_bits & 0x1:
        raise PluginPackageError("encrypted plugin archive entries are forbidden")
    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise PluginPackageError("plugin archive compression method is forbidden")
    unix_mode = info.external_attr >> 16
    file_type = stat.S_IFMT(unix_mode)
    if file_type and file_type != stat.S_IFREG:
        raise PluginPackageError("plugin archive links and devices are forbidden")
    return name


def _read_zip_member(package: zipfile.ZipFile, info: zipfile.ZipInfo, maximum: int) -> bytes:
    try:
        with package.open(info, "r") as member:
            content = member.read(maximum + 1)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise PluginPackageError("plugin archive entry could not be read safely") from exc
    if len(content) > maximum:
        raise PluginPackageError("plugin archive entry exceeds its size limit")
    return content


def _ensure_private_root(root: Path) -> None:
    if root.exists():
        try:
            info = root.lstat()
        except OSError as exc:
            raise PluginRegistryError("plugin registry root is unavailable") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or root.is_symlink()
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise PluginRegistryError("plugin registry root must be a private real directory")
        return
    if not root.parent.is_dir() or root.parent.is_symlink():
        raise PluginRegistryError("plugin registry parent must be a real directory")
    root.mkdir(mode=0o700)


def _ensure_private_directory(path: Path) -> None:
    if not path.exists():
        path.mkdir(mode=0o700)
    try:
        info = path.lstat()
    except OSError as exc:
        raise PluginRegistryError("plugin registry directory is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise PluginRegistryError("plugin registry directories must be private and real")


def _ensure_private_file(path: Path) -> None:
    if not path.exists():
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        os.close(fd)
    try:
        info = path.lstat()
    except OSError as exc:
        raise PluginRegistryError("plugin registry database is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or path.is_symlink()
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise PluginRegistryError("plugin registry database must be a private regular file")


def _write_package(staging: Path, files: Mapping[str, bytes]) -> None:
    for relative, content in sorted(files.items()):
        target = staging / relative
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(target, flags, 0o600)
        try:
            view = memoryview(content)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("plugin file write made no progress")
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
    for current, _, _ in os.walk(staging, topdown=False):
        _fsync_directory(Path(current))


def _validate_installed_package(root: Path) -> PluginManifest:
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise PluginRegistryError("installed plugin is unavailable") from exc
    if not stat.S_ISDIR(root_info.st_mode) or root.is_symlink():
        raise PluginRegistryError("installed plugin root is invalid")
    inventory: set[str] = set()
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            candidate = current_path / directory
            if candidate.is_symlink():
                raise PluginRegistryError("installed plugin contains a linked directory")
        for filename in files:
            candidate = current_path / filename
            relative = candidate.relative_to(root).as_posix()
            try:
                validate_resource_path(relative)
                info = candidate.lstat()
            except (OSError, ValueError) as exc:
                raise PluginRegistryError("installed plugin contains an invalid path") from exc
            if (
                candidate.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o111
            ):
                raise PluginRegistryError("installed plugin contains an unsafe file")
            inventory.add(relative)
            if len(inventory) > 1025:
                raise PluginRegistryError("installed plugin has too many files")
    try:
        manifest = PluginManifest.model_validate_json(
            _read_regular_file(root / "plugin.json", 65_536)
        )
    except PluginPackageError as exc:
        raise PluginRegistryError("installed plugin manifest is unavailable") from exc
    except Exception as exc:
        raise PluginRegistryError("installed plugin manifest is invalid") from exc
    declarations = {item.path: item for item in manifest.files}
    if inventory != set(declarations) | {"plugin.json"}:
        raise PluginRegistryError("installed plugin inventory does not match its manifest")
    for relative, declaration in declarations.items():
        try:
            content = _read_regular_file(root / relative, declaration.size)
        except PluginPackageError as exc:
            raise PluginRegistryError("installed plugin file is unavailable") from exc
        if (
            len(content) != declaration.size
            or hashlib.sha256(content).hexdigest() != declaration.sha256
        ):
            raise PluginRegistryError("installed plugin integrity check failed")
    return manifest


def _remove_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for filename in files:
            (current_path / filename).unlink()
        for directory in directories:
            (current_path / directory).rmdir()
    root.rmdir()


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
