# Codex SCS usage analysis

Date: 2026-09-14

Status: Analysis and recommendations

## Implementation status

The report's product recommendations were implemented on 2026-09-14:

- `search_code` and `graph_context` accept bounded query angles and explicit
  search modes, and report evidence, timings, and enrichment degradation.
- `get_graph_stats` reports structural/semantic readiness, redacted scoped job
  state, retry guidance, and optional bounded job observation.
- `regression_risk_report` uses incoming-edge traversal and batch hydration,
  bounds its output, and reports completeness, timing, and direct test-target
  evidence.
- daemon-wide, content-free hourly metrics persist under `SCS_HOME` with HMAC
  repository identities, owner-only files, retention and row bounds, corruption
  recovery, and fail-open behavior. `scs metrics --days N --json` reads them.
- MCP descriptions now guide callers toward the intended search, traversal,
  inspection, and reference workflow without expanding the tool inventory.

The existing full-result and thorough-search defaults remain unchanged for
compatibility. The fixed MCP inventory remains eleven tools.

The post-change `scs-search-v1` evaluation completed successfully against this
repository with Recall@10 `0.667`, MRR `0.431`, nDCG@10 `0.432`, mean latency
`1.164s`, and p95 latency `1.388s`. These live-model values are observational;
they establish the review baseline for later same-suite comparisons rather
than a portable CI threshold.

## Executive summary

Recent Codex history shows that SCS is broadly useful across repositories and
usually returns results successfully. The main opportunity is no longer basic
search recall. It is reducing the time, response size, and number of serial tool
calls needed to understand a codebase.

During the sampled 30-day window, Codex made 1,723 SCS calls across 207 threads
and more than 25 repository roots. `search_code` accounted for 1,017 calls and
had a 95.3% success rate. Only 6.6% of completed searches returned no results.

The most common workflow was a readiness check followed by several consecutive
searches:

```text
get_graph_stats
       |
       v
  search_code ---> search_code ---> search_code
       |                 |
       v                 v
graph_context      regression_risk_report
```

There were 647 consecutive `search_code` calls. Fifty-six threads made at least
five searches, 31 made at least ten, and the largest thread made 72. This is the
strongest evidence that SCS should support broader exploration with fewer round
trips.

The recommended priorities are:

1. Add bounded-latency search with explicit quality modes and reranker timeout.
2. Make compact search results the default.
3. Support multi-query exploration within the existing search surface.
4. Expose active ingestion progress through repository readiness responses.
5. Optimize regression-risk analysis for large affected sets.
6. Return actionable search diagnostics and stage timings.
7. Improve guidance for the underused structural tools.
8. Add durable, privacy-preserving operational metrics outside MCP.

## Scope and methodology

The analysis used the local Codex thread-history database under `~/.codex` and
the current SCS source tree. It was read-only.

The primary window covered the 30 days ending at the newest recorded event:
2026-08-15 through 2026-09-14. The available thread-item history begins on
2026-08-11, so the primary window represents nearly all available structured
tool history. A seven-day slice was also checked to distinguish current behavior
from older failures.

The sample included all repository paths present in SCS tool arguments. It was
not limited to the SCS repository. High-volume repositories included Mentagen,
VitaFlow, Cortex, SolidScript, MES, Meteor, TypeFerry, infrastructure projects,
and SCS itself. Temporary test repositories and malformed test inputs were kept
visible when classifying failures but were not treated as normal product usage.

The following facts were aggregated:

- Tool frequency, success status, latency, and per-thread usage.
- Explicit repository scope and result-detail arguments.
- Search result count, retrieval mode, and serialized response size.
- Consecutive SCS tool transitions within threads.
- Repeated readiness and ingestion checks.
- Failure categories without retaining or reproducing unrelated chat content.

A second, cross-tool pass covered 289,630 history items, 892 threads, and 5,709
turns in the same window. It compared SCS activity with command execution, file
changes, verification, context compaction, and conservative user-correction
signals. This pass emitted no message or source content.

This report does not evaluate whether every returned result was semantically
correct. Search-quality conclusions are therefore limited to observable use,
empty-result rates, retrieval modes, and repeated agent behavior. Recall, MRR,
and ranking-quality claims require the existing versioned search evaluations.

## Usage profile

### Tool adoption

