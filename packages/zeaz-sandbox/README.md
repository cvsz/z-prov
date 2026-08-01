# zeaz-sandbox

Isolated execution service for ZeaZ. It accepts only immutable argv jobs bound
to explicit approvals and digest-pinned images. Runtime backends must prove
rootless isolation and emit bounded, redacted output plus an execution receipt.

This package is separate from both `zeaz-provider` and `zeaz-agent`; neither
gateway requests nor model output can directly invoke its backend.

The runtime requires a digest-pinned local image and a rootless Docker daemon
that reports seccomp and AppArmor support. Networking is disabled unless an
exact destination policy is enforced through the internal-network SOCKS
sidecar. A private journal persists receipts and cleanup leases.

The backend verifies isolation twice: daemon startup must advertise rootless,
seccomp, and AppArmor support, and every created container must report the
requested AppArmor profile plus its seccomp and no-new-privileges settings.
If a runtime silently ignores any setting, the container is removed before it
can start.

## Unprivileged LXC backend

`RootlessLxcBackend` is the alternate runtime for hosts where rootless Docker
cannot apply AppArmor. The operator supplies a fixed mapping from approved
image digest references to pre-provisioned root filesystems. Root filesystems
must be real directories and cannot be group/world writable.

Each job receives a private LXC definition with:

- the preloaded `lxc-container-default-cgns` AppArmor profile in enforce mode;
- LXC's common seccomp policy and an empty capability set;
- an empty network namespace;
- exact CPU, memory, swap, PID, file-size, temporary-space, and output limits;
- one caller-mapped workspace bind mount;
- clean environment and exact argv execution as an unprivileged job UID.

Startup verifies the effective AppArmor label, zero effective capabilities,
PID limit, and memory limit from inside the container before job argv runs.
The backend currently accepts network-disabled jobs only; allow-listed egress
continues to require the Docker backend's isolated proxy network.
