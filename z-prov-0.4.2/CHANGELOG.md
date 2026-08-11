# Changelog

## 0.4.2 - 2026-08-11

Audit-driven bugfix release, same methodology as 0.4.1: each fix below was
verified by reading the source, reproducing the gap with a new test, then
patching.

- **Fixed: streaming calls never emitted a usage event.** `log_usage_event()`
  was only ever called from the non-streaming branch of `/v1/messages`,
  `/v1/chat/completions`, and `/v1/responses`, despite the README's "every
  completed ... call emits one structured JSON line" claim. Any client
  using `"stream": true` — the common case for interactive use — produced
  zero usage telemetry. Added a non-buffering usage tracker that observes
  the SSE bytes already being forwarded to the client across all three
  protocols (Anthropic `message_start`/`message_delta`, an OpenAI-compatible
  backend's final-chunk `usage`, and a Responses `response.completed`
  event), plus tracking of which provider/model actually served the
  request after a pre-first-byte fallback, so the usage event's `provider`
  and `model` fields are correct even when the primary target failed over.
  A usage event (with zero counts, not omitted) is still logged if a
  backend never reports usage on its stream, or if the client disconnects
  mid-stream. Covered by four new tests in `tests/test_stream_usage.py`.

- **Fixed: `make install-dry-run` failed on a clean checkout.** The
  README's own documented first step of the standalone installer failed
  immediately with `release wheel is missing`, because `scripts/install.sh`
  checked for the release wheel before branching on `--dry-run` vs.
  `--apply`, and no `make` target anywhere actually built that wheel (`make
  build` only builds the Docker image; the Dockerfile builds its own wheel
  internally and never writes it to `dist/`). A preview command should
  never require a build artifact that doesn't exist yet. Added a
  `build-wheel` target that builds `dist/z_prov-<version>-py3-none-any.whl`
  the same way the Dockerfile does, made `install-systemd` depend on it,
  and moved the wheel existence check in `install.sh` so `--dry-run` only
  reports whether the wheel is present instead of failing without it.
  Verified end-to-end: `make install-dry-run` now succeeds on a clean
  checkout, and `make build-wheel && bash scripts/install.sh --apply`
  installs a working `z-prov` binary into a scratch prefix.

- **Fixed: `__version__` was a hardcoded literal, duplicated across
  `pyproject.toml`, `src/z_prov/__init__.py`, `compose.yaml`, and the
  README, with no mechanism keeping them in sync.** This is exactly how
  the stale-README-version bug above happened, and how the SBOM generator
  ended up hardcoding `0.4.0rc1` as the app version regardless of what was
  actually installed. `__init__.py` now reads `__version__` from the
  installed distribution's metadata via `importlib.metadata.version()`, so
  `pyproject.toml` is the only place the version is set by hand; `/health/live`,
  `z-prov --version`, `update.sh`'s version comparison, and
  `scripts/generate-sbom.py` all now derive from that single source of truth.

