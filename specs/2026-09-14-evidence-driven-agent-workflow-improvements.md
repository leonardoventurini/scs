# Evidence-driven agent workflow improvements

Date: 2026-09-14

Status: Approved and validated for implementation

## Problem

Recent Codex history shows that SCS usually finds useful code, but agents spend
too much time and context on serial searches, inline reranking, readiness
polling, and manual test-target discovery.

The 30-day sample contains 1,723 SCS calls across 207 threads and more than 25
repository roots. Search returned no results only 6.6% of the time, but
semantic-reranked search had a 4.1-second median and 40.4-second p95. There were
647 consecutive searches and 301 readiness calls. Regression-risk analysis had
a 39-millisecond median and 46.7-second p95.

The wider workflow contained 775 context compactions and 1,337 turns with at
least 50 recorded items. Of 2,091 turns with a completed file change, 94.2%
included tests or static analysis. SCS should reduce discovery and verification
friction without replacing authoritative source reads or executed tests.

## Agreed scope

Deliver the complete five-phase roadmap in separately tested and committed
units:

1. Measure search and regression-risk stages.
2. Add bounded search modes while preserving existing defaults.
3. Add optional multi-query search, job-aware readiness, and better tool
   guidance.
4. Batch and enrich regression-risk analysis.
5. Persist privacy-preserving aggregate metrics and expose them through the
   CLI, not MCP.

Public changes are additive. Existing parameter defaults and the fixed MCP tool
inventory remain unchanged. `search_code` keeps the required singular `query`
and gains optional additional `queries`.

The user explicitly approved a new local persistence and security boundary for
metrics: a separate owner-only database and HMAC key under `SCS_HOME`, bounded
allowlisted aggregates, 30-day retention, and fail-open recovery.

## Evidence and uncertainty

- The configured reranker has a 30-second HTTP timeout, but the search service
  itself does not bound injected or alternative provider calls.
- Search already retains semantic availability and degradation internally, but
  public routes discard those facts.
- Full search results remain the compatibility default even though compact
  results are materially smaller.
- Durable ingestion jobs already contain phase and progress. Repository stats
  currently return before consulting jobs when the graph does not exist.
- Rust and PyO3 already expose `batch_get_nodes`; the typed Python graph adapter
  does not.
- Regression risk batches edges but hydrates dependent nodes individually and
  asks for both edge directions before filtering incoming dependencies.
- Current MCP telemetry belongs to each short-lived bridge and cannot aggregate
  all clients or survive restart.
- Search timing and quality depend on repository size, model state, and machine
  load. Performance claims require measured representative cohorts.
- Direct test-dependency edges can explain candidate test targets. They cannot
  prove complete runtime coverage or transitive behavior.

## Contracts

### Search policy

Add a typed search mode:

```text
fast      hybrid retrieval, no reranking
balanced  hybrid retrieval, reranking bounded to 5 seconds
thorough  current behavior, reranking bounded to 30 seconds
```

`thorough` remains the default. Timeout or provider failure returns the
deterministic fused ordering. `graph_context` accepts the same optional mode and
uses it for seed search.

Search results add:

- the effective ordered query list;
- semantic availability;
- whether reranking was applied;
- typed degradation stage and timeout state;
- a bounded human-readable degradation reason;
- lexical, embedding, vector, reranking, and total elapsed milliseconds.

Timing fields measure caller-visible elapsed time, including thread scheduling.
They never influence ordering.

### Multi-query search

`query` remains required. `queries` accepts up to four additional non-empty
strings, for a maximum of five effective queries. Exact duplicates are removed
while preserving first occurrence and primary-query order.

Each effective query performs bounded lexical and semantic candidate retrieval.
Queries run concurrently only after graph-thread-safety assumptions are proven.
Candidates are merged by stable node ID with deterministic reciprocal-rank
fusion. Reranking occurs once over the bounded merged set using a deterministic
combined query representation.

Per-result query evidence is returned in a top-level mapping from node ID to
matching zero-based query indexes. Existing full and compact result item shapes
remain unchanged.

### Repository readiness and job observation

`get_graph_stats` retains every existing field and adds:

- explicit structural and semantic readiness;
- the most recently updated active job, if present;
- the latest job, if present;
- a suggested retry interval for active work;
- the outcome of an optional bounded wait.

Job summaries omit payloads, results, leases, store IDs, and store generations.
They include only ID, mode, status, phase, progress, message, attempts, error,
and timestamps.

Optional waiting requires a repository path and exact job ID. The timeout
defaults to zero and is capped at 10 seconds. It observes durable state and
never runs, retries, cancels, or otherwise changes a job. Typed outcomes are
`terminal`, `timeout`, and `not_found`. A job from another repository is treated
as not found within the scoped contract.

### Regression risk

Regression-risk analysis will:

