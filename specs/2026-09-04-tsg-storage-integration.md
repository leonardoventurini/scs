# TSG storage integration

Project: `scs`

Project root: `/Users/leonardo/Repositories/mentagen/scs`

## Problem

SCS duplicates the durable graph/vector engine now owned by TSG. Its
`scs-store` crate independently implements schema management, node and edge
CRUD, canonical embeddings, `USearch` lifecycle, graph traversal, and recovery.
The duplication prevents one reusable architecture from owning those invariants.

## Desired outcome

TSG becomes the only graph-and-embedding authority behind SCS. `scs-store`
becomes an SCS-specific adapter that maps repositories, ingestion state, typed
nodes and relationships, metadata, queries, and observability onto generic TSG
primitives. Python, wire, MCP, and CLI behavior remain compatible.

## Scope and constraints

- Depend on an immutable released TSG tag, initially 0.2.0.
- Remove direct `rusqlite` and `usearch` ownership from `scs-store` wherever
  TSG owns the equivalent behavior.
- Do not add SCS concepts or imports to the TSG repository.
- Preserve Python method signatures and serialized model shapes.
- Preserve enum parity, GIL release, bounded query, single-writer, and explicit
  indexing invariants.
- A legacy SCS index is never mutated into the new schema. On explicit reindex,
  move it to a timestamped rollback backup and create a fresh TSG store.
- A fresh or restarted SCS root stays empty until an explicit index request.
- Do not read or derive state from External product.

## Contracts

- One adapter transaction durably commits nodes, edges, embeddings, and
  ingestion acknowledgement for an indexed batch.
- A failed parse or missing durable vector never records a successful hash.
- Repository and metadata queries retain their current scope and pagination.
- Exact and accelerated searches preserve ordering and filtering contracts.
- Startup detects legacy state and reports reindex-required without silently
  deleting or converting it.
- Explicit reindex performs a recoverable backup before creating new state.
- SCS remains fully functional without the sibling TSG checkout.

## Risks

- Schema/model impedance could leak SCS concepts into TSG.
- A hard cutover could lose indexed state or change Python JSON payloads.
- TSG's current full sidecar rebuild may regress ingestion ceilings.
- Single-writer TSG ownership may conflict with SCS's current connection pool.

Mitigations are contract tests around the existing adapter surface, procedural
equivalence fixtures, performance gates, fresh-index rollout, immutable
dependency pinning, and retention of the legacy database.

## Recovery and rollback

Stop SCS, restore the retained legacy database and sidecar names, revert the SCS
adapter commit, and restart the previous binary. New TSG state is derived index
data and may be removed only after resolving its explicit paths. Published TSG
tags are immutable.

## Direct rollout

1. Release the generic TSG capabilities.
2. Add the immutable Git dependency and compatibility adapter.
3. Run shadow equivalence in tests, not production.
4. Ship the cutover inactive for existing legacy roots until explicit reindex.
5. Back up legacy state, rebuild from source, verify, then retire the backup only
   through a later explicit operator action.

## Verification

- Existing Rust unit tests and Python native contract tests.
- Full/incremental equivalence and persistence-fault integration tests.
- Fresh-root, isolation, restart, and legacy-backup/reindex E2E tests.
- Exact-versus-accelerated search and graph-context parity.
- Performance ceilings and full `just verify`.
- TSG operates independently; SCS builds without a sibling checkout.

## Executable checklist

- [ ] Capture current adapter behavior as contract tests.
- [ ] Consume released TSG through an immutable Git tag.
- [ ] Implement SCS-to-TSG model and error mapping.
- [ ] Move ingestion transactions onto the unified commit boundary.
- [ ] Implement legacy detection, backup, and explicit reindex cutover.
- [ ] Remove replaced graph/vector implementation and dependencies.
- [ ] Update operations, architecture, and rollback documentation.
- [ ] Run targeted tests followed by `just verify`.
- [ ] Record the architecture decision and commit each verified unit.
