"""Headless MCP host exposing only SCS code-intelligence operations."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from mcp.types import ToolAnnotations

from scs.mcp.contracts import (
    GraphContextOutput,
    GraphStatsOutput,
    IngestionOutput,
    InspectFileOutput,
    ListSymbolsOutput,
    ReferencesOutput,
    RegressionRiskOutput,
    RelatedOutput,
    SearchCodeOutput,
)
from scs.mcp.gateway import ServiceGateway
from scs.mcp.observability import ObservedFastMCP, ToolRecorder
from scs.mcp.paths import (
    canonical_repo_path,
    contained_deleted_path,
    contained_file_path,
)

MAX_RESULTS = 200
MAX_TRAVERSAL_DEPTH = 3

# Query tools inspect only SCS-owned state derived from local repositories.
# Ingestion tools are separately annotated because they mutate the index even
# though the repository source itself remains immutable.
READ_ONLY_LOCAL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
INDEX_MUTATING_LOCAL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    openWorldHint=False,
)


def _limit(value: int) -> int:
    return max(1, min(value, MAX_RESULTS))


def build_mcp(
    gateway: ServiceGateway,
    *,
    recorder: ToolRecorder | None = None,
) -> ObservedFastMCP:
    """Build an isolated MCP application over SCS's public service contract."""

    mcp = ObservedFastMCP("scs", recorder=recorder or ToolRecorder())

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    async def search_code(
        query: str,
        node_type: str | None = None,
        limit: int = 10,
        repo_path: str | None = None,
    ) -> SearchCodeOutput:
        """Search indexed code using semantic and lexical retrieval."""
        return cast(
            SearchCodeOutput,
            await gateway.call(
                "knowledge.search",
                {
                    "query": query,
                    "node_type": node_type,
                    "limit": _limit(limit),
                    "repo_path": canonical_repo_path(repo_path),
                },
            ),
        )

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    async def get_related(
        symbol_name: str | None = None,
        node_id: str | None = None,
        depth: int = 2,
        relationship: str | None = None,
        direction: str = "outgoing",
        repo_path: str | None = None,
    ) -> RelatedOutput:
        """Traverse relationships from exactly one symbol name or node ID."""
        return cast(
            RelatedOutput,
            await gateway.call(
                "knowledge.related",
                {
                    "symbol_name": symbol_name,
                    "node_id": node_id,
                    "depth": max(1, min(depth, MAX_TRAVERSAL_DEPTH)),
                    "relationship": relationship,
                    "direction": direction,
                    "repo_path": canonical_repo_path(repo_path),
                },
            ),
        )

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    async def graph_context(
        query: str,
        node_type: str | None = None,
        vector_limit: int = 5,
        hop_limit: int = 2,
        repo_path: str | None = None,
    ) -> GraphContextOutput:
        """Combine code search seeds with bounded graph traversal."""
        return cast(
            GraphContextOutput,
            await gateway.call(
                "knowledge.graph_context",
                {
                    "query": query,
                    "node_type": node_type,
                    "vector_limit": _limit(vector_limit),
                    "hop_limit": max(1, min(hop_limit, MAX_TRAVERSAL_DEPTH)),
                    "repo_path": canonical_repo_path(repo_path),
                },
            ),
        )

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    async def list_symbols(
        node_type: str = "function",
        limit: int = 50,
        offset: int = 0,
        repo_path: str | None = None,
    ) -> ListSymbolsOutput:
        """List indexed code symbols with stable pagination."""
        return cast(
            ListSymbolsOutput,
            await gateway.call(
                "knowledge.nodes.list",
                {
                    "node_type": node_type,
                    "limit": _limit(limit),
                    "offset": max(0, offset),
                    "repo_path": canonical_repo_path(repo_path),
                },
            ),
        )

    @mcp.tool(annotations=INDEX_MUTATING_LOCAL)
    async def ingest_files(
        repo_path: str,
        file_paths: list[str] | None = None,
        deleted_paths: list[str] | None = None,
    ) -> IngestionOutput:
        """Queue explicit changed and deleted source files for indexing."""
        repo = canonical_repo_path(repo_path)
        assert repo is not None
        files = [contained_file_path(path, repo) for path in (file_paths or [])]
        deleted = [contained_deleted_path(path) for path in (deleted_paths or [])]
        if not files and not deleted:
            raise ValueError("at least one changed or deleted file is required")
        return cast(
            IngestionOutput,
            await gateway.call(
                "repository.ingest_files",
                {"repo_path": repo, "file_paths": files, "deleted_paths": deleted},
            ),
        )

    @mcp.tool(annotations=INDEX_MUTATING_LOCAL)
    async def ingest_project(repo_path: str) -> IngestionOutput:
        """Queue an explicit full indexing pass for one repository."""
        return cast(
            IngestionOutput,
            await gateway.call(
                "repository.index", {"repo_path": canonical_repo_path(repo_path)}
            ),
        )

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    async def get_graph_stats(repo_path: str | None = None) -> GraphStatsOutput:
        """Return index readiness and graph statistics, optionally for one repository."""
        return cast(
            GraphStatsOutput,
            await gateway.call(
                "knowledge.stats", {"repo_path": canonical_repo_path(repo_path)}
            ),
        )

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    async def inspect_file(repo_path: str, file_path: str) -> InspectFileOutput:
        """Inspect indexed entities and edges for one repository file."""
        repo = canonical_repo_path(repo_path)
        assert repo is not None
        source = contained_file_path(file_path, repo)
        return cast(
            InspectFileOutput,
            await gateway.call(
                "knowledge.inspect_file",
                {"repo_path": repo, "file_path": str(Path(source).relative_to(repo))},
            ),
        )

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    async def regression_risk_report(
        repo_path: str, file_paths: list[str]
    ) -> RegressionRiskOutput:
        """Estimate dependent and test blast radius for changed source files."""
        repo = canonical_repo_path(repo_path)
        assert repo is not None
        paths = [contained_file_path(path, repo) for path in file_paths]
        return cast(
            RegressionRiskOutput,
            await gateway.call(
                "knowledge.composite.regression_risk",
                {"file_paths": paths, "repo_path": repo},
            ),
        )

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    async def find_references(file_path: str, line: int) -> ReferencesOutput:
        """Find indexed references to the narrowest symbol containing a source line."""
        return cast(
            ReferencesOutput,
            await gateway.call(
                "lsp.references",
                {
                    "file_path": contained_file_path(file_path),
                    "line": max(0, line),
                },
            ),
        )

    return mcp
