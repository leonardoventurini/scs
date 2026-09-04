# SCS

SCS is the headless Semantic Code System extracted from External product. It indexes
source repositories into a structural and vector-backed code graph and exposes
that intelligence through a local control socket and MCP.

SCS starts with an empty index. It does not migrate, inspect, or recreate any
legacy External product graph data. Repositories are added only through an explicit CLI,
MCP, or client request.

## Install

Stable releases support Apple Silicon macOS and x86-64 Linux with CPython
3.14. Download the versioned installer and checksum manifest from the same
[GitHub Release](https://github.com/leonardoventurini/scs/releases), verify the
script, then run it:

```bash
VERSION=0.1.0
curl -fsSLO "https://github.com/leonardoventurini/scs/releases/download/v${VERSION}/scs-installer-${VERSION}.sh"
curl -fsSLO "https://github.com/leonardoventurini/scs/releases/download/v${VERSION}/SHA256SUMS"
shasum -a 256 -c SHA256SUMS --ignore-missing
sh "scs-installer-${VERSION}.sh"
```

On Linux, use `sha256sum -c SHA256SUMS --ignore-missing`. The installer pins
the release, verifies its wheel and constraints, provisions a checksum-verified
`uv` binary when necessary, and installs SCS without `sudo`. Current macOS
artifacts are not Apple-signed or notarized; checksums and GitHub build
provenance provide release integrity.

Configure each MCP harness to run `/Users/you/.local/bin/scs mcp` (use the
corresponding home path on Linux). Each harness owns a small stdio bridge. The
first bridge starts the shared daemon, concurrent bridges reuse it, and closing
the final bridge shuts it down cleanly.

## Storage architecture

SCS uses [TSG](https://github.com/leonardoventurini/tsg) as its sole durable
graph and embedding engine. The Rust `scs-store` crate is a compatibility
adapter: it maps SCS repository scopes, typed code nodes and relationships,
ingestion checkpoints, metadata filters, traversal, and semantic search onto
generic TSG primitives. The Python, SCSWire, MCP, and CLI contracts therefore
remain SCS-owned without coupling TSG to code intelligence.

The dependency is pinned to the immutable `v0.2.0` Git tag and its resolved
commit in `Cargo.lock`; building SCS does not require a sibling TSG checkout.
TSG keeps canonical graph, catalog, and embedding state transactionally in
SQLite and treats its vector index as a rebuildable accelerator.

An index created by the former SCS storage engine is not migrated in place.
When that incompatible database is first opened, SCS moves the database, WAL,
SHM, and legacy `.usearch` sidecar (when present) to unique
`*.pre-tsg.backup` names, then creates an empty TSG index. Run
`scs reindex <repo>` to rebuild derived state from repository source. For
rollback, stop SCS, retain the new TSG files separately, restore the backed-up
legacy filenames, and run the previous SCS binary. Backup removal is always an
explicit operator action.

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
the file. Storing the key in the owner-only configuration file makes it
available to lazily spawned daemon processes without placing it in MCP config.

The unconfigured default is:

```toml
embedding_provider = "openai"
embedding_model = "text-embedding-3-large"
embedding_dimension = 3072
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

MCP uses stdio between each harness and its bridge, then SCSWire over one
owner-only Unix socket between bridges and the daemon. No TCP port or platform
service manager is required. A bootstrap lock serializes simultaneous first
clients; the daemon independently holds the storage writer lock. Each bridge
connection is its lease, so abrupt termination cannot leave an orphan lease.

Runtime artifacts live under `~/Library/Application Support/SCS/` on macOS and
`$XDG_RUNTIME_DIR/scs` on Linux, falling back to
`~/.local/state/scs/runtime`. Persistent indexes live only under `SCS_HOME`.

## Development

```bash
just setup
just verify
```

`just setup` installs Python dependencies, builds the private `scs._scs_native`
extension, and installs the repository's pre-commit hook. The daemon can then
be run directly with `scs serve`, while explicit repository enrollment uses
`scs index <repo>` or `scs reindex <repo>`.

Operate the lazy daemon explicitly when diagnosing it:

```bash
scs daemon start
scs daemon status
scs daemon restart
scs daemon stop
scs doctor
```

`scs status` is non-mutating. Commands that require the daemon start it lazily
and hold a temporary lease. `uv tool uninstall scs` removes installed code but
preserves `SCS_HOME`, configuration, indexes, and logs.

## Verification

`just verify` runs strict Basedpyright checks, Ruff, all Python tests with
branch coverage, and the Rust workspace. `just coverage` reports uncovered
Python lines and enforces the committed risk-based floor. The pre-commit hook
runs the same whole-source type gate.
Isolation gates cover exact stdio MCP inventory, multi-bridge daemon
convergence, bounded frames, generation-safe cleanup, stale/live socket
ownership, legacy sentinel preservation, External product-import denial, repository
source fingerprints, and committed RSS/index/query budgets.
