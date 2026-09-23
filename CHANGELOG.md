# Changelog

## Unreleased

- fix(cli): list saved project indexes without starting reconciliation

## 0.1.22 - 2026-09-22

- fix(indexing): bound semantic signatures and embedding requests while
  preserving actionable provider error details

## 0.1.21 - 2026-09-22

- feat(cli): list, delete, and fully reingest projects through stable numeric
  catalog IDs or repository paths

## 0.1.20 - 2026-09-22

- feat(providers): replace product-specific local model-server settings with a
  single OpenAI-compatible embedding and reranking transport

## 0.1.19 - 2026-09-21

- feat(parser): add native Go ingestion for packages, imports, types,
  interfaces, embedding, fields, functions, methods, values, calls,
  documentation, generics, and cyclomatic complexity
- build: bound local Cargo artifacts by disabling incremental caches and
  retaining only line-table debug information

## 0.1.16 - 2026-09-15

- fix(search): accept omitted or null optional query lists at the public service
  boundary, so default MCP search calls succeed while invalid list values remain
  rejected

## 0.1.15 - 2026-09-14

- feat(search): add bounded multi-query retrieval, explicit search modes,
  per-result query evidence, stage timings, and truthful enrichment degradation
- feat(readiness): expose structural and semantic readiness, redacted scoped
  job progress, retry guidance, and bounded read-only job observation
- feat(risk): batch dependent hydration and return bounded completeness,
  timings, and direct evidence for deduplicated test targets
- feat(metrics): persist content-free daemon operation aggregates with HMAC
  repository identities, owner-only storage, retention bounds, corruption
  recovery, fail-open behavior, and a machine-readable CLI report
- docs: add a reusable SCS-first workflow skill and evidence-backed analysis of
  recent Codex productivity patterns

## 0.1.14 - 2026-09-09

- feat(mcp): durably forget indexed repositories through an idempotent deletion
  operation

## 0.1.13 - 2026-09-08

- fix: allow upgrades to proceed after an exited daemon remains as a zombie
  child of its MCP bridge

## 0.1.12 - 2026-09-08

- fix: resolve reference lookups through lazily opened project stores
- test: cover available and unavailable reference results through SCSWire and
  FastMCP

## 0.1.11 - 2026-09-08

- fix: keep the SCSWire control plane reachable while recovered durable work is
  active and move native graph opening off the event loop
- fix: make upgrades cancel indexing cooperatively, wait for writer-lock
  release, and abort before replacing an installation when shutdown fails
- perf: delete stale file batches with one native vector-accelerator rebuild

## 0.1.10 - 2026-09-08

- feat: fuse semantic and lexical code search with deterministic reciprocal
  rank fusion and optional fail-open oMLX reranking
- feat: add opt-in compact search results and a versioned relevance, payload,
  and latency evaluation suite
- refactor: activate reranking only through an explicitly configured model,
  with no model selected for new installations

## 0.1.9 - 2026-09-05

- fix: allow explicitly trusted OMLX hosts while keeping loopback-only defaults

## 0.1.8 - 2026-09-05

- Consume TSG 0.2.3, which updates changed vectors in the existing search index
  instead of rebuilding the full corpus after every embedding or metadata batch.
- Preserve committed vectors, generation checks, sidecar persistence, and recovery
  when an accelerator update fails; no model or stored-format change is required.

## 0.1.7 - 2026-09-05

- fix: preserve indexed source alias identities through MCP inspection,
  incremental ingestion, regression risk, and reference lookup validation

## 0.1.6 - 2026-09-05

- fix: retain distinct in-repository symlink paths through ordinary, forced, and
  incremental ingestion while consistently rejecting external or excluded targets
- fix: report the cumulative number of embeddings created across ingestion batches

## 0.1.5 - 2026-09-05

- perf: delete replaced ingestion nodes in one native transaction and accelerator rebuild

- test: wait for the requested isolation-test job with a bounded completion deadline

- fix: use indexed qualified-name relationship lookups through the upstream TSG
  attribute-query fix, avoiding repeated full graph scans during ingestion

## 0.1.4 - 2026-09-05

- fix: retain repeated parsed entity occurrences with unique deterministic IDs
  and associate each occurrence with its own embedding
- test: add generated anonymized fixtures for real-world identity collisions
- test: accept Linux reset semantics when closing incomplete socket frames

- fix: detect repeated edits to already-dirty files during automatic reindexing
- fix: close idle daemon clients on shutdown while draining active requests
- fix: preserve sockets when ownership probing fails without connection refusal
- fix: resume force indexing from job acknowledgements instead of old source hashes
- deps: upgrade TSG to v0.2.1 for adaptive-search fallback, scope integrity, and
  literal name-search fixes

## 0.1.3 - 2026-09-04

- refactor: remove foreign-product provenance and compatibility assumptions
- docs: describe SCS exclusively through standalone product contracts

## 0.1.2 - 2026-09-04

- fix: keep the daemon observable until durable background jobs are terminal

## 0.1.1 - 2026-09-04

- feat: route all SCS graph and embedding persistence through TSG 0.2
- feat: replace launchd and TCP MCP services with lazy per-harness stdio bridges
- feat: package SCS as one native wheel with a verified cross-platform installer
- ci: add stable GitHub Releases with checksums, SBOMs, and attestations
- security: upgrade PyO3 to 0.29.2 and refresh yanked WebAssembly transitive locks
- fix: migrate the MCP host and public contracts to MCP SDK 2.1.1
- ci: install the pinned task runner and enforce a clean RustSec audit

The `v0.1.0` tag failed its clean-install release gate and did not publish a
GitHub Release. Per the immutable-tag policy, `v0.1.1` is the first published
SCS release.

## 2026-08-27

- feat: use local OMLX for semantic code embeddings

## 2026-08-26

- fix: harden MCP graph and reference contracts

## 2026-08-05

- fix: preserve graph state when rejecting invalid embedding dimensions
- ci: enforce strict Python types before commit
- remove: reduce MCP surface to essential code intelligence tools
- remove: retire file summarization

## 2026-07-15

- feat: establish independent SCS service contracts
- feat: isolate the native parser and code-only graph store
- feat: add independent providers, durable indexing, watchers, and code search
- feat: add bounded SCSWire framing, routing, events, and Unix socket transport
- feat: add the standalone daemon, explicit indexing CLI, and launchd lifecycle
- feat(mcp): serve SCS code intelligence independently
- feat: publish generation-scoped proxy and daemon ownership records
- fix: preserve live foreign sockets during stale-owner detection
- fix: emit machine-readable unavailable status when the daemon is stopped
- test: enforce empty-index, runtime isolation, source read-only, bounded
  transport, restart ownership, RSS, indexing, and query convergence gates
