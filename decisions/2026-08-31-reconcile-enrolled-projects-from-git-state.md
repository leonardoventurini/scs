# Reconcile enrolled projects from Git-visible state

## Context

Filesystem notifications provided low-latency incremental indexing but could
not recover events missed while SCS was stopped. They also required watching
each repository recursively and duplicated file-eligibility filtering that now
belongs to discovery. Every project already has durable hash checkpoints and a
full ingestion mode that parses only changed content.

## Decision

Replace recursive filesystem watchers with one adaptive Git-state poller per
active enrolled project. A fingerprint combines `HEAD` with
`git status --porcelain=v1 -z --untracked-files=all`, so it changes for commits,
branch switches, staged or unstaged modifications, deletions, renames, and
non-ignored untracked files.

Daemon startup always queues a full reconciliation before establishing the
polling baseline. Later fingerprint changes queue the same full reconciliation
after a short debounce. Full discovery is the correctness boundary; stored
content hashes keep parsing and embedding incremental, and the deletion sweep
removes files hidden by changed ignore rules.

Pollers begin at two seconds, exponentially back off unchanged repositories to
thirty seconds, and reset after change. Git failures retain the previous
successful baseline and back off. The durable queue merges pending work per
repository, while its single runner bounds indexing concurrency.

## Rejected alternatives

- Filesystem events alone cannot repair changes made during daemon downtime.
- Fixed frequent polling spends steady resources on idle projects.
- Committed-`HEAD` polling omits the working-tree edits agents need indexed.
- Path-level Git diff orchestration adds separate correctness rules for branch
  switches, ignore changes, renames, and missed baselines when full discovery
  already provides hash-incremental reconciliation.

## Rationale

Git owns repository visibility and ignore semantics. Polling its stable
machine-readable state provides reliable recovery without maintaining another
recursive filesystem event dependency. Reusing durable full reconciliation
keeps a single authority for eligibility, hashes, and deletions.

## Consequences

- Enrolled projects converge automatically after restarts and working-tree
  changes.
- Long-idle projects can take up to thirty seconds to notice a new change.
- Every daemon restart queues one discovery pass per active project.
- Repositories with very large untracked working trees may make Git status
  expensive; ignore rules and configurable polling intervals are the controls.
- The `watchfiles` runtime dependency is removed.
