# Evaluate live SCS MCP tool contracts

## Goal and scope

Exercise every publicly exposed SCS MCP tool against the indexed SCS checkout,
record observed behavior and validation boundaries, and publish an evidence-led
assessment of defects and improvements. This is an operational evaluation, not
an implementation rollout: no product source, tool contract, or service
configuration will be changed.

## Evidence and uncertainty

- Project: `scs`
- Project root: `/Users/leonardo/Repositories/mentagen/scs`
- SCS exposes ten tools: search, graph context, related traversal, symbol
  listing, file inspection, reference lookup, regression-risk reporting, full
  and incremental ingestion, and graph statistics.
- The current project full-ingestion job completed successfully, supplying a
  realistic indexed corpus. Embeddings are degraded because the optional MLX
  provider module is absent, so semantic-vector quality cannot be evaluated in
  this environment.
- Risk tier: medium because the two ingestion tools mutate SCS-owned durable
  index state. Main uncertainty: representative results can be correct while
  invalid input, empty results, or async completion semantics remain confusing.

## Contracts and decisions

- Every read-only tool receives at least one live success-path request; where
  input validation is load-bearing, it also receives a safe negative request.
- `ingest_project` and `ingest_files` are tested only through their intended
  background acknowledgement and durable terminal-job observations. They must
  not be awaited as if they synchronously indexed source.
- Incremental ingestion uses an existing indexed project file and causes no
  repository source edit; full ingestion is not repeated unless needed to test
  the public tool acknowledgement.
- Source invariance is checked before and after all tool calls. Findings are
  classified as observed defects, environment limitations, or proposals.

## Risks and recovery

- A mutating tool unexpectedly affects source → detect clean Git worktree;
  stop and report before recovery.
- A background job fails → preserve job error and runtime logs; do not retry
  blindly.
- A misleading diagnosis from sparse corpus results → record the query, scope,
  and environment limitation, and avoid calling absence of a result a defect.

## Verification gauntlet

- **Hard gate — exact inventory coverage:** one recorded outcome for all ten
  inventory tools, including both ingestion operations.
- **Hard gate — async truthfulness:** every queued job is matched by ID through
  SCSWire `jobs.recent` to a terminal state.
- **Hard gate — repository boundary:** `git diff --exit-code` and a clean
  `git status --short` after the live evaluation.
- **Hard gate — reproducible report:** findings document identifies method,
  inputs, outcomes, limitations, priority, and an actionable improvement or
  explicit rationale for no action.

## Execution checklist

- [x] Capture preflight graph/source state and build the per-tool test matrix —
  files: indexed SCS data, Git worktree; verify: scoped graph stats and Git
  cleanliness; done when representative symbols/files are selected.
- [x] Exercise all eight read-only MCP tools and their safe validation edges —
  files: none; verify: recorded MCP responses; done when each tool has an
  observed success outcome and needed negative boundary.
- [x] Exercise the two background ingestion tools and observe exact job IDs —
  files: `~/.scs/{jobs.db,index.db}`; verify: SCSWire `jobs.recent`; done when
  both jobs are terminal and source remains unchanged.
- [x] Publish and review the findings — files:
  `specs/2026-08-26-mcp-tool-evaluation.md`; verify: evidence/priority review,
  `git diff --check`; done when all recommendations are traceable to observed
  results.

## Verification and rollout

The evaluation is complete only after every tool has evidence and all async
jobs finish. A failed test is reported as evidence, not repaired in this task.
No rollback is required for normal ingestion because the user has explicitly
authorized SCS-owned index updates; the separate drop-index operation remains
available for explicit cleanup.
