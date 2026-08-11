# Z-Prov

Current release: `0.4.2`.

Standalone multi-provider AI gateway with two client-compatible surfaces:

- Anthropic Messages API: `POST /v1/messages`
- OpenAI Chat Completions: `POST /v1/chat/completions`
- OpenAI Responses/Codex: `POST /v1/responses`
- shared model discovery: `GET /v1/models`
- per-provider Files and Batches control-plane proxy: see
  [Files and Batches proxy](#files-and-batches-proxy)

It is an API gateway, not a foundation model. It keeps provider credentials
server-side and maps stable local model aliases to native Anthropic, native
OpenAI Responses, Azure, Ollama, LiteLLM, or any OpenAI-compatible endpoint.

## Supported integrations

Presets are included for Anthropic, OpenAI, Ollama, LiteLLM, OpenRouter,
Gemini, Azure OpenAI, Groq, Mistral, DeepSeek, xAI, Together, Fireworks,
NVIDIA NIM, Perplexity, and a configurable custom provider.

Any service implementing `/chat/completions` can be added without changing
source:

```yaml
providers:
  my-provider:
    api: openai
    base_url: https://provider.example/v1
    api_key: ${MY_PROVIDER_API_KEY}

models:
  my-model:
    provider: my-provider
    model: provider-model-id
```

Every public model name follows `z-prov-<name>`. Upstream model IDs stay private
in the route configuration, so applications do not change when a backend model
or provider is replaced. Included aliases cover `z-prov-auto`, `z-prov-local`,
`z-prov-free`, `z-prov-claude`, `z-prov-codex`, `z-prov-openrouter`, `z-prov-gemini`, `z-prov-azure`,
`z-prov-groq`, `z-prov-mistral`, `z-prov-deepseek`, `z-prov-xai`, `z-prov-together`,
`z-prov-fireworks`, `z-prov-nvidia`, `z-prov-perplexity`, and `z-prov-custom`.

Provider `api` values:

- `anthropic`: native `/messages`
- `responses`: native OpenAI `/responses`
- `openai`: OpenAI-compatible `/chat/completions`
- `azure`: Azure `/chat/completions` with `api-key` and `api-version`

## Quick start

```bash
make env-init
# Edit .env and config/providers.yaml.
make install
make validate
make run
```

Container:

```bash
make env-init
docker compose config --quiet
make up
make health
```

## Standalone installer

The release contains a versioned installer derived from deterministic installer
requirements, not copied implementation:

```bash
make install-dry-run
make build-wheel
CONFIRM_INSTALL=yes make install-systemd
```

`install-systemd` builds the wheel itself if you skip the explicit step, but
`install-dry-run` never builds anything — it only previews what `--apply`
would do (and, since 0.4.2, no longer requires a wheel to already exist).

It installs under `~/.local/share/z-prov/versions/<version>`, retains
existing provider configuration, and atomically switches `current`.

## Verified update and auto-update

Updates require an operator-controlled HTTPS JSON manifest containing version,
release URL, and SHA-256. Checking never mutates the installation:

```bash
export Z_PROV_UPDATE_MANIFEST_URL=https://example.com/z-prov/latest.json
make update-check
CONFIRM_UPDATE=yes make update
```

Daily auto-update is opt-in:

```bash
CONFIRM_AUTO_UPDATE=yes make auto-update
```

The timer downloads only over HTTPS and applies a release only after SHA-256
verification. For higher assurance, the upgrade roadmap requires signed
manifests before unattended production rollout. Review
[`docs/UPGRADE_SOURCES.md`](docs/UPGRADE_SOURCES.md) for the five inspected
source repositories, commit pins, accepted behaviors, exclusions, and roadmap.

The Compose port binds to loopback. Place it behind Cloudflare Tunnel, Traefik,
Caddy, or another authenticated TLS reverse proxy for remote access.

## Anthropic client

```python
from anthropic import Anthropic

client = Anthropic(
    api_key="your-z-prov-client-key",
    base_url="http://127.0.0.1:8080",
)
message = client.messages.create(
    model="z-prov-claude",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
)
print(message.content[0].text)
```

Use `z-prov-local` to send the same Anthropic request format to Ollama. Non-native
Messages requests are converted to Chat Completions for non-streaming calls.

## OpenAI and Codex-compatible clients

```python
from openai import OpenAI

client = OpenAI(
    api_key="your-z-prov-client-key",
    base_url="http://127.0.0.1:8080/v1",
)

response = client.responses.create(
    model="z-prov-codex",
    input="Review this function",
)
print(response.output_text)
```

`z-prov-codex` routes to a native Responses provider. Chat Completions clients
can use the same base URL and model aliases.

## Authentication

Set comma-separated gateway client keys:

```dotenv
Z_PROV_CLIENT_KEYS=key-one,key-two
```

Both `x-api-key` and `Authorization: Bearer` are accepted. The gateway refuses
all authenticated endpoints when no client keys are configured. Local
unauthenticated development requires the explicit
`Z_PROV_ALLOW_UNAUTHENTICATED=true` setting.

## Local-first free fallback

`z-prov-free` works through both Anthropic Messages and OpenAI/Codex request
surfaces. It uses the configured Ollama model first:

```text
Ollama → OpenRouter free route → Gemini free-tier route → Groq free-tier route
```

Cloud fallback is disabled by default because it can send prompts and file
content outside the machine. Enable it explicitly:

```dotenv
Z_PROV_LOCAL_MODEL=qwen3:8b
FREE_CLOUD_FALLBACK_ENABLED=true
OPENROUTER_API_KEY=...
GEMINI_API_KEY=...
GROQ_API_KEY=...
```

Free tiers, quotas, available models, and provider terms can change. The
gateway does not claim that external calls are permanently free. Configure
current provider model IDs in `.env`. Non-streaming calls automatically move
to the next route for missing models, rate limits, timeouts, and transient
upstream failures. Authentication and invalid-request errors do not silently
fall through.

## Streaming limitation in 0.2

Streaming is passed through without buffering when the selected backend uses
the same protocol as the client:

- Anthropic Messages client → native Anthropic backend
- OpenAI Chat client → OpenAI-compatible backend
- Responses/Codex client → native Responses backend

Cross-protocol non-streaming conversion is implemented. Cross-protocol
stream-event translation is intentionally deferred rather than emitting
incorrect event contracts.

## Files and Batches proxy

Provider-native Files and Batches APIs are reachable through a namespaced
control-plane proxy rather than a normalized cross-provider surface, since
payload shapes differ enough between providers that normalizing them would
lose information:

```text
GET|POST     /v1/providers/{provider}/files
GET|DELETE   /v1/providers/{provider}/files/{file_id}
GET|POST     /v1/providers/{provider}/batches
GET          /v1/providers/{provider}/batches/{batch_id}
POST         /v1/providers/{provider}/batches/{batch_id}/cancel
```

`{provider}` is a provider name from `config/providers.yaml` (for example
`anthropic`, `openai`, `groq`), not a model alias. Requests and responses are
forwarded to that provider's native endpoint unmodified — for Anthropic,
batches route to `/messages/batches`; for every OpenAI-compatible provider,
to `/batches`. `Content-Type` and `anthropic-beta` request headers are
forwarded; provider authentication is injected server-side the same way it
is for `/v1/messages`. Client authentication (`Z_PROV_CLIENT_KEYS`) is
required as usual. Uploads are capped independently by
`Z_PROV_MAX_FILE_BYTES` (default 32 MiB) since they are typically larger
than a chat request.

## Usage events

Every completed `/v1/messages`, `/v1/chat/completions`, and `/v1/responses`
call emits one structured JSON line via the `z_prov.usage` logger:
request id, alias, provider, backend model, surface, duration, and
normalized token counts (including cache tokens where the backend reports
them). This covers streaming calls as well as non-streaming ones (fixed in
0.4.2 — streaming previously emitted no usage event at all); for streaming,
token counts are a best-effort read of whatever usage the backend included
in its own SSE events; some OpenAI-compatible backends only report usage on
a stream when the client sets `stream_options.include_usage`, and if a
backend never sends it, the event is still emitted with zero counts rather
than skipped. No dollar-cost figure is included — provider pricing is out of
this gateway's control, so it does not assert a cost it cannot verify
against the provider's own billing.

## Production checklist

- Replace the generated client key and configure only providers in use.
- Use separate keys per provider and environment.
- Pin dependency locks and container image digests.
- Keep the port loopback-only or on a private proxy network.
- Require Cloudflare Access or equivalent outer authentication.
- Rate-limit by client key at the reverse proxy.
- Never log request authorization headers or provider credentials.
- Disable cloud fallbacks for data that must stay local.
