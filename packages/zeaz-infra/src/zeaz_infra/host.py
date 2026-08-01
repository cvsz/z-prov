"""Bounded, read-only host capability detection for the I1 installer."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GpuVendor = Literal["nvidia", "amd", "none", "unknown"]
InstallMode = Literal["nvidia", "amd", "cpu-only"]
Virtualization = Literal["vmware", "kvm", "hyperv", "virtualized", "bare-metal", "unknown"]


class HostInventory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cpu_model: str = Field(max_length=256)
    logical_cpus: int = Field(ge=1, le=4096)
    memory_bytes: int = Field(ge=0, le=1 << 50)
    disk_total_bytes: int = Field(ge=0, le=1 << 60)
    disk_free_bytes: int = Field(ge=0, le=1 << 60)
    virtualization: Virtualization
    gpu_vendor: GpuVendor
    gpu_devices: tuple[str, ...] = Field(max_length=32)
    install_mode: InstallMode
    warnings: tuple[str, ...] = Field(max_length=32)


class InstallStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(max_length=96)
    details: str = Field(max_length=384)
    required: bool = True


class InstallPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    package_sources: tuple[str, ...] = Field(max_length=16)
    packages: tuple[str, ...] = Field(max_length=32)
    optional_packages: tuple[str, ...] = Field(max_length=16)
    steps: tuple[InstallStep, ...] = Field(max_length=16)
    warnings: tuple[str, ...] = Field(max_length=32)


class HostDetector:
    """Read-only detector with injectable roots for deterministic tests."""

    def __init__(
        self,
        *,
        proc_root: Path = Path("/proc"),
        sys_root: Path = Path("/sys"),
        dev_root: Path = Path("/dev"),
        disk_path: Path = Path("/"),
    ) -> None:
        self.proc_root = _absolute(proc_root, "proc_root")
        self.sys_root = _absolute(sys_root, "sys_root")
        self.dev_root = _absolute(dev_root, "dev_root")
        self.disk_path = _absolute(disk_path, "disk_path")

    def detect(self) -> HostInventory:
        warnings: list[str] = []
        cpu_model, flags = self._cpu(warnings)
        memory = self._memory(warnings)
        total, free = self._disk(warnings)
        virtualization = self._virtualization(flags)
        vendor, devices = self._gpu(warnings)
        mode: InstallMode = (
            "nvidia" if vendor == "nvidia" else "amd" if vendor == "amd" else "cpu-only"
        )
        return HostInventory(
            cpu_model=cpu_model[:256] or "unknown",
            logical_cpus=min(max(os.cpu_count() or 1, 1), 4096),
            memory_bytes=memory,
            disk_total_bytes=total,
            disk_free_bytes=free,
            virtualization=virtualization,
            gpu_vendor=vendor,
            gpu_devices=tuple(devices[:32]),
            install_mode=mode,
            warnings=tuple(dict.fromkeys(warnings))[:32],
        )

    def plan(self, inventory: HostInventory | None = None) -> InstallPlan:
        inventory = inventory or self.detect()
        package_sources = ["docker-official-apt"]
        packages = [
            "docker-ce",
            "docker-ce-cli",
            "containerd.io",
            "docker-buildx-plugin",
            "docker-compose-plugin",
            "ufw",
        ]
        optional_packages: list[str] = []
        steps = [
            InstallStep(
                name="detect-host",
                details=(
                    "Use read-only CPU, memory, disk, virtualization, and GPU metadata to "
                    f"select the {inventory.install_mode} installation mode."
                ),
            ),
            InstallStep(
                name="install-docker-engine",
                details=(
                    "Install Docker Engine from the official Docker repository with pinned "
                    "package names and no implicit fallback repository."
                ),
            ),
        ]
        if inventory.install_mode == "nvidia":
            optional_packages.append("nvidia-container-toolkit")
            steps.append(
                InstallStep(
                    name="configure-nvidia-container-toolkit",
                    details=(
                        "Install the NVIDIA Container Toolkit only after NVIDIA hardware or "
                        "driver support is detected."
                    ),
                )
            )
        elif inventory.install_mode == "amd":
            steps.append(
                InstallStep(
                    name="skip-nvidia-toolkit",
                    details=(
                        "Skip NVIDIA-specific container packages and rely on the detected AMD "
                        "host drivers."
                    ),
                )
            )
        else:
            steps.append(
                InstallStep(
                    name="cpu-only-mode",
                    details=(
                        "Skip GPU runtime packages entirely and keep the installation CPU-only."
                    ),
                )
            )
        if inventory.virtualization == "vmware":
            optional_packages.append("open-vm-tools")
            steps.append(
                InstallStep(
                    name="preserve-vmware-integration",
                    details=(
                        "Preserve VMware guest tools and host time synchronization instead of "
                        "disabling the integration stack."
                    ),
                )
            )
        steps.extend(
            [
                InstallStep(
                    name="apply-kernel-tuning",
                    details=(
                        "Apply conservative sysctl, file-limit, swap, and filesystem tuning "
                        "before exposing the gateway."
                    ),
                ),
                InstallStep(
                    name="configure-ufw",
                    details=(
                        "Configure UFW with gateway ports closed to the public by default and "
                        "retain the host firewall policy across reboot."
                    ),
                ),
                InstallStep(
                    name="install-systemd-services",
                    details=(
                        "Install and enable the ZeaZ provider service, update timer, and health "
                        "check entry points."
                    ),
                ),
                InstallStep(
                    name="verify-backup-update-rollback",
                    details=(
                        "Retain atomic backups, verified updates, and rollback markers for "
                        "recovery after a failed switch."
                    ),
                ),
                InstallStep(
                    name="install-uninstall-path",
                    details=(
                        "Install the reversible uninstall path that disables services and removes "
                        "wrappers without exposing credentials."
                    ),
                ),
            ]
        )
        warnings = list(inventory.warnings)
        if inventory.memory_bytes and inventory.memory_bytes < 4 * 1024**3:
            warnings.append("host memory is below the recommended 4 GiB baseline")
        if inventory.disk_free_bytes and inventory.disk_free_bytes < 12 * 1024**3:
            warnings.append("host free disk is below the recommended 12 GiB baseline")
        if inventory.virtualization not in {"vmware", "bare-metal", "unknown"}:
            warnings.append("guest integration is not VMware-specific")
        return InstallPlan(
            package_sources=tuple(package_sources),
            packages=tuple(packages),
            optional_packages=tuple(dict.fromkeys(optional_packages)),
            steps=tuple(steps),
            warnings=tuple(dict.fromkeys(warnings))[:32],
        )

    def _cpu(self, warnings: list[str]) -> tuple[str, set[str]]:
        value = _read(self.proc_root / "cpuinfo")
        model = ""
        flags: set[str] = set()
        for line in value.splitlines():
            key, _, item = line.partition(":")
            if key.strip().lower() in {"model name", "hardware", "processor"} and not model:
                model = item.strip()
            if key.strip().lower() in {"flags", "features"}:
                flags.update(item.split())
        if not value:
            warnings.append("cpu metadata unavailable")
        return model or platform.processor() or "unknown", flags

    def _memory(self, warnings: list[str]) -> int:
        match = re.search(
            r"^MemTotal:\s+(\d+)\s+kB$", _read(self.proc_root / "meminfo"), re.MULTILINE
        )
        if not match:
            warnings.append("memory metadata unavailable")
            return 0
        return min(int(match.group(1)) * 1024, 1 << 50)

    def _disk(self, warnings: list[str]) -> tuple[int, int]:
        try:
            usage = shutil.disk_usage(self.disk_path)
        except OSError:
            warnings.append("disk metadata unavailable")
            return 0, 0
        return min(usage.total, 1 << 60), min(usage.free, 1 << 60)

    def _virtualization(self, flags: set[str]) -> Virtualization:
        text = " ".join(
            _read(self.sys_root / "class" / "dmi" / "id" / name).lower()
            for name in ("sys_vendor", "product_name", "product_version")
        )
        if "vmware" in text:
            return "vmware"
        if "microsoft corporation" in text or "virtual machine" in text:
            return "hyperv"
        if "kvm" in text or (self.dev_root / "kvm").exists():
            return "kvm"
        if "hypervisor" in flags:
            return "virtualized"
        if text.strip():
            return "bare-metal"
        return "unknown"

    def _gpu(self, warnings: list[str]) -> tuple[GpuVendor, list[str]]:
        devices: list[str] = []
        vendors: set[str] = set()
        drm = self.sys_root / "class" / "drm"
        try:
            entries = sorted(drm.iterdir())
        except OSError:
            entries = []
        for entry in entries[:64]:
            if not entry.name.startswith("card") or "-" in entry.name:
                continue
            vendor_id = _read(entry / "device" / "vendor").strip().lower()
            if vendor_id == "0x10de":
                vendors.add("nvidia")
                devices.append(entry.name)
            elif vendor_id == "0x1002":
                vendors.add("amd")
                devices.append(entry.name)
        if (self.proc_root / "driver" / "nvidia" / "version").exists():
            vendors.add("nvidia")
            devices.append("nvidia-driver")
        if not vendors:
            return "none", devices
        if len(vendors) > 1:
            warnings.append("multiple GPU vendors detected; selecting NVIDIA mode")
            return "nvidia", devices
        return next(iter(vendors)), devices


def _absolute(value: Path, name: str) -> Path:
    if not value.is_absolute():
        raise ValueError(f"{name} must be absolute")
    return value


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:65_536]
    except (OSError, UnicodeError):
        return ""


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="zeaz-host-detect", description="Read-only ZeaZ host capability detection"
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--plan",
        action="store_true",
        help="include the dry-run installer plan derived from the detected host",
    )
    args = parser.parse_args()
    detector = HostDetector()
    inventory = detector.detect()
    if args.plan and args.json:
        print(
            json.dumps(
                {
                    "inventory": inventory.model_dump(mode="json"),
                    "plan": detector.plan(inventory).model_dump(mode="json"),
                },
                sort_keys=True,
            )
        )
        return
    if args.plan:
        plan = detector.plan(inventory)
        print(f"Installation mode: {inventory.install_mode}")
        print(f"Package sources: {', '.join(plan.package_sources)}")
        print(f"Packages: {', '.join(plan.packages)}")
        if plan.optional_packages:
            print(f"Optional packages: {', '.join(plan.optional_packages)}")
        for step in plan.steps:
            print(f"- {step.name}: {step.details}")
        for warning in plan.warnings:
            print(f"Warning: {warning}")
        return
    if args.json:
        print(json.dumps(inventory.model_dump(mode="json"), sort_keys=True))
        return
    print(f"CPU: {inventory.cpu_model} ({inventory.logical_cpus} logical CPUs)")
    print(f"Memory: {inventory.memory_bytes} bytes; disk free: {inventory.disk_free_bytes} bytes")
    print(f"Virtualization: {inventory.virtualization}; GPU: {inventory.gpu_vendor}")
    print(f"Recommended installation mode: {inventory.install_mode}")
