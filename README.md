# SCS

SCS is a headless semantic code-intelligence service. It indexes
source repositories into a structural and vector-backed code graph and exposes
that intelligence through a local control socket and MCP.

SCS starts with an empty index. It does not migrate, inspect, or recreate any
data from other applications. Repositories are added only through an explicit CLI,
MCP, or client request.

## Quick start

Stable releases support Apple Silicon macOS and x86-64 Linux with CPython
3.14. Download the versioned installer and checksum manifest from the same
[GitHub Release](https://github.com/leonardoventurini/scs/releases), verify the
script, then run it:

```bash
VERSION=0.1.15
curl -fsSLO "https://github.com/leonardoventurini/scs/releases/download/v${VERSION}/scs-installer-${VERSION}.sh"
curl -fsSLO "https://github.com/leonardoventurini/scs/releases/download/v${VERSION}/SHA256SUMS"
shasum -a 256 -c SHA256SUMS --ignore-missing
sh "scs-installer-${VERSION}.sh"
scs version
```

On Linux, use `sha256sum -c SHA256SUMS --ignore-missing`. The installer pins
the release, verifies its wheel and constraints, provisions a checksum-verified
`uv` binary when necessary, and installs SCS without `sudo`. Current macOS
artifacts are not Apple-signed or notarized; checksums and GitHub build
provenance provide release integrity.

Configure an embedding provider in `~/.scs/config.toml` before indexing. For
OpenAI, the minimum configuration is:

```toml
embedding_provider = "openai"
embedding_model = "text-embedding-3-large"
embedding_dimension = 3072
openai_api_key = "replace-with-your-key"
```

Keep that file owner-readable only (`chmod 600 ~/.scs/config.toml`). Local
OpenAI-compatible and in-process MLX examples are documented under
[Embedding configuration](#embedding-configuration).

Replace any old SCS entry, register the installed stdio MCP bridge with Codex,
then explicitly index the current repository:

```bash
codex mcp remove scs 2>/dev/null || true
codex mcp add scs -- "$HOME/.local/bin/scs" mcp
codex mcp get scs
scs index "$PWD"
scs status
```

Manage enrolled projects with stable numeric IDs:

```bash
scs list
scs list --json
scs reingest 3
scs delete 3
```

`scs list` reads the saved index directly and does not start the daemon or
schedule reconciliation. Its state column describes saved index availability.

`delete` and `reingest` also accept a repository path. Both return a durable
job acknowledgement immediately. Deletion removes only SCS-owned derived state;
it never modifies repository source. A deleted numeric ID is never reused.

Indexing is durable background work. `scs status` reports progress, and Codex
can call `get_graph_stats` once the index is ready. Restart open Codex clients
after changing MCP configuration. The equivalent manual entry in
`~/.codex/config.toml` is:

```toml
[mcp_servers.scs]
command = "/Users/you/.local/bin/scs"
args = ["mcp"]
```

Use `/home/you/.local/bin/scs` on Linux. Each Codex session owns a small stdio
bridge. The first bridge starts the shared daemon, concurrent bridges reuse it,
and closing the final bridge shuts it down cleanly.

Re-run the versioned installation procedure to upgrade or reinstall SCS. The
installer cancels active indexing at a durable boundary, waits for the older
daemon to release its writer lock, and then replaces the `uv` tool atomically.
preserves `SCS_HOME`, including configuration and indexes.

## Storage architecture

SCS uses [TSG](https://github.com/leonardoventurini/tsg) as its sole durable
graph and embedding engine. The Rust `scs-store` crate is a compatibility
adapter: it maps SCS repository scopes, typed code nodes and relationships,
ingestion checkpoints, metadata filters, traversal, and semantic search onto
generic TSG primitives. The Python, SCSWire, MCP, and CLI contracts therefore
remain SCS-owned without coupling TSG to code intelligence.

The dependency is pinned to the immutable `v0.2.1` Git tag and its resolved
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
to the OpenAI embeddings API, while local OpenAI-compatible and in-process MLX
providers are available by explicit configuration. SCS does not send repository files to a
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

To use a local OpenAI-compatible server without an API key:

```toml
embedding_provider = "openai_compatible"
embedding_model = "Qwen3-Embedding-8B-4bit-DWQ"
embedding_dimension = 4096
openai_compatible_base_url = "http://127.0.0.1:10001/v1"
```

The compatible provider defaults to loopback HTTP URLs. To use a server on an
explicitly trusted network host, configure both its URL and exact hostname:

```toml
openai_compatible_base_url = "http://m3:10001/v1"
openai_compatible_trusted_hosts = ["m3"]
```

The trust list is empty by default and matches hostnames case-insensitively;
it does not match subdomains or wildcards. Only opt in to a host and network
that you trust with source-derived embedding text. SCS continues to run locally.
The environment equivalent is
`SCS_OPENAI_COMPATIBLE_TRUSTED_HOSTS='["m3"]'`. In compatible-provider mode,
SCS ignores OpenAI credentials and sends no authorization header. Changing the provider, model,
or dimension quarantines incompatible vectors; the next indexing pass
regenerates embeddings while preserving the structural graph.

Search always fuses bounded semantic and lexical candidates. To rerank that
candidate set through the same local reranking endpoint, opt in explicitly:

~~~toml
reranking_model = "your-installed-reranker"
~~~

An absent `reranking_model` disables reranking. A configured model uses the
same validated `openai_compatible_base_url` as local embeddings and sends no
credentials. An unavailable or malformed reranker degrades to deterministic
fused retrieval without making search unavailable.

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
polls fingerprint `HEAD`, Git porcelain status, and dirty-path metadata, covering
commits, branch switches, repeated staged and unstaged edits, deletions, and
non-ignored untracked files. Nanosecond modification/change times and file identity
detect edits even when a path's Git status stays unchanged. Polling reads metadata
without reading whole source files or following symlink targets. Ignored files do
not trigger work; ingestion still verifies source content hashes.

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
SCSWire, or MCP index statistics for operational visibility. `scs metrics
--days 7 --json` reports daemon-wide hourly operation aggregates without query
text, source text, file paths, job payloads, or results. Metrics use an HMAC
repository identity, retain at most 30 days, and live in owner-only
`metrics.db` and `metrics.key` files under `SCS_HOME`.

## MCP tools

SCS exposes five model-facing operations: `query_code`, `ingest_project`,
`ingest_files`, `delete_repository`, and `get_graph_stats`. Query tools are annotated
read-only and closed-world. Ingestion tools are marked destructive because
reconciliation can remove stale SCS-owned index state.

`query_code(goal=..., repo_path=..., mode="balanced")` selects one bounded
playbook and returns compact evidence, routing details, stage traces, and
completeness flags. Optional anchors are `node_type`, `symbol_name`, `node_ids`,
`file_paths`, and `source_position`. Modes are `fast`, `balanced`, and
`thorough`. The seven former read tools are available only as internal service
routes; their MCP names have been removed.

Routing is deterministic by default. To use the local Laya MLX classifier on
Apple Silicon, sync the optional dependency with
`uv sync --all-groups --extra laya`, install its pinned, verified bundle with
`uv run --extra laya python scripts/install-laya.py`, and set
`decision_model = "laya"` in the SCS configuration before restarting the daemon.
The default bundle path is under the SCS model cache; `decision_model_path` can
override it with an absolute path. The classifier sees only the goal and
explicit anchors, never repository source or retrieved evidence. Failed
inference falls back to deterministic routing. A configured daemon becomes
ready only after the classifier has loaded and warmed; invalid model material
prevents startup. If the worker exits, SCS restarts it automatically and waits
for readiness before routing new queries. Queries never download a model.

For Python projects using a `src/` layout, a full `scs reindex <repo-path>`
connects previously unresolved imports to indexed symbols. IMPACT then reports
test targets only when the graph contains a dependency edge.

The [query migration guide](docs/query-code-migration.md) maps each retired
read tool to a goal and its optional anchors. Run `just eval-query` to compare
the unified tool with versioned internal-route sequences on this repository.

`delete_repository(repo_path=...)` durably removes one repository's SCS-owned
index and catalog registration, stops its watcher, and supersedes pending
indexing work without reading, changing, or deleting repository source. The
source directory does not need to exist. The operation is annotated
non-read-only, destructive, closed-world, and idempotent; deleting an already
absent repository succeeds with `already_absent=true` and no queued job. All SCS
tools preserve repository source.

Operational diagnostics remain available through the CLI and SCSWire instead
of occupying the model's tool catalog.

`query_code` returns compact evidence, stage timings, and truthful degradation.
It does not expose the former search tool's full node records, raw query-angle
controls, or pagination. Use the documented SCSWire service routes for callers
that need those lower-level responses.

`get_graph_stats` separately reports structural and semantic readiness. With a
repository path it also includes redacted active/latest durable jobs and a
retry hint. `wait_job_id` can observe one job for up to 10 seconds without
running, retrying, or cancelling it.

The `IMPACT` playbook uses bounded incoming dependency analysis and includes
test-file targets backed by direct dependency edges.

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
just eval-search
```

`just setup` installs Python dependencies, builds the private `scs._scs_native`
extension, and installs the repository's pre-commit hook. The daemon can then
be run directly with `scs serve`, while explicit repository enrollment uses
`scs index <repo>` or `scs reindex <repo>`. Use `scs list` to discover enrolled
projects and their numeric IDs, `scs reingest ID|PATH` for a forced full
rebuild, and `scs delete ID|PATH` to retire one project's SCS-owned state.

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
ownership, empty-startup behavior, runtime isolation, repository
source fingerprints, and committed RSS/index/query budgets.

The search evaluation recipe runs the versioned suite in
evals/scs-search-v1.json against the current checkout through the public SCS
route. It reports Recall@k, MRR, nDCG@k, response size, and latency as JSON.
Suite schema and comparison guidance live in evals/README.md; live model timing
is evidence, not a machine-independent CI threshold.
