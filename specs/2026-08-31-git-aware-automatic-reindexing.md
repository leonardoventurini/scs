# Git-aware automatic reindexing

## Problem

SCS restores filesystem watchers for enrolled repositories, but watcher events
can be missed while the daemon is stopped and the current watcher filters out
non-parser text files. Users need every enrolled project to converge
automatically after commits, branch switches, staging, unstaged edits,
deletions, and non-ignored untracked files.

## Evidence

- `SCSDaemon.start()` restores one `RepositoryWatcher` per active catalog record.
- `RepositoryWatcher` depends on live `watchfiles` events and explicitly avoids
  startup reconciliation.
- The durable job store already merges queued work per repository.
- A `full` ingestion job performs discovery but hashes and parses only changed
  files, so it is a safe incremental reconciliation primitive.
- The job runner currently drains one job at a time, providing a hard indexing
  concurrency bound.
- Live rollout sampling showed full reconciliation spending nearly all CPU in
  repeated retained-symbol resolution. The lookup filtered a JSON qualified
  name without an index, making large structural plans effectively quadratic.

## Uncertainty

- Polling every repository incurs Git subprocess overhead. Adaptive backoff
  bounds idle cost but allows up to 30 seconds of latency for long-idle roots.
- Git status can fail transiently during repository maintenance or if a root
  disappears. Such failures must not terminate the daemon or erase the last
  successful observation.
- Very large untracked working trees may make `git status` expensive despite
  ignore rules and ingestion pruning.

## Contracts

1. Every active, existing catalog repository receives one Git-state poller.
2. Git-visible state includes `HEAD`, staged changes, tracked working-tree
   changes, deletions, and non-ignored untracked files.
3. Pollers enqueue durable `full` reconciliation jobs; the ingestion pipeline
   remains responsible for hash-level incremental selection and deletion sweep.
4. Startup enqueues reconciliation before establishing the observation baseline,
   recovering changes made while SCS was stopped.
5. Repeated identical Git state does not enqueue duplicate work.
6. A changed observation is debounced for 500 ms and coalesced by the durable
   per-repository queue.
7. Each poller starts at a 2-second cadence, backs off unchanged repositories
   exponentially to at most 30 seconds, and resets to 2 seconds after change.
8. Git command failures back off without changing the successful baseline and
   recover automatically.
9. Dropping a project or stopping the daemon stops its poller.
10. Poll cadence, maximum backoff, debounce, and Git-command timeout have typed
    conservative configuration defaults.
11. Indexing concurrency remains bounded by the single durable job runner.
12. Retained-symbol lookup uses a forward-migrated repository/qualified-name
    expression index while preserving a legacy metadata fallback.

## Test strategy and acceptance criteria

- Unit-test Git fingerprints for committed, staged, unstaged, deleted, and
  non-ignored untracked changes while ignored files do not alter state.
- Unit-test startup reconciliation, unchanged-state suppression, debounce, and
  adaptive backoff/reset behavior with injected Git observations and sleeps.
- Unit-test transient Git failure recovery and clean shutdown.
- Unit-test typed settings defaults, environment overrides, and interval
  invariants.
- Integration-test daemon restoration for all active catalog projects and
  poller removal during project drop/daemon stop.
- Run targeted tests, strict type checking, `just verify`, then rebuild and
  restart the installed services.

## Risks

- Automatic full reconciliation may enqueue while explicit indexing is active.
  Existing job merging and the single runner bound prevent parallel writes.
- A repository that changes continuously may remain at the active interval and
  consume sustained Git/embedding resources.
- An ignored-file rule change affects Git status visibility; the status change
  triggers full discovery so stale indexed nodes are swept.
- Deployment restart interrupts current requests; launchd and durable jobs
  provide recovery.

## Recovery

- Disable automatic polling through configuration and restart the daemon.
- Increase active/idle intervals to reduce overhead.
- Revert the implementation and restart services; explicit indexing continues
  to work and durable project data remains intact.

## Direct rollout

Enable Git polling by default. On the first daemon start after deployment,
enqueue one incremental full reconciliation for every active, existing project.
After verification, explicitly reindex enrolled projects once so the preceding
text-fallback feature is reflected immediately rather than waiting for a Git
state change.

## Executable checklist

- [ ] Add failing configuration and Git-state poller tests.
- [ ] Implement typed Git fingerprint collection.
- [ ] Implement startup reconciliation, debounce, and adaptive backoff.
- [ ] Restore pollers for every active catalog project.
- [ ] Stop pollers on drop and daemon shutdown.
- [ ] Document operation and environment controls.
- [ ] Record the architectural decision.
- [ ] Run targeted and full verification.
- [ ] Add and verify the retained-symbol lookup index migration.
- [ ] Rebuild, restart, and health-check daemon and proxy.
- [ ] Reindex all enrolled projects and verify successful completion.

## Verification

The feature is complete when tests prove all selected Git-visible transitions
trigger reconciliation, identical observations remain quiet, every enrolled
project has an active poller after startup, `just verify` passes, installed
services report healthy new generations, and all enrolled projects complete a
post-deployment reindex.
