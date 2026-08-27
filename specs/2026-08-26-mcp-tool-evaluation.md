# Live SCS MCP tool evaluation

## Scope and environment

Evaluation date: 2026-08-26

Project: `scs`

Project root: `/Users/leonardo/Repositories/mentagen/scs`

The local SCS daemon indexed this checkout successfully before evaluation:
111 files, 2,322 nodes, and 2,004 created edges. All ten public MCP tools were
exercised or, for the two background tools, their live acknowledgement and
durable completed job were observed. The worktree was clean before evaluation.

The MLX embedding provider was unavailable (`No module named
mlx_embedding_models`), so vector-semantic retrieval quality could not be
evaluated. Structural and lexical behavior was evaluated normally.

## Method and tool matrix

- `get_graph_stats` — passed. Project scope reported `status: ready`, 2,322
  nodes, 111 ingested files, zero embeddings, and a usable vector store.
- `list_symbols` — passed. Stable first page returned 20 of 325 function
  nodes with content, locations, and qualified names.
- `search_code` — passed. Exact lexical query `IngestionPipeline` returned five
  import references. A natural-language query returned no results, expected in
  the no-embedding environment rather than a defect.
- `graph_context` — passed its response contract. The exact query produced five
  seeds but no traversal context; see Improvement 3.
- `get_related` — passed node-ID traversal, returning the selected node and its
  incoming file relationship. The exact-one-selector and empty-repository
  validation failures were correctly rejected.
- `inspect_file` — passed. `src/scs/indexing/pipeline.py` returned 59 nodes,
  59 edge keys, and 116 edges; see Improvement 4.
- `find_references` — failed through the public MCP transport; see P1.
- `regression_risk_report` — returned a structurally valid report; see P2 for
  a correctness defect in dependent selection.
- `ingest_project` — passed. Job `ingest_212bf694ad21` completed with 111
  discovered/changed files, 2,322 entities, and no failed files.
- `ingest_files` — passed. Job `ingest_7ae14e6e1d79` completed for the existing
  `pipeline.py` without a content change, confirming asynchronous
  acknowledgement and no-op incremental processing.

## Findings

### P1 — `find_references` cannot return a usable MCP result

An indexed request for `src/scs/indexing/pipeline.py` at line 95 returned
`Output validation error: None is not of type 'string'` from the MCP adapter.
The raw SCSWire call to `lsp.references` succeeds and returns the symbol plus
one indexed reference, so the failure is in the public output contract rather
than reference resolution.

`ReferencesOutput` models mutually exclusive fields through `NotRequired`
fields in `src/scs/mcp/contracts.py`. The available service variant omits the
unavailable-only fields, while FastMCP's generated schema validates their
implicit `null` defaults against non-null types. The unavailable variant has
the reciprocal failure.

Impact: one of ten advertised public tools is unusable by MCP clients.

Recommended change: replace the optional-field TypedDict with discriminated
Pydantic available/unavailable result models, or make the omitted fields
explicitly nullable and return a consistent full shape. Add live MCP transport
tests for both variants that assert no tool error and exact variant fields.

### P2 — regression-risk reports include containment as a dependent

The report for `src/scs/indexing/pipeline.py` included its own file node in
both `affected_node_ids` and `dependents`. This is caused by incoming
file-to-entity `contains` edges being treated as a dependency, without
excluding affected IDs or filtering relationship semantics.

Impact: a report can appear to have downstream impact when it has no external
dependent. It inflates blast radius and makes automation less trustworthy.

Recommended change: exclude all affected node IDs from dependents and only
accept intended dependency/reference edge kinds. Add a regression test proving
a file-to-symbol containment edge does not create a dependent while an external
reference edge does.

## Improvements

### P3 — distinguish symbol lookup from generic entity-name lookup

`get_related(symbol_name=...)` searches unrestricted indexed node types. A
name can resolve to both an import node and a declaration, even though the tool
description calls it a symbol lookup. Restrict the selector to symbol node
types, or rename/document it as an entity-name lookup and add a deterministic
disambiguator.

### P3 — make graph-context traversal direction intentional

`graph_context` found five `IngestionPipeline` import seeds but no context,
because it uses the graph traversal default direction. `get_related` found
incoming parent relationships for comparable seeds. Traverse both directions
for context, add a direction parameter, or document the intentional outgoing
only behavior and test it.

### P3 — report semantic-search readiness separately from vector-store health

Stats said `vector_available: true` while `embedding_count` and
`vector_index_count` were zero. That correctly describes an available store,
but it is easy to misread as semantic search being ready. Add an explicit
`semantic_search_ready` boolean plus degradation reason.

### P3 — bound `inspect_file` response size

One ordinary Python file returned 59 nodes and 116 edges, with no pagination or
response-size control. Add optional node/edge limits with truncation metadata
before a large generated or central file exhausts client context.

### P4 — normalize path-validation errors

Some invalid paths fail during adapter path resolution before the service can
return its domain-specific containment error. Preserve safety, but normalize
the public error shape so clients can reliably distinguish an unreadable path
from an out-of-repository path.

## Verification and recovery

Both indexed jobs reached `completed` in the durable job store. No repository
source mutation was observed: `git diff --exit-code` passed and the worktree
contains only this evaluation's documentation. No remediation is applied in
this evaluation; each change above remains a separately scoped implementation
decision.
