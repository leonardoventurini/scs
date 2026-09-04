# Changelog

## Unreleased

- feat: route all SCS graph and embedding persistence through TSG 0.2
- feat: replace launchd and TCP MCP services with lazy per-harness stdio bridges
- feat: package SCS as one native wheel with a verified cross-platform installer
- ci: add stable GitHub Releases with checksums, SBOMs, and attestations
- security: upgrade PyO3 to 0.29.2 and refresh yanked WebAssembly transitive locks
- ci: install the pinned task runner and enforce a clean RustSec audit

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
- test: enforce empty-index, External product independence, source read-only, bounded
  transport, restart ownership, RSS, indexing, and query convergence gates
