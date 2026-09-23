"""Exact model-facing SCS MCP tool inventory."""

MCP_TOOL_NAMES = frozenset(
    {
        "query_code",
        "search_code",
        "get_related",
        "graph_context",
        "list_symbols",
        "ingest_files",
        "ingest_project",
        "delete_repository",
        "get_graph_stats",
        "inspect_file",
        "regression_risk_report",
        "find_references",
    }
)
