# ZeaZ Platform Roadmap

This file is the execution contract for Codex CLI. It converts the behavioral
inventory from zcoder v1.36.0 into clean-room ZeaZ work packages without
copying source, proprietary prompts, or undocumented implementation details.

## Goal

Build a standalone AI platform with:

- Anthropic Messages API compatibility
- OpenAI Chat Completions compatibility
- OpenAI Responses/Codex compatibility
- multi-provider routing under stable `zeaz-*` model aliases
- local-first and policy-controlled cloud fallback
- isolated agents, tools, skills, MCP, and execution
- optional enterprise control-plane APIs
- Ubuntu 26.04 VM deployment with Cloudflare TLS and Terraform

## Repository boundaries

| Component | Responsibility | Forbidden |
|---|---|---|
| `zeaz-provider` | Protocol gateway, provider routing, reliability, capabilities | Shell execution, plugins, user files, agent state |
| `zeaz-agent` | Agent loop, sessions, memory, skills, MCP, permissions | Provider keys exposed to tools, unrestricted host execution |
| `zeaz-sandbox` | Tool/code workers in containers or microVMs | Host filesystem/network access by default |
| `zeaz-control` | Files, batches, usage, admin and compliance adapters | Sharing high-privilege credentials with gateway |
| `zeaz-web` | Web console and optional TUI client | Direct provider or compliance credentials |
| `zeaz-infra` | VMware guest bootstrap, Terraform, Cloudflare, TLS, observability | Secrets committed to Git |

Components may begin as directories in a monorepo, but process and credential
boundaries are mandatory.

## Source policy

Allowed references:

- `aicoder`: public behavior and Anthropic API coverage inventory
- `zai-coder`: safe-execution, audit, and clean-room architecture inventory
- `zc/app`: provider abstraction and FastAPI behavior inventory
- `zc/.zc/agents`: behavioral inventory only
- `zcoder-v1.36.0`: behavioral inventory and gap discovery only
- official provider documentation and public API schemas

Never copy implementation source or proprietary prompts from reference
projects. Every feature must have a ZeaZ design note, independent tests, and
provenance links to public specifications.

## Current baseline

`zeaz-provider` `0.4.0rc1` currently provides:

- typed provider errors
- bounded retries and total deadlines
- per-provider circuit breakers
- pre-first-byte-only fallback
- dynamic capability registry with TTL and provenance
- retired-model-aware aliases
- Anthropic, Chat Completions, and Responses request conversion
- cross-protocol SSE text and function-tool translation
- request IDs, local rate limiting, redaction, and security headers
- standalone installer, updater, SBOM, and 537 passing tests under `make validate`

External production gates remain open: Docker validation, signed update
manifests, Cloudflare/Terraform deployment, VM smoke tests, and live-provider
contract tests.

## Delivery rules for Codex CLI

For every task:

1. Read `AGENTS.md` and this file.
2. Work only on the first milestone whose status is not complete.
3. Inspect existing code and preserve unrelated changes.
4. Write or update tests before declaring behavior complete.
5. Use dry-run for installers, infrastructure, destructive actions, and
   external mutations.
6. Run the milestone validation commands.
7. Update the milestone status, evidence, and remaining risks in this file.
8. Do not advance when a required gate is skipped; record it as blocked.
9. Do not commit `.env`, provider keys, Cloudflare tokens, Terraform state,
   compliance credentials, prompts containing secrets, or generated user data.

Suggested Codex CLI invocation:

```bash
codex exec --full-auto \
  "Read AGENTS.md and ROADMAP.md. Implement only the next uncompleted task. \
  Add tests, run the listed validation, update ROADMAP.md evidence, and stop \
  on external credentials, destructive actions, or unresolved security risk."
```

For review-only runs:

```bash
codex exec \
  "Read AGENTS.md and ROADMAP.md. Review the current milestone against its \
  acceptance criteria. Do not modify external systems. Report blockers and \
  propose the smallest safe patch."
```

## Milestone status

| ID | Milestone | Status |
|---|---|---|
| P0 | Gateway reliability kernel | Complete |
| P1 | Capability registry and lifecycle | Complete |
| P2 | Core protocol conversion and SSE | Complete |
| P3 | Gateway production hardening | Complete |
| A1 | Agent runtime foundation | Complete |
| A2 | Skills, plugins, MCP, and hooks | Complete |
| S1 | Isolated execution service | Complete |
| C1 | Files, batches, usage, and models control plane | Complete |
| C2 | Admin, compliance, WIF, vaults, and deployments | Complete |
| W1 | Web console and TUI | Complete |
| I1 | Ubuntu/VMware installer and host optimization | Planned |
| I2 | Terraform, Cloudflare, TLS, and release automation | Planned |
| R1 | v1.0 production release | Planned |

## P3 — Gateway production hardening

### Tasks

