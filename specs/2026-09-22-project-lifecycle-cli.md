---
status: implemented
project: scs
project-root: /Users/leonardo/Repositories/scs
created: 2026-09-22
updated: 2026-09-22
owner: repository-lifecycle
decision:
supersedes:
superseded-by:
implementation:
  commits:
    - fc0612d
    - 5b852a6
    - 6a9e603
    - d2a009f
  pull-request:
---

# Project Lifecycle CLI

## Outcome

Operators can list, delete, and fully reingest enrolled SCS projects through
top-level CLI commands. Every enrolled project has a stable, small numeric ID,
so destructive and rebuilding operations do not require copying an absolute
path.

## Problem and evidence

`scs index PATH` and `scs reindex PATH` already submit durable background jobs,
and the daemon already implements durable repository deletion. The CLI exposes
neither deletion nor catalog-wide discovery. `repositories.status` reports only
paths supplied by its caller, and the project-store catalog identifies records
with canonical paths and SHA-256 store IDs rather than human-friendly IDs.

## Scope and non-goals

- Add `scs list`, `scs delete SELECTOR`, and `scs reingest SELECTOR`.
- Accept either a durable numeric project ID or a repository path as a selector.
- Render a concise table from `scs list`; support `scs list --json` for scripts.
- Return deletion and reingestion job acknowledgements immediately.
- Preserve `scs index PATH` and `scs reindex PATH`.
- Do not mutate repository source, add interactive confirmation, wait for jobs,
  expose lifecycle operations through new MCP tools, or renumber project IDs.

## Contracts

The catalog adds an integer `project_id` primary key allocated monotonically.
Existing catalog rows receive IDs once during an atomic, idempotent migration.
Deleting a project retires its catalog row; a later fresh enrollment receives a
new ID rather than reusing the deleted identity.

```text
scs list [--json]
scs delete ID|PATH
scs reingest ID|PATH
        |
        v
SCSWire selector resolution in the daemon
        |-- delete   -> repository.drop_index durable job
        `-- reingest -> repository.reindex durable job
```

Numeric selector resolution occurs inside the single-writer daemon immediately
before mutation. Paths retain the existing canonicalization rules. An unknown
numeric ID fails explicitly and cannot be interpreted as a filesystem path.

The listing is ordered by numeric ID and reports ID, state, file count, last
indexed time, and canonical path. JSON output has one top-level `projects`
array with the same typed fields. Empty catalogs produce a header-only table or
`{"projects": []}`.

Deletion remains idempotent for path selectors. A numeric selector identifies
an enrolled row and therefore fails after that row has been retired. Reingest
requires a currently enrolled project whose source path remains a readable
repository, as required by the existing forced-index route.

## Security and data

This changes the persisted catalog schema but not stored source-derived graph
formats. Migration is local, transactional, and preserves canonical roots,
store IDs, generations, and lifecycle states. Numeric IDs are opaque selectors,
not authorization tokens. Deletion still affects only SCS-owned state below
`SCS_HOME`; repository source remains read-only.

## Acceptance criteria

- Existing catalog rows gain unique positive IDs exactly once and retain them
  across daemon restarts and ordinary reindexing.
- New projects receive monotonically increasing IDs that are not reused after
  deletion.
- `scs list` displays every enrolled project in deterministic ID order, and
  `--json` returns the corresponding machine-readable records.
- `scs delete ID|PATH` submits the existing durable deletion behavior without
  touching repository source.
- `scs reingest ID|PATH` submits a forced full ingestion job.
- Unknown, zero, negative, malformed, and ambiguous selectors fail without
  submitting work.
- Existing path-based index and reindex commands remain compatible.

## Risks and recovery

An interrupted schema migration must roll back without exposing a partially
migrated catalog. A pre-migration binary cannot read the new table shape safely;
rollback after deployment therefore uses a corrective forward release or a
catalog backup restored while the daemon is stopped. Derived project indexes
remain rebuildable from repository source.

## Execution checklist

- [x] Add catalog migration and identity tests.
- [x] Add catalog-wide lifecycle wire contracts and selector tests.
- [x] Add CLI parser, table, JSON, and dispatch tests.
- [x] Implement the catalog migration and daemon operations.
- [x] Implement the three CLI commands and update documentation.
- [x] Run focused tests and `just verify`.
- [x] Release and install the new SCS version.
- [x] Verify the installed CLI against the local catalog.

## Verification results

- Focused lifecycle suite: 42 tests passed.
- `just verify`: strict Python types and lint passed; 360 Python tests and 104
  Rust tests passed; native extension and doc tests passed.
- Release workflow `35767694537`: validation, source distribution, macOS and
  Linux wheels, attestations, and publication passed.
- Installed `scs version`: `0.1.21`.
- Legacy catalog backup:
  `~/.scs/catalog.db.pre-v0.1.21-project-ids`; SHA-256 matched the original
  before migration.
- Migrated catalog: SQLite integrity check passed; all 30 rows received unique,
  ordered IDs 1 through 30.
- Installed CLI: table and JSON listings returned the same 30 projects;
  `scs reingest 26` acknowledged durable forced job `ingest_58bca7d93cc3`;
  deleting an absent path returned the expected idempotent acknowledgement.