| Tool | Calls | Threads | Success rate |
|---|---:|---:|---:|
| `search_code` | 1,017 | 185 | 95.3% |
| `get_graph_stats` | 301 | 150 | 95.7% |
| `graph_context` | 91 | 40 | 96.7% |
| `regression_risk_report` | 72 | 44 | 95.8% |
| `inspect_file` | 60 | 25 | 93.3% |
| `find_references` | 53 | 11 | 71.7% |
| `get_related` | 15 | 7 | 80.0% |
| `list_symbols` | 6 | 3 | 83.3% |
| `ingest_project` | 53 | 36 | 96.2% |
| `ingest_files` | 54 | 5 | 98.1% |

The most recent seven days were healthier than the full window:

- `search_code`: 498 calls, 99.0% successful.
- `graph_context`: 48 calls, 100% successful.
- `inspect_file`: 40 calls, 100% successful.
- `find_references`: 33 calls, 87.9% successful.

The older `find_references` failures included output-schema validation errors.
The recent improvement suggests that current contract hardening has already
resolved part of this problem. It should be monitored rather than treated as a
new top-priority defect.

### Discovery behavior

Completed searches used these retrieval modes:

| Retrieval mode | Searches | Median latency | p95 latency |
|---|---:|---:|---:|
| Semantic | 467 | 166 ms | 7.5 s |
| Semantic reranked | 434 | 4.1 s | 40.4 s |
| Lexical | 43 | 95 ms | 2.1 s |
| None | 25 | 15 ms | 3.4 s |

The median query was eight words and 67 characters. Repeated identical queries
were rare: only six additional calls repeated an identical normalized query in
the same repository and thread. The long search chains therefore represent
iterative exploration, not accidental retries.

`graph_context` was usually productive. Of 88 successful calls with structured
results, four returned no seeds and six returned no context. Its p95 latency was
59 seconds, however, because it performs search before traversal and can inherit
the same reranking cost.

`get_related` and `list_symbols` were barely used. This may indicate weak tool
selection guidance: agents repeatedly formulate another search instead of
following a known node or requesting an inventory.

### Readiness behavior

Repository readiness is a routine part of the workflow:

- 130 of 207 SCS-using threads began with `get_graph_stats`.
- 60 threads called it at least twice.
- Individual threads reached 11, 15, and 23 readiness calls.
- There were 31 immediate `ingest_project` or `ingest_files` to
  `get_graph_stats` transitions.
- There were 27 consecutive `get_graph_stats` transitions.

Some repetition is required because ingestion is durable background work. The
current response does not provide a purpose-built way to follow the queued job,
so callers use repository-wide statistics as a polling interface.

### Response size

Search result detail materially affects context size:

| Requested detail | Calls | Median size | p95 size | Maximum |
|---|---:|---:|---:|---:|
| Compact | 208 | 9.4 KB | 17 KB | 45 KB |
| Explicit full | 242 | 12.3 KB | 74 KB | 184 KB |
| Omitted, full default | 519 | 7 KB | 22 KB | 303 KB |

The omitted group has a smaller median because callers often requested fewer
results, but it produced the largest individual response. Full records contain
timestamps, repository IDs, parser metadata, and other fields that are rarely
needed during initial discovery.

### Cross-tool productivity context

The wider Codex workflow reinforces the need to keep SCS responses compact and
composable:

- 1,337 of 5,709 turns contained at least 50 recorded items.
- 775 context compactions occurred during the 30-day window.
- In the recent seven-day slice, the median turn contained 25 items, 308 of 927
  turns contained at least 50 items, and 203 compactions occurred.
- Over 30 days, 17.9% of turns containing SCS calls were compacted, compared
  with 11.0% of turns without SCS. In the recent slice the rates converged to
  18.7% and 16.6%, so this is evidence of shared workflow pressure rather than
  proof that SCS causes compaction.

Direct source discovery remained common: commands classified as `rg`, `find`,
`sed`, or similar reads accounted for 41,981 executions. This is not inherently
waste. SCS is a discovery and structural-analysis layer, while current source
remains authoritative. Product changes should therefore optimize the handoff
from SCS results to exact file and line reads instead of trying to eliminate
direct inspection.

Verification was also central to the workflow. Of 2,091 turns with a completed
file change, 1,970, or 94.2%, contained a test or static-analysis command. At
least one such command failed in 937 change turns, although many later recovered
within the same task. This supports better test targeting and failure
explanation, but it does not show that verification quality is poor.

## Findings and recommendations

### P0: Bound search latency

Semantic reranking is the main latency source. Its 4.1-second median is about 25
times the non-reranked semantic median, its p95 is 40.4 seconds, and the slowest
search took 145 seconds.

