"""Rootless Docker internal-network controller for exact egress allow-lists."""

from __future__ import annotations

import json
from pathlib import Path

from zeaz_sandbox.backend import (
    CommandRunner,
    NetworkAttachment,
    SandboxBackendError,
    SubprocessCommandRunner,
)
from zeaz_sandbox.schemas import JobSpec, NetworkMode


class DockerProxyEgressController:
    """Attach jobs only to an internal network fronted by a trusted SOCKS proxy."""

    def __init__(
        self,
        proxy_image: str,
        *,
        runner: CommandRunner | None = None,
        docker_path: Path = Path("/usr/bin/docker"),
        docker_host: str,
    ) -> None:
        if (
            "@sha256:" not in proxy_image
            or len(proxy_image.rsplit("@sha256:", 1)[1]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in proxy_image.rsplit("@sha256:", 1)[1]
            )
        ):
            raise ValueError("proxy_image must be pinned by SHA-256 digest")
        if not docker_host.startswith("unix:///") or "\x00" in docker_host:
            raise ValueError("docker_host must be an absolute unix socket URL")
        self._proxy_image = proxy_image
        self._docker_path = docker_path
        self._runner = runner or SubprocessCommandRunner(
            {
                "DOCKER_HOST": docker_host,
                "XDG_RUNTIME_DIR": docker_host.removeprefix("unix://").rsplit("/", 1)[0],
            }
        )

    async def prepare(self, job: JobSpec) -> NetworkAttachment:
        if job.policy.network_mode is not NetworkMode.ALLOW_LIST:
            raise SandboxBackendError("egress controller requires allow-list policy")
        await self._verify_proxy_image()
        suffix = job.id.hex
        network = f"zeaz-net-{suffix}"
        proxy_name = f"zeaz-egress-{suffix}"
        proxy_id: str | None = None
        try:
            await self._require_success(
                (
                    str(self._docker_path),
                    "network",
                    "create",
                    "--internal",
                    "--driver",
                    "bridge",
                    "--label",
                    f"zeaz.sandbox.job={job.id}",
                    network,
                ),
                "sandbox egress network creation failed",
            )
            proxy_args = [
                str(self._docker_path),
                "create",
                "--name",
                proxy_name,
                "--label",
                f"zeaz.sandbox.job={job.id}",
                "--label",
                "zeaz.sandbox.role=egress",
                "--network",
                "bridge",
                "--read-only",
                "--user",
                "65532:65532",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--security-opt",
                "seccomp=builtin",
                "--security-opt",
                f"apparmor={job.policy.apparmor_profile}",
                "--pids-limit",
                "32",
                "--cpus",
                "0.25",
                "--memory",
                "67108864",
                "--memory-swap",
                "67108864",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=16777216",
                self._proxy_image,
            ]
            for destination in job.policy.allowed_destinations:
                for port in destination.ports:
                    proxy_args.extend(("--allow", f"{destination.host}:{port}"))
            create = await self._require_success(
                tuple(proxy_args),
                "sandbox egress proxy creation failed",
            )
            proxy_id = _container_id(create.stdout)
            await self._require_success(
                (
                    str(self._docker_path),
                    "network",
                    "connect",
                    "--alias",
                    "egress-proxy",
                    network,
                    proxy_id,
                ),
                "sandbox egress proxy attachment failed",
            )
            await self._require_success(
                (str(self._docker_path), "start", proxy_id),
                "sandbox egress proxy start failed",
            )
        except Exception:
            if proxy_id is not None:
                await self._best_effort(
                    (str(self._docker_path), "rm", "--force", proxy_id)
                )
            await self._best_effort(
                (str(self._docker_path), "network", "rm", network)
            )
            raise
        token = json.dumps(
            {"schema_version": "1", "proxy_id": proxy_id, "network": network},
            separators=(",", ":"),
            sort_keys=True,
        )
        return NetworkAttachment(
            docker_network=network,
            proxy_url="socks5h://egress-proxy:1080",
            cleanup_token=token,
        )

    async def cleanup(self, attachment: NetworkAttachment) -> None:
        if attachment.cleanup_token is None:
            return
        try:
            value = json.loads(attachment.cleanup_token)
            if (
                not isinstance(value, dict)
                or value.get("schema_version") != "1"
                or value.get("network") != attachment.docker_network
            ):
                raise ValueError
            proxy_id = value["proxy_id"]
            network = value["network"]
            _container_id(proxy_id.encode())
            if (
                not isinstance(network, str)
                or not network.startswith("zeaz-net-")
                or len(network) != 41
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SandboxBackendError("sandbox egress cleanup token is invalid") from exc
        proxy_result = await self._runner.run(
            (str(self._docker_path), "rm", "--force", proxy_id),
            timeout_seconds=30,
            max_stdout_bytes=4096,
            max_stderr_bytes=16_384,
        )
        network_result = await self._runner.run(
            (str(self._docker_path), "network", "rm", network),
            timeout_seconds=30,
            max_stdout_bytes=4096,
            max_stderr_bytes=16_384,
        )
        if proxy_result.returncode != 0 or network_result.returncode != 0:
            raise SandboxBackendError("sandbox egress cleanup failed")

    async def _verify_proxy_image(self) -> None:
        result = await self._runner.run(
            (
                str(self._docker_path),
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                self._proxy_image,
            ),
            timeout_seconds=15,
            max_stdout_bytes=65_536,
            max_stderr_bytes=16_384,
        )
        if result.returncode != 0:
            raise SandboxBackendError("trusted egress proxy image is unavailable")
        try:
            digests = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxBackendError("egress proxy image metadata is invalid") from exc
        if not isinstance(digests, list) or self._proxy_image not in digests:
            raise SandboxBackendError("egress proxy image digest does not match")

    async def _require_success(
        self,
        argv: tuple[str, ...],
        message: str,
    ):
        result = await self._runner.run(
            argv,
            timeout_seconds=30,
            max_stdout_bytes=65_536,
            max_stderr_bytes=16_384,
        )
        if result.returncode != 0:
            raise SandboxBackendError(message)
        return result

    async def _best_effort(self, argv: tuple[str, ...]) -> None:
        try:
            await self._runner.run(
                argv,
                timeout_seconds=30,
                max_stdout_bytes=4096,
                max_stderr_bytes=16_384,
            )
        except Exception:
            pass


def _container_id(value: bytes) -> str:
    try:
        identifier = value.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise SandboxBackendError("container runtime returned an invalid proxy ID") from exc
    if len(identifier) != 64 or any(
        character not in "0123456789abcdef" for character in identifier
    ):
        raise SandboxBackendError("container runtime returned an invalid proxy ID")
    return identifier
