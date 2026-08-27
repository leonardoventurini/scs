# Isolate SCS storage by project and migrate safely

## Goal and scope

Replace SCS's one shared graph/vector store with one isolated store per
canonical project root. Each store owns its own SQLite knowledge graph,
USearch embedding sidecar, and provider metadata beneath `SCS_HOME`; the
daemon, public MCP endpoint, runtime socket, and durable queue remain shared.

The cutover deliberately discards the existing shared graph and vector data
after a verified backup. It does not attempt to partition the shared USearch
file. Once a project has an isolated store, semantic content is recreated only
by an explicit durable reindex request. This is a plan-only artifact; no schema,
runtime, or live data is changed by this document.

## Evidence and uncertainty

- Project: `scs`
- Project root: `/Users/leonardo/Repositories/mentagen/scs`
- Risk tier: high. The implementation will replace persistent graph/vector
  topology and must remain correct across crashes, restarts, concurrent
  watchers, and every registered project.
- Today `SCSPaths.resolve` fixes one global `index.db`, `index.usearch`,
  `provider.json`, and `jobs.db`; `SCSDaemon.start` builds one `NativeGraph`
  and one job runner around that graph.
- Rust schema startup is idempotent DDL with retired-object drops, not a
  versioned migration protocol. SQLite and USearch mutate independently.
- The current USearch sidecar has no supported project-scoped vector export or
  durable vector-membership ledger. Copying it into several stores would make
  semantic scope unverifiable.
- User decision: drop existing indexed data and reindex projects anew. This
  removes the unsafe vector-extraction alternative and retains the existing
  no-eager-ingestion policy.
- Main remaining implementation uncertainty is the exact native API required
  to verify and activate a staged USearch sidecar. Resolve it with a native
  prototype before changing the live path contract.

## Contracts and decisions

### Persistent topology

- `SCS_HOME/catalog.db` is the authoritative catalog. It maps one canonical
  repository root to one opaque, immutable `store_id`, active generation,
  lifecycle state, provider fingerprint, schema version, and migration journal
  reference.
- `SCS_HOME/jobs.db` remains a central durable queue. Every job snapshots
  `store_id` and store generation at enqueue time; claim serialization is by
  `store_id`, not merely repository path.
- `SCS_HOME/projects/<store_id>/` is mode `0700`, with a mutable active
  `generations/<generation>/` directory containing `index.db`, `index.usearch`,
  and `provider.json`. A checksummed, immutable migration-activation manifest
  attests the verified staging/schema identity, and an atomically replaced
  `CURRENT` file selects the active generation. Runtime indexing mutates only
  that selected generation through its embedding journal; migration staging
  directories are immutable once verified. The vector
  filename remains derived from its SQLite path, matching the current PyO3
  contract.
- A store contains exactly one canonical repository row. Routes resolve
  canonical root through the catalog before opening a handle; no public request
  accepts a store filesystem path or crosses stores by numeric `repo_id`.
- Provider identity is per store. A model, provider, or dimension mismatch
  quarantines/rebuilds only that store's vectors and marks only that store
  semantic-stale.

### State and ownership

- Store lifecycle: `uninitialized` → `ready_structural` → `semantic_stale` or
  `semantic_ready`; migration adds `migrating` and terminal
  `migration_failed_recoverable`.
- The daemon-wide process lock remains the sole writer lock. A handle registry
  serializes writes per `store_id` and closes only an idle handle; watcher and
  queue work route through the same resolver.
- The one-root invariant is persisted in every project database, not merely
  route-validated: a singleton store-identity table records the canonical root
  and guarded native creation rejects a second `repos` row. Direct native test
  fixtures must prove a second root cannot be inserted.
- The catalog migration journal is forward-only and idempotent:
  `planned` → `quiescing` → `snapshot_verified` → `staging` →
  `structural_verified` → `vector_verified` → `ready_to_activate` →
  `activated` → `cleanup_complete`. `failed_recoverable` and `aborted` are
  terminal evidence states. Restart resumes only from journal evidence, never
  from directory names.

### Schema and graph/vector integrity

- Replace startup DDL as migration behavior with ordered, forward-only Rust
  migrations. Every catalog and store database records a migration ledger and
  `PRAGMA user_version`; each SQLite migration runs under `BEGIN IMMEDIATE`
  with foreign keys enabled.
- Add durable embedding records to each project database: node ID, collision-
  checked USearch key, provider fingerprint, dimension, content fingerprint,
  canonical f32 payload, payload digest, and vector generation. SQLite is the
  authoritative embedding database; USearch is a rebuildable search sidecar.
  Records, payload digests, and sidecar membership form the graph/vector
  completeness oracle.
- Runtime writes validate vector shape and complete embedding records before
  publishing semantic readiness. An ingestion job first marks the store
  `semantic_stale`, commits structural rows, flushes validated sidecar changes,
  then commits matching embedding records. At restart, any parity failure
  rebuilds the sidecar from verified SQLite payloads when possible; otherwise
  it clears only the derived sidecar/records, leaves structure intact, and keeps
  the store `semantic_stale` until an explicit reindex. Thus a crash can cost
  derived vectors but never falsely advertise semantic readiness.
