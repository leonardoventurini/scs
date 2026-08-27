# Index the local SCS project

## Goal and scope

Queue and complete one explicit full SCS indexing pass for the repository at
`/Users/leonardo/Repositories/mentagen/scs`. The daemon performs the work in
its durable background queue. This rollout changes only SCS-owned SQLite and
vector index state; it does not alter repository files, launchd registration,
or another repository's indexed data.

## Evidence and uncertainty

- Project: `scs`
- Project root: `/Users/leonardo/Repositories/mentagen/scs`
- The SCS MCP `ingest_project` tool calls `repository.index`, which enqueues a
  full job; `jobs.recent` and `repositories.status` expose its durable state.
- The currently running daemon is healthy and its graph has zero indexed nodes
  for this project.
- Risk tier: medium because indexing creates persistent local data and runs
  asynchronously. The main uncertainty is parser or embedding failure during
  the full source walk.

## Contracts and decisions

- Use `ingest_project` with the canonical project root; do not reindex or
  issue deletion/drop-index operations.
- Success requires the returned job to reach `completed`, repository status to
  be `indexed`, and project-scoped graph statistics to show indexed data.
- Source immutability is load-bearing: a before/after clean Git worktree is
  the narrow observable guard for tracked and untracked repository mutations.
- If the job fails, leave its error evidence intact and report it; do not retry
  blindly or alter source. A user-requested recovery can enqueue a reindex or
  use the separately exposed drop-index operation.

## Risks and recovery

- Parse/provider failure → incomplete index. Detect a failed terminal job and
  error field through `jobs.recent`; preserve evidence and stop.
- Repository mutation → source integrity breach. Detect `git status --short`
  and `git diff --exit-code`; stop and report before taking any recovery action.
- Wrong target → unrelated index changes. Prevent by passing the absolute
  project root and checking returned `repo_path` exactly.

## Verification gauntlet

- **Hard gate — correct durable request:** `ingest_project` returns an accepted
  full job with the exact canonical project root.
- **Hard gate — terminal success:** poll `jobs.recent` for the exact job ID
  until status is `completed`; a failed/cancelled job blocks completion.
- **Hard gate — useful indexed state:** `repositories.status` reports
  `indexed` and project-scoped `get_graph_stats` reports a positive node count.
- **Hard gate — source boundary:** after completion, `git diff --exit-code`
  passes and `git status --short` remains empty.

## Execution checklist

- [x] Capture clean-source preflight and enqueue the full job — files:
  SCS-owned `~/.scs/{jobs.db,index.db}`; verify: `ingest_project`; done when
  accepted output names the exact project root.
- [x] Observe the exact background job to a completed terminal state — files:
  `~/.scs/jobs.db`; verify: SCSWire `jobs.recent`; done when that job's status
  is `completed` with no error.
- [x] Confirm indexed content and preserved source — files: SCS-owned
  `~/.scs/index.db`, project worktree; verify: `repositories.status`,
  project-scoped graph stats, `git diff --exit-code`, `git status --short`;
  done when stats are nonempty and source is unchanged.

## Verification and rollout

Queue the operation once and monitor durable progress. The service owns all
long-running work. On failure, retain the job record and service logs; no
automatic retry or destructive cleanup is authorized in this rollout.
