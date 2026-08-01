# zeaz-control

Standard ZeaZ control plane for model lifecycle, files, message batches, and
usage/cost events. It has its own process, persistence, credentials, and
deployment profile; provider credentials never enter gateway or portable
resource records.

## Message batches

`BatchService` provides a provider-neutral submit, list, retrieve, cancel, and
result-stream lifecycle. Submissions accept only batch-purpose files already
catalogued by the Files service. Create and cancel calls require a bounded
idempotency key; reservations and completed responses persist in SQLite so a
retry reuses the provider key without duplicating the operation.

Provider pagination is fully validated before reconciliation commits. Duplicate
records, cursor loops, item/page bounds, stale updates, terminal-state
regressions, and decreasing result counts fail closed. Result streams reject
duplicate request IDs and enforce configured record and byte limits. State,
idempotency completion, and sanitized audit metadata commit in one transaction.

## Usage, cost, and extensions

Usage events use bounded integer counters. Cost events use exact decimal
amounts, uppercase ISO-style currency codes, and require both a pricing source
identifier and its observation date. Event IDs are durable idempotency keys;
reusing an ID with different content fails closed. A cost must reference a
stored usage event in the same provider, account, and model scope.

Nonportable data is accepted only below `extensions.<provider>`. Namespace and
field names are bounded and validated, and an extension for one provider cannot
be attached to another provider's portable record.

## Deployment

Run the isolated profile with `docker compose --profile control up -d control`.
It listens only on `127.0.0.1:8090` by default and stores its owner-only SQLite
state in the `control-state` volume. Supply control-plane provider credentials
only to the `control` service (for example via a Compose override or secret
manager); it does not load the gateway `.env` or mount gateway configuration.
The image runs as UID 10002 with a read-only root filesystem, no capabilities,
and no-new-privileges.
