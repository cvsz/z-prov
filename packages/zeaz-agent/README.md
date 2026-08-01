# zeaz-agent

Provider-neutral agent runtime for ZeaZ. This package owns session state,
permission decisions, tool orchestration, memory, skills, and MCP integration.
It never receives provider credentials and does not execute tools inside the
provider gateway process.

The current foundation includes immutable session/tool schemas, a bounded
Responses-compatible model loop, explicit permission and plan gates,
hash-chained audit events, optimistic SQLite session and memory stores,
deterministic context compaction, durable token budgets, bounded subagents,
scripted deterministic model fixtures, integrity-checked local skill packages,
and bounded MCP stdio and Streamable HTTPS transports with exact allow-lists.
Tool policy extensions use ordered pre/post callbacks with immutable snapshots,
individual deadlines, and explicit fail-open or fail-closed outcomes.
Plugin packages require exact-byte Ed25519 signatures and closed manifests;
their registry installs versions atomically, keeps them disabled by default,
and supports audited, recoverable removal.
