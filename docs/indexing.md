# Indexing and project management

SCS starts with an empty index. Repositories are enrolled only through an
explicit CLI, MCP, or client request. Indexing is durable background work;
`scs status` reports progress and `get_graph_stats` reports index readiness.

## Supported files and limits

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
polls fingerprint `HEAD`, Git porcelain status, and dirty-path metadata,
covering commits, branch switches, repeated staged and unstaged edits,
deletions, and non-ignored untracked files. Nanosecond modification/change
times and file identity detect edits even when a path's Git status stays
unchanged. Polling reads metadata without reading whole source files or
following symlink targets. Ignored files do not trigger work; ingestion still
verifies source content hashes.

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

## Project commands

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
job acknowledgement immediately. Deletion removes only SCS-owned derived
state; it never modifies repository source. A deleted numeric ID is never
reused.

For Python projects using a `src/` layout, a full `scs reindex <repo-path>`
connects previously unresolved imports to indexed symbols. The `IMPACT` query
playbook then reports test targets only when the graph contains a dependency
edge.

## Diagnostics

The service has no graphical interface. Use `scs status`, `scs doctor`, logs,
SCSWire, or MCP index statistics for operational visibility. `scs status` is
non-mutating; commands that require the daemon start it lazily and hold a
temporary lease.

`scs metrics --days 7 --json` reports daemon-wide hourly operation aggregates
without query text, source text, file paths, job payloads, or results. Metrics
use an HMAC repository identity, retain at most 30 days, and live in owner-only
`metrics.db` and `metrics.key` files under `SCS_HOME`.
