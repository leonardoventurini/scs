# Resumable batch-committed ingestion

## Goal and scope

Replace SCS's all-or-nothing ingestion-hash acknowledgement with deterministic,
bounded embedding batches. A file whose new content hash is acknowledged must
never be embedded again after a later provider failure, daemon restart, or job
retry.

This change covers explicit full, force-full, changed-file, cleanup, and
watcher-driven ingestion for one project store. It does not change public MCP
tool inventory, make indexing synchronous, or change project-store routing.

## Evidence and uncertainty

- Project: `scs`
- Project root: `/Users/leonardo/Repositories/mentagen/scs`
- Mode: plan-only.
- Risk tier: medium. The change alters durable retry semantics across SQLite,
  a non-transactional USearch sidecar, the durable job runner, and OMLX.
- Current `IngestionPipeline._ingest_entries` parses all changed files,
  replaces all structure, obtains all embeddings, flushes once, then writes
  every `ingested_files` hash. A transient provider failure repeats completed
  work on retry.
- SolidScript exposes the cost: 29,385 entities become roughly 919 serial
  32-item OMLX requests. Its job has no durable batch progress, and the vector
  write plus hash acknowledgement are deferred to the end.
- Current `_prepare_edges` resolves endpoints only from the passed parsed set.
  Naively splitting it into file batches would silently discard cross-batch
  calls and imports.
- Current native deletion removes graph rows, vector entries, and the
  `ingested_files` record together; it cannot preserve a retry checkpoint if
  the subsequent sidecar flush fails.
- `embedding_records` exists in schema but is not populated by the current
  vector write path. Until that is implemented, reopened USearch is the
  available vector-integrity oracle.

## Contracts and decisions

### Planning and graph completeness

- Discovery hashes and sorts every candidate `FileEntry` by relative path.
  Parsing happens before destructive mutation. A parse failure leaves that
  file's prior nodes, edges, and hash intact; it is excluded from the new
  commit plan and reported in `files_failed`.
- The successful parse set becomes one immutable `StructuralPlan`: deterministic
  node IDs for all changed entities, lookup results for unchanged endpoint
  symbols, and every valid planned edge. This prevents an import/call edge from
  disappearing merely because its endpoints fall in different embedding
  batches.
- SCS installs all nodes and valid edges in the `StructuralPlan` before it
  acknowledges the first new content hash. Structural rows may therefore be
  newer than their file hash after an interrupted job; the hash remains the
  only success checkpoint, and a retry safely replaces unacknowledged files.
- Edge resolution is explicit: targets in the successful parse set use the
  plan's deterministic IDs; retained targets use a repository-scoped native
  qualified-name lookup. Unresolvable external targets remain absent under the
  existing parser contract, with a measured counter in progress diagnostics.

### Batch identity and acknowledgement boundary

- `IngestionBatch` contains complete successfully parsed files, ordered by
  relative path. It is formed from `StructuralPlan` using named
  `INGESTION_BATCH_MAX_FILES` and `INGESTION_BATCH_MAX_ENTITIES` limits. The
  planner never splits a file; one oversized file is its own batch.
- Provider request limits remain independent. A batch can make multiple OMLX
  requests, but it retains vectors only for the current batch and releases
  them immediately after acknowledgement.
- For each changed batch, the ordered durable boundary is:

  1. Set the store `semantic_stale` before any structural plan mutation.
  2. Install the complete `StructuralPlan` once, including cross-batch edges.
  3. Request and validate every embedding for the current complete-file batch.
  4. Upsert those vectors and flush the sidecar. Flush must fsync the temporary
     file before rename and fsync the parent directory after rename; a success
     means process- and power-loss persistence at the OS boundary.
  5. In one SQLite transaction, upsert every `ingested_files` record in that
     batch. This transaction is the batch acknowledgement.

- A successful content hash proves that exact content parsed, its structural
  plan was installed, every embeddable node was accepted by the provider, the
  reopened sidecar contains each batch vector, and the sidecar flush preceded
  the single transactional hash acknowledgement.
- A provider, validation, reopen, or flush failure produces no hash update for
  its current batch. A retry skips earlier acknowledged batches and retries
  only the unacknowledged work. It must never publish `semantic_ready`.
- A SQLite acknowledgement transaction failure leaves no partial hash update
  for that batch. The already-flushed vectors are harmless derived state and
  are deterministically overwritten on retry.
