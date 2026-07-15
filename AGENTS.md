# SCS — Semantic Code System

SCS is a headless semantic code-intelligence service. It owns source parsing,
code indexing, structural graph storage/search, repository watching, LSP reads,
and the agent-facing MCP endpoint.

## Product boundary

- SCS has no UI, webview, dashboard, tray app, or settings screen.
- SCS reads repository source but never mutates it.
- Persistent state lives only under `SCS_HOME`; runtime sockets and discovery
  live under `~/Library/Application Support/SCS`.
- SCS never reads, imports, copies, or derives state from External product `brain.db` or
  its sidecars. A new installation starts with an empty index.
- External product is an optional client. SCS must build, test, start, index, search, and
  restart with no External product checkout or process available.

## Architecture

- `src/scs/` — typed Python service, indexing orchestration, SCSWire, MCP, LSP,
  providers, CLI, and lifecycle management.
- `crates/` — Rust code-only types, tree-sitter parsers, SQLite/USearch store,
  and `_scs_native` PyO3 bindings.
- `proxy/` — always-on public MCP proxy owned by SCS.
- `tests/contract/`, `tests/integration/`, `tests/isolation/` — public boundary
  and independence gates.

## Commands

- `just setup` — sync Python dependencies and build the native extension.
- `just test` — run Python tests.
- `just native-test` — run the Rust workspace tests.
- `just typecheck` — compile-check typed Python sources.
- `just verify` — run all required checks.

## Invariants

- Python and Rust node/relationship enums must match exactly.
- Every storage, parser, filesystem-heavy, or unbounded PyO3 method releases
  the GIL.
- Long indexing work is durable/background work and reports typed progress.
- A failed parse never records a successful ingestion hash.
- Only one daemon writes an SCS data root.
- Unknown SCSWire fields are ignored; unsupported protocol ranges fail with a
  typed compatibility error.
- MCP exposes only the inventory in `scs.mcp.inventory`.
- No repository-mutating LSP capability or MCP tool is allowed.

## Development discipline

- Use `uv` for Python dependency changes and `cargo` for Rust dependencies.
- Every behavior change receives tests.
- Run targeted tests first, then `just verify` before committing.
- Use semantic commits and path-limited staging. Never commit secrets or local
  runtime data.

