# Delete an ingested repository

## Problem

SCS can index and incrementally reconcile repositories through MCP, but it has
no model-facing operation that durably forgets one repository.

The existing internal `repository.drop_index` route is incomplete for that
task. It deletes graph-owned repository data and stops the current watcher, but
leaves the central project-store catalog entry and store directory active. A
later daemon can restore the watcher and repopulate the index.

The requested outcome is an idempotent `delete_repository` MCP tool that works
even after the source directory was moved or removed. It deletes only SCS-owned
state and never mutates repository source.

## Evidence

- `src/scs/mcp/inventory.py` exposes ten tools and no repository deletion.
- `src/scs/main.py` queues `drop_index` and stops the in-memory watcher.
- `src/scs/indexing/runner.py` calls `delete_repo_sync` but does not retire the
  project-store catalog record or directory.
- daemon startup restores watchers for every active catalog record.
- `decisions/2026-08-05-reduce-mcp-tool-footprint.md` intentionally fixed the
  public inventory at ten tools, so adding an eleventh requires a superseding
  decision.

## Desired contract

The MCP tool is:

```text
delete_repository(repo_path: str)
    -> {
         accepted: bool,
         already_absent: bool,
         job: object | null
       }
```

The supplied path identifies the prior canonical repository root. It need not
exist on the source filesystem. An empty path is rejected.

When deletion work is needed, SCS returns a durable job acknowledgement without
waiting for cleanup. When no catalog entry, project store, or deletion
tombstone exists, it returns a successful idempotent no-op with
`already_absent=true` and `job=null`.

The tool annotations are:

```text
readOnlyHint     = false
destructiveHint  = true
idempotentHint   = true
openWorldHint    = false
```

Deletion removes the repository's SCS-owned graph nodes, edges, vectors,
ingestion records, central catalog registration, watcher enrollment, and
project-store files. It does not remove the source directory or any source
file.

## Durable deletion design

Deletion remains a background job and keeps the existing highest-priority
queue mode. The runner dispatches deletion before constructing an ingestion
pipeline, so cleanup does not require readable source or a still-active graph
binding.

```text
delete request
    |
    +-- stop watcher
    +-- enqueue/reuse durable deletion job
             |
             v
       delete graph contents
             |
             v
       evict cached graph handle
             |
             v
       store dir --atomic rename--> deterministic tombstone
             |
             v
       conditional catalog unregister
             |
             v
       remove tombstone
```

The tombstone name is derived from validated store and job identities and is
strictly contained below `SCS_HOME/projects`. It records that graph deletion
and store retirement crossed their durable boundary. A retry resumes from any
of these states:

- graph and store active: repeat graph deletion, then retire the store;
- tombstone present and catalog bound to the job: unregister, then remove it;
- catalog absent and tombstone present: remove it;
- catalog, store, and tombstone absent: succeed as already absent;
- catalog points at another generation: never remove the newer store; clean
  only this job's tombstone and report the old deletion as superseded.

Catalog removal is conditional on the immutable store ID and generation held
by the job. This prevents an old recovered job from unregistering a newly
indexed generation.

While deletion is active, explicit full, forced, and incremental indexing for
the same root is rejected instead of returning a misleading acknowledgement.
Startup does not restore a watcher for a repository with active deletion work.
If deletion exhausts its retries while the catalog remains active, SCS restores
the watcher; partial retirement stays recoverable by another idempotent delete.

## Uncertainty and constraints

- SQLite, native graph, and vector handles must be released before the retired
  store is removed.
- Filesystem cleanup can fail after graph deletion. The job must remain safely
  retryable rather than treating catalog absence as an error.
- The source path can disappear between indexing and deletion. Canonical
  identity normalization therefore cannot require filesystem existence.
- One daemon owns an SCS data root, but request handling and the background
  runner still interleave. Queue and catalog binding checks must close the
  re-index race.
- No persisted source-derived format changes are required. A deterministic
  deletion tombstone is transient SCS-owned recovery state.

## Test strategy and acceptance criteria

Tests are added before or alongside implementation.

- [x] The exact MCP inventory contains eleven tools including
  `delete_repository`.
- [x] Its schema and safety annotations match the public contract.
- [x] MCP normalizes a prior repository identity without requiring the source
  directory and rejects an empty path before gateway dispatch.
- [x] Repeated deletion is a successful no-op when no SCS state remains.
- [x] Catalog unregister is conditional, idempotent, and isolated from sibling
  repositories.
- [x] Store retirement is contained under `SCS_HOME`, preserves source and
  sibling stores, and resumes from tombstone/catalog crash windows.
- [x] The deletion runner never constructs an ingestion pipeline and retries a
  transient failure to convergence.
- [x] Queue tests cover full-before-delete, delete-before-full, duplicate
  deletion, and running-work ordering.
- [x] Index and incremental-ingestion requests cannot resurrect a repository
  while deletion is active.
- [x] A daemon integration test proves catalog, graph, vectors, ingestion
  records, watcher, and store files remain absent after restart.
- [x] A moved or removed source repository can be deleted.
- [x] Repository source fingerprints are unchanged.
- [x] Targeted tests, strict type checking, and `just verify` pass.

## Documentation and decision

Update the README tool inventory and deletion semantics. Clarify that the
installer's lack of a global automatic purge is separate from per-repository
deletion. Add a decision record superseding the fixed ten-tool inventory for
this single distinct lifecycle task.

## Recovery and rollback

An interrupted deletion is resumed by its durable job and deterministic
tombstone. A terminal failure reports the failed job and retains enough state
for an explicit retry; it never mutates source.

Before release, rollback is a normal code revert. After release, removing the
tool would be a public API break, so a corrective patch release should preserve
the tool and fix its implementation. Already deleted SCS indexes are rebuilt
only by a later explicit index request.

## Direct rollout

- [x] Implement and verify the feature on `main`.
- [x] Update all version identities to `0.1.14` and add the changelog entry.
- [x] Run `python3 scripts/check-release-version.py v0.1.14` and `just verify`.
- [ ] Push `main` and wait for its GitHub CI run to succeed.
- [ ] Create and push annotated tag `v0.1.14`.
- [ ] Wait for the GitHub release workflow and verify the seven expected
  assets, checksums, and attestations.
- [ ] Exercise the installed MCP inventory and deletion/restart behavior where
  the release environment permits it.

## Verification record

Executed locally on 2026-09-09:

- focused repository-deletion suite: 80 passed;
- follow-up uninitialized-store and daemon suite: 24 passed;
- `basedpyright`: 0 errors, 0 warnings;
- `ruff check src tests`: passed;
- full Python suite through `just verify`: 317 passed with 86.56% coverage;
- Rust workspace through `just verify`: 99 tests passed;
- `scripts/check-release-version.py v0.1.14`: returned `0.1.14`;
- `uv lock --check`, locked Cargo metadata, and whitespace checks: passed.

The GitHub `main` CI run, tag release run, release assets, checksums,
attestations, and installed-release checks remain pending until the verified
commit is pushed.
