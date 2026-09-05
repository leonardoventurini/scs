# Review fixes and TSG patch upgrade

## Problem and evidence

Review reproduced three defects: repeated changes to an already dirty file leave
the Git-status fingerprint unchanged; idle clients prevent shutdown from reaching
ownership release; and force-job recovery accepts hashes from a prior index as
proof that the current job ran. TSG review found writable adaptive-search failure
after sidecar failure, scope-changing upserts invalidating retained edges, and
incorrect literal-backslash name search. TSG fixes were committed and tagged
v0.2.1 before beginning SCS changes.

## Contracts, scope, and uncertainty

Upgrade the existing TSG production dependency to published v0.2.1. Preserve MCP
inventory, public signatures, persisted job layout, schema formats, and repository
read-only behavior. Restore watcher, shutdown, and force-retry correctness. Also
cover conservative socket ownership: only connection refusal proves staleness.
This is a focused correctness review; it does not certify all concurrency or
capacity behavior.

## Test strategy and acceptance

- Generate Git repositories and reproduce successive edits with unchanged status;
  cover staged/untracked files, rename records, and unusual filenames.
- Real Unix sockets must shut down with idle/partial-frame/attached clients while
  allowing a dispatched request to finish. Timeout and permission failures during
  ownership probing must preserve the existing socket.
- Run real pipeline + durable queue regressions with deterministic provider/storage
  fixtures: crash before force execution must rebuild already-indexed source;
  provider retries must skip only job-acknowledged batches; a crash before the queue
  acknowledgement may replay its unacknowledged batch.
- Verify the Rust adapter against the new TSG tag and rebuild the Python extension.
- Run targeted tests first and `just verify` before committing task-owned paths.

## Design, risks, and recovery

Augment Git status with bounded dirty-path metadata (including nanosecond change
and modification times), preserving Git ignore semantics. Avoid following symlink
targets. Metadata polling is not a filesystem event journal; source hashes remain
the indexing authority.

Close idle client connections on shutdown and drain active requests. Python's
[server lifecycle documentation](https://docs.python.org/3/library/asyncio-eventloop.html)
specifies that `wait_closed` also waits for active connections.

Use only durable acknowledgements in the existing job manifest for force-job
completion. Always force still-pending snapshot files, even if old hashes match.
This deliberately replaces ambiguous hash-based recovery: replaying a batch after
its native commit but before its queue acknowledgement is safe and requires no
new persisted job fields. Already-acknowledged batches are not repeated.

Rollback by reverting this commit and restoring the prior dependency pin. No data
migration is required. Already skipped completed force jobs require a new explicit
force request; these fixes do not rewrite old terminal jobs or live user data.

## Executable checklist and direct rollout

- [x] Confirm TSG v0.2.1 release and published archive/checksum.
- [x] Implement failing regression tests before each fix.
- [x] Upgrade TSG and rebuild the native extension.
- [x] Implement and verify watcher, shutdown/socket ownership, and force recovery.
- [x] Update documentation and decision record.
- [x] Run `just verify` and inspect the final diff.
- [x] Commit with hooks enabled (`44c1576`; strict Python type hook passed).

SCS release publication is not part of this requested rollout; deliver committed
SCS fixes after the upstream release and dependency upgrade.

## Executed verification

- TSG release and both CI runs succeeded for `6aa395c246ab0342342662387dfdffe7dbea0be3`.
  Downloaded `tsg-0.2.1.crate` matched SHA256SUMS:
  `cdfb0c261ad9b0fba862f4c1e8bcd50325f984c8e8f261a34127c40646e2a0ad`.
- Before implementation, 13 watcher cases, 9 shutdown/ownership cases, and all
  3 real-pipeline force recovery regressions failed. After fixes, all 58 targeted
  tests passed. Force tests use deterministic fake graph/provider implementations
  with the real pipeline and durable SQLite job queue.
- `just verify` passed: strict Basedpyright, Ruff, native extension rebuild,
  all 236 Python tests, coverage 84.50% against an 83% minimum, and Rust workspace
  tests including the TSG adapter, parser, and core contracts. Performance ceilings
  were preserved and passed. No public signatures or persisted formats changed.
- TSG's opt-in million-vector capacity harness was not run. cargo-deny emitted
  existing duplicate-transitive-version and unmatched-license-allowance warnings.
  The successful GitHub release run recorded nonblocking cache-cleanup ENOENT
  annotations for absent test directories; artifact build and publication passed.

Recommended review order: force runner and integration regressions; watcher and
Git fixtures; wire shutdown and real-socket tests; dependency lock and docs.

## Local installation follow-up

At the user's request, built an optimized locked wheel from source commit
`8fef52df2771e2508d64a947144ad8712cdccce9` with TSG v0.2.1 and installed it into
the global `uv` tool environment. Python dependencies were constrained to the
existing `uv.lock`. This local patched build retains package version 0.1.3; no
new SCS release was published. Artifact:
`dist/local-8fef52d/scs-0.1.3-cp314-cp314-macosx_11_0_arm64.whl`, SHA-256
`530490d35aa7e6154d65848ede4284d8b1c57b10911b9e38ffd7783b8c26e3bc`.

Compared installed watcher, force runner, and wire server source against the
repository. An isolated smoke test using the globally installed native parser,
TSG store, pipeline, and name search passed.

Restart required terminating old bridge processes and force-stopping the old
daemon after graceful shutdown remained inside a costly native vector rebuild.
A stack sample identified full accelerator reconstruction on individual node
deletions. A first replacement reached readiness but hit its unattached startup
grace while processing recovered work; it was restarted with an immediate
temporary client lease. These observations identify follow-up lifecycle and
indexing-performance issues, not additional fixes in this installation task.

Final verified daemon PID: 51909; generation:
`39ea30cd1c6d4e18b527ff34d7ff57d8`. Health and readiness passed from the installed
environment. The interrupted job was marked reclaimed into a queued follow-up,
and indexing resumed; full queue completion was not awaited. No index files were
deleted. Existing MCP sessions require reconnection after bridge termination.
Rollback remains reinstalling the published 0.1.3 wheel and restarting; retained
runtime data does not require migration.