- [x] Request IDs
- [x] Basic per-client in-memory rate limiting
- [x] Response security headers
- [x] Provider-error secret redaction
- [x] CycloneDX SBOM generation
- [x] Structured JSON audit events without prompts or credentials
- [x] Prometheus metrics and optional OpenTelemetry export
- [x] Distributed rate-limit backend interface with Redis implementation
- [x] Trusted-proxy policy for Cloudflare; never trust forwarded headers by default
- [x] Constant-time client-key verification with hashed key configuration
- [x] Request concurrency and response-byte limits
- [x] Read-only container filesystem and dropped Linux capabilities
- [x] Pin base image by digest
- [x] Lock Python dependencies with hashes
- [x] Sign update manifests and verify before installation
- [x] Automated rollback and interrupted-update tests
- [x] Remove the Starlette/httpx test deprecation warning

### Acceptance

- Permanent client errors do not retry or open provider circuits.
- Requests and logs never expose provider/client keys.
- Multi-worker deployments use a shared rate-limit backend.
- Containers run non-root with a read-only root filesystem.
- Update artifacts fail closed on signature or checksum mismatch.
- All unit, protocol, installer, container, dependency, and secret scans pass.

### Validation

```bash
make validate
docker compose config --quiet
shellcheck scripts/*.sh
docker build --pull --no-cache -t zeaz/provider:0.4.0 .
docker run --rm --read-only --cap-drop=ALL zeaz/provider:0.4.0 --version
```

## A1 — Agent runtime foundation

Create a separate `zeaz-agent` package.

### Tasks

- [x] Define session, turn, content-block, tool-call, and tool-result schemas.
- [x] Implement provider-neutral agent loop using `zeaz-provider`.
- [x] Add explicit permission decisions: allow, deny, ask, and policy rule.
- [x] Add plan mode that cannot mutate until approved.
- [x] Add append-only audit events with correlation IDs.
- [x] Add resumable sessions with optimistic concurrency.
- [x] Add bounded context compaction and token budgets.
- [x] Add memory interface with local SQLite implementation.
- [x] Add subagent limits, depth limits, budgets, and cancellation.
- [x] Add deterministic fake-provider fixtures.

### Acceptance

- No tool executes without a recorded decision.
- Session replay produces the same state.
- Concurrent session writes cannot silently overwrite each other.
- Agent and subagent budgets are enforced.
- Provider credentials never enter tool input or session storage.

## A2 — Skills, plugins, MCP, and hooks

### Tasks

- [x] Load local `SKILL.md` packages using a versioned manifest.
- [x] Validate skill names, paths, sizes, and referenced resources.
- [x] Add MCP stdio and HTTPS transports in the agent process.
- [x] Add host and method allow-lists for remote MCP.
- [x] Add pre/post-tool hooks with timeouts and immutable input snapshots.
- [x] Add plugin registry with disabled-by-default state.
- [x] Require signed plugin archives.
- [x] Reject traversal, links, devices, duplicate paths, excessive expansion,
  and archive bombs.
- [x] Install plugins atomically into versioned directories.
- [x] Keep plugin executables out of the gateway process.

### Acceptance

- Malicious archive fixtures are rejected.
- MCP cannot reach unapproved hosts or methods.
- Hook failure policy is explicit and tested.
- Plugin removal is recoverable and audited.

## S1 — Isolated execution service

Create `zeaz-sandbox`.

### Tasks

- [x] Define a job API with immutable command argv; never accept shell strings.
- [x] Run jobs in rootless containers or microVMs.
- [x] Disable network by default and add destination allow-lists.
- [x] Mount a single workspace with configurable read/write policy.
- [x] Enforce CPU, memory, process, file, output, and time limits.
- [x] Use seccomp/AppArmor and drop all unnecessary capabilities.
- [x] Stream stdout/stderr with byte limits and secret redaction.
- [x] Generate an execution receipt with image digest, policy, and exit state.
- [x] Add cancellation and cleanup reconciliation.

### Acceptance

- Escape, fork-bomb, disk-fill, metadata-service, and symlink tests fail safely.
- Every execution has an approval and receipt.
- Worker compromise does not expose gateway or control-plane credentials.

## C1 — Standard control plane

Create `zeaz-control` with separate credentials and deployment profile.

### Tasks

- [x] Provider model discovery and lifecycle reconciliation
- [x] Files upload/list/get/download/delete with strict size and MIME policy
- [x] Message Batch submit/list/get/cancel/results
- [x] Usage and normalized cost-event ingestion
- [x] Idempotency keys for create/cancel operations
- [x] Transactional state and audit history
- [x] Provider extension namespace for nonportable features

### Acceptance

- Large or malformed files cannot exhaust service memory.
- Batch pagination and retries never skip or duplicate records.
- Cost values identify their pricing source and observation date.

## C2 — Enterprise control plane

Deploy separately from C1.

### Tasks

- [x] Admin API adapter
- [x] Compliance API adapter
- [x] Workload Identity Federation
- [x] Vault references without secret material in application state
- [x] Managed-agent environment/deployment adapters
- [x] Memory-store and outcome/dream adapters where publicly documented
- [x] Webhooks with signature validation and replay protection
- [x] Dry-run-by-default permanent deletion
- [x] Dual authorization for organization-wide destructive actions

### Acceptance

- Regular, admin, and compliance credentials have separate stores and roles.
- Permanent deletion requires target resolution, dry-run evidence, and explicit
  confirmation.
