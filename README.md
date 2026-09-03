# SCS

SCS is the headless Semantic Code System extracted from External product. It indexes
source repositories into a structural and vector-backed code graph and exposes
that intelligence through a local control socket and MCP.

SCS starts with an empty index. It does not migrate, inspect, or recreate any
legacy External product graph data. Repositories are added only through an explicit CLI,
MCP, or client request.

Semantic embeddings are generated from parser-owned entity text. SCS defaults
to the OpenAI embeddings API, while local OMLX and in-process MLX providers are
available by explicit configuration. SCS does not send repository files to a
summarization service; an embedding provider receives only the entity text used
to build the semantic index.

## Embedding configuration

Persistent configuration lives at `~/.scs/config.toml`. Explicit Python
settings take precedence over environment variables, environment variables
take precedence over TOML, and TOML takes precedence over defaults. The
standard `OPENAI_API_KEY` environment variable overrides `openai_api_key` in
the file. For a background launchd service, storing the key in the owner-only
configuration file is more reliable than relying on an interactive shell
environment.

The unconfigured default is:

```toml
embedding_provider = "openai"
embedding_model = "text-embedding-3-small"
embedding_dimension = 1536
openai_base_url = "https://api.openai.com/v1"
openai_api_key = "replace-with-your-key"
```

To use OMLX without an API key:

```toml
embedding_provider = "omlx"
embedding_model = "Qwen3-Embedding-8B-4bit-DWQ"
embedding_dimension = 4096
omlx_base_url = "http://127.0.0.1:10000/v1"
```

OMLX endpoints must be loopback HTTP URLs. In OMLX mode, SCS ignores OpenAI
credentials and sends no authorization header. Changing the provider, model,
or dimension quarantines incompatible vectors; the next indexing pass
regenerates embeddings while preserving the structural graph.

Files supported by a native parser are indexed structurally. Other regular
UTF-8 text files—including `Dockerfile`, dotfiles, extensionless files, and
configuration formats—are indexed as file-level text for lexical and semantic
search. Git ignore rules remain authoritative; common dependency, cache, VCS,
and build directories are always skipped. Large directories are additionally
pruned only when they cross a resource limit and exhibit generated or vendored
evidence. Binary and oversized files are not indexed.

The default ingestion limits can be changed with environment variables:

- `SCS_INDEX_TEXT_FALLBACK` enables or disables non-parser text ingestion.
- `SCS_INDEX_MAX_FILE_BYTES` limits each indexed file (default 1 MiB).
- `SCS_INDEX_TEXT_SAMPLE_BYTES` controls bounded UTF-8 detection (default 8 KiB).
- `SCS_INDEX_LARGE_DIR_FILES` sets the large-directory file threshold (default 10,000).
- `SCS_INDEX_LARGE_DIR_BYTES` sets the aggregate-size threshold (default 512 MiB).

The text sample size must not exceed the maximum file size.

## Automatic reindexing

SCS automatically reconciles every active enrolled project from Git-visible
state. Each daemon start queues a full discovery pass, which uses stored hashes
to parse and embed only changed files and removes stale file graphs. Subsequent
polls fingerprint `HEAD` plus Git porcelain status, covering commits, branch
switches, staged and unstaged edits, deletions, and non-ignored untracked files.
Ignored files do not trigger work.

Active repositories are checked every 2 seconds. Unchanged repositories back
off exponentially to 30 seconds; any change resets the interval to 2 seconds
and is debounced for 500 ms. Durable jobs coalesce per project and the single
job runner bounds indexing concurrency. The behavior is configurable with:

- `SCS_AUTO_REINDEX_ENABLED` (default `true`).
- `SCS_AUTO_REINDEX_ACTIVE_SECONDS` (default `2`).
- `SCS_AUTO_REINDEX_IDLE_SECONDS` (default `30`).
- `SCS_AUTO_REINDEX_DEBOUNCE_SECONDS` (default `0.5`).
- `SCS_AUTO_REINDEX_GIT_TIMEOUT_SECONDS` (default `10`).

The idle interval must be at least the active interval. Disabling automatic
reindexing does not affect explicit `scs index` or `scs reindex` requests.

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