SCS currently runs reranking inline after candidate fusion. Provider errors
degrade safely to fused results, but there is no caller-selectable quality level
or deadline.

Recommended behavior:

- Add `search_mode="fast" | "balanced" | "thorough"`, or an equivalent
  bounded-latency policy.
- Give reranking an explicit timeout.
- Return deterministic fused results when the timeout expires.
- Include whether reranking ran, degraded, or timed out.
- Apply the same policy to the search phase of `graph_context`.

Acceptance criteria:

- Balanced search p95 is below five seconds on representative repositories.
- Timeout fallback returns valid fused results instead of failing the call.
- Versioned Recall@k, MRR, and nDCG measurements show no accepted-threshold
  regression.
- Results identify the actual retrieval and degradation path.

### P0: Default to compact results

Compact results already preserve stable identity, source location, signature,
bounded content, and semantic distance. These fields are sufficient for initial
discovery and subsequent `get_related`, `inspect_file`, or direct source reads.

Recommended behavior:

- Change the MCP `search_code` default from `full` to `compact`.
- Keep `full` as an explicit opt-in.
- Document which follow-up tool supplies omitted structure.

Acceptance criteria:

- Search response p95 is below 25 KB for the representative workload.
- Compact results contain the node ID, symbol name and type, qualified name,
  file path, lines, signature, bounded content, and semantic distance.
- Existing explicit `result_detail="full"` callers remain unchanged.

This is a public default-behavior change and requires explicit approval before
implementation.

### P1: Add multi-query exploration without another tool

The 647 consecutive searches and scarcity of exact repeated queries show that
agents explore a topic from several angles. Each query currently pays separate
MCP, embedding, vector-search, and possibly reranking costs.

Recommended behavior:

- Extend `search_code` with an optional bounded query list while preserving the
  existing single-query input.
- Execute lexical and semantic retrieval concurrently per query.
- Deduplicate candidates by stable node ID.
- Fuse evidence across queries and rerank the combined candidate set once.
- Return the matching query indexes or labels for each result.

This keeps the fixed MCP inventory small and avoids adding a near-duplicate
tool.

Acceptance criteria:

- A three-concept exploration requires one SCS call.
- Results are deterministic for the same index and inputs.
- Duplicate nodes appear once with their matching-query evidence.
- Representative discovery tasks use at least 50% fewer sequential searches.
- Latency is lower than executing the equivalent calls serially.

### P1: Expose active ingestion progress with readiness

The service already persists detailed job state including status, phase,
current, total, message, attempts, error, and timestamps. MCP returns this state
when work is enqueued, but `get_graph_stats` does not expose the active job used
to explain an indexing or queued state.

Recommended behavior:

- Add the repository's active or latest failed job summary to
  `get_graph_stats`.
- Distinguish structural readiness from semantic readiness.
- Include a suggested retry interval for active work.
- Consider a bounded `wait_ms` option rather than introducing an additional
  job-status tool.

Acceptance criteria:

- A caller can follow an ingestion job to a terminal state without guessing
  from node counts.
- Progress is monotonic and tied to the job ID returned by ingestion.
- Waiting remains bounded and does not change durable/background semantics.
- Failed jobs expose a typed, actionable reason.

### P1: Optimize regression-risk analysis

`regression_risk_report` has a 39-millisecond median but a 46.7-second p95 and
an 83-second maximum. This suggests that small changes are cheap while large
affected sets trigger expensive graph hydration.

The current route batches edge retrieval but retrieves dependent nodes one at a
time.

Recommended behavior:

- Deduplicate affected node IDs before graph access.
- Add a native batch node lookup.
- Bound dependent and test-dependent results.
- Return truncation and completeness metadata.
- Measure file lookup, edge traversal, and node hydration separately.
- Return deduplicated test-file targets with the dependency path that explains
  why each test is relevant.
- Preserve stable file and line coordinates so agents can move directly from
  the report to authoritative source and test reads.

Acceptance criteria:

- Normal change-set p95 is below five seconds on representative repositories.
- Runtime scales with unique affected and dependent nodes rather than duplicate
  occurrences.
- Truncated reports never imply completeness.
- Existing dependency and test classification remains deterministic.
- Each suggested test target includes inspectable relationship evidence.
- Representative change tasks require fewer manual discovery commands to select
  relevant tests, without reducing the tests ultimately executed.

### P1: Return actionable search diagnostics

The internal search response already tracks semantic availability and a
degradation reason, but the public MCP contract exposes only
`retrieval_mode`. This makes a slow or degraded call difficult for an agent to
interpret.

