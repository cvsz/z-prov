"""Versioned, integrity-checked local SKILL.md package loading."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, StringConstraints, field_validator, model_validator

from zeaz_agent.schemas import StrictModel

SkillName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=64),
]
SemVer = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
            r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
            r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
        ),
        max_length=128,
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_RESOURCE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_INLINE_LINK = re.compile(r"!?\[[^\]]*]\(\s*(?:<([^>]+)>|([^\s)]+))")
_REFERENCE_LINK = re.compile(
    r"(?m)^[ \t]{0,3}\[[^\]]+]:[ \t]*(?:<([^>]+)>|([^\s]+))"
)


class SkillResource(StrictModel):
    path: str
    size: int = Field(ge=0, le=1_048_576)
    sha256: Sha256

    @field_validator("path")
    @classmethod
    def path_is_portable_and_relative(cls, value: str) -> str:
        return validate_resource_path(value)


class SkillManifest(StrictModel):
    schema_version: Literal["1"]
    name: SkillName
    version: SemVer
    description: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    entrypoint: Literal["SKILL.md"]
    resources: tuple[SkillResource, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def resources_are_unique_and_include_entrypoint(self) -> SkillManifest:
        paths = [resource.path for resource in self.resources]
        if len(paths) != len(set(paths)):
            raise ValueError("skill resource paths must be unique")
        if self.entrypoint not in paths:
            raise ValueError("skill entrypoint must be declared as a resource")
        return self


class LoadedSkillResource(StrictModel):
    path: str
    sha256: Sha256
    content: bytes


class LoadedSkill(StrictModel):
    manifest: SkillManifest
    instructions: str
    resources: tuple[LoadedSkillResource, ...]

    def resource(self, path: str) -> bytes:
        normalized = validate_resource_path(path)
        for resource in self.resources:
            if resource.path == normalized:
                return resource.content
        raise KeyError(normalized)


class SkillPackageError(RuntimeError):
    pass


class LocalSkillLoader:
    def __init__(
        self,
        root: Path,
        *,
        max_manifest_bytes: int = 65_536,
        max_file_bytes: int = 1_048_576,
        max_package_bytes: int = 10_485_760,
        max_resources: int = 256,
    ) -> None:
        if not 1024 <= max_manifest_bytes <= 1_048_576:
            raise ValueError("max_manifest_bytes must be between 1 KiB and 1 MiB")
        if not 256 <= max_file_bytes <= 16_777_216:
            raise ValueError("max_file_bytes must be between 256 bytes and 16 MiB")
        if not max_file_bytes <= max_package_bytes <= 134_217_728:
            raise ValueError("max_package_bytes must be between one file and 128 MiB")
        if not 1 <= max_resources <= 4096:
            raise ValueError("max_resources must be between 1 and 4096")
        _require_directory(root, "skill root")
        self._root = root
        self._max_manifest_bytes = max_manifest_bytes
        self._max_file_bytes = max_file_bytes
        self._max_package_bytes = max_package_bytes
        self._max_resources = max_resources

    def load(self, name: str) -> LoadedSkill:
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", name) or len(name) > 64:
            raise SkillPackageError("invalid skill name")
        package = self._root / name
        _require_directory(package, "skill package")
        manifest_bytes = _read_regular_file(
            package / "skill-manifest.json",
            self._max_manifest_bytes,
        )
        try:
            manifest = SkillManifest.model_validate_json(manifest_bytes)
        except Exception as exc:
            raise SkillPackageError("skill manifest failed schema validation") from exc
        if manifest.name != name:
            raise SkillPackageError("skill manifest name does not match its directory")
        if len(manifest.resources) > self._max_resources:
            raise SkillPackageError("skill manifest exceeds the resource-count limit")
        declared = {resource.path for resource in manifest.resources}
        inventory = _package_inventory(package, self._max_resources + 1)
        expected = declared | {"skill-manifest.json"}
        if inventory != expected:
            raise SkillPackageError("skill package files do not exactly match its manifest")

        loaded: list[LoadedSkillResource] = []
        total = 0
        for resource in manifest.resources:
            maximum = min(self._max_file_bytes, resource.size + 1)
            content = _read_regular_file(package / resource.path, maximum)
            if len(content) != resource.size:
                raise SkillPackageError("skill resource size does not match its manifest")
            digest = hashlib.sha256(content).hexdigest()
            if digest != resource.sha256:
                raise SkillPackageError("skill resource checksum does not match its manifest")
            total += len(content)
            if total > self._max_package_bytes:
                raise SkillPackageError("skill package exceeds its total-size limit")
            loaded.append(
                LoadedSkillResource(
                    path=resource.path,
                    sha256=digest,
                    content=content,
                )
            )

        entrypoint = next(
            resource.content for resource in loaded if resource.path == manifest.entrypoint
        )
        try:
            instructions = entrypoint.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillPackageError("SKILL.md must be valid UTF-8") from exc
        if "\x00" in instructions:
            raise SkillPackageError("SKILL.md cannot contain NUL bytes")
        _validate_markdown_references(instructions, declared)
        return LoadedSkill(
            manifest=manifest,
            instructions=instructions,
            resources=tuple(loaded),
        )


def validate_resource_path(value: str) -> str:
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or not _RESOURCE_PATH.fullmatch(value)
    ):
        raise ValueError("resource path is not portable")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts):
        raise ValueError("resource path must stay within the package")
    normalized = path.as_posix()
    if normalized != value:
        raise ValueError("resource path must be normalized")
    return normalized


def _require_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SkillPackageError(f"{label} is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise SkillPackageError(f"{label} must be a real directory")


def _read_regular_file(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SkillPackageError("unable to open skill file safely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SkillPackageError("skill resources must be regular files")
        if info.st_size > maximum:
            raise SkillPackageError("skill file exceeds its size limit")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65_536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > maximum:
            raise SkillPackageError("skill file exceeds its size limit")
        return content
    finally:
        os.close(fd)


def _package_inventory(package: Path, maximum: int) -> set[str]:
    inventory: set[str] = set()
    for current, directories, files in os.walk(package, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            candidate = current_path / directory
            if candidate.is_symlink():
                raise SkillPackageError("skill package directories cannot be symlinks")
        for filename in files:
            candidate = current_path / filename
            relative = candidate.relative_to(package).as_posix()
            try:
                validate_resource_path(relative)
            except ValueError as exc:
                raise SkillPackageError("skill package contains an invalid path") from exc
            if candidate.is_symlink() or not stat.S_ISREG(candidate.lstat().st_mode):
                raise SkillPackageError("skill package entries must be regular files")
            inventory.add(relative)
            if len(inventory) > maximum:
                raise SkillPackageError("skill package exceeds the resource-count limit")
    return inventory


def _validate_markdown_references(markdown: str, declared: set[str]) -> None:
    destinations = [
        first or second
        for pattern in (_INLINE_LINK, _REFERENCE_LINK)
        for first, second in pattern.findall(markdown)
    ]
    for destination in destinations:
        parsed = urlsplit(destination)
        if parsed.scheme in {"http", "https", "mailto"} or destination.startswith("#"):
            continue
        if parsed.scheme or parsed.netloc:
            raise SkillPackageError("SKILL.md contains a forbidden resource URI")
        try:
            local = validate_resource_path(unquote(parsed.path))
        except ValueError as exc:
            raise SkillPackageError("SKILL.md contains an invalid local reference") from exc
        if local not in declared:
            raise SkillPackageError("SKILL.md references an undeclared local resource")
