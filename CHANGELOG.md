# Changelog

## Unreleased

## 0.1.5 - 2026-09-05

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
