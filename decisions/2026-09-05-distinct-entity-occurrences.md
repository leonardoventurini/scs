# Preserve occurrence identity at the SCS ingestion boundary

## Context

Valid source can contain multiple parsed entities with one kind and qualified
name: separate imports, scoped CSS variables, and repeated declarations. SCS
assigned identical node IDs and TSG correctly rejected the batch. Qualified-name
embedding lookup also conflated entities of different kinds or occurrences.

## Decision

Keep identity construction in SCS. The first occurrence retains the existing
repository/path/kind/qualified-name hash. Later occurrences use a separate
occurrence-prefixed hash with a deterministic ordinal in that identity group.
Associate vectors and vector acknowledgement checks by file and parser entity
position. Preserve every parsed occurrence and retain TSG's batch validation.

Generated anonymized fixtures reproduce the source categories with invented
names, modules, selectors, and values; they contain no copied application code.
Native integration tests verify storage and vector association, including
same-line declarations and shared type/value qualified names. Lifecycle tests
verify repeat indexing, whitespace stability, and stale occurrence cleanup.

## Alternatives and consequences

- Deduplicating batches would discard valid distinct source and embeddings.
- Weakening TSG to accept duplicates would hide an identity defect in its caller.
- Adding source lines to all IDs would cause unrelated whitespace edits to
  invalidate stable references. Ordinals preserve non-colliding IDs and tolerate
  line shifts, though insertion/removal within a repeated group can shift later IDs.
- Qualified-name relationships remain canonical symbol references. Parser edges
  lack occurrence coordinates; expanding ambiguous relationships into a Cartesian
  product would invent precision and increase graph size without evidence.

No storage/wire schema migration or TSG version change is required. Whole-file
replacement handles stale occurrence IDs. Existing successful file hashes remain
valid; failed files can be retried. After publication, the immutable SCS 0.1.4 release will be
installed through the documented GitHub installer, then Mentagen will be indexed
and searched through the service. Existing unpublished commits included in this release
are listed in the associated spec.

## Verification and recovery

The original TS/CSS regressions failed at native batch storage before the fix.
The focused fixture suite passes after the fix. Full local and release checks,
installed artifact identity, and final ingestion results are recorded in the spec.
A prior compatible installer restores earlier code without deleting SCS_HOME;
revert the source commit for code rollback. Older code still cannot ingest these
colliding sources. Do not purge user indexes or bypass failed ingestion jobs.

The release gate also exposed an existing platform-specific test assumption:
Linux may report a reset when an incomplete frame is discarded during shutdown.
The partial-frame tests now accept reset or EOF, while idle/attached connections
still require EOF. This changes test portability, not production behavior or
shutdown guarantees. Both macOS and Linux daemon tests verify the corrected contract.
