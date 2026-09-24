# Architecture

SCS owns source parsing, indexing, query orchestration, its local daemon, and
its CLI and MCP contracts. [TSG](https://github.com/leonardoventurini/tsg) is
its durable graph and embedding engine.

A new SCS installation starts with an empty index. SCS does not migrate,
inspect, or recreate data from other products.

## Storage

The Rust `scs-store` crate maps SCS repository scopes, typed code nodes and
relationships, ingestion checkpoints, metadata filters, traversal, and semantic
search onto generic TSG primitives. The Python, SCSWire, MCP, and CLI contracts
remain SCS-owned without coupling TSG to code intelligence.

TSG is pinned to the immutable `v0.2.4` Git tag and its resolved commit in
`Cargo.lock`; building SCS does not require a sibling TSG checkout. TSG keeps
canonical graph, catalog, and embedding state transactionally in SQLite and
treats its vector index as a rebuildable accelerator.

An index created by the former SCS storage engine is not migrated in place.
When that incompatible database is first opened, SCS moves the database, WAL,
SHM, and legacy `.usearch` sidecar (when present) to unique
`*.pre-tsg.backup` names, then creates an empty TSG index. Run
`scs reindex <repo>` to rebuild derived state from repository source. For
rollback, stop SCS, retain the new TSG files separately, restore the backed-up
legacy filenames, and run the previous SCS binary. Backup removal is always an
explicit operator action.

Semantic embeddings are generated from parser-owned entity text. SCS does not
send repository files to a summarization service; an embedding provider receives
only the entity text used to build the semantic index. See
[Embedding configuration](configuration.md) for provider choices.

## Runtime ownership

MCP uses stdio between each harness and its bridge, then SCSWire over one
owner-only Unix socket between bridges and the daemon. No TCP port or platform
service manager is required. A bootstrap lock serializes simultaneous first
clients; the daemon independently holds the storage writer lock. Each bridge
connection is its lease, so abrupt termination cannot leave an orphan lease.

Runtime artifacts live under `~/Library/Application Support/SCS/` on macOS and
`$XDG_RUNTIME_DIR/scs` on Linux, falling back to
`~/.local/state/scs/runtime`. Persistent indexes live only under `SCS_HOME`.

## Verification

`just coverage` reports uncovered Python lines and enforces the committed
risk-based floor. The pre-commit hook runs the whole-source type gate.
Isolation gates cover exact stdio MCP inventory, multi-bridge daemon
convergence, bounded frames, generation-safe cleanup, stale/live socket
ownership, empty-startup behavior, runtime isolation, repository source
fingerprints, and committed RSS/index/query budgets.
