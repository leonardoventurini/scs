# `query_code` migration

The seven former read-oriented MCP tools have been replaced by `query_code`.
The current MCP inventory is `query_code`, `ingest_project`, `ingest_files`,
`get_graph_stats`, and `delete_repository`. Calls to retired tool names receive
an unknown-tool error. Update hard-coded callers to send an investigation goal,
an absolute `repo_path`, and any known anchors to `query_code`.

| Retired MCP tool | `query_code` goal and anchor |
|---|---|
| `search_code` | Describe the symbol or behavior to find; optionally set `node_type`. |
| `graph_context` | Ask to understand a symbol's context; pass `node_ids` or `symbol_name` when known. |
| `get_related` | Ask for relationships; pass `node_ids` or `symbol_name`. |
| `list_symbols` | Ask for an inventory and pass a symbol `node_type`. |
| `inspect_file` | Ask to inspect files and pass `file_paths`. |
| `find_references` | Ask for references and pass `source_position`, `node_ids`, or `symbol_name`. |
| `regression_risk_report` | Ask for change impact and pass changed `file_paths`. |

For example, replace `find_references(file_path="/repo/src/main.py", line=42)`
with `query_code(goal="Find references to this symbol", repo_path="/repo",
source_position={"file_path": "src/main.py", "line": 42})`. The line remains
zero-based. Paths in `file_paths` and `source_position` must stay within the
repository. The default mode is `balanced`; `fast` and `thorough` are also
available.

`query_code` returns one envelope with `routing`, compact `evidence`, ordered
`trace`, `complete`, `truncated`, `degraded_stages`, and `timings`. Its evidence
categories are `symbols`, `files`, `relationships`, `references`, and
`test_targets`. Callers should read the selected playbook and completeness flags
before relying on the evidence. The response is not a byte-for-byte replacement
for any former tool's output.

The former tools' raw full-node search records, explicit query-angle controls,
relationship filters and directions, inventory pagination, and raw file-edge
lists are not `query_code` inputs. Callers needing those lower-level contracts
can use the corresponding SCSWire service routes. Those routes remain available
for orchestration, evaluation, and rollback; they are no longer model-facing MCP
tools.

The optional local classifier receives the goal and anchors only, never source
or retrieved evidence. Its failure returns bounded discovery evidence with a
degradation reason. A first request after daemon restart can take this fallback
while the lazy classifier starts; check `routing.degraded_reason` and retry if
the selected playbook is necessary. `ingest_project`, `ingest_files`,
`delete_repository`, and `get_graph_stats` remain separate MCP operations.

Run `just eval-query` to compare `query_code` with versioned internal-route
sequences on this repository. The v2 fast, balanced, and thorough five-tool
reports in `evals/reports/` passed the routing, evidence, latency, call-count,
and byte gates after wrapper removal.