- Structural schema changes verify `integrity_check`, `foreign_key_check`, the
  single-root invariant, and expected node/edge/file counts. Vector activation
  rebuilds the sidecar from authoritative SQLite payloads, then verifies
  provider fingerprint, dimension, unique vector keys, payload digests, and
  exact record parity for the generation.

### Cutover policy

- Do not copy rows, vectors, repository records, or queued jobs from the
  existing shared index into project stores. Before activation, create a
  manifest-backed backup of the legacy shared SQLite, USearch, provider
  metadata, and jobs DB; verify checksums and restore it into an isolated home.
- Enter a global migration admission gate before quiescing. It rejects direct
  queue requests, watcher requests, and drop-index requests; it then drains or
  cancels every legacy queued/retrying/running job into a terminal archived
  record. No legacy job is mapped to a fresh project store. Restart preserves
  this gate until migration journal evidence reaches a terminal outcome.
- Activate an empty catalog only after the backup verifies. Read, search,
  status, and watcher discovery never register a root or create a store. Only
  an explicit `repository.index` / `repository.reindex` starts an idempotent
  catalog-registration operation, creates the empty store, and durably
  reconciles its generation-bound job after restart.
- Keep the verified legacy backup outside active paths for one release window.
  During that window rollback means stopping the new daemon, restoring the
  verified archive to the legacy paths, and running the prior compatible SCS
  release. `CURRENT` switches only between new-format generations; it cannot
  reactivate the legacy topology.

## Atomic implementation slices

1. Establish typed store and catalog contracts — files: `src/scs/paths.py`,
   `src/scs/config.py`, new catalog/store modules and unit tests. Define
   `StoreId`, `StoreGeneration`, `StoreState`, `ProjectStorePaths`,
   `CatalogRecord`, and journal transitions. Verify canonical-root uniqueness,
   stable opaque IDs, path containment, permissions, and no store creation on
   read-only requests. Abort if a proposed path can escape `SCS_HOME/projects`.

2. Make native storage versioned and verifiable — files: `crates/scs-store`
   schema/connection/graph/vector modules and PyO3 bindings. Replace direct
   startup cleanup with a migration ledger, transaction runner, authoritative
   embedding-record table,
   table, and generation-aware vector activation primitives. Verify a negative
   fixture with an orphan, duplicate, missing, or wrong-dimension vector fails
   activation. Abort and retain staging if any integrity oracle fails.

3. Route daemon, jobs, watchers, and services through store identity — files:
   `src/scs/main.py`, `src/scs/services/routes.py`, `src/scs/indexing/jobs.py`,
   `runner.py`, `pipeline.py`, `watcher.py`, and MCP contracts. Replace the
   singleton graph with a catalog-resolving handle registry; capture store ID
   and generation in jobs; report per-store vector counts. Verify two projects
   can index/search concurrently without nodes, vectors, job leases, or
   watchers crossing stores. Abort if a job's catalog generation differs from
   its snapshot or a non-explicit read path attempts registration.

4. Build the cutover orchestrator and recovery protocol — files: new migration
   coordinator, catalog journal, service startup gate, and integration tests.
   Close the global mutation-admission gate, archive legacy jobs, wait for
   active work at durable boundaries, checkpoint and checksum legacy artifacts,
   build empty staged catalog/store layout, verify, then atomically activate a
   manifest/`CURRENT` pointer. Define global readiness as maintenance/read-only
   until every registered store reaches the target schema; no mixed-version
   store is writable. Verify fault-injected interruption at every journal
   transition leaves either the old active topology or a fully verified new
   one. Abort leaves the old topology active and staging retained only as
   journaled evidence.

5. Perform the explicit live cutover and fresh indexing — files: SCS-owned
   runtime data only plus operator documentation. Back up, migrate, restart,
   confirm every registered project is `semantic_stale` with no old global
   vectors exposed, then explicitly reindex projects selected by the operator.
   Verify each completed job has a matching store-generation and semantic
   search is scoped to that project. Roll back by stopping the new daemon,
   restoring the verified legacy archive, and running the prior compatible
   release before any destructive backup cleanup.

6. Remove the legacy compatibility window only after validation — files:
   migration coordinator, documentation, and tests. After one release window
   and an operator-confirmed backup restore rehearsal, remove legacy active
   path handling but retain an explicit archive-retention policy. Verify a
   fresh `SCS_HOME` creates no project store until an explicit index request.

## Risks and recovery

- Crash during cutover → active graph/vector mismatch. Prevent with staging,
  immutable generation manifests, and journaled activation; detect through
  restart recovery and embedding-record parity; recover by retaining/switching to the
  last verified `CURRENT` generation.
- Schema migration fails for one project → unnoticed partial fleet migration.
  Prevent deterministic store order and per-store journal status; detect a
  nonterminal catalog migration state; recover by resuming only failed stores
  while preserving verified stores and blocking their writes until the catalog
  policy is resolved.