- A normal job selects work from `ingested_files` hashes. A new `force_full`
  job instead creates a durable force-attempt snapshot keyed by its job ID,
  store binding, sorted paths, and source hashes. Each target begins `pending`;
  its batch acknowledgement marks it `acknowledged` in the same SQLite
  transaction as its ingestion hash. Retry of that same durable job selects
  only `pending` targets. A new explicit force request creates a new snapshot
  and may reprocess every target even when their hashes match.
- Every planned file is read through one stable-content helper: it reads bytes,
  computes their hash, and parses those same bytes. Immediately before batch
  acknowledgement, it re-hashes every path in the batch; any mismatch from the
  parsed bytes aborts acknowledgement for the entire current batch and leaves
  all of its files pending. Force retries additionally require every parsed
  hash to equal its attempt snapshot hash. A mismatch marks that attempt
  stale/failed and requires a new rediscovery attempt, preventing new source
  from being acknowledged under an old snapshot.

### Deletions, jobs, and state

- Native deletion becomes a two-step capability: `remove_file_graph_and_vector`
  deletes nodes, edges, and vector membership but retains its ingestion record;
  `delete_ingestion_records_batch` removes records transactionally only after a
  successful sidecar flush. The legacy one-step deletion API is retired from
  pipeline use.
- Deleted paths are sorted into deterministic batches. Each batch removes graph
  and vector state, flushes/reopens the sidecar, then transactionally deletes
  its ingestion records. A failed flush leaves records present for retry.
- `IngestionResult` is cumulative across acknowledged batches. `files_failed`
  counts parse failures plus provider/flush failures that prevented an
  acknowledgement; processed and acknowledged counts remain distinct.
- `IngestionJob` remains the coarse retry unit. It gains typed durable progress
  fields for phase, planned/completed batches, planned/acknowledged files, and
  current path; it never persists source text or vector payloads in `jobs.db`.
- The force-attempt snapshot is the narrow exception to hash-only checkpoints:
  it is necessary because force requests intentionally disregard matching
  hashes. It records only job/store identity, relative path, source hash, and
  pending/acknowledged state; terminal snapshots follow the existing durable
  job retention policy.
- Completion publishes `semantic_ready` only after every changed and deletion
  batch is acknowledged and a reopened-sidecar parity check passes. Failed,
  cancelled, and interrupted jobs retain `semantic_stale`.
- Existing immutable store-generation binding and single-daemon ownership remain
  the write boundary; no cross-store lock is introduced.

### Minimum-complexity rationale

- Rejected: full-job in-memory vector accumulation. It repeats successful OMLX
  calls after one failure and has unbounded memory use for large repositories.
- Rejected: a checkpoint table for normal incremental jobs. Content hashes plus
  transactional batch acknowledgement already provide that checkpoint. Force
  jobs need a minimal attempt snapshot because they intentionally ignore hashes.
- Rejected: direct file batches for structural planning. Current edge resolution
  would drop cross-batch relationships; one `StructuralPlan` is the smallest
  mechanism that preserves graph semantics.
- Rejected: acknowledge before flush/reopen. That can make a file look complete
  after a crash while its semantic vector is absent.

## Atomic implementation slices

1. Build typed structural and embedding-batch plans — files:
   `src/scs/indexing/pipeline.py`, parser/graph protocols, and Python tests.
   Add `StructuralPlan`, `IngestionBatch`, complete-file limits, and
   repository-scoped retained-symbol lookup. Verify a caller and callee forced
   into different batches retain their relationship. Abort if an acknowledged
   file can lack a planned incident edge.

2. Add safe native acknowledgement, deletion, and force-attempt primitives — files:
   `crates/scs-store/src/ingestion_files.rs`, graph/PyO3 bindings,
   `src/scs/graph/native.py`, `src/scs/indexing/jobs.py`, and Rust tests. Add
   transactional batch upsert/delete operations and a job-bound force-attempt
   snapshot; split graph/vector removal from record deletion. Verify a forced
   SQLite error rolls back an entire batch and a flush failure leaves deletion
   records intact.

3. Commit changed embedding batches — files: `pipeline.py`, provider fakes,
   vector-index implementation, and targeted tests. Install one structural
   plan, embed/flush/reopen one batch at a time, then acknowledge its hashes in
   one transaction. Add file and directory fsync around sidecar activation.
   Verify provider failure on batch two causes retry to call OMLX only for
   batch two and later.

4. Make state and progress reflect acknowledgement — files:
   `src/scs/indexing/jobs.py`, `runner.py`, `main.py`, events/MCP tests.
   Persist acknowledged batch progress, distinguish it from provider progress,
   and retain `semantic_stale` until reopened-sidecar parity succeeds. Verify a
   generation mismatch aborts before mutation and no failed job becomes ready.

