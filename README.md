# SCS

SCS is the headless Semantic Code System extracted from External product. It indexes
source repositories into a structural and vector-backed code graph and exposes
that intelligence through a local control socket and MCP.

SCS starts with an empty index. It does not migrate, inspect, or recreate any
legacy External product graph data. Repositories are added only through an explicit CLI,
MCP, or client request.

Semantic embeddings are generated locally from parser-owned entity text. SCS
does not call a remote file-summarization service and requires no API key for
indexing or search.

The service has no graphical interface. Use `scs status`, `scs doctor`, logs,
SCSWire, or MCP index statistics for operational visibility.

## MCP tools

SCS exposes ten model-facing operations with distinct code-intelligence jobs:
`search_code`, `graph_context`, `get_related`, `list_symbols`, `inspect_file`,
`find_references`, `regression_risk_report`, `ingest_project`, `ingest_files`,
and `get_graph_stats`. Repository-query tools are annotated read-only and
closed-world. Ingestion tools are marked destructive because reconciliation can
remove stale SCS-owned index state; SCS never mutates repository source.

Operational diagnostics remain available through the CLI and SCSWire instead
of occupying the model's tool catalog.

## Runtime ownership

- `com.mentagen.scs.proxy` owns public MCP at `127.0.0.1:28463`, `mcp.json`,
  and `proxy-service.json`.
- `com.mentagen.scs.daemon` owns private MCP at `127.0.0.1:28465`, `scs.sock`,
  and `daemon-service.json`.

Runtime artifacts live under `~/Library/Application Support/SCS/`. Each
service record contains its PID, start time, generation, artifact digest, and
protocol range. Atomic publication and generation-checked cleanup let either
process restart without deleting the survivor's artifacts. Persistent indexes
live only under `SCS_HOME`; logs default to `~/Library/Logs/SCS/`.

## Development

```bash
just setup
just verify
```

`just setup` installs Python dependencies, builds the private `_scs_native`
extension, and installs the repository's pre-commit hook. The daemon can then
be run directly with `scs serve`, while explicit repository enrollment uses
`scs index <repo>` or `scs reindex <repo>`.

Install and operate the independent user services with:

```bash
scs service install
scs service start
scs service status
scs service restart
scs service stop
scs service uninstall
```

`scs status` and `scs doctor` always emit JSON. A stopped daemon produces an
explicit `daemon.available: false` payload and a nonzero exit code, while
`scs status` still reports launchd registration state.

Uninstall removes only service registrations and runtime ownership. It
preserves `SCS_HOME` and every SCS-owned index. SCS never reads External product's legacy
index and never enrolls repositories merely because External product knows about them.

## Verification

`just verify` runs strict Basedpyright checks, Ruff, all Python tests with
branch coverage, and the Rust workspace. `just coverage` reports uncovered
Python lines and enforces the committed risk-based floor. The pre-commit hook
runs the same whole-source type gate.
`cd proxy && uv run --all-groups pytest -v` verifies the separately packaged
public proxy. Isolation gates cover exact MCP inventory, bounded frames,
generation-safe cleanup, stale/live socket ownership, legacy sentinel
preservation, External product-import denial, repository source fingerprints, and
committed RSS/index/query budgets.
