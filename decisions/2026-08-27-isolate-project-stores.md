# Isolate SCS graph and vector stores per project

Date: 2026-08-27
Status: Accepted

## Context

SCS currently keeps every repository in one SQLite graph and one USearch
sidecar under `SCS_HOME`. This makes the store grow with every indexed project,
leaves vector statistics globally scoped, and makes future schema changes an
all-project operation without a durable fleet migration record.

The shared USearch sidecar cannot be safely partitioned by project with the
current storage API: it lacks exportable vectors and durable membership
metadata. Copying it into several new stores would make semantic integrity
unprovable.

## Decision

SCS will use a central catalog and job queue with one graph/vector store per
canonical project root. Each project store contains its own SQLite database,
USearch sidecar, and provider metadata. The catalog owns root-to-store routing,
generation activation, and migration journaling; public APIs continue to take
repository roots, never storage paths or cross-store numeric IDs.

Migrations are forward-only, versioned, staged, journaled, and verified before
activation. New-format stores use a mutable active generation selected by a
checksummed atomic `CURRENT` manifest; verified migration staging generations
are immutable. The daemon remains the only writer. SQLite embedding records
hold canonical vector payloads and digests, making USearch a rebuildable
sidecar. A normal indexing crash that breaks parity rebuilds or degrades only
semantic state, never falsely advertises semantic readiness.

For the initial topology cutover, SCS will back up then discard the existing
shared indexed data. The empty catalog creates no stores; only an explicit
durable index or reindex request creates its project store and rebuilds
structural and semantic data. It will not attempt automatic reindexing or
shared-vector extraction. A global migration
admission gate archives/cancels every legacy queued job and rejects all new
mutations until the new catalog is active; old jobs are never routed to new
stores. Read-only requests never create a catalog record or project store.

## Rejected alternatives

- Keep one global graph and merely add per-repository filters. This does not
  bound per-project storage, isolate vector files, or make individual-store
  schema recovery possible.
- Copy the shared USearch sidecar into every project store. This duplicates and
  leaks vectors across projects with no supported correctness proof.
- Extract vectors from the shared sidecar during migration. The present API has
  no safe project-scoped export or membership ledger; adding an extraction path
  solely for legacy data would add high-risk migration complexity that the user
  has explicitly declined.
- Automatically reindex all projects after cutover. This violates SCS's
  explicit-indexing/no-eager-ingestion policy and could create uncontrolled
  local compute work.

## Consequences

- Per-project storage growth, vector integrity, backup, and migration status
  become independently observable.
- The daemon must replace its singleton graph assumptions with catalog-routed
  store handles and generation-aware durable jobs.
- The initial migration intentionally sacrifices reconstructible index data;
  repository source remains authoritative and read-only, and explicit reindex
  rebuilds SCS-owned derived state.
- Operators gain an executable rollback: stop the new daemon, restore the
  verified legacy archive, and run the prior compatible SCS release before
  archival cleanup. New-format `CURRENT` pointers never claim to switch back
  to the incompatible legacy topology.
