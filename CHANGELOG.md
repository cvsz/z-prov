# Changelog

## Unreleased

- Stream request bodies incrementally and reject invalid JSON, token limits,
  malformed protocol shapes, and oversized requests before provider dispatch.
- Validate provider and route configuration at startup, including URLs,
  numeric limits, headers, and fallback entries.
- Keep upstream provider/model identifiers private in model discovery and
  public responses, including native and cross-protocol SSE paths.
- Sanitize malformed provider JSON, SSE frames, HTTP errors, and retry hints
  without forwarding untrusted provider content to clients.
- Expand `.gitignore` and `.dockerignore` coverage for environment files,
  secrets, and private key material.
- Document the source checkout/repository rename to `z-prov` while preserving
  the `zeaz-provider` runtime identifiers required for upgrade compatibility.

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

- Added `zeaz-free` local-first automatic fallback.
- Added provider-specific model IDs per fallback target.
- Kept cloud fallback explicitly opt-in.

## 0.1.0 - 2026-07-26

- Initial Anthropic Messages, OpenAI Chat Completions, and OpenAI
  Responses/Codex compatible standalone gateway.
