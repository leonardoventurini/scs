---
status: implemented
project: scs
project-root: /Users/leonardo/Repositories/scs
created: 2026-09-22
updated: 2026-09-22
owner: repository-lifecycle
decision:
supersedes: 2026-09-22-project-lifecycle-cli.md (listing contract only)
superseded-by:
implementation:
  commits:
    - 1b55b3a
  pull-request:
---

# Read-only project listing

## Outcome

`scs list` reports saved project indexes without starting the daemon or scheduling
reconciliation. Table and JSON listings retain their existing shape and ID order.

## Problem and evidence

The CLI currently calls `projects.list` through `_call_daemon`, which starts a
missing daemon. Startup queues one reconciliation job per enrolled project, so
repeated list invocations can repeatedly show `queued` even when each batch
later completes. The job history confirms repeated completed startup batches.

## Scope and contract

The CLI reads the SCS-owned catalog and per-project ingestion records in
read-only SQLite mode. `STATE` reports saved index availability (`indexed` or
`unindexed`), not transient job progress. File count and last-indexed time come
from committed ingestion records. JSON retains `id`, `repo_path`, `state`,
`file_count`, `last_indexed`, and `active_job_id` (`null` for a saved snapshot).
The daemon's `projects.list` and `repositories.status` live-state contracts
remain available to existing callers. Delete, reingest, and explicit indexing
continue to use the daemon.

An absent catalog produces an empty listing and creates no SCS directories.
The read path must not migrate a legacy catalog or open a writable graph.

## Security and data

No authentication boundary, repository source, or persisted format changes.
SQLite paths remain contained by existing SCS path validation. Stored JSON
is read only from SCS-owned project databases.

## Acceptance criteria

- Listing while the daemon is absent does not spawn it, enqueue jobs, or write
  persistent files.
- Repeated listings show the same saved state until index data changes.
- Populated and empty completed indexes report their stored file count and
  last-indexed value as available.
- Table and JSON remain ordered by project ID with their existing fields.
- A missing catalog returns an empty listing without creating directories.

## Verification results

- Contract tests: 15 passed, including saved populated/empty indexes, no daemon
  call, and an absent catalog without directory creation.
- `just verify`: Basedpyright and Ruff passed; 364 Python tests and 108 Rust
  unit tests passed; Rust doc tests passed; coverage was 87.05%.
- Local `uv run scs list --json` reported saved indexes as `indexed`; the latest
  durable job creation time remained unchanged and the active-job count was 0.
- The installed CLI outside this checkout was not changed or checked.