Recommended additions:

```text
semantic_available
reranker_applied
degraded_reason
timings:
  lexical_ms
  embedding_ms
  vector_ms
  rerank_ms
```

Acceptance criteria:

- Timings cover all material search stages without including query or source
  content.
- Provider failure and timeout reasons are typed and bounded.
- Diagnostics do not change result ordering.

### P2: Improve structural-tool selection

Tool descriptions should guide agents from broad discovery into structural
inspection:

```text
search_code
    |
    +--> known node ID ------> get_related
    |
    +--> known source file --> inspect_file
    |
    +--> source position ----> find_references
    |
    +--> exhaustive list ----> list_symbols
```

Recommended changes:

- Say explicitly that `get_related` should follow a search result node ID.
- Recommend `inspect_file` instead of repeated filename-oriented searches.
- Reserve `list_symbols` for exhaustive, paginated inventories.
- Describe accepted node types and relationship directions in the schemas.
- State prominently that `find_references.line` is zero-based.

Improve descriptions before considering additional tools.

### P2: Add durable privacy-preserving metrics

Current MCP observability is a bounded process-local buffer. It records tool
name, duration, status, and error type, but it cannot support longitudinal
analysis after a daemon restart.

Recommended metrics:

- Tool name and retrieval mode.
- Total and stage latency.
- Result count and serialized response size.
- Empty, degraded, timeout, and truncation flags.
- A locally salted repository identifier.

Do not record query text, source content, file paths, tool arguments, or result
payloads. Expose aggregate diagnostics through the CLI rather than increasing
the MCP tool inventory.

Acceptance criteria:

- Metrics survive daemon restart within a bounded retention period.
- Storage has a documented maximum size and cleanup policy.
- Repository identities cannot be correlated across installations.
- No source or natural-language content is persisted.
- Metrics failures remain fail-open for code-intelligence operations.

## Proposed delivery sequence

### Phase 1: Establish performance evidence

- Add stage-level timing to internal search and regression-risk execution.
- Define representative queries and changes across small, medium, and large
  repositories.
- Record current quality, latency, and response-size baselines.

### Phase 2: Reduce search cost

- Implement bounded search modes and reranker fallback.
- Change the default result detail after explicit approval.
- Run the versioned quality evaluation and performance gates.

### Phase 3: Reduce round trips

- Implement bounded multi-query search within `search_code`.
- Add active job summaries to `get_graph_stats`.
- Update tool descriptions to encourage structural follow-up.

### Phase 4: Improve large-graph operations

- Add native batch node hydration.
- Optimize and bound regression-risk results.
- Validate behavior on large real repositories.

### Phase 5: Operationalize measurement

- Add durable privacy-preserving aggregates.
- Expose them through the CLI.
- Re-run this analysis after sufficient post-release usage.

## Risks and constraints

- Search modes, new response fields, and a compact default affect the public MCP
  contract. Compatibility and explicit approval are required.
- Faster search must not be assumed better until ranking quality is measured.
- Multi-query search must remain bounded to prevent one call from creating
  unbounded provider or graph work.
- Job progress must remain tied to durable job state rather than inferred from
  changing graph counts.
- Metrics must preserve SCS's local, source-private product boundary.
- The fixed MCP inventory should remain small; improvements should consolidate
  behavior into existing tools where the task remains coherent.

## Additional limitations

- The cross-tool command categories are lexical heuristics. For example, a
  source-discovery command may be required verification rather than rework.
- A failed test or static-analysis command is an intermediate workflow event,
  not evidence that the final result was incorrect.
- Conservative message analysis found no reportable cohort of explicit user
  correction phrases. This does not prove the absence of quality issues; it
  means the available high-precision heuristic did not support a claim.
- Context compaction correlates with long, complex turns. The history does not
  establish that any one tool caused it.
- Repository metadata was unavailable for a substantial portion of the broader
  history, so cross-project conclusions rely on aggregate recurrence rather
  than complete per-repository attribution.

## Review order

Reviewers should examine the proposals in this order:

1. Search latency evidence and the bounded-search contract.
2. Compact-result compatibility and response-size evidence.
3. Multi-query bounds and deterministic fusion semantics.
4. Ingestion progress ownership and durable job linkage.
5. Regression-risk completeness, explainable test targets, and truncation
   semantics.
6. Privacy and retention properties of durable metrics.

No implementation decision is made by this report. Each public contract change
must be specified and approved before implementation.