- **Fixed: README claimed the wrong current release version** (`0.4.0`,
  one release behind `pyproject.toml`'s actual `0.4.1`). Version strings in
  `README.md` and `compose.yaml` are now `0.4.2`, and (per the fix above)
  can no longer drift silently.

No public API, config file schema, or environment variable changed.
Existing `config/providers.yaml` and `.env` files work unchanged.

## 0.4.1 - 2026-08-11

Audit-driven bugfix release. Both fixes below were verified by reading
the actual source and reproducing the gap with a new test before
patching, rather than assumed.

- **Fixed: `ProviderClient.stream()` never touched the circuit breaker.**
  Non-streaming calls (`_json()`) drove `ResilienceExecutor`'s
  `CircuitBreaker` on success and failure; the streaming path
  (`stream()`, used by `/v1/messages`, `/v1/chat/completions`, and
  `/v1/responses` with `"stream": true`) did not call
  `breaker.before_call()` / `.success()` / `.failure()` at all. A
  provider that was persistently down for streaming traffic therefore
  never tripped its breaker, so the router kept retrying that dead
  provider first on every streaming request instead of learning to
  prefer a healthy fallback, the way it already did for non-streaming
  calls. `stream()` now drives the same breaker instance (still without
  retrying mid-stream, since that remains unsafe once bytes have reached
  the client). Covered by
  `test_stream_failures_trip_the_same_circuit_breaker_as_non_streaming_calls`
  and `test_successful_stream_closes_the_circuit_breaker` in
  `tests/test_provider_errors.py`.

- **Fixed: `InMemoryRateLimiter` leaked one dict entry per distinct
  client for the life of the process.** A bucket's event deque was only
  pruned when *that same bucket* made another request. A client seen
  once and never again (a rotated key, a one-off source IP, scanner
  traffic) left its entry in `_events` permanently, since nothing else
  ever touched that key again to trigger the existing eviction loop. On
  a long-running, internet-facing gateway this is unbounded memory
  growth. Added a periodic sweep (at most once per rate-limit window,
  amortized O(active buckets), no extra timer/thread) that drops buckets
  with no activity left in the current window. Covered by
  `test_rate_limiter_evicts_buckets_that_go_permanently_idle` and
  `test_rate_limiter_does_not_evict_active_buckets` in
  `tests/test_security.py`.

No public API, config file schema, or environment variable changed.
Existing `config/providers.yaml` and `.env` files work unchanged.

## 0.4.0 - 2026-08-09

- Completed the remaining P1 items from `docs/DEEP_UPGRADE_AUDIT_ZCODER_1_36.md`
  feature disposition table:
  - Added structured, machine-readable usage events (`z_prov.usage`
    logger) emitted for every completed `/v1/messages`,
    `/v1/chat/completions`, and `/v1/responses` call: request id, alias,
    provider, model, surface, duration, and normalized token counts
    (including cache tokens where available). No cost/dollar figures are
    synthesized, since Z-Prov cannot verify provider billing.
  - Added prompt-cache token pass-through across protocol conversion:
    Anthropic's `cache_read_input_tokens` / `cache_creation_input_tokens`
    now survive the trip to an OpenAI-shaped client (surfaced as
    `prompt_tokens_details.cached_tokens` plus a namespaced extension
    field), and OpenAI-compatible backends' `prompt_tokens_details
    .cached_tokens` now survives the trip to an Anthropic-shaped client
    (surfaced as `cache_read_input_tokens`). Previously both were silently
    dropped on cross-protocol paths.
  - Added a Files and Batches control-plane proxy under
    `/v1/providers/{provider}/files` and `/v1/providers/{provider}/batches`,
    per the audit's "provider extension namespace before cross-provider
    normalization" guidance: requests are forwarded to each provider's
    native endpoint (Anthropic's `/messages/batches`, everything else's
    `/batches`) without attempting to normalize payload shapes across
    providers. Guarded by the same client authentication as every other
    endpoint, with an independent `Z_PROV_MAX_FILE_BYTES` size limit
    (default 32 MiB) since uploads are larger than a typical chat request.

## 0.4.0rc1 - 2026-07-27

- Added typed errors with separate retry, fallback, and circuit-health policies.
- Added total deadlines, bounded Retry-After, jittered retries, and circuit breakers.
- Added pre-first-byte-only streaming fallback with replay prevention.
- Added capability discovery with TTL, provenance, account/region partitioning,
  lifecycle state, and retired-target-aware aliases.
- Expanded Anthropic, Chat Completions, and Responses request conversion.
- Added cross-protocol SSE translation for text and function-tool streaming.
- Added request IDs, rate limiting, security headers, and secret redaction.

## 0.3.0 - 2026-07-26

- Added dry-run-first versioned standalone installer.
- Added optional hardened systemd user service.
- Added HTTPS and SHA-256 verified update checks and guarded apply.
- Added opt-in daily systemd auto-update timer.
- Added upgrade-source inventory for `aicoder`, `zai-coder`, `zcodex`, `zc`,
  and `z-platform` with inspected commit pins and clean-room acceptance rules.

## 0.2.0 - 2026-07-26

- Added `z-prov-free` local-first automatic fallback.
- Added provider-specific model IDs per fallback target.
- Kept cloud fallback explicitly opt-in.

## 0.1.0 - 2026-07-26

- Initial Anthropic Messages, OpenAI Chat Completions, and OpenAI
  Responses/Codex compatible standalone gateway.
