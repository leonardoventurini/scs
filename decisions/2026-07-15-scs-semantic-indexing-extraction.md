# SCS owns semantic code intelligence independently

Date: 2026-07-15  
Status: Accepted

## Decision

SCS is the sole headless owner of semantic and structural code indexing,
code/provenance graph persistence, read-only code intelligence, SCSWire, and
the agent-facing MCP endpoint. It is a sibling product, not a External product subsystem.

SCS starts with a fresh empty index. It has no migration, legacy import, or
automatic repository enrollment path. External product may consume the public SCSWire
contract but does not own SCS lifecycle, storage, MCP, or graph internals.

The public proxy and daemon restart independently. The proxy owns discovery
and `proxy-service.json`; the daemon owns `scs.sock` and
`daemon-service.json`. Atomic records identify one generation and permit only
matching-owner cleanup. Repository source remains read-only.

## Consequences

- SCS builds, tests, starts, indexes, searches, and restarts without External product.
- Stopped-daemon CLI state is explicit JSON rather than a socket exception.
- Uninstall and rollback preserve SCS indexes and legacy External product data.
- MCP contains no UI, recording, rendering, personalization, source-editing,
  or general External product product tools.
- Live rollout rejects dual owners, identity drift, sentinel mutation, missing
  MCP inventory, or performance-budget failure.

## Verification

Evidence comes from the Rust, Python, and proxy suites; AST import denial;
legacy and source fingerprints; generation/restart and bounded-frame tests;
and committed RSS/index/query budgets. External product dictation contention remains a
live cutover measurement because it cannot be measured truthfully by SCS alone.
