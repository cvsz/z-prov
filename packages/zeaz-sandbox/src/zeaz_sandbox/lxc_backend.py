"""Unprivileged LXC backend with explicit AppArmor and cgroup verification."""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path

from zeaz_sandbox.backend import (
    CommandRunner,
    ContainerExecutionResult,
    ContainerStopReason,
    NetworkAttachment,
    SandboxBackendError,
    SubprocessCommandRunner,
    _require_executable,
    _validate_workspace,
)
from zeaz_sandbox.schemas import JobSpec, NetworkMode, WorkspaceAccess
from zeaz_sandbox.streaming import BoundedOutputStreamer, OutputChannel


class RootlessLxcBackend:
    """Run non-root job argv in pre-provisioned, digest-bound LXC root filesystems."""

    def __init__(
        self,
        *,
        state_root: Path,
        image_roots: Mapping[str, Path],
        runner: CommandRunner | None = None,
        apparmor_profile: str = "lxc-container-default-cgns",
        subuid_start: int = 100_000,
        subgid_start: int = 100_000,
        job_uid: int = 65_532,
        command_paths: Mapping[str, Path] | None = None,
    ) -> None:
        paths = dict(
            command_paths
            or {
                "start": Path("/usr/bin/lxc-start"),
                "stop": Path("/usr/bin/lxc-stop"),
                "attach": Path("/usr/bin/lxc-attach"),
                "info": Path("/usr/bin/lxc-info"),
            }
        )
        if set(paths) != {"start", "stop", "attach", "info"}:
            raise ValueError("LXC command path mapping is incomplete")
        for path in paths.values():
            _require_executable(path)
        self._paths = paths
        self._state_root = _private_state_root(state_root)
        self._cli_env = {
            "HOME": str(self._state_root),
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        }
        self._runner = runner or SubprocessCommandRunner(self._cli_env)
        self._apparmor_profile = _safe_token(apparmor_profile, "AppArmor profile")
        if not 65_536 <= subuid_start <= 2_000_000_000:
            raise ValueError("subuid_start is invalid")
        if not 65_536 <= subgid_start <= 2_000_000_000:
            raise ValueError("subgid_start is invalid")
        if not 1 <= job_uid <= 65_534:
            raise ValueError("job_uid is invalid")
        self._subuid_start = subuid_start
        self._subgid_start = subgid_start
        self._job_uid = job_uid
        self._image_roots = {
            image: _trusted_rootfs(rootfs) for image, rootfs in image_roots.items()
        }
        if not self._image_roots:
            raise ValueError("at least one digest-bound LXC image is required")
        self._commands: dict[str, tuple[str, ...]] = {}
        self._probed = False

    async def probe(self) -> None:
        try:
            enabled = Path("/sys/module/apparmor/parameters/enabled").read_text().strip()
        except OSError as exc:
            raise SandboxBackendError("AppArmor runtime status is unavailable") from exc
        if enabled != "Y":
            raise SandboxBackendError("AppArmor is not enabled")
        seccomp = Path("/usr/share/lxc/config/common.seccomp")
        if not seccomp.is_file():
            raise SandboxBackendError("LXC seccomp policy is unavailable")
        result = await self._runner.run(
            (str(self._paths["info"]), "--version"),
            timeout_seconds=10,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
        )
        if result.returncode != 0:
            raise SandboxBackendError("unprivileged LXC runtime is unavailable")
        self._probed = True

    async def prepare_network(self, job: JobSpec) -> NetworkAttachment:
        if job.policy.network_mode is not NetworkMode.DISABLED:
            raise SandboxBackendError(
                "LXC backend supports only network-disabled jobs"
            )
        return NetworkAttachment(docker_network="none")

    async def verify_image(self, image: str) -> str:
        self._require_probe()
        if image not in self._image_roots:
            raise SandboxBackendError("approved LXC image is not provisioned")
        return "sha256:" + image.rsplit("@sha256:", 1)[1]

    async def create(
        self,
        job: JobSpec,
        *,
        container_name: str,
        attachment: NetworkAttachment,
    ) -> str:
        await self.verify_image(job.image)
        if attachment.docker_network != "none" or attachment.proxy_url is not None:
            raise SandboxBackendError("LXC network attachment is unsafe")
        if job.policy.apparmor_profile != self._apparmor_profile:
            raise SandboxBackendError("requested LXC AppArmor profile is not allowed")
        name = _container_name(container_name)
        workspace = _validate_workspace(job.workspace)
        definition = self._state_root / name
        try:
            definition.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise SandboxBackendError("LXC job definition already exists") from exc
        config = _render_config(
            rootfs=self._image_roots[job.image],
            workspace=workspace,
            job=job,
            apparmor_profile=self._apparmor_profile,
            subuid_start=self._subuid_start,
            subgid_start=self._subgid_start,
            job_uid=self._job_uid,
        )
        try:
            _write_exclusive(definition / "config", config)
            await self._run_required(
                (
                    str(self._paths["start"]),
                    "--name",
                    name,
                    "--lxcpath",
                    str(self._state_root),
                    "--daemon",
                ),
                "LXC container failed to start",
                timeout=30,
            )
            await self._verify_started(name, job)
        except Exception:
            await self._stop_if_present(name)
            _remove_definition(definition)
            raise
        self._commands[name] = tuple(job.command)
        return name

    async def _verify_started(self, name: str, job: JobSpec) -> None:
        state = await self._runner.run(
            (
                str(self._paths["info"]),
                "--name",
                name,
                "--lxcpath",
                str(self._state_root),
                "--state",
                "--no-humanize",
            ),
            timeout_seconds=10,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
        )
        if state.returncode != 0 or state.stdout.strip() != b"RUNNING":
            raise SandboxBackendError("LXC container did not reach running state")
        commands = (
            ("/bin/cat", "/proc/self/attr/current"),
            ("/bin/cat", "/proc/self/status"),
            ("/bin/cat", "/sys/fs/cgroup/pids.max"),
            ("/bin/cat", "/sys/fs/cgroup/memory.max"),
        )
        actual = tuple(
            [await self._attach_capture(name, command) for command in commands]
        )
        if (
            actual[0] != f"{self._apparmor_profile} (enforce)"
            or "CapEff:\t0000000000000000" not in actual[1]
            or actual[2] != str(job.policy.limits.process_count)
            or actual[3] != str(job.policy.limits.memory_bytes)
        ):
            raise SandboxBackendError(
                "LXC runtime did not apply the required isolation policy"
            )

    async def execute(
        self,
        container_id: str,
        streamer: BoundedOutputStreamer,
        *,
        timeout_seconds: int,
        cancel_event: asyncio.Event | None = None,
    ) -> ContainerExecutionResult:
        name = _container_name(container_id)
        try:
            command = self._commands[name]
        except KeyError as exc:
            raise SandboxBackendError("LXC job command is unavailable") from exc
        try:
            process = await asyncio.create_subprocess_exec(
                *self._attach_argv(name, command),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._cli_env,
                limit=65_537,
            )
        except OSError as exc:
            raise SandboxBackendError("LXC job failed to start") from exc
        if process.stdout is None or process.stderr is None:
            process.kill()
            await process.wait()
            raise SandboxBackendError("LXC output pipes are unavailable")

        output_limit = asyncio.Event()

        async def read_channel(
            channel: OutputChannel,
            stream: asyncio.StreamReader,
        ) -> None:
            while chunk := await stream.read(65_536):
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
            elif readers in done and readers.exception() is not None:
                reason = ContainerStopReason.OUTPUT_FAILURE
            else:
                exit_code = await process_wait
                reason = (
                    ContainerStopReason.EXITED
                    if 0 <= exit_code <= 255
                    else ContainerStopReason.RUNTIME_FAILURE
                )
                if reason is ContainerStopReason.RUNTIME_FAILURE:
                    exit_code = None
            if reason is not ContainerStopReason.EXITED:
                await self._stop_if_present(name)
                if process.returncode is None:
                    process.terminate()
            await asyncio.gather(process_wait, readers, return_exceptions=True)
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

    async def remove(self, container_id: str) -> None:
        name = _container_name(container_id)
        await self._stop_if_present(name)
        self._commands.pop(name, None)
        _remove_definition(self._state_root / name)

    async def managed_containers(self) -> tuple[str, ...]:
        names: list[str] = []
        for child in self._state_root.iterdir():
            if child.is_dir() and child.name.startswith("zeaz-job-"):
                try:
                    names.append(_container_name(child.name))
                except ValueError:
                    continue
        return tuple(sorted(names))

    async def cleanup_network(self, attachment: NetworkAttachment) -> None:
        if attachment.docker_network != "none":
            raise SandboxBackendError("unexpected LXC network cleanup request")

    async def _attach_capture(self, name: str, command: Sequence[str]) -> str:
        argv = self._attach_argv(name, command)
        result = await self._runner.run(
            tuple(argv),
            timeout_seconds=10,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
        )
        if result.returncode != 0:
            raise SandboxBackendError("LXC isolation probe failed")
        try:
            return result.stdout.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise SandboxBackendError("LXC isolation probe returned invalid data") from exc

    def _attach_argv(self, name: str, command: Sequence[str]) -> list[str]:
        return [
            str(self._paths["attach"]),
            "--name",
            name,
            "--lxcpath",
            str(self._state_root),
            "--uid",
            str(self._job_uid),
            "--gid",
            str(self._job_uid),
            "--clear-env",
            "--set-var",
            "HOME=/tmp",
            "--set-var",
            "TMPDIR=/tmp",
            "--",
            *command,
        ]

    async def _run_required(
        self,
        argv: Sequence[str],
        failure: str,
        *,
        timeout: float,
    ) -> None:
        result = await self._runner.run(
            argv,
            timeout_seconds=timeout,
            max_stdout_bytes=4096,
            max_stderr_bytes=16_384,
        )
        if result.returncode != 0:
            raise SandboxBackendError(failure)

    async def _stop_if_present(self, name: str) -> None:
        state = await self._container_state(name)
        if state is None or state == "STOPPED":
            return
        if state != "RUNNING":
            raise SandboxBackendError("LXC container has an unsafe runtime state")
        result = await self._runner.run(
            (
                str(self._paths["stop"]),
                "--name",
                name,
                "--lxcpath",
                str(self._state_root),
                "--kill",
            ),
            timeout_seconds=15,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
        )
        if result.returncode != 0 or await self._container_state(name) != "STOPPED":
            raise SandboxBackendError("LXC container could not be stopped")

    async def _container_state(self, name: str) -> str | None:
        result = await self._runner.run(
            (
                str(self._paths["info"]),
                "--name",
                name,
                "--lxcpath",
                str(self._state_root),
                "--state",
                "--no-humanize",
            ),
            timeout_seconds=10,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
        )
        if result.returncode != 0:
            return None
        try:
            return result.stdout.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise SandboxBackendError("LXC state metadata is invalid") from exc

    def _require_probe(self) -> None:
        if not self._probed:
            raise SandboxBackendError("LXC backend has not passed its runtime probe")


