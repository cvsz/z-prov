# AGENTS.md — ZeaZ Provider

## Language and Coding Omega Advanced Professional
- **Communication**: Always talk in Thai when interacting with users.
- **Code & Technical Assets**: All code, comments, documentation, and technical definitions must be in English.

## Scope

This repository implements a standalone dual-protocol AI gateway:

- Anthropic Messages API compatibility
- OpenAI Chat Completions compatibility
- OpenAI Responses/Codex compatibility
- configurable routing to native and OpenAI-compatible providers

## Rules

- Never commit or print provider keys, client keys, tokens, or `.env`.
- Keep provider credentials server-side.
- Keep protocol conversion separate from provider transport.
- Never execute model-generated commands or code in the gateway process.
- Treat provider responses and errors as untrusted input.
- Limit request bytes, output tokens, connections, timeouts, and logs.
- Do not silently send local/private requests to a cloud fallback.
- Add contract tests for every protocol conversion.
- Pin production images and dependency locks before production deployment.
- Use dry-run or validation before external changes.

## Required validation

```bash
make validate
docker compose config --quiet
```
