# Expose durable repository deletion through MCP

Date: 2026-09-09
Status: Accepted

## Context

SCS exposes indexing through MCP but does not expose the inverse lifecycle
operation. The internal `repository.drop_index` route deletes graph-owned data
and stops the current watcher, but it leaves the project-store catalog record
and files enrolled. A later daemon can therefore restore the watcher and
repopulate an index that the caller intended to remove.

Repository deletion must remain safe when the source directory has been moved
or removed. It must affect only derived state below `SCS_HOME`, preserve other
project stores, survive process interruption, and remain safe if a recovered
deletion job encounters a newer store generation for the same canonical root.

The accepted decision from 2026-08-05 fixed the model-facing surface at ten
tools to remove aliases and unsupported diagnostics. Repository deletion is a
distinct, necessary lifecycle task rather than an alias for an existing query
or ingestion operation.

## Decision

Add `delete_repository(repo_path)` as the eleventh MCP tool. It is a closed-world
operation with `readOnlyHint=false`, `destructiveHint=true`,
`idempotentHint=true`, and `openWorldHint=false`. Its destructive scope is
limited to SCS-owned catalog, graph, vector, ingestion, watcher, and
project-store state. It never deletes or modifies repository source.

The source path does not need to exist. When no catalog entry, project store,
or deletion tombstone exists, the request succeeds immediately as an
idempotent no-op. Otherwise, the tool returns a durable job acknowledgement and
the daemon stops the repository watcher.

The durable job retires the bound project store in recoverable phases:

```text
delete graph contents
        |
        v
release cached handle
        |
        v
store --atomic rename--> deterministic tombstone
        |
        v
conditional catalog unregister
        |
        v
remove tombstone
```

Deletion dispatch occurs before ingestion-pipeline construction, so recovery
does not depend on readable source or an active graph path. The tombstone is
derived from validated job and store identities and remains strictly contained
under `SCS_HOME/projects`.

Catalog removal is conditional on the immutable store ID and generation held
by the deletion job. A recovered job may clean only its own tombstone when the
catalog points at a newer generation; it must never unregister or remove that
newer store. Retries resume from an active store, a renamed store with its
catalog binding, an unregistered tombstone, or a fully absent store. Startup
does not restore watchers for repositories with active deletion work, and new
indexing for the same root is rejected while deletion is active.

This decision supersedes
`decisions/2026-08-05-reduce-mcp-tool-footprint.md` only where that decision
requires exactly ten tools. Its rationale against aliases, dynamic discovery,
and unsupported diagnostics remains accepted.

## Rationale

A durable job preserves SCS's existing acknowledgement and recovery model for
potentially expensive destructive work. Atomic store retirement separates the
live project-store name from cleanup, while the deterministic tombstone gives a
recovered worker an unambiguous continuation point. Conditional catalog
removal preserves generation isolation when requests and recovered work
interleave.

Treating an absent repository as success makes deletion safely repeatable and
allows callers to converge on the desired state without first discovering
whether the source or index still exists. Keeping all deletion paths beneath
validated SCS-owned storage preserves the product boundary that repository
source is read-only.

## Rejected alternatives

- **Expose the existing `repository.drop_index` behavior unchanged.** It leaves
  catalog enrollment and project-store files behind, so restart can recreate
  the supposedly deleted index.
- **Remove only the catalog record.** This prevents watcher restoration but
  leaks graph and vector files and provides no recoverable cleanup boundary.
- **Delete the store directory before establishing a tombstone.** A crash can
  leave a catalog record pointing at a partially removed store that the bound
  job can no longer open or safely resume.
- **Require the source directory to exist.** Source is not SCS-owned state and
  may legitimately disappear before its derived index is deleted.
- **Return an error for an already absent repository.** This makes retries and
  automation less reliable without protecting additional state.
- **Unconditionally unregister by repository root.** A stale recovered job
  could remove a newer generation created after its original deletion target.
- **Perform filesystem retirement only after marking the job complete.** A
  crash would report success while leaving SCS-owned state behind and no active
  job capable of finishing cleanup.

## Consequences

- MCP discovery grows from ten to eleven tools for one explicit lifecycle
  operation. Clients can distinguish durable acceptance from an already-absent
  no-op.
- Deletion remains observable through the existing durable job APIs and can be
  retried after transient graph, catalog, or filesystem failures.
- A repository cannot be indexed or incrementally updated while its deletion
  is active. A later explicit index request, after deletion completes, creates
  a fresh project-store generation.
- Startup watcher restoration must consult active deletion work as well as the
  catalog. A failed terminal job may restore the watcher only while its original
  catalog binding remains active and usable.
- Store retirement must evict native graph handles before removing files and
  must reject symlink or containment violations. Sibling stores and repository
  source remain untouched.
- Rolling back the implementation restores the ten-tool surface but cannot
  recover derived indexes that users already deleted. Those indexes can be
  rebuilt only by a later explicit indexing request.
