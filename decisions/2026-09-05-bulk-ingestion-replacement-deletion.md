# Delete ingestion replacement sets as one native operation

## Context

After indexed attribute queries were repaired, runtime sampling exposed repeated
accelerator reconstruction during file replacement. The Python pipeline called
single-node deletion for every replaced node. TSG already accepts a node-ID batch
and performs one transactional cascade and one accelerator rebuild.

## Decision

Expose the existing TSG capability through the internal typed SCS native bridge.
The ingestion pipeline passes its sorted unique replacement set once, after
capturing inbound edges and before inserting replacement nodes. Empty plans
perform no deletion; the native adapter also returns zero for empty batches.
PyO3 releases the GIL throughout native deletion.

## Alternatives and consequences

Repeated single deletes preserve correctness but amplify accelerator work.
Changing TSG rebuild semantics would expand scope unnecessarily. The internal
batch bridge preserves existing cascades, vector durability, and inbound-edge
retention without changing MCP, wire, or stored formats. Existing individual
node-deletion entrypoints remain available for their callers.

Generated native tests prove one generation transition for a multi-node deletion,
empty-batch stability, incident-edge removal, deleted-vector absence, and survivor
vector retention. Native pipeline fixtures prove one bulk invocation per forced
replacement and preserve stable IDs and stale occurrence cleanup. Existing
cross-file edge and property tests cover replacement equivalence.

Rollback needs no migration and restores the previous slower deletion path.