- Webhook duplicates are idempotent and forged signatures are rejected.

## W1 — Web console and TUI

### Tasks

- [x] Provider/model/route health dashboard
- [x] Streaming chat for all three compatibility protocols
- [x] Session, plan, approval, and tool-result views
- [x] Audit and execution-receipt explorer
- [x] Admin surface isolated behind stronger authentication
- [x] Accessible keyboard navigation and responsive layout
- [x] No secrets in browser storage

## I1 — Ubuntu 26.04 on VMware/Windows 11

### Tasks

- [x] Detect CPU, RAM, disk, virtualization, NVIDIA, AMD, and CPU-only modes.
- [x] Install Docker Engine from the official repository.
- [x] Configure NVIDIA Container Toolkit only when supported.
- [x] Apply conservative sysctl, file-limit, swap, and filesystem tuning.
- [x] Preserve VMware guest integration and time synchronization.
- [x] Configure UFW with gateway ports closed to the public by default.
- [x] Add systemd services, health checks, backup, update, rollback, and uninstall.
- [ ] Test clean install and upgrade on a fresh Ubuntu 26.04 VM snapshot.

### Acceptance

- The installer is idempotent and dry-run-first.
- CPU-only installation succeeds without GPU packages.
- Reboot retains services, models, configuration, and firewall policy.
- Rollback restores the prior working version.

## I2 — Terraform, Cloudflare, TLS, and releases

Target domain: `zeaz.dev`.

### Tasks

- [ ] Terraform modules for Cloudflare DNS, proxied records, and origin policy
- [ ] Cloudflare Tunnel option for hosts without inbound port forwarding
- [ ] Authenticated origin pulls or tunnel-only origin access
- [ ] Full-strict TLS and minimum TLS policy
- [ ] Rate-limit/WAF rules for public API routes
- [ ] Secrets supplied through environment or secret manager, never state output
- [ ] Remote encrypted Terraform state with locking
- [ ] GitHub Actions: tests, builds, SBOM, scans, signatures, and release manifest
- [ ] Signed update channel: stable, candidate, and rollback metadata
- [ ] Automated post-deploy health and protocol smoke tests

### Required variables

```text
CLOUDFLARE_API_TOKEN
CLOUDFLARE_ACCOUNT_ID
CLOUDFLARE_ZONE_ID
ZEAZ_DOMAIN=zeaz.dev
ZEAZ_ORIGIN_HOST
ZEAZ_RELEASE_PUBLIC_KEY
```

Never place variable values in this file, Git, CI logs, or Terraform outputs.

## R1 — Production v1.0

Release only when:

- [ ] P3, A1, A2, S1, C1, W1, I1, and I2 are complete.
- [ ] C2 is complete or explicitly excluded from the v1.0 support statement.
- [ ] Threat model and incident-response runbook are reviewed.
- [ ] Backup restore and disaster recovery are exercised.
- [ ] All supported providers pass live restricted-key contract tests.
- [ ] Independent security review has no unresolved critical/high findings.
- [ ] Release artifacts, images, SBOMs, and manifests are signed.
- [ ] Documentation identifies experimental and provider-specific features.

## Evidence log

Update this table after validated work. Do not record credentials or sensitive
environment details.

