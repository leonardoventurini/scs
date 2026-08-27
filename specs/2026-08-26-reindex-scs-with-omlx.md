# Reindex the SCS project with local OMLX embeddings

## Goal and scope

Run one explicit, durable full reindex of
`/Users/leonardo/Repositories/mentagen/scs` after the embedding provider was
changed to the local OMLX OpenAI-compatible endpoint. The operation may change
only SCS-owned state under `SCS_HOME`; it must not mutate the repository or
drop another repository's index.

## Evidence and uncertainty

- Project: `scs`
- Project root: `/Users/leonardo/Repositories/mentagen/scs`
- Risk tier: medium. A reindex rewrites persistent graph/vector state across
  the daemon boundary, although it is limited to one SCS-owned repository.
- `system.health` reports the daemon ready and `provider.json` identifies the
  local OMLX Qwen embedding model with dimension 4096.
- Before this operation, this repository has indexed graph nodes but zero
  embeddings because its preceding jobs used the unavailable MLX provider.
- Main uncertainty: the OMLX-backed batch may fail after structural indexing.

## Contracts and decisions

- Invoke `repository.reindex` only through `scs reindex` with the canonical
  absolute repository root. It creates a `force_full` job with
  `explicit_reindex` provenance.
- The daemon alone owns queued work. The caller enqueues and observes; it does
  not write SCS SQLite or vector files directly.
- Success requires this exact job to reach `completed`, the repository to
  report indexed data with positive embeddings, and a semantic search to use
  semantic retrieval.
- Source immutability is a hard invariant. A clean Git diff before and after
  is the observable guard.

## Risks and recovery

- OMLX/provider failure can leave partial semantic state. Preserve the durable
  job error and stop; do not retry blindly or delete state.
- A wrong path could rebuild another repository. Prevent this by verifying the
  accepted job's canonical `repo_path` and job mode before polling.
- A source mutation would breach SCS's product boundary. Stop and report any
  non-clean Git status; no repository recovery action is authorized here.
- The reindex is recoverable through a later explicit reindex after diagnosing
  the provider; source rollback is inapplicable because the operation is
  read-only to the project.

## Verification gauntlet

- **Hard gate — durable request:** `uv run --all-groups scs reindex <root>`
  returns an accepted `force_full` job for the exact root.
- **Hard gate — completion:** `jobs.recent` reports that exact job as
  `completed` with no error.
- **Hard gate — semantic state:** `knowledge.stats` reports positive
  project-scoped embeddings and `knowledge.search` returns `semantic` mode.
- **Hard gate — source boundary:** `git diff --exit-code` and
  `git status --short` remain clean after completion.

## Execution checklist

- [x] Confirm a clean worktree and ready daemon before queuing — files:
  SCS-owned runtime and storage; verify: `scs doctor`; done when the canonical
  root is clean and the daemon reports ready.
- [x] Queue and observe the exact OMLX reindex job — files:
  `~/.scs/jobs.db`, `~/.scs/index.db`, and vector sidecar; verify:
  `scs reindex` plus `jobs.recent`; done when `ingest_b0b6c1af7397` completed
  with 2,396 embeddings and no error.
- [x] Verify semantic retrieval and source immutability — files: SCS-owned
  state and project worktree; verify: `knowledge.stats`, `knowledge.search`,
  and Git status; done when stats report 2,396 embeddings, search is semantic,
  and Git is clean.

## Verification and rollout

The daemon completed `ingest_b0b6c1af7397` on 2026-08-27T02:12:08Z. Its
durable result reports 112 discovered and changed files, 2,396 created entities
and embeddings, 2,077 created edges, zero failed files, and no semantic
degradation. Project-scoped graph stats report 2,396 nodes and embeddings with
semantic search ready; the vector-index count remains correctly marked as
global. A semantic query returned results from the OMLX provider implementation.
`git diff --exit-code` passed and `git status --short` showed only this newly
created operation record.
