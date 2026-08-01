# zeaz-infra

Dry-run host capability detection for the ZeaZ installer. It reads local
kernel and filesystem metadata only; it does not install packages, change
firewall state, load drivers, or contact a network service.

Run zeaz-host-detect with --json to emit bounded JSON describing CPU, memory,
available disk, virtualization hints, NVIDIA/AMD device hints, and the
recommended nvidia, amd, or cpu-only installation mode.

Run `zeaz-host-detect --plan` to emit the dry-run install plan, including the
official Docker repository, optional NVIDIA Container Toolkit, VMware guest
integration, conservative tuning, UFW defaults, service hooks, health checks,
backup and rollback retention, and uninstall support.