| Date | Milestone | Evidence | Result |
|---|---|---|---|
| 2026-07-27 | P0–P2 | `make validate` | 33 tests passed; one dependency deprecation warning |
| 2026-07-27 | RC installer | isolated temporary prefix install | `0.4.0rc1` imported and CLI version passed |
| 2026-07-27 | RC artifact | ZIP integrity and secret-pattern scan | passed |
| 2026-07-27 | Container gates | Docker unavailable in build environment | blocked |
| 2026-07-27 | TLS/Cloudflare/Terraform | credentials and target VM unavailable | blocked |
| 2026-07-26 | P3 structured audit events | `make validate` | 37 tests passed; lint, compile, and shell parse passed; 7 Python 3.14 dependency warnings |
| 2026-07-26 | P3 remaining milestone gates | Docker and `shellcheck` executables unavailable | blocked; Compose, container, and standalone ShellCheck validation not run |
| 2026-07-26 | P3 audit validation retry | `shellcheck scripts/*.sh` | passed; Docker executable remains unavailable |
| 2026-07-26 | P3 Docker validation retry | `docker compose config --quiet`; `make validate` | Compose passed after safe local `make env-init`; 37 tests passed; image build/run blocked by missing `cvsz` membership in the `docker` group |
| 2026-07-26 | P3 structured audit completion | `make validate`; Compose; ShellCheck; no-cache Docker build; read-only/cap-drop runtime | passed; 37 tests, image `zeaz/provider:0.4.0`, non-root user `zeaz`, CLI `0.4.0rc1` |
| 2026-07-26 | P3 metrics and OTLP | `make validate`; Compose; ShellCheck; no-cache Docker build; read-only/cap-drop runtime | passed; 41 tests; fixed-cardinality Prometheus labels; OTLP/HTTP opt-in; direct Prometheus scraping remains process-local |
| 2026-07-26 | P3 distributed rate limiting | `make validate`; live disposable Redis; Compose; ShellCheck; no-cache Docker build; read-only/cap-drop runtime | passed; 46 tests; atomic Redis sliding window; shared server time; sanitized fail-closed `503`; no in-memory fallback |
| 2026-07-26 | P3 Cloudflare trusted proxy | `make validate`; Compose; ShellCheck; no-cache Docker build; read-only/cap-drop runtime | passed; 53 tests; forwarded headers ignored by default; explicit direct-peer CIDRs; IPv4/IPv6 and spoofing coverage |
| 2026-07-26 | P3 hashed client keys | `make validate`; Compose; ShellCheck; no-cache Docker build; read-only/cap-drop runtime | passed; 59 tests; hash-only production configuration; constant-time all-digest comparison; legacy keys hashed immediately; no plaintext runtime storage |
| 2026-07-26 | P3 concurrency and response limits | `make validate`; Compose; ShellCheck; no-cache Docker build; read-only/cap-drop runtime | passed; 69 tests; fail-fast per-worker admission; bounded decoded provider JSON/errors/streams; bounded translated SSE and final responses; slot cleanup coverage |
| 2026-07-26 | P3 container hardening | `make validate`; Compose; ShellCheck; no-cache Docker build; roadmap runtime command; `make validate-container` | passed; 71 tests; UID 10001; root mount `ro`; owner-write probe denied; bounded `/tmp` writable; `CapEff=0`; `NoNewPrivs=1` |
| 2026-07-26 | P3 base-image pin | `make validate`; Compose; ShellCheck; no-cache Docker build; runtime hardening; base inspect | passed; 73 tests; both stages resolved to `sha256:d50fb…aa30b`; secret-safe build context reduced from about 111 MB to 599 kB |
| 2026-07-26 | P3 hashed Python dependency locks | `make validate`; Compose; ShellCheck; hash-enforced local install; no-cache Python 3.12 Docker build; read-only/cap-drop runtime; `make validate-container` | passed; 73 tests; exact runtime/dev/build locks; Docker installs hash-verified dependencies from a local wheelhouse; editable build dependencies explicit |
| 2026-07-26 | P3 signed update manifests | `make validate`; signed/tampered/untrusted/missing-key update contracts; Compose; ShellCheck; no-cache Docker build; read-only/cap-drop runtime; `make validate-container` | passed; 77 tests; exact manifest bytes verified with detached Ed25519 signature before parsing or installation; signature downloads bounded; unattended updater receives only the public key |
| 2026-07-26 | P3 transactional installer rollback | `make validate`; success/post-switch-failure/SIGTERM installer contracts; Compose; ShellCheck; no-cache Docker build; read-only/cap-drop runtime; `make validate-container` | passed; 80 tests; private staging and import validation precede atomic publication; interrupted staging is removed; post-switch failures restore the prior current target |
| 2026-07-26 | P3 warning-free dependency compatibility | `pytest -W error::DeprecationWarning`; `make validate`; Compose; ShellCheck; no-cache Docker build; read-only/cap-drop runtime; `make validate-container` | passed; 80 tests without warnings; FastAPI 0.117.1 uses Python 3.14-safe coroutine inspection while retaining the compatible Starlette 0.47.3/httpx 0.28.1 stack |
| 2026-07-26 | A1 provider-neutral schemas | `pip wheel --no-deps --no-build-isolation packages/zeaz-agent`; `make validate` | passed; separate `zeaz-agent` wheel built; 89 tests; immutable versioned session/turn/content/tool models; round-trip, discriminator, ownership, ordering, timestamp, bound, and rejection contracts |
| 2026-07-26 | A1 provider-neutral agent loop | `make validate`; standalone `zeaz-agent` wheel build; Compose validation | passed; 103 tests; bounded Responses gateway client; stable alias enforcement; immutable model-turn state machine; exact tool-result resumption; no automatic tool execution; Responses function definitions converted for Chat providers |
| 2026-07-26 | A1 explicit permission decisions | `make validate`; standalone `zeaz-agent` wheel build; Compose validation | passed; 116 tests; immutable allow/deny/ask records; bounded attribute rules; secure precedence; ask resolution provenance; allow bound to session, correlation, call identity, tool name, and canonical argument digest |
| 2026-07-26 | A1 mutation-safe plan mode | `make validate`; standalone `zeaz-agent` wheel build; Compose validation | passed; 129 tests; revisioned plan-mode state; unknown tools fail closed as mutating; approvals bind exact plan/session/action digests; combined execution boundary requires independent tool allow and plan approval |
| 2026-07-26 | A1 append-only correlated audit ledger | `make validate`; standalone `zeaz-agent` wheel build; Compose validation | passed; 140 tests; owner-only no-follow JSONL; locked append sequencing; SHA-256 chain verification; content/credential redaction; size/depth bounds; loop, permission, and plan metadata emissions |
| 2026-07-26 | A1 resumable optimistic sessions | `make validate`; standalone `zeaz-agent` wheel build; Compose validation | passed; 149 tests; owner-only SQLite repository; full schema revalidation on load; revision compare-and-swap under `BEGIN IMMEDIATE`; stale and concurrent writers fail explicitly; session size and credential-shaped tool arguments bounded |
| 2026-07-26 | A1 bounded context and token budgets | `make validate`; standalone `zeaz-agent` wheel build; Compose validation | passed; 157 tests; durable context/output/total budgets; deterministic request-only summary projection; full replay history retained; tool schemas counted; remaining-budget output reduction; untrusted usage validated and accumulated |
| 2026-07-26 | A1 scoped local memory | `make validate`; standalone `zeaz-agent` wheel build; Compose validation | passed; 166 tests; provider-neutral memory interface; namespace-isolated SQLite create/get/search; escaped literal queries; bounded records/results; optimistic revision conflicts; secure shared local database |
| 2026-07-26 | A1 bounded subagent scheduler | `make validate`; standalone `zeaz-agent` wheel build; Compose validation | passed; 174 tests; manager-derived depth; bounded concurrency/lifetime/time; atomic token reservations; failure/cancellation charged fail-closed; hierarchical cancellation; sanitized lifecycle audit |
| 2026-07-26 | A1 deterministic provider fixtures and acceptance audit | `make validate`; focused 55-test A1 acceptance suite; standalone `zeaz-agent` wheel build; Compose validation; agent execution/credential scan | passed; 179 tests; finite scripted model outputs/faults; exact request fingerprints; deterministic UUID/clock injection; identical replay state; no host-command path or provider credential identifiers in agent source; all A1 tasks and acceptance criteria evidenced |
| 2026-07-26 | A2 versioned local skill packages | `make validate`; focused 18-test skill security suite; standalone `zeaz-agent` wheel build; Compose validation | passed; 197 tests; strict versioned manifest; closed-world SHA-256 inventory; bounded no-follow reads; portable names and paths; declared Markdown references; traversal, links, devices, encoding, expansion, and executable non-execution fixtures |
| 2026-07-26 | A2 bounded MCP transports and policy | `make validate`; focused 19-test MCP transport suite; standalone `zeaz-agent` wheel build; Compose validation | passed; 216 tests; MCP 2025-11-25 newline stdio and Streamable HTTPS JSON/SSE; no shell or inherited environment; strict JSON-RPC correlation; exact host/method allow-lists; no redirects; bounded requests, responses, events, and time |
| 2026-07-26 | A2 bounded pre/post-tool hooks | `make validate`; focused 11-test hook policy suite; standalone `zeaz-agent` wheel build; Compose validation | passed; 227 tests; ordered phase callbacks; canonical immutable input/output snapshots and digests; per-hook deadlines; explicit fail-open/fail-closed outcomes; sanitized failures; cancellation propagation |
| 2026-07-26 | A2 signed atomic plugin lifecycle and acceptance audit | `make validate`; focused 68-test A2 suite; standalone `zeaz-agent` wheel build; Compose validation; gateway build-boundary inspection | passed; 247 tests; exact-byte Ed25519 trust; closed manifests; traversal/link/device/duplicate/expansion/bomb rejection; private atomic version publication; disabled default; activation-time integrity; audit-failure rollback; recoverable audited removal; agent package excluded from gateway image |
| 2026-07-26 | S1 immutable approved job contracts | `make validate`; focused 22-test sandbox schema suite; standalone `zeaz-sandbox` wheel build; Compose validation | passed; 269 tests; separate package; immutable bounded argv with no shell/environment field; digest-only images; exact-spec expiring approvals; default-deny egress; bounded policy; terminal receipt schema |
| 2026-07-26 | S1 isolation runtime implementation (live rootless gate open) | `make validate`; focused 59-test sandbox suite; standalone `zeaz-sandbox` wheel; pinned egress image build/inspect; live system-daemon rejection probe | implementation passed; 306 tests; internal-only exact SOCKS egress; single workspace; CPU/memory/PID/file/tmp/output/time bounds; seccomp/AppArmor/cap-drop argv; cross-chunk redaction; durable receipts; cancellation and orphan reconciliation; rootful daemon rejected; positive live rootless and adversarial container acceptance remain open because host unprivileged user namespaces require administrator configuration |
| 2026-07-26 | S1 live rootless retry and confinement verification | temporary user-owned Ubuntu rootlesskit/slirp4netns toolchain; official Docker rootless launcher; digest-pinned container probes; focused backend tests; `make validate`; standalone wheel build; Compose validation | implementation passed; 362 tests; rootless daemon and seccomp passed, but AppArmor requests were silently ignored and processes ran as `runc (unconfined)`, so live gate remains open; backend now verifies per-container AppArmor/security metadata and removes downgraded containers before start |
| 2026-07-26 | S1 unprivileged LXC completion | focused 32-test backend/service suite; live AppArmor/capability/cgroup/workspace probes; approved end-to-end service execution and durable receipt; live escape, disk-fill, metadata-service, symlink, and fork-bomb probes; `make validate`; standalone wheel build; Compose validation | passed; 367 tests; unprivileged user-mapped LXC; enforced `lxc-container-default-cgns`; common seccomp; zero job capabilities; empty network namespace; bounded CPU/memory/swap/PIDs/files/tmp/output/time; exact argv and clean environment; single workspace; adversarial jobs failed safely and containers cleaned |
| 2026-07-26 | C1 provider model discovery and lifecycle | `make validate`; focused 21-test control-model suite; standalone `zeaz-control` wheel build; Compose validation | passed; 327 tests; bounded credential-isolated OpenAI/Anthropic adapters; cursor and duplicate defenses; complete-snapshot SQLite reconciliation; delayed retirement and reactivation; same-transaction audit history; no credential persistence |
| 2026-07-26 | C1 bounded Files lifecycle | `make validate`; focused 24-test control-file suite; standalone `zeaz-control` wheel build; Compose validation | passed; 340 tests; bounded private streaming staging; strict purpose/MIME/magic/UTF-8/JSONL validation; OpenAI multipart/content/delete adapter; metadata and digest binding; stable cursor catalog; bounded integrity-checked downloads; transactional audit |
| 2026-07-26 | C1 Message Batch lifecycle and idempotency | `make validate`; focused 27-test batch/adapter suite; standalone `zeaz-control` wheel build; Compose validation | passed; 354 tests; submit/list/get/cancel/results; batch-file binding; durable bounded create/cancel keys; retry-safe provider propagation; atomic state/idempotency/audit completion; bounded pagination and result streams; duplicate, stale, count-regression, and terminal-regression defenses |
| 2026-07-26 | C1 usage, normalized cost, transactional audit, and extensions | `make validate`; focused control-plane suite; standalone `zeaz-control` wheel build; Compose validation | passed; 361 tests; bounded usage counters; exact non-float decimal costs; mandatory pricing source and observation date; usage-scope binding; conflict-safe idempotent event IDs; same-transaction audit writes; provider-matched extension namespaces |
| 2026-07-26 | C1 separate deployment profile | `make validate`; control health/state test; `docker compose --profile control config --quiet`; pinned image build; read-only/cap-drop/no-new-privileges runtime probe | passed; 361 tests; separate process, image, UID 10002, persistent private state, loopback port, network, and credential boundary; gateway `.env` and configuration are not shared |
| 2026-07-26 | C2 credential-isolated Admin API adapters | focused 22-test enterprise-admin suite; `make validate`; standalone `zeaz-enterprise` wheel build; Compose validation; official OpenAI and Anthropic contract review | passed; 389 tests; separate enterprise package; bounded OpenAI users/projects and Anthropic users/workspaces; provider-specific pagination/auth; strict lifecycle schemas; nonassignable role rejection; idempotency keys; redirect/size/error sanitization; no removal or permanent deletion surface |
| 2026-07-26 | C2 read-only Anthropic Compliance API adapter | focused 12-test compliance suite; `make validate`; standalone `zeaz-enterprise` wheel build; Compose validation; official Anthropic Compliance contract review | passed; 401 tests; distinct typed Compliance Access Key role and declared scopes; bounded activity ID-cursor and organization opaque-token pagination; strict normalized records; duplicate/cursor/page/size/filter defenses; redirect and provider-error sanitization; permanent deletion intentionally absent |
| 2026-07-26 | C2 Anthropic Workload Identity Federation | focused 14-test WIF suite; `make validate`; standalone `zeaz-enterprise` wheel build; Compose validation; official Anthropic WIF and RFC 7523 contract review | passed; 415 tests; typed rule/org/service-account/workspace binding; fresh async OIDC assertion provider; exact bounded token exchange; single-flight early refresh; short-lived secret credential cache only; redirect/size/malformed/error sanitization; no cloud metadata assumptions or assertion persistence |
| 2026-07-26 | C2 provider vault references | focused 18-test vault-reference suite; `make validate`; standalone `zeaz-enterprise` wheel build; Compose validation; official AWS, Google Cloud, and Azure identifier/version contract review | passed; 433 tests; closed provider-discriminated application-state schemas; complete AWS secret ARNs with rotation selectors; explicit GCP version resources; immutable versioned Azure secret URLs; unknown providers, local paths, secret-value fields, partial identifiers, and malformed locators rejected |
| 2026-07-26 | C2 Managed Agents environments and deployments | focused 12-test managed-agent suite; `make validate`; standalone `zeaz-enterprise` wheel build; Compose validation; official Anthropic beta environment/deployment contract review | passed; 445 tests; distinct regular credential role; mandatory beta/version headers; limited-network cloud environments; bounded packages/metadata/IDs/timestamps; agent/environment/initial-event/cron/vault deployment binding; opaque pagination integrity; idempotent create/archive/pause/unpause/run; sanitized errors; permanent delete absent |
| 2026-07-26 | C2 agent memory, outcomes, and dreams | focused 12-test agent-memory suite; `make validate`; standalone `zeaz-enterprise` wheel build; Compose validation; official Anthropic memory/outcome/dream contract review | passed; 457 tests; endpoint-specific non-combined memory beta; Managed Agents outcome events with bounded text/file rubrics and iterations; research-preview dream creation from one store and 1–100 unique sessions; exact dual beta; opaque pagination; idempotent archive/cancel; malformed/size/redirect/error defenses; no memory deletion surface |
| 2026-07-26 | C2 verified idempotent webhook ingress | focused 17-test webhook suite; `make validate`; standalone `zeaz-enterprise` wheel build; Compose validation; official Anthropic SDK and Standard Webhooks contract review | passed; 474 tests; exact raw-body HMAC-SHA256; typed 32-byte `whsec_` key; constant-time multi-`v1` rotation checks; five-minute past/future rejection; signed/payload event-ID binding; bounded normalized envelopes; private payload-free SQLite replay ledger; atomic cross-thread/process event claims; restart duplicates explicit |
| 2026-07-26 | C2 permanent deletion and dual authorization boundary | focused 12-test deletion-control suite; `make validate`; standalone `zeaz-enterprise` wheel build; Compose validation | passed; 486 tests; non-mutating expiring dry-run plans; canonical resolution and dependency evidence digest; exact typed confirmation; fresh re-resolution and drift rejection; opaque plan/digest-bound grants; explicit authorization for every deletion; two distinct principals for organization-wide targets; bounded idempotency and immutable receipts; executor never called on failed controls |
| 2026-07-26 | W1 provider/model/route health dashboard | focused 3-test web suite; `make validate`; Compose and web-profile validation; standalone `zeaz-web` wheel build | passed; 493 tests; separate FastAPI web package and pinned web image definition; server-side gateway key injection; bounded concurrent health/model aggregation; provider route grouping; responsive keyboard-accessible static UI; no local/session browser storage or third-party runtime assets; loopback-only browser port |
| 2026-07-26 | W1 streaming chat across compatibility protocols | focused 6-test web suite; `make validate`; Compose and web-profile validation; standalone `zeaz-web` wheel build | passed; 496 tests; server-side bounded proxy for Anthropic Messages, OpenAI Chat Completions, and OpenAI Responses; exact path selection; forced streaming; request/response byte limits; sanitized redirect/error events; no browser credential or storage path; accessible protocol/model/prompt composer and SSE transcript |
| 2026-07-26 | C2 credential-role and acceptance audit | focused 23-test admin suite; cross-role rejection; credential persistence/source scan; webhook key introspection regression; `make validate`; wheel content inspection | passed; 490 tests; provider-bound admin credentials; regular/admin/compliance models are non-interchangeable and remain in-memory only; resource records and the sole SQLite ledger contain no credential material; all deletion and webhook acceptance cases directly evidenced |
| 2026-07-26 | C2 separate enterprise deployment profile | enterprise health/private-state test; static image/profile boundary tests; `docker compose config --quiet`; `docker compose --profile enterprise config --quiet`; standalone wheel build | passed for required validation; dedicated executable, pinned image definition, UID 10003, isolated network/state/loopback port, read-only/cap-drop/no-new-privileges policy, and no gateway `.env` or C1 state sharing; live image build/runtime not rerun because this session lacks Docker socket access and non-interactive sudo |

