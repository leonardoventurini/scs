# Use TSG as the SCS storage authority

Project: `scs`

Project root: `/Users/leonardo/Repositories/mentagen/scs`

Date: 2026-09-04

## Context

SCS and TSG independently owned substantially the same SQLite graph schema,
embedding persistence, USearch lifecycle, traversal, filtering, and recovery
logic. Keeping both engines made correctness and production hardening diverge.
SCS still needs code-specific enums, repository lifecycle, ingestion
checkpoints, Python bindings, and stable public payloads that do not belong in
a generic graph database.

## Decision

TSG is the sole durable graph, catalog, and embedding engine used by SCS.
`scs-store` remains as a typed compatibility adapter and owns every mapping
between the generic TSG model and SCS concepts. SCS consumes the immutable TSG
`v0.2.0` Git tag, with the resolved commit recorded in `Cargo.lock`; it never
depends on the sibling checkout.

Legacy SCS indexes are derived data and are not migrated. On incompatibility,
the adapter moves the legacy database and sidecars to unique
`*.pre-tsg.backup` paths and opens a fresh TSG store. Source reindexing is the
only forward data path. Rollback uses the retained files and the previous SCS
binary.

## Rejected alternatives

- Continue maintaining the SCS-native SQLite/USearch engine. This preserves
  duplication and splits future reliability work.
- Add SCS repository and ingestion concepts to TSG. This compromises TSG's
  general-purpose boundary.
- Copy TSG into the SCS workspace or use a relative path dependency. This
  makes builds depend on local filesystem layout and weakens reproducibility.
- Migrate the old schema in place. The index is reproducible from source, so
  migration risk provides little value compared with a recoverable rebuild.

## Consequences

- Storage correctness and evolution are centralized in independently released
  TSG; SCS keeps its public contracts and domain model.
- TSG releases must precede SCS dependency updates and tags are immutable.
- Existing installations rebuild indexes once and temporarily consume space
  for rollback backups.
- The SCS Rust minimum toolchain follows TSG's supported toolchain.
- Compatibility adapter tests and full Python integration tests guard the
  boundary in addition to TSG's own test suite.
