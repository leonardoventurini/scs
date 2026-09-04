# Keep the MCP surface limited to essential code intelligence

Date: 2026-08-05
Status: Accepted

## Context

SCS exposed 27 model-facing tools that had not been selected from evidence
about agent tasks. Several names reached the same route, several diagnostics duplicated
CLI or internal operations, and some composite analyses overstated what their
implementations established. The full discovery payload occupied 12,137
serialized characters before a client added any other MCP server.

Repository history contains no per-tool adoption rationale. Local sibling
client repositories contain no hard-coded tool or backing-route consumers, and
the available MCP recorder is process-local rather than durable usage evidence.

## Decision

Expose exactly ten MCP tools:

- `search_code`
- `graph_context`
- `get_related`
- `list_symbols`
- `inspect_file`
- `find_references`
- `regression_risk_report`
- `ingest_project`
- `ingest_files`
- `get_graph_stats`

These cover semantic retrieval, graph context, relationship traversal,
exhaustive symbol browsing, file inspection, indexed references, change impact,
full and incremental indexing, and index readiness. Each operation has a
distinct agent task. Query tools declare read-only, non-destructive,
idempotent, closed-world behavior. Ingestion tools declare
`destructiveHint=true` because deletion requests and stale-file reconciliation
can remove SCS index state; this is separate from the invariant that SCS never
mutates repository source.

Retired names receive no compatibility aliases. Useful behavior is consolidated
into the retained contracts: exact node lookup and incoming contracts belong to
`get_related`; repository readiness belongs to `get_graph_stats`; file symbols
belong to `inspect_file`. Git-history ingestion, heuristic coverage and
consistency reports, and model-facing development diagnostics are no longer
public routes.

## Rejected alternatives

- **Keep all tools and rely on client-side filters.** This preserves ambiguity,
  misleading contracts, and maintenance cost for clients that discover the
  complete server.
- **Hide tools dynamically.** SCS has a fixed product boundary; runtime catalog
  changes would make behavior harder to reason about without removing obsolete
  capability.
- **Keep deprecated aliases.** Aliases retain the schema footprint and make tool
  choice less deterministic, defeating the outcome.
- **Add column-precise reference lookup during this rollout.** Parser entities
  persist line ranges, not column ranges. `find_references` now exposes the real
  line-based contract instead of implying unsupported precision.

## Consequences

- Tool count falls from 27 to 10. The discovery schema remains typed and gains
  explicit top-level output contracts and exact safety annotations while
  materially shrinking.
- Hard-coded callers of retired names receive an unknown-tool error and must use
  a retained operation or the CLI.
- `get_related` requires exactly one symbol name or node ID and supports
  repository scoping. `regression_risk_report` requires a repository. Symbol
  listing rejects non-symbol node types.
- Operational health and developer checks remain owned by `scs status`,
  `scs doctor`, logs, and internal service methods that are still required.
- No repository source or persisted index data is changed. Rollback is a normal
  code revert and service restart.