| 2026-07-26 | W1 session, plan, approval, tool-result, audit, and receipt explorer | focused state-reader/web suite; make validate; Compose and web-profile validation | passed; 498 tests; bounded read-only SQLite/JSONL/receipt projections; server-side sensitive-field redaction; derived plan/approval views; tool-call/result summaries; responsive sessions page with no browser storage |
| 2026-07-26 | W1 dedicated admin surface authentication | focused admin-auth web suite; make validate; Compose and web-profile validation | passed; 499 tests; admin UI has no data without a dedicated hash-only credential; constant-time digest matching; unauthenticated responses are indistinguishable from not-found; admin key is transient in browser memory and never persisted |
| 2026-07-26 | W1 accessibility, responsive layout, and browser-storage audit | focused static-contract web suite; make validate; Compose and web-profile validation | passed; 500 tests; viewport metadata, skip links, focus-visible controls, mobile media queries, and no local/session storage across console scripts; no third-party runtime assets |
| 2026-07-26 | I1 host detection, dry-run planner, doctor, and uninstall utilities | focused infra/maintenance suite; `make validate`; `docker compose config --quiet`; `shellcheck scripts/*.sh` | passed; 506 tests; bounded CPU/RAM/disk/virtualization/GPU inventory; dry-run install plan covers official Docker repository, optional NVIDIA toolkit, VMware integration, sysctl, UFW, systemd hooks, backup/update/rollback, and uninstall; reversible uninstall and health-check scripts added; Docker build/runtime gate remains blocked by host socket permissions in this environment |
| 2026-07-26 | I1 Ubuntu 26.04 bootstrap implementation | `tests/test_host_bootstrap.py`; `make validate`; `docker compose config --quiet`; `shellcheck scripts/*.sh`; `make host-bootstrap-dry-run` | passed; 509 tests; dry-run-first root bootstrap uses Docker and NVIDIA official APT sources, detects or selects CPU/AMD/NVIDIA mode, writes persistent sysctl/nofile/Docker policy, retains VMware tools/time sync, enables default-deny UFW without opening gateway ports, and installs the existing unprivileged service/update paths; fresh Ubuntu 26.04 VMware snapshot install and upgrade test remains open |
| 2026-07-26 | I1 VMware snapshot acceptance runner | `tests/test_vm_snapshot_runner.py`; focused I1 suite; `shellcheck scripts/*.sh`; `make vm-snapshot-dry-run VM_HOST=ubuntu@vmware.test VM_INSTALL_USER=zeaz` | passed; dry-run-only remote runner proves no SSH connection occurs until `--apply`, excludes `.env` and local configuration during transfer, requires a signed update manifest for apply, persists its public key under `/etc/zeaz`, and verifies clean install, Docker/UFW, reboot disconnect/reconnect persistence, and signed upgrade; actual VMware snapshot target and signed release manifest remain required to close the acceptance test |
| 2026-07-26 | I1 bounded DRM auto-detection | `tests/test_host_bootstrap.py`; `shellcheck scripts/bootstrap-host.sh`; `timeout 15s make host-bootstrap-dry-run` | passed; replaced recursive sysfs GPU search, which can block on host DRM symlinks, with bounded `card*/device/vendor` checks; AMD auto-detection regression coverage added |
| 2026-07-26 | I1 remote snapshot connection bounds | `tests/test_vm_snapshot_runner.py`; `shellcheck scripts/test-vm-snapshot.sh` | passed; every SSH/SCP operation now uses non-interactive single-attempt 15-second connection bounds; `cvsz@core.zeaz.dev` returned SSH exit 255 in this environment, so no remote bootstrap or acceptance evidence was produced |
| 2026-07-26 | I1 external VMware reachability check | read-only DNS and TCP/22 probes for `core.zeaz.dev` | blocked; IPv4 DNS resolves but TCP port 22 timed out after 10 seconds; no SSH authentication, source transfer, bootstrap, reboot, or upgrade operation reached the VM |
| 2026-07-26 | I1 VMware management endpoint check | read-only TCP/22 and SSH probes for `cvsz@192.168.74.130` | blocked; TCP/22 is reachable, but SSH host-key verification failed; host identity must be verified and trusted explicitly before any bootstrap operation |
| 2026-07-26 | I1 explicit VM SSH identity support | `tests/test_vm_snapshot_runner.py`; `shellcheck scripts/test-vm-snapshot.sh`; VM snapshot dry-run | passed; runner supports a validated absolute `--identity-file` and `ZEAZ_VM_IDENTITY_FILE`; a separate local Ed25519 automation key was generated without replacing the malformed default key; VM console must add its public half to `cvsz` authorized keys before acceptance can run |
| 2026-07-26 | I1 VMware authentication preflight | `tests/test_vm_snapshot_runner.py`; `shellcheck scripts/test-vm-snapshot.sh`; snapshot apply to `cvsz@192.168.74.130` | blocked; new SSH identity authenticates, but `sudo -n` requires interactive authentication; runner now preflights passwordless sudo before source/key transfer; no bootstrap, reboot, or upgrade operation ran |
| 2026-07-26 | I1 explicit sudo modes and bounded recovery | `tests/test_vm_snapshot_runner.py`; `make vm-snapshot-dry-run VM_SUDO_MODE=interactive`; `make validate`; `docker compose config --quiet`; `shellcheck scripts/*.sh` | passed; safe `noninteractive` default remains fail-closed; terminal-only `interactive` mode allocates a TTY and uses `sudo -v`; root commands are base64-encoded before remote execution to avoid command-string interpolation; SSH uses bounded retries/keepalives and reboot recovery uses exponential backoff; 516 tests passed |
| 2026-07-26 | I1 interactive sudo transfer transport fix | `tests/test_vm_snapshot_runner.py`; interactive VM snapshot dry-run; `shellcheck scripts/test-vm-snapshot.sh` | passed; interactive TTY is now allocated only for sudo commands, never source/archive transfer; prevents tar bytes from being emitted to the terminal and remote extraction from treating stdin as a TTY |
| 2026-07-26 | I1 remote sudo payload decode fix | `tests/test_vm_snapshot_runner.py`; `make validate`; `docker compose config --quiet`; `shellcheck scripts/*.sh` | passed; base64-encoded root command payloads are decoded on the VM before execution by Bash; regression test prevents encoded text from reaching the shell as a command; 518 tests passed |
| 2026-07-26 | I1 Docker Compose package-conflict recovery | `tests/test_host_bootstrap.py`; `make validate`; `docker compose config --quiet`; `shellcheck scripts/*.sh`; VMware snapshot apply | passed; VM apply exposed Ubuntu `docker-compose-v2` conflict with Docker official `docker-compose-plugin`; bootstrap now removes documented distro Docker conflicts and recovers incomplete dpkg state before official package installation; 519 tests passed; snapshot requires rerun from the updated transferred source |
| 2026-07-26 | I1 Python virtualenv prerequisite | direct retained-VM installer diagnostic; `tests/test_host_bootstrap.py`; `make validate`; `docker compose config --quiet`; `shellcheck scripts/*.sh` | passed; VM installer exposed missing Ubuntu `python3-venv`, preventing isolated release runtime creation; bootstrap now installs it before user installation; 521 tests passed; snapshot requires rerun from the updated transferred source |

## Definition of done

A checked task is not complete merely because code exists. It requires:

- tests for success, failure, boundary, and security behavior;
- user-facing or operator documentation;
- no skipped required validation;
- no embedded secrets;
- migration and rollback behavior where state changes;
- evidence recorded above.
