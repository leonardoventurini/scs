# SCS — Semantic Code System

SCS is a headless semantic code-intelligence service. It owns source parsing,
code indexing, structural graph storage/search, repository watching, LSP reads,
and the agent-facing MCP endpoint.

## Product boundary

- SCS has no UI, webview, dashboard, tray app, or settings screen.
- SCS reads repository source but never mutates it.
- Persistent state lives only under `SCS_HOME`; runtime sockets and identity
  records use the platform runtime directory.
- Per-harness MCP stdio bridges share one lazily spawned SCSWire daemon. Each
  bridge connection is a lease; the final disconnect triggers clean shutdown.
- SCS never reads, imports, copies, or derives state from External product `brain.db` or
  its sidecars. A new installation starts with an empty index.
- External product is an optional client. SCS must build, test, start, index, search, and
  restart with no External product checkout or process available.

## Architecture

- `src/scs/` — typed Python service, indexing orchestration, SCSWire, MCP, LSP,
  providers, CLI, and lifecycle management.
- `crates/` — Rust code-only types, tree-sitter parsers, the SCS-to-TSG storage
  adapter, and `_scs_native` PyO3 bindings.
- `tests/contract/`, `tests/integration/`, `tests/isolation/` — public boundary
  and independence gates.

## Commands

- `just setup` — sync Python dependencies and build the native extension.
- `just test` — run Python tests.
- `just native-test` — run the Rust workspace tests.
- `just typecheck` — strict-check first-party Python sources with Basedpyright.
- `just verify` — run all required checks.

## Invariants

- Python and Rust node/relationship enums must match exactly.
- Every storage, parser, filesystem-heavy, or unbounded PyO3 method releases
  the GIL.
- Long indexing work is durable/background work and reports typed progress.
- A failed parse never records a successful ingestion hash.
- Only one daemon writes an SCS data root.
- Concurrent bridges must converge on one daemon generation without TCP ports
  or platform service managers.
- Unknown SCSWire fields are ignored; unsupported protocol ranges fail with a
  typed compatibility error.
- SCSWire never unlinks a successfully connected live socket. Only a same-UID,
  connection-refused stale socket may be reclaimed.
- MCP exposes only the inventory in `scs.mcp.inventory`.
- No repository-mutating LSP capability or MCP tool is allowed.
- A fresh root stays empty across restart until an explicit index request.

## Development discipline

- Use `uv` for Python dependency changes and `cargo` for Rust dependencies.
- Every behavior change receives tests.
- Run targeted tests first, then `just verify` before committing.
- Preserve `tests/performance/` ceilings unless measured evidence and an
  accepted decision revise them.
- Use semantic commits and path-limited staging. Never commit secrets or local
  runtime data.
