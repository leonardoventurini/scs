# Retire file summarization

## Goal and scope

Remove file summarization as an SCS capability in one rollout. SCS will generate
embeddings directly from parser-owned entity text, expose no summarization
configuration or provider contract, persist no new summary metadata, and offer
no summary-specific query filters or native scan APIs.

This change does not remove the unrelated storage-observability aggregation
named `QueryOperationSummary`. Existing summary keys in user databases remain
inert data: startup will remove the obsolete summarizable-node index, but will
not destructively rewrite user metadata.

## Evidence and uncertainty

- `SCSDaemon.start` always constructs `OpenAIFileSummarizer`, while missing
  `SCS_OPENAI_API_KEY` degrades indexing enrichment rather than embeddings.
- `IngestionPipeline` sends generated summaries into node metadata and prefixes
  entity embedding input with them.
- `knowledge.sample` and the MCP `sample_nodes` tool expose a `summary_status`
  filter.
- Rust retains a summarizable-node partial index, graph scan, PyO3 binding, and
  summary-specific compatibility tests even though the active Python pipeline
  does not call that scan.
- Generic JSON metadata merging and ingestion-record deletion also protect
  parser and future provider metadata, so their behavior remains while their
  obsolete summarizer rationale is removed.
- Main uncertainty: existing databases may contain old summary keys. Automatic
  deletion would destroy user-owned indexed metadata and has no recovery path,
  so retirement makes those keys inert instead of rewriting them.

Stop and revisit scope if removal reveals an active external/public contract
outside the enumerated MCP, Python, and PyO3 surfaces, or if schema cleanup
requires rebuilding user data.

## Contracts and decisions

- `IngestionPipeline` accepts only parser, graph, embedding, and progress ports.
- `IngestionResult` contains no summarization outcome.
- Every embedding document is exactly the parser entity's `embed_text()`; no
  remote enrichment can alter vector input.
- Settings contain no OpenAI or summarizer fields, and production source has no
  OpenAI provider module.
- MCP sampling filters by node type/file/repository only.
- Native storage exposes no summarization-specific scan or cleanup method.
- Existing user metadata is preserved; the obsolete partial SQLite index is
  dropped idempotently at schema initialization.

## Risks and recovery

- Stale API/config surface could imply API-key use. Prevent and detect with a
  repository-wide forbidden-symbol scan and contract tests. Recovery is a
  normal code revert.
- Embedding text could accidentally retain summary prefixes. Detect with an
  integration regression asserting exact provider input.
- Removing the native scan could break compilation or bindings. Detect with
  Rust workspace tests and Python integration tests.
- Removing only future DDL could leave the obsolete index in existing stores.
  Detect with a schema migration test that seeds the old index and confirms
  initialization removes it.
- Automatic metadata deletion could cause irreversible loss. Avoid it; old
  keys remain inert and can be removed only by a separately authorized data
  operation.

## Verification gauntlet

- Hard gate — direct embedding contract: a targeted pipeline integration test
  records provider inputs and requires exact parser entity text. Sensitivity is
  established by first adding the assertion against the current summary-aware
  constructor/flow, where the retired argument or prefix makes the test fail.
- Hard gate — schema retirement: a Rust schema test creates the legacy index,
  initializes current schema, and requires it to be absent.
- Hard gate — surface removal: targeted `rg` queries must find none of the
  retired configuration, provider, pipeline, MCP-filter, or native-scan symbols
  in active product source; the idempotent legacy-index cleanup is exempt.
- Hard gate — repository correctness: targeted Python/Rust tests, strict type
  checking, lint, and `just verify` must pass.
- Hard gate — independent review: a read-only reviewer challenges completeness,
  persistence safety, and test adequacy before final verification.

## Execution checklist

- [x] Add exact embedding-input regression coverage in
  `tests/integration/indexing`; verify with its targeted pytest module.
- [x] Remove Python configuration, provider, pipeline, daemon, result, and MCP
  summarization surfaces; verify with targeted provider/pipeline/service tests.
- [x] Remove Rust scan/binding and obsolete summary-specific tests/comments;
  add idempotent legacy-index cleanup and verify `cargo test -p scs-store` plus
  `cargo test -p scs-python`.
- [x] Record the durable provider-boundary decision and update user-visible
  documentation where the operational contract needs clarification.
- [x] Run the forbidden-symbol audit, independent review, `just verify`, final
  diff/status review, and create a path-limited semantic commit.

## Verification and rollout

The code rollout is direct: a daemon restart loads the reduced configuration
and idempotently drops the unused SQLite index. No user source or vector data is
rewritten. Rollback is the task commit's revert; the old partial index is
recreated automatically by the prior idempotent schema initializer if needed.