5. Validate restart, deletion, and large-repository recovery — files:
   integration/performance tests and operator documentation. Inject process
   termination after one acknowledged batch and after vector flush before hash
   acknowledgement. Verify restart skips only acknowledged work, retries
   unacknowledged files, preserves cross-batch edges, and bounds retained
   vector memory to one batch.

## Risks and recovery

- Provider fails mid-batch → new structure exists without vectors. Prevent by
  delaying hashes until flush/reopen; detect stale state and absent hash;
  recover by retrying that batch.
- Power loss after sidecar rename → false acknowledgement. Prevent file and
  directory fsync; detect reopened-sidecar parity; recover by leaving the
  missing batch unacknowledged.
- Cross-batch caller/callee → lost relationship. Prevent global structural
  planning; detect forced split-batch edge fixture; recover by rebuilding the
  unacknowledged structural plan.
- Deletion flush fails → stale vector or lost checkpoint. Prevent split native
  delete/finalize operations; detect deletion fault fixture; recover from the
  preserved ingestion record.
- SQLite acknowledgement fails partway → partial retry checkpoint. Prevent a
  transactional batch API; detect injected SQLite failure; recover by retrying
  the whole unacknowledged batch.
- Force job restarts → previously completed targets repeat. Prevent a durable,
  job-bound force-attempt snapshot; detect a forced multi-batch crash/retry
  fixture; recover by selecting only snapshot-pending targets.
- Source changes during an attempt → a new file is acknowledged with an old
  parse or force snapshot. Prevent stable reads and pre-ack re-hash checks;
  detect a mutation-between-parse-and-ack fixture; recover by leaving that
  path pending and creating a new discovery attempt when force snapshot hashes
  no longer match.

## Verification gauntlet

- **Hard gate — resumability:** fail OMLX on deterministic batch two, rerun,
  and assert calls include only unacknowledged batches. Removing hash
  acknowledgement must make this sensitivity fixture fail.
- **Hard gate — cross-batch graph integrity:** force a caller and its callee
  into different batches; after success, assert the edge is present and both
  hashes are acknowledged.
- **Hard gate — acknowledgement atomicity:** inject a SQLite write failure in
  the batch acknowledgement. Assert zero hashes from that batch changed, then
  retry successfully.
- **Hard gate — sidecar persistence:** inject failure after vector upsert and
  after rename; reopen a new graph handle and require every acknowledged node
  vector to be present before readiness.
- **Hard gate — deletion safety:** force a deletion sidecar flush failure;
  confirm the ingestion record remains, then retry and verify the vector and
  record disappear together.
- **Hard gate — restart safety:** terminate after one acknowledged batch,
  restart with the same store generation, and confirm only later batches call
  the provider while the catalog remains stale until terminal completion.
- **Hard gate — forced retry safety:** start a force-full job, terminate after
  its first acknowledged batch, then retry the same job. Assert the provider
  sees no path from that acknowledged batch; a new force-full job must still
  be permitted to select it.
- **Hard gate — source-snapshot integrity:** mutate a path after it was parsed
  but before acknowledgement. Assert its old hash is not recorded; for a force
  retry, assert snapshot mismatch fails/stales the attempt rather than
  acknowledging the new bytes under the old force job.
- **Integration gate — progress:** observe queued, running acknowledged-batch
  progress, failure, and completion events without blocking the request caller.
- **Regression gates:** targeted Python/Rust tests, `just verify`, and a live
  OMLX multi-batch fixture. Preserve performance ceilings unless comparable
  evidence and a decision revise them.

## Execution checklist

- [ ] Preserve cross-batch graph semantics — files: pipeline/protocol tests;
  verify: forced caller/callee split fixture; done when no valid relationship
  vanishes because of batching.
- [ ] Add transactional batch checkpoints, force-attempt snapshots, and
  two-step deletion — files: native store, PyO3, graph adapter, jobs, tests;
  verify: SQLite/flush/forced-retry fault tests; done when a failed batch
  changes zero hashes and a force retry skips its acknowledged paths.
- [ ] Commit vectors and hashes one bounded batch at a time — files: pipeline,
  provider/vector tests; verify: provider failure/retry and sidecar reopen;
  done when prior acknowledged files are never re-embedded.
- [ ] Publish acknowledged progress and strict stale/ready state — files: jobs,
  runner, daemon, event tests; verify: live event sequence; done when clients
  can distinguish in-progress work from durable acknowledgement.
- [ ] Prove crash/deletion/large-index recovery — files: integration/perf
  tests; verify: termination and deletion fault injection plus measured vector
  bound; done when recovery preserves graph correctness and skips completed
  batches.