def _render_config(
    *,
    rootfs: Path,
    workspace: Path,
    job: JobSpec,
    apparmor_profile: str,
    subuid_start: int,
    subgid_start: int,
    job_uid: int,
) -> bytes:
    access = "bind,create=dir"
    if job.policy.workspace_access is WorkspaceAccess.READ_ONLY:
        access += ",ro"
    limits = job.policy.limits
    quota = max(1, round(float(limits.cpu_cores) * 100_000))
    uid_tail = 65_536 - job_uid - 1
    lines = [
        "lxc.include = /usr/share/lxc/config/common.conf",
        "lxc.include = /usr/share/lxc/config/userns.conf",
        "lxc.arch = linux64",
        f"lxc.rootfs.path = dir:{rootfs}",
        "lxc.net.0.type = empty",
        f"lxc.apparmor.profile = {apparmor_profile}",
        "lxc.cap.keep = none",
        f"lxc.idmap = u 0 {subuid_start} {job_uid}",
        f"lxc.idmap = u {job_uid} {os.getuid()} 1",
        f"lxc.idmap = u {job_uid + 1} {subuid_start + job_uid + 1} {uid_tail}",
        f"lxc.idmap = g 0 {subgid_start} {job_uid}",
        f"lxc.idmap = g {job_uid} {os.getgid()} 1",
        f"lxc.idmap = g {job_uid + 1} {subgid_start + job_uid + 1} {uid_tail}",
        f"lxc.cgroup2.pids.max = {limits.process_count}",
        f"lxc.cgroup2.memory.max = {limits.memory_bytes}",
        "lxc.cgroup2.memory.swap.max = 0",
        f"lxc.cgroup2.cpu.max = {quota} 100000",
        f"lxc.prlimit.fsize = {limits.file_bytes}",
        "lxc.prlimit.nofile = 1024",
        f"lxc.mount.entry = {workspace} workspace none {access} 0 0",
        (
            "lxc.mount.entry = tmpfs tmp tmpfs "
            f"rw,nosuid,nodev,noexec,size={limits.temporary_bytes},mode=1777,"
            "create=dir 0 0"
        ),
        (
            "lxc.mount.entry = tmpfs run tmpfs "
            "rw,nosuid,nodev,noexec,size=4194304,mode=755,create=dir 0 0"
        ),
    ]
    return ("\n".join(lines) + "\n").encode()


def _private_state_root(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise ValueError("LXC state root must be private and caller-owned")
    return resolved


def _trusted_rootfs(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("LXC rootfs cannot be a symbolic link")
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o022:
        raise ValueError("LXC rootfs must not be group/world writable")
    if "\n" in str(resolved) or len(str(resolved)) > 2048:
        raise ValueError("LXC rootfs path is invalid")
    return resolved


def _safe_token(value: str, label: str) -> str:
    if (
        not value
        or len(value) > 128
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for character in value)
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _container_name(value: str) -> str:
    if (
        not value.startswith("zeaz-job-")
        or len(value) > 128
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in value)
    ):
        raise ValueError("LXC container name is invalid")
    return value


def _write_exclusive(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_definition(path: Path) -> None:
    try:
        (path / "config").unlink()
        path.rmdir()
    except FileNotFoundError:
        return
