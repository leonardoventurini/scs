# `query_code` migration

`query_code` is available beside the seven existing read tools during Phase A.
Pass an investigation goal, an absolute `repo_path`, and any known anchors.
The selected playbook, evidence, stage trace, and completeness flags are returned
in one envelope. The default mode is `balanced`.

| Existing tool | `query_code` goal and anchor |
|---|---|
| `search_code` | Describe the symbol or behavior to find; optionally set `node_type`. |
| `graph_context` | Ask to understand a symbol's context; pass `node_ids` or `symbol_name` when known. |
| `get_related` | Ask for relationships; pass `node_ids` or `symbol_name`. |
| `list_symbols` | Ask for an inventory and pass `node_type`. |
| `inspect_file` | Ask to inspect files and pass `file_paths`. |
| `find_references` | Ask for references and pass `source_position`, `node_ids`, or `symbol_name`. |
| `regression_risk_report` | Ask for change impact and pass changed `file_paths`. |

Paths in `file_paths` and `source_position` must stay within the repository.
`source_position.line` is zero-based. The goal and anchors are sent only to the
optional local classifier; repository source and retrieved evidence are not.
Classifier failure returns bounded discovery evidence with a degradation reason.

`query_code` does not cover `ingest_project`, `ingest_files`,
`delete_repository`, or `get_graph_stats`. The existing read tools remain
available until the compatibility and live evaluation gates pass in a breaking
release. The versioned evaluation suite can be run with `just eval-query`.