- Job/watcher writes the wrong store → cross-project contamination. Prevent
  immutable job store snapshot, catalog generation comparison, and per-store
  locks; detect route/job integration tests; recover by rejecting the job before
  graph mutation and retaining error evidence.
- Invalid sidecar/provider → false semantic readiness. Prevent embedding-record
  provider fingerprint checks; detect startup activation gate; recover by
  marking only the affected store `semantic_stale` and requiring explicit
  reindex.
- Crash during a normal embedding batch → sidecar/embedding-record divergence.
  Prevent by clearing semantic readiness before structural mutation and writing
  records only after a flushed sidecar; detect parity at startup and before
  serving semantic search; recover by rebuilding from durable payloads or by
  clearing the derived pair and requiring explicit reindex.
- Backup is unusable → irreversible data loss. Prevent preactivation checksum,
  SQLite read-only integrity checks, and a restore rehearsal; detect any failed
  oracle; recover by aborting before the `CURRENT` pointer moves.

## Verification gauntlet

- **Hard gate — isolation:** migrate/index two fixture repositories, then prove
  each store directory contains only its root's rows and embedding records; search A
  must never return B. Use new multi-store integration tests and negative
  cross-root fixtures.
- **Hard gate — schema integrity:** run every migration version over empty and
  populated fixture stores; `integrity_check` returns `ok`, `foreign_key_check`
  is empty, and ledger/version equal the target. A deliberately bad FK fixture
  must fail before activation.
- **Hard gate — vector parity:** compare authoritative embedding records against
  embeddable nodes, vector key uniqueness, provider fingerprint, dimension, and
  canonical payload digests for each active generation. Rebuild USearch solely
  from those SQLite payloads, then require `USearch.size == record count` and
  `contains(node_id)` for every record. Delete, add, overwrite, or collide one
  fixture vector; activation must reject/rebuild it. This proves actual vector
  values through their durable payload digests rather than key presence alone.
- **Hard gate — crash recovery:** inject failure/SIGKILL at every journal
  transition and restart the daemon. Assert exactly one active verified
  generation, no unjournaled store, and deterministic resumability.
- **Hard gate — runtime vector recovery:** inject a kill after structural
  commit, after sidecar flush, and after embedding-record commit. Restart must never
  report semantic-ready unless the parity oracle passes; divergent derived data
  must become semantic-stale without damaging structure.
- **Hard gate — cutover safety:** produce and verify backup manifest/checksums,
  then restore it in an isolated SCS home before live activation. A corrupt
  backup fixture must abort before activation.
- **Hard gate — explicit reindex policy:** after cutover, observe no queued
  ingestion until an explicit request; then complete an exact store-generation
  job and confirm only that project's semantic search becomes ready.
- **Hard gate — legacy job containment:** seed queued, retrying, and running
  legacy jobs plus a restart mid-quiesce. Migration must archive/cancel them
  with terminal evidence, reject new admission, and never route one into a
  new-format store.
- **Regression gates:** targeted Python/Rust migration suites, proxy tests,
  `just verify`, and source-read-only Git checks. Preserve current performance
  budgets or establish comparable per-store ceilings with measured evidence.

## Execution checklist

- [ ] Define catalog, store, generation, and journal contracts — files:
  path/config/catalog modules; verify: typed unit/property tests; done when
  canonical roots map one-to-one to contained mode-0700 stores.
- [ ] Add versioned native migrations and authoritative embedding records — files:
   `crates/scs-store`; verify: Rust migration/integrity negative tests; done when
  no schema/vector generation activates without parity evidence.
- [ ] Replace singleton graph routing with per-store handles — files:
  daemon, routes, jobs, runner, watcher; verify: two-repository integration
  tests; done when queue, watcher, search, and stats share one store identity.
- [ ] Implement journaled staging, backup, activation, and restart recovery —
  files: migration coordinator/catalog; verify: transition fault-injection and
  restore rehearsal; done when every interruption preserves one verified active
  generation and migration admission blocks legacy/new writes deterministically.
- [ ] Run the live clean cutover — files: SCS-owned data only; verify: backup
  manifest, service health, empty per-store state, and no automatic jobs; done
  when the catalog is active and all registered projects are semantic-stale.
- [ ] Explicitly reindex chosen projects — files: per-store graph/vector data;
  verify: exact durable jobs plus project-scoped semantic search; done when
  each selected project is semantic-ready with no cross-store results.
- [ ] Retire legacy active-path support after the release window — files:
  migration/docs/tests; verify: restore rehearsal and fresh-home isolation;
  done when legacy state is archived under the retention policy.

## Verification and rollout

Implementation must begin with a native staging prototype and fault-injection
test harness, not a live data migration. The first production run is a
deliberate maintenance operation: announce the quiesce, retain the verified
backup, observe journal transitions and store counts, and abort on the first
failed hard gate. The supported rollback stops the new daemon, restores the
verified legacy archive, and runs the prior compatible release; no reverse
migration or source mutation is authorized.
