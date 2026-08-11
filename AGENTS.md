# AGENTS.md — Z-Prov Provider

## Language and Communication

- Communication: When interacting with end users, prefer Thai for natural-language conversations and user-facing messages.
- Code & Technical Assets: All source code, comments, tests, documentation, configuration examples, and technical definitions MUST be written in English.

## Scope

This repository implements a standalone dual-protocol AI gateway:

- Anthropic Messages API compatibility
- OpenAI Chat Completions compatibility
- OpenAI Responses/Codex compatibility
- Configurable routing to native and OpenAI-compatible providers

## Rules

- Never commit or print provider keys, client keys, tokens, or `.env` files.
- Keep provider credentials server-side and out of repository history.
- Keep protocol conversion separate from provider transport.
- Never execute model-generated commands or code inside the gateway runtime.
- Treat provider responses and errors as untrusted input and validate all external data.
- Limit request payload sizes, output tokens, concurrent connections, timeouts, and logs.
- Do not silently forward local/private requests to an external cloud fallback without explicit opt-in.
- Add contract tests for every protocol conversion.
- Pin production container images and dependency lock files before production deployment.
- Use dry-run or validation steps before making external changes.

## Project name and consistency

Use "Z-Prov" as the canonical project name in documentation, package metadata, and release notes.

## Required validation

```bash
make validate
# Ensure docker-compose file is syntactically valid
docker compose config --quiet
```