- deduplicate affected node IDs deterministically;
- retrieve only incoming dependency edges;
- hydrate dependent nodes through one Python-to-native batch call;
- retain existing `dependents` and `test_dependents` fields and defaults;
- accept optional bounded dependent and test-target limits;
- report total counts, truncation, and overall completeness;
- report file lookup, edge traversal, node hydration, projection, and total
  elapsed milliseconds;
- return deduplicated test-file targets with direct edge evidence explaining
  each target.

Test evidence contains dependent node ID, affected node ID, and relationship.
It does not claim transitive reachability or that the suggested tests are
sufficient.

### Durable aggregate metrics

Metrics are recorded at the shared daemon service boundary so every bridge
contributes to one aggregate stream. MCP inventory does not change.

Persistent artifacts:

```text
SCS_HOME/
|-- metrics.db   owner-only aggregate SQLite database
`-- metrics.key  owner-only random 32-byte HMAC key
```

The metrics database uses an explicit schema version. It stores hourly buckets,
not raw events. Allowed dimensions are fixed enums for operation, retrieval
mode, outcome, and error category. Measures include count, latency histogram or
bounded summaries, result and response-size summaries, and empty, degraded,
timeout, and truncated counters.

Repository identity is `HMAC-SHA256(local_key, canonical_repository_path)`. The
path and key are never stored in the database. Query text, source, filenames,
arguments, responses, messages, and raw error strings are never recorded.

Retention is 30 days. Cleanup runs opportunistically and storage has a defined
page or byte ceiling. Writes are batched away from request latency and fail
open. Corruption recovery quarantines the metrics database and creates a clean
one; it never affects graph or job stores. Key loss creates a new key and ends
longitudinal repository continuity.

`scs metrics --days N --json` reads aggregate metrics through an operational
SCSWire method. Human-readable output remains concise. The command cannot emit
repository paths or recover a path from its digest.

### Tool guidance

Descriptions will guide agents from broad discovery to existing structural
tools:

```text
search_code
    |
    +--> node ID --------> get_related
    +--> file -----------> inspect_file
    +--> source line ----> find_references
    `--> exhaustive list -> list_symbols
```

Descriptions will state valid directions and node types, the zero-based line
contract, and when direct source verification remains necessary.

## Test strategy

Tests precede or accompany each behavior change.

### Search

- Unit tests prove fast skip, balanced timeout, thorough compatibility,
  fail-open ordering, non-negative timings, and diagnostic truthfulness.
- Unit tests prove multi-query bounds, stable deduplication, deterministic
  fusion, query evidence, and one reranker call.
- Integration tests prove service and MCP forwarding, unchanged defaults,
  unchanged result-item shapes, empty repository diagnostics, and graph-context
  policy propagation.
- Search evaluations compare Recall@k, MRR, nDCG, payload bytes, and warmed
  latency for all modes and multi-query versus serial calls.

### Readiness

- Job-store tests prove active/latest ordering, repository isolation, terminal
  exclusion, and concurrent queued/running selection.
- Service tests prove fresh unindexed job visibility, live progress, bounded
  timeout, early terminal return, mismatch handling, and readiness semantics.
- MCP tests prove optional argument forwarding and additive output schemas.
- Isolation tests prove a fresh root remains empty without explicit ingestion.

### Regression risk

- Adapter tests prove one batch hydration call and GIL release through the
  existing PyO3 method.
- Integration tests use procedurally generated dependency graphs to prove
  deduplication, stable ordering, direct evidence, target-file grouping, bounds,
  truncation, completeness, and timing fields.
- Performance tests establish a representative wide-graph ceiling.

### Metrics

- Unit tests prove schema creation and migration, HMAC stability and isolation,
  permission enforcement, aggregation, bounded labels, retention, size cleanup,
  batching, and fail-open corruption recovery.
- Integration tests prove daemon-wide recording across operations and metrics
  survival across restart.
- CLI contract tests prove JSON and human output, date bounds, and the absence
  of paths, query text, source, arguments, responses, and raw errors.
- Failure injection proves metrics cannot break search, ingestion, or shutdown.

## Risks and recovery

- Additive output fields can break consumers that incorrectly assert exact
  object keys. Existing SCS contract tests will be updated only for intended
  additions; result item shapes remain stable.
- Timing fields are nondeterministic. Tests assert shape and invariants, never
  exact elapsed values.
- Multi-query work can amplify provider and graph load. Query and candidate
  counts stay bounded, and concurrency is enabled only after thread-safety
  verification.
- Keeping `thorough` as default preserves compatibility but means existing
  callers do not receive latency improvement until they select another mode.
- Waiting inside stats can occupy an MCP call. The exact job ID and 10-second
  cap prevent unbounded or ambiguous waiting.
- Truncation can hide dependencies. Counts and `complete` make this explicit.
- Metrics introduce a persisted format and sensitive local key. Owner-only
  creation, allowlisted fields, data minimization, retention, and isolated
  fail-open recovery contain that risk.

Every phase is independently reversible through a normal code revert. Search,
readiness, and risk changes do not migrate graph data. Metrics rollback stops
new recording; `metrics.db` and `metrics.key` can be removed separately without
affecting SCS indexes or jobs.

