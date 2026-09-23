"""Exact model-facing SCS MCP tool inventory."""

MCP_TOOL_NAMES = frozenset(
    {
        "query_code",
        "ingest_files",
        "ingest_project",
        "delete_repository",
        "get_graph_stats",
    }
)
