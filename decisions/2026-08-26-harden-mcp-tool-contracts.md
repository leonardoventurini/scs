# Harden MCP tool contracts

## Context

Project: `scs`
Project root: `/Users/leonardo/Repositories/mentagen/scs`

The live MCP evaluation found two correctness defects: `find_references`
could not serialize either runtime variant through FastMCP, and regression
risk treated containment/self edges as semantic dependents. It also exposed
ambiguous symbol resolution, one-direction context, misleading semantic-store
status, unbounded file inspection, unstable path errors, and a launchd restart
teardown race.

## Decision

Use a Pydantic discriminated union for reference output, preserve only
dependency relationships between non-affected nodes in regression reporting,
and resolve named related entities against symbol node types with exact-name
precedence. Make graph-context direction explicit and default it to `both`.
Expose semantic-search readiness separately from vector-store availability,
bound inspection responses with flags, normalize unavailable source paths at
the MCP boundary, and wait briefly for a failed launchd teardown before
bootstrapping a replacement service.

## Rejected alternatives

- Keep one TypedDict with nullable optional fields. FastMCP/Pydantic schema
  generation converted omitted values into invalid non-null defaults.
- Treat every inbound relationship as regression risk. Ownership and affected
  nodes do not describe an external consumer that can regress.
- Leave graph traversal outgoing-only. Import/file context commonly sits on
  inbound graph edges, so this omitted useful surrounding context.
- Retry `kickstart` indefinitely. A bounded wait followed by bootstrap is
  deterministic and does not mask a still-live service.

## Consequences

MCP clients receive stricter, self-describing output schemas and bounded
payloads. They can request a larger inspection window only up to explicit
service limits and can distinguish unavailable semantic search from a usable
but empty vector store. No storage migration is required; rollback is a source
revert followed by a paired-service restart.