## Direct rollout

Ship each phase directly after its focused tests and `just verify` pass. Keep
existing search defaults and MCP inventory. Do not automatically enable a
faster mode for existing callers. Metrics default to enabled local aggregation
with the approved minimal schema; operators can disable it through typed
configuration without affecting code intelligence.

## Executable checklist

- [x] Validate every architectural assumption against current code and sign off
  this spec.
- [x] Add stage timing and typed search policy tests.
- [x] Implement bounded search modes and additive diagnostics.
- [x] Add multi-query tests and deterministic merged retrieval.
- [x] Extend graph-context policy and diagnostics.
- [x] Add focused durable-job queries and readiness tests.
- [x] Implement scoped job summaries and bounded waiting.
- [x] Expose native batch node hydration in the Python adapter.
- [x] Add regression-risk bounds, evidence, completeness, and timing tests.
- [x] Implement optimized regression-risk analysis.
- [x] Improve MCP descriptions without expanding inventory.
- [x] Add metrics persistence and privacy/security tests.
- [x] Implement daemon aggregation and `scs metrics`.
- [x] Update README, evaluation artifacts, and operational documentation.
- [x] Run targeted checks after each unit and commit it separately.
- [x] Run search quality/performance evaluations.
- [x] Run `just verify` and report every acceptance criterion.

## Verification targets

- Balanced search p95 below five seconds on representative repositories.
- Search timeout always returns deterministic fused results.
- No accepted-threshold regression in Recall@k, MRR, or nDCG.
- Compact response p95 below 25 KB remains measurable, although compact is not
  made the default in this compatibility posture.
- A three-angle exploration uses one SCS call and is faster than three serial
  calls under equivalent model conditions.
- Job observation reaches a typed terminal or timeout state without guessing
  from node counts.
- Normal regression-risk p95 below five seconds with explainable test targets.
- Durable metrics remain bounded, content-free, owner-only, and unable to break
  code-intelligence operations.

## Research references

- Python `asyncio.timeout` provides safely nestable bounded async deadlines:
  <https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout>
- MCP tool structured results must conform to their declared output schemas:
  <https://modelcontextprotocol.io/specification/draft/server/tools>
- OpenTelemetry recommends data minimization and avoiding collection of
  sensitive data when aggregates are sufficient:
  <https://opentelemetry.io/docs/security/handling-sensitive-data/>

## Assumption validation and sign-off

The following assumptions were checked against the current source before
implementation:

- **Search deadline:** confirmed. The oMLX HTTP adapter has a fixed 30-second
  timeout, while `CodeSearchService` does not enforce a provider-independent
  deadline. A service-level timeout is required for injected and future
  providers.
- **Additive MCP schema:** confirmed. The current MCP SDK derives object input
  and output schemas from typed Python signatures and return types. Optional
  list inputs and additive object fields fit the current contract; existing
  defaults and result item shapes will remain unchanged.
- **Graph concurrency:** confirmed with a constraint. Native store access is
  protected by its internal lock and PyO3 search/storage work releases the GIL.
  Bounded concurrent retrieval is safe, although native graph work can
  serialize at the store lock. Performance tests must prove that concurrency is
  beneficial before relying on it for the acceptance target.
- **Batch node hydration:** confirmed. `batch_get_nodes` already exists in the
  Rust store and PyO3 layer and releases the GIL. Only the Python protocol and
  facade are missing; no graph-format or Rust API change is needed.
- **Job lookup:** confirmed. `IngestionJobStore` persists progress and has a
  `(repo_path, updated_at DESC)` index. Focused active/latest helpers can avoid
  the current global 200-job scan.
- **Fresh-job visibility:** confirmed. `KnowledgeServiceRoutes.stats` currently
  returns immediately when no graph exists, so durable job lookup must precede
  that branch.
- **Regression-risk cost:** confirmed. Edges are FFI-batched but requested in
  both directions, affected IDs are not deduplicated, and dependent nodes are
  hydrated one at a time. The existing Rust edge batch loops per node, so a
  later true storage batch may still be warranted after measurement.
- **Shared metrics boundary:** confirmed. MCP recorders live in short-lived
  bridges; the daemon router/service boundary is the correct aggregation point.
- **Filesystem security:** confirmed. SCS already creates owned directories as
  `0700` and identity files as `0600`; metrics will reuse and test those
  conventions.
- **CLI extension:** confirmed. The CLI already calls operational SCSWire
  methods for daemon-backed commands, so `scs metrics` does not require an MCP
  tool.
- **Recovery isolation:** confirmed. Metrics can use a separate database and
  key under `SCS_HOME`; quarantine or deletion cannot alter project graph stores
  or `jobs.db`.

The implementation may proceed under the agreed additive compatibility posture
and approved metrics persistence boundary. Any discovery that requires changing
existing defaults, graph data formats, the MCP inventory, remote telemetry, or
source privacy must return for explicit approval.
