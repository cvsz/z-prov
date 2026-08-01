# zeaz-enterprise

Enterprise-only control adapters for ZeaZ. This package is deployed separately
from both the protocol gateway and `zeaz-control`; admin credentials are passed
directly to an adapter and are never stored in resource records or SQLite.

Run the isolated process with
`docker compose --profile enterprise up -d enterprise`. It uses its own image,
UID 10003, network, state volume, and loopback port. The base Compose definition
does not inject gateway or C1 configuration, `.env`, or credentials; deployments
must supply each required credential only to this service through an external
secret mechanism.

## OpenAI Admin API

`OpenAIAdminAdapter` implements bounded organization user and project reads,
project creation/update/archive, and user-role updates. Pagination is consumed
without gaps or duplicates before results are returned. Redirects, oversized
responses, malformed JSON, unknown fields, repeated cursors, and duplicate IDs
fail closed. Mutations require a bounded idempotency key. Both admin adapters
require a provider-bound `AdminCredential`; raw strings, regular credentials,
compliance credentials, and the other provider's admin credential are rejected.

Permanent user removal and key deletion are intentionally absent until the
roadmap's dry-run and dual-authorization deletion controls are implemented.

Public contract references:

- https://platform.openai.com/docs/api-reference/users
- https://platform.openai.com/docs/api-reference/projects

`AnthropicAdminAdapter` separately supports organization users and workspaces,
including bounded `after_id` pagination, assignable role updates, and workspace
create/update/archive. It accepts either a dedicated Admin API key or OAuth
bearer without conflating either credential with standard Anthropic API keys.

- https://platform.claude.com/docs/en/api/admin/users/list
- https://platform.claude.com/docs/en/api/admin/workspaces/list

## Anthropic Compliance API

`AnthropicComplianceAdapter` provides bounded, read-only activity and
organization-directory exports. Compliance Access Keys use a distinct typed
credential role with explicitly declared scopes; they cannot be substituted
with Admin or ordinary API credentials. Both ID-cursor and opaque-token
pagination fail closed on duplicates, inconsistent pages, malformed records,
redirects, or configured byte/item/page limits.

Permanent deletion is intentionally absent until the roadmap's dry-run,
retention, and dual-authorization controls are implemented.

- https://platform.claude.com/docs/en/api/compliance
- https://platform.claude.com/docs/en/api/compliance/activities/list
- https://platform.claude.com/docs/en/api/compliance/organizations/list

## Workload Identity Federation

`AnthropicWIFExchange` exchanges a freshly supplied OIDC assertion through the
documented RFC 7523 flow and caches only the resulting short-lived bearer
credential. The assertion comes from an async provider boundary, so ZeaZ does
not persist upstream identity tokens or assume a cloud-specific metadata
service. Exchanges are single-flight, bounded by timeout and byte limits, and
refresh before expiry. Redirects and provider error bodies fail closed without
entering application errors.

- https://platform.claude.com/docs/en/manage-claude/workload-identity-federation
- https://platform.claude.com/docs/en/manage-claude/wif-reference

## Vault references

Persisted enterprise configuration uses closed, provider-discriminated
references for AWS Secrets Manager, Google Cloud Secret Manager, and Azure Key
Vault. These models contain only a purpose and provider locator. They have no
field for secret bytes, and require complete cross-account-safe AWS ARNs,
explicit GCP versions or aliases, and versioned Azure secret identifiers.
Resolution remains an injected deployment concern and secret values never enter
these application-state models.

- https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_GetSecretValue.html
- https://cloud.google.com/secret-manager/docs/access-secret-version
- https://learn.microsoft.com/en-us/azure/key-vault/general/about-keys-secrets-certificates

## Managed-agent environments and deployments

`AnthropicManagedAgentsAdapter` implements the publicly documented beta
environment and scheduled-deployment lifecycle with a separately typed regular
API credential. Cloud environment creation accepts only limited networking
with an explicit hostname allow-list; unrestricted networking is intentionally
outside this production adapter. Scheduled deployments bind a validated agent,
environment, initial message, five-field cron schedule, and optional vault
references.

Opaque pagination, IDs, timestamps, response sizes, redirects, and provider
errors are bounded and fail closed. Mutations require idempotency keys.
Archive, pause, unpause, and manual run are supported, while permanent delete
is held for the later dry-run and dual-authorization roadmap controls.

- https://platform.claude.com/docs/en/managed-agents/environments
- https://platform.claude.com/docs/en/managed-agents/scheduled-deployments
- https://platform.claude.com/docs/en/api/beta/environments
- https://platform.claude.com/docs/en/api/beta/deployments

## Agent memory, outcomes, and dreams

`AnthropicAgentMemoryAdapter` keeps endpoint-specific beta contracts separate:
memory-store calls send only `agent-memory-2026-07-22`, outcome events send the
Managed Agents beta, and research-preview dream jobs send both Managed Agents
and Dreaming betas. Memory stores support bounded create/list/get/archive;
outcomes support bounded text or file rubrics and iteration limits; dreams bind
one input store and 1–100 unique sessions and support list/get/cancel/archive.

All pagination and responses are bounded and provider errors are sanitized.
Memory deletion is intentionally absent until the permanent-deletion controls
later in C2 are complete. Dream inputs are never modified by the adapter.

- https://platform.claude.com/docs/en/managed-agents/memory
- https://platform.claude.com/docs/en/managed-agents/dreams
- https://platform.claude.com/docs/en/api/beta/memory_stores
- https://platform.claude.com/docs/en/api/typescript/beta/sessions/events

## Webhook verification

`AnthropicWebhookVerifier` verifies the exact raw body with Anthropic's
Standard Webhooks headers and a typed `whsec_` signing secret. It enforces a
five-minute delivery window, supports overlapping `v1` signatures during key
rotation, compares MACs in constant time, binds the signed delivery ID to the
payload event ID, and validates a bounded event envelope before returning it.

`SQLiteWebhookReplayStore` atomically claims event IDs across processes and
restarts. It stores only the event ID and receipt timestamp—not webhook
payloads—and uses a private regular database file. Repeated deliveries are
returned explicitly as duplicates for idempotent acknowledgement.

- https://platform.claude.com/docs/en/managed-agents/webhooks
- https://platform.claude.com/docs/en/api/beta/webhooks
- https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md

## Permanent deletion controls

`PermanentDeletionCoordinator` is the sole boundary intended to wrap provider
delete operations. `preview()` is always non-mutating and returns expiring,
canonical SHA-256 evidence of the resolved target, its current state, and
dependent resources. Execution requires the exact human-readable confirmation,
fresh opaque authorization grants bound to the plan UUID and resolution digest,
and a fresh resolution whose digest has not changed.

Every deletion requires an explicit authorization; organization-wide deletion
requires two grants from distinct verified principals. The provider executor is
called only after all checks pass and receives a bounded idempotency key.
Failures are sanitized, and a successful operation returns a receipt binding
the target, evidence digest, principals, operation ID, and deletion time.
