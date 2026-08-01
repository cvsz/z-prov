"""Fail-closed rootless Docker backend command construction."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from zeaz_sandbox.schemas import JobSpec, NetworkMode, WorkspaceAccess
from zeaz_sandbox.streaming import (
    BoundedOutputStreamer,
    OutputChannel,
)


class SandboxBackendError(RuntimeError):
    """A sanitized backend or isolation-policy failure."""


class CommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    returncode: int
    stdout: bytes = Field(max_length=1_048_576)
    stderr: bytes = Field(max_length=65_536)


class ContainerStopReason(StrEnum):
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    OUTPUT_LIMIT = "output_limit"
    CANCELLED = "cancelled"
    OUTPUT_FAILURE = "output_failure"
    RUNTIME_FAILURE = "runtime_failure"


class ContainerExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: ContainerStopReason
    exit_code: int | None = Field(default=None, ge=0, le=255)
    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)
    output_truncated: bool


class CommandRunner(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    """Run fixed runtime argv without a shell or inherited environment."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        clean = dict(env or {})
        if len(clean) > 16 or any(
            key not in {"DOCKER_HOST", "XDG_RUNTIME_DIR", "HOME", "PATH"}
            or not value
            or "\x00" in value
            or "\n" in value
            or len(value) > 4096
            or (
                key == "PATH"
                and value != "/usr/sbin:/usr/bin:/sbin:/bin"
            )
            for key, value in clean.items()
        ):
            raise ValueError("runtime CLI environment contains an unsupported value")
        self._env = clean

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> CommandResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *tuple(argv),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
                limit=max(max_stdout_bytes, max_stderr_bytes) + 1,
            )
        except OSError as exc:
            raise SandboxBackendError("container runtime command failed to start") from exc
        if process.stdout is None or process.stderr is None:
            process.kill()
            await process.wait()
            raise SandboxBackendError("container runtime pipes are unavailable")
        overflow = asyncio.Event()
        stdout_task = asyncio.create_task(
            _read_bounded(process.stdout, max_stdout_bytes, overflow)
        )
        stderr_task = asyncio.create_task(
            _read_bounded(process.stderr, max_stderr_bytes, overflow)
        )
        process_wait = asyncio.create_task(process.wait())
        overflow_wait = asyncio.create_task(overflow.wait())
        done, _ = await asyncio.wait(
            {process_wait, overflow_wait},
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        timed_out = not done
        exceeded = overflow_wait in done and overflow_wait.result()
        if (timed_out or exceeded) and process.returncode is None:
            process.kill()
        await process_wait
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        overflow_wait.cancel()
        await asyncio.gather(overflow_wait, return_exceptions=True)
        if timed_out:
            raise SandboxBackendError("container runtime command timed out")
        if exceeded:
            raise SandboxBackendError("container runtime response exceeded its byte limit")
        return CommandResult(
            returncode=process.returncode or 0,
            stdout=stdout,
            stderr=stderr,
        )


class NetworkAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    docker_network: str
    proxy_url: str | None = None
    cleanup_token: str | None = None


class EgressController(Protocol):
    async def prepare(self, job: JobSpec) -> NetworkAttachment: ...

    async def cleanup(self, attachment: NetworkAttachment) -> None: ...


class RootlessDockerBackend:
    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        docker_path: Path = Path("/usr/bin/docker"),
        egress_controller: EgressController | None = None,
        allowed_apparmor_profiles: Sequence[str] = ("docker-default",),
        docker_host: str | None = None,
    ) -> None:
        _require_executable(docker_path)
        self._docker_path = docker_path
        resolved_host = docker_host or f"unix:///run/user/{os.geteuid()}/docker.sock"
        if (
            not resolved_host.startswith("unix:///")
            or "\x00" in resolved_host
            or len(resolved_host) > 4096
        ):
            raise ValueError("docker_host must be an absolute unix socket URL")
        self._cli_env = {
            "DOCKER_HOST": resolved_host,
            "XDG_RUNTIME_DIR": f"/run/user/{os.geteuid()}",
        }
        self._runner = runner or SubprocessCommandRunner(self._cli_env)
        self._egress_controller = egress_controller
        self._allowed_apparmor_profiles = frozenset(allowed_apparmor_profiles)
        if (
            not self._allowed_apparmor_profiles
            or len(self._allowed_apparmor_profiles) > 32
            or any(
                not profile
                or len(profile) > 128
                or any(
                    character
                    not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
                    for character in profile
                )
                for profile in self._allowed_apparmor_profiles
            )
        ):
            raise ValueError("allowed AppArmor profiles are invalid")
        self._probed = False

    async def probe(self) -> None:
        result = await self._runner.run(
            (
                str(self._docker_path),
                "info",
                "--format",
                "{{json .SecurityOptions}}",
            ),
            timeout_seconds=10,
            max_stdout_bytes=16_384,
            max_stderr_bytes=16_384,
        )
        if result.returncode != 0:
            raise SandboxBackendError("rootless container runtime is unavailable")
        try:
            options = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxBackendError("container runtime returned invalid security metadata") from exc
        if (
            not isinstance(options, list)
            or "name=rootless" not in options
            or "name=seccomp,profile=builtin" not in options
            or "name=apparmor" not in options
        ):
            raise SandboxBackendError(
                "container runtime must prove rootless, seccomp, and AppArmor isolation"
            )
        self._probed = True

    async def prepare_network(self, job: JobSpec) -> NetworkAttachment:
        if job.policy.network_mode is NetworkMode.DISABLED:
            return NetworkAttachment(docker_network="none")
        if self._egress_controller is None:
            raise SandboxBackendError("allow-list networking has no secure egress controller")
        attachment = await self._egress_controller.prepare(job)
        if (
            not attachment.docker_network
            or attachment.docker_network in {"host", "bridge", "default"}
        ):
            raise SandboxBackendError("egress controller returned an unsafe network")
        return attachment

    async def verify_image(self, image: str) -> str:
        self._require_probe()
        result = await self._runner.run(
            (
                str(self._docker_path),
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}|{{json .Config.Volumes}}",
                image,
            ),
            timeout_seconds=15,
            max_stdout_bytes=65_536,
            max_stderr_bytes=16_384,
        )
        if result.returncode != 0:
            raise SandboxBackendError("approved image is not available locally")
        try:
            digests_json, volumes_json = result.stdout.decode().strip().split("|", 1)
            digests = json.loads(digests_json)
            volumes = json.loads(volumes_json)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise SandboxBackendError("container image metadata is invalid") from exc
        if not isinstance(digests, list) or image not in digests:
            raise SandboxBackendError("local image does not match its approved digest")
        if volumes is not None and volumes != {}:
            raise SandboxBackendError("sandbox images cannot declare implicit volumes")
        return "sha256:" + image.rsplit("@sha256:", 1)[1]

    def create_argv(
        self,
        job: JobSpec,
        *,
        container_name: str,
        attachment: NetworkAttachment,
    ) -> tuple[str, ...]:
        self._require_probe()
        workspace = _validate_workspace(job.workspace)
        limits = job.policy.limits
        if (
            not container_name.startswith("zeaz-job-")
            or len(container_name) > 128
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in container_name)
        ):
            raise ValueError("container_name is invalid")
        if job.policy.apparmor_profile not in self._allowed_apparmor_profiles:
            raise SandboxBackendError("requested AppArmor profile is not allowed")
        mount = f"type=bind,src={workspace},dst=/workspace"
        if job.policy.workspace_access is WorkspaceAccess.READ_ONLY:
            mount += ",readonly"
        argv = [
            str(self._docker_path),
            "create",
            "--name",
            container_name,
            "--label",
            f"zeaz.sandbox.job={job.id}",
            "--network",
            attachment.docker_network,
            "--read-only",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--security-opt",
            f"seccomp={job.policy.seccomp_profile}",
            "--security-opt",
            f"apparmor={job.policy.apparmor_profile}",
            "--pids-limit",
            str(limits.process_count),
            "--cpus",
            _format_cpu(limits.cpu_cores),
            "--memory",
            str(limits.memory_bytes),
            "--memory-swap",
            str(limits.memory_bytes),
            "--ulimit",
            f"fsize={limits.file_bytes}:{limits.file_bytes}",
            "--ulimit",
            "nofile=1024:1024",
            "--tmpfs",
            (
                "/tmp:rw,noexec,nosuid,nodev,mode=700,"
                f"uid=65532,gid=65532,size={limits.temporary_bytes}"
            ),
            "--mount",
            mount,
            "--workdir",
            "/workspace",
            "--env",
            "HOME=/tmp",
            "--env",
            "TMPDIR=/tmp",
        ]
        if attachment.proxy_url is not None:
            argv.extend(
                (
                    "--env",
                    f"HTTP_PROXY={attachment.proxy_url}",
                    "--env",
                    f"HTTPS_PROXY={attachment.proxy_url}",
                    "--env",
                    f"ALL_PROXY={attachment.proxy_url}",
                    "--env",
                    "NO_PROXY=",
                )
            )
        argv.extend(("--entrypoint", job.command[0], job.image, *job.command[1:]))
        return tuple(argv)

    async def create(
        self,
        job: JobSpec,
        *,
        container_name: str,
        attachment: NetworkAttachment,
    ) -> str:
        await self.verify_image(job.image)
        result = await self._runner.run(
            self.create_argv(
                job,
                container_name=container_name,
                attachment=attachment,
            ),
            timeout_seconds=30,
            max_stdout_bytes=4096,
            max_stderr_bytes=16_384,
        )
        if result.returncode != 0:
            raise SandboxBackendError("sandbox container creation failed")
        container_id = result.stdout.decode("ascii", errors="strict").strip()
        if (
            len(container_id) != 64
            or any(character not in "0123456789abcdef" for character in container_id)
        ):
            raise SandboxBackendError("container runtime returned an invalid container ID")
        try:
            await self._verify_container_isolation(
                container_id,
                apparmor_profile=job.policy.apparmor_profile,
                seccomp_profile=job.policy.seccomp_profile,
            )
        except Exception:
            try:
                await self.remove(container_id)
            except Exception:
                pass
            raise
        return container_id

    async def _verify_container_isolation(
        self,
        container_id: str,
        *,
        apparmor_profile: str,
        seccomp_profile: str,
    ) -> None:
        result = await self._runner.run(
            (
                str(self._docker_path),
                "inspect",
                "--format",
                "{{json .AppArmorProfile}}|{{json .HostConfig.SecurityOpt}}",
                container_id,
            ),
            timeout_seconds=15,
            max_stdout_bytes=16_384,
            max_stderr_bytes=16_384,
        )
        if result.returncode != 0:
            raise SandboxBackendError("container isolation metadata is unavailable")
        try:
            apparmor_json, options_json = result.stdout.decode().strip().split("|", 1)
            actual_apparmor = json.loads(apparmor_json)
            security_options = json.loads(options_json)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise SandboxBackendError("container isolation metadata is invalid") from exc
        required = {
            "no-new-privileges=true",
            f"seccomp={seccomp_profile}",
            f"apparmor={apparmor_profile}",
        }
        if (
            actual_apparmor != apparmor_profile
            or not isinstance(security_options, list)
            or not required.issubset(security_options)
        ):
            raise SandboxBackendError(
                "container runtime did not apply the required isolation policy"
            )

    async def execute(
        self,
        container_id: str,
        streamer: BoundedOutputStreamer,
        *,
        timeout_seconds: int,
        cancel_event: asyncio.Event | None = None,
    ) -> ContainerExecutionResult:
        self._require_probe()
        _validate_container_id(container_id)
        try:
            process = await asyncio.create_subprocess_exec(
                str(self._docker_path),
                "start",
                "--attach",
                container_id,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._cli_env,
                limit=65_537,
            )
        except OSError as exc:
            raise SandboxBackendError("sandbox container failed to start") from exc
        if process.stdout is None or process.stderr is None:
            process.kill()
            await process.wait()
            raise SandboxBackendError("sandbox output pipes are unavailable")

        output_limit = asyncio.Event()

        async def read_channel(
            channel: OutputChannel,
            stream: asyncio.StreamReader,
        ) -> None:
            while True:
                chunk = await stream.read(65_536)
                if not chunk:
                    return
                if not await streamer.feed(channel, chunk):
                    output_limit.set()
                    return

        readers = asyncio.gather(
            read_channel(OutputChannel.STDOUT, process.stdout),
            read_channel(OutputChannel.STDERR, process.stderr),
        )
        process_wait = asyncio.create_task(process.wait())
        output_wait = asyncio.create_task(output_limit.wait())
        cancel_wait = (
            asyncio.create_task(cancel_event.wait())
            if cancel_event is not None
            else None
        )
        watched = {process_wait, output_wait, readers}
        if cancel_wait is not None:
            watched.add(cancel_wait)
        reason = ContainerStopReason.RUNTIME_FAILURE
        exit_code: int | None = None
        try:
            done, _ = await asyncio.wait(
                watched,
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                reason = ContainerStopReason.TIMED_OUT
            elif cancel_wait is not None and cancel_wait in done and cancel_wait.result():
                reason = ContainerStopReason.CANCELLED
            elif output_wait in done and output_wait.result():
                reason = ContainerStopReason.OUTPUT_LIMIT
            elif readers in done:
                if readers.exception() is not None:
                    reason = ContainerStopReason.OUTPUT_FAILURE
                else:
                    exit_code = await process_wait
                    if exit_code < 0 or exit_code > 255:
                        reason = ContainerStopReason.RUNTIME_FAILURE
                        exit_code = None
                    else:
                        reason = ContainerStopReason.EXITED
            elif process_wait in done:
                exit_code = process_wait.result()
                if exit_code < 0 or exit_code > 255:
                    reason = ContainerStopReason.RUNTIME_FAILURE
                    exit_code = None
                else:
                    reason = ContainerStopReason.EXITED
            else:
                reason = ContainerStopReason.RUNTIME_FAILURE

            if reason is not ContainerStopReason.EXITED:
                await self.kill(container_id)
                if process.returncode is None:
                    process.terminate()
            await asyncio.gather(process_wait, return_exceptions=True)
            await asyncio.gather(readers, return_exceptions=True)
            await streamer.finish()
        finally:
            output_wait.cancel()
            if cancel_wait is not None:
                cancel_wait.cancel()
            await asyncio.gather(
                output_wait,
                *([cancel_wait] if cancel_wait is not None else []),
                return_exceptions=True,
            )
            if process.returncode is None:
                process.kill()
                await process.wait()
        return ContainerExecutionResult(
            reason=reason,
            exit_code=exit_code,
            stdout_bytes=streamer.stdout_bytes,
            stderr_bytes=streamer.stderr_bytes,
            output_truncated=streamer.truncated,
        )

    async def kill(self, container_id: str) -> None:
        _validate_container_id(container_id)
        result = await self._runner.run(
            (str(self._docker_path), "kill", container_id),
            timeout_seconds=15,
            max_stdout_bytes=4096,
            max_stderr_bytes=16_384,
        )
        if result.returncode != 0:
            raise SandboxBackendError("sandbox container could not be stopped")

    async def remove(self, container_id: str) -> None:
        _validate_container_id(container_id)
        result = await self._runner.run(
            (str(self._docker_path), "rm", "--force", container_id),
            timeout_seconds=30,
            max_stdout_bytes=4096,
            max_stderr_bytes=16_384,
        )
        if result.returncode != 0:
            raise SandboxBackendError("sandbox container cleanup failed")

    async def managed_containers(self) -> tuple[str, ...]:
        self._require_probe()
        result = await self._runner.run(
            (
                str(self._docker_path),
                "ps",
                "--all",
                "--quiet",
                "--filter",
                "label=zeaz.sandbox.job",
                "--no-trunc",
            ),
            timeout_seconds=15,
            max_stdout_bytes=1_048_576,
            max_stderr_bytes=16_384,
        )
        if result.returncode != 0:
            raise SandboxBackendError("sandbox container inventory failed")
        try:
            identifiers = tuple(
                line for line in result.stdout.decode("ascii").splitlines() if line
            )
        except UnicodeDecodeError as exc:
            raise SandboxBackendError("sandbox container inventory is invalid") from exc
        if len(identifiers) > 10_000:
            raise SandboxBackendError("sandbox container inventory is excessive")
        for identifier in identifiers:
            _validate_container_id(identifier)
        return identifiers

    async def cleanup_network(self, attachment: NetworkAttachment) -> None:
        if attachment.cleanup_token is None:
            return
        if self._egress_controller is None:
            raise SandboxBackendError("network cleanup controller is unavailable")
        await self._egress_controller.cleanup(attachment)

    def _require_probe(self) -> None:
        if not self._probed:
            raise SandboxBackendError("container runtime isolation has not been verified")


def _require_executable(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError("Docker CLI is unavailable") from exc
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or not os.access(path, os.X_OK)
    ):
        raise ValueError("Docker CLI must be an absolute executable regular file")


def _validate_workspace(path: Path) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SandboxBackendError("workspace is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or info.st_uid != os.geteuid()
    ):
        raise SandboxBackendError("workspace must be a real caller-owned directory")
    return path


def _format_cpu(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


async def _read_bounded(
    stream: asyncio.StreamReader,
    maximum: int,
    overflow: asyncio.Event,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            return b"".join(chunks)
        remaining = maximum - total
        if remaining > 0:
            accepted = chunk[:remaining]
            chunks.append(accepted)
            total += len(accepted)
        if len(chunk) > remaining:
            overflow.set()


def _validate_container_id(value: str) -> None:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("container ID is invalid")
