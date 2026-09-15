"""Headless MCP host exposing only SCS code-intelligence operations."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from typing import Literal, TypeVar, cast

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from scs.mcp.contracts import (
    GraphContextOutput,
    GraphStatsOutput,
    IngestionOutput,
    InspectFileOutput,
    ListSymbolsOutput,
    ReferencesOutput,
    RegressionRiskOutput,
    RepositoryDeletionOutput,
    RelatedOutput,
    SearchCodeOutput,
)
from scs.mcp.gateway import ServiceGateway
from scs.mcp.observability import ObservedMCPServer, ToolRecorder
from scs.mcp.paths import (
    canonical_repo_path,
    canonical_repository_identity,
    contained_deleted_path,
    contained_file_path,
)

MAX_RESULTS = 200
MAX_TRAVERSAL_DEPTH = 3
MINIMUM_INSPECT_LIMIT = 1
ToolInputT = TypeVar("ToolInputT")

# Query tools inspect only SCS-owned state derived from local repositories.
# Ingestion tools are separately annotated because they mutate the index even
# though the repository source itself remains immutable.
READ_ONLY_LOCAL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
INDEX_MUTATING_LOCAL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    open_world_hint=False,
)
DELETE_LOCAL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)


def _limit(value: int) -> int:
    return max(1, min(value, MAX_RESULTS))


def _validated(operation: Callable[[], ToolInputT]) -> ToolInputT:
    """Preserve actionable input errors across MCP's public tool boundary."""

    try:
        return operation()
    except ValueError as error:
        raise ToolError(str(error)) from error


def build_mcp(
    gateway: ServiceGateway,
    *,
    recorder: ToolRecorder | None = None,
) -> ObservedMCPServer:
    """Build an isolated MCP application over SCS's public service contract."""

    mcp = ObservedMCPServer("scs", recorder=recorder or ToolRecorder())

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    async def search_code(
        query: str,
        node_type: str | None = None,
        limit: int = 10,
        result_detail: Literal["full", "compact"] = "full",
        repo_path: str | None = None,
        queries: list[str] | None = None,
        search_mode: Literal["fast", "balanced", "thorough"] = "thorough",
    ) -> SearchCodeOutput:
        """Find code; use result node IDs with get_related for dependencies."""
        return cast(
            SearchCodeOutput,
            await gateway.call(
                "knowledge.search",
                {
                    "query": query,
                    "node_type": node_type,
                    "limit": _limit(limit),
                    "result_detail": result_detail,
                    "repo_path": _validated(lambda: canonical_repo_path(repo_path)),
                    "queries": queries,
                    "search_mode": search_mode,
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
        """Traverse dependencies from one search result node ID or symbol name."""
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
                    "repo_path": _validated(lambda: canonical_repo_path(repo_path)),
                },
            ),
        )

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    async def graph_context(
        query: str,
        node_type: str | None = None,
        vector_limit: int = 5,
        hop_limit: int = 2,
        direction: str = "both",
        repo_path: str | None = None,
        queries: list[str] | None = None,
        search_mode: Literal["fast", "balanced", "thorough"] = "thorough",
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
                    "direction": direction,
                    "repo_path": _validated(lambda: canonical_repo_path(repo_path)),
                    "queries": queries,
                    "search_mode": search_mode,
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
        """List an exhaustive page of indexed symbols of one code node type."""
        return cast(
            ListSymbolsOutput,
            await gateway.call(
                "knowledge.nodes.list",
                {
                    "node_type": node_type,
                    "limit": _limit(limit),
                    "offset": max(0, offset),
                    "repo_path": _validated(lambda: canonical_repo_path(repo_path)),
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
        repo = _validated(lambda: canonical_repo_path(repo_path))
        assert repo is not None
        files = [
            _validated(lambda path=path: contained_file_path(path, repo))
            for path in (file_paths or [])
        ]
        deleted = [
            _validated(lambda path=path: contained_deleted_path(path))
            for path in (deleted_paths or [])
        ]
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
                "repository.index",
                {"repo_path": _validated(lambda: canonical_repo_path(repo_path))},
            ),
        )

    @mcp.tool(annotations=DELETE_LOCAL)
    async def delete_repository(repo_path: str) -> RepositoryDeletionOutput:
        """Durably forget one repository while preserving its source files."""

        return cast(
            RepositoryDeletionOutput,
            await gateway.call(
                "repository.drop_index",
                {
                    "repo_path": _validated(
                        lambda: canonical_repository_identity(repo_path)
                    )
                },
            ),
        )

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    async def get_graph_stats(
        repo_path: str | None = None,
        wait_job_id: str | None = None,
        wait_timeout_seconds: float = 0.0,
    ) -> GraphStatsOutput:
        """Return readiness and progress; optionally wait up to 10s for one job."""
        return cast(
            GraphStatsOutput,
            await gateway.call(
                "knowledge.stats",
                {
                    "repo_path": _validated(lambda: canonical_repo_path(repo_path)),
                    "wait_job_id": wait_job_id,
                    "wait_timeout_seconds": min(
                        10.0,
                        max(0.0, wait_timeout_seconds),
                    ),
                },
            ),
        )

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    async def inspect_file(
        repo_path: str,
        file_path: str,
        node_limit: int = 50,
        edge_limit: int = 100,
    ) -> InspectFileOutput:
        """Inspect indexed symbols and edges after search identifies a source file."""
        repo = _validated(lambda: canonical_repo_path(repo_path))
        assert repo is not None
        source = _validated(lambda: contained_file_path(file_path, repo))
        return cast(
            InspectFileOutput,
            await gateway.call(
                "knowledge.inspect_file",
                {
                    "repo_path": repo,
                    "file_path": str(Path(source).relative_to(repo)),
                    "node_limit": max(MINIMUM_INSPECT_LIMIT, node_limit),
                    "edge_limit": max(MINIMUM_INSPECT_LIMIT, edge_limit),
                },
            ),
        )

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    async def regression_risk_report(
        repo_path: str, file_paths: list[str]
    ) -> RegressionRiskOutput:
        """Estimate dependent and test blast radius for changed source files."""
        repo = _validated(lambda: canonical_repo_path(repo_path))
        assert repo is not None
        paths = [
            _validated(lambda path=path: contained_file_path(path, repo))
            for path in file_paths
        ]
        return cast(
            RegressionRiskOutput,
            await gateway.call(
                "knowledge.composite.regression_risk",
                {"file_paths": paths, "repo_path": repo},
            ),
        )

    @mcp.tool(annotations=READ_ONLY_LOCAL)
    async def find_references(file_path: str, line: int) -> ReferencesOutput:
        """Find references to the narrowest symbol containing a zero-based line."""
        return cast(
            ReferencesOutput,
            await gateway.call(
                "lsp.references",
                {
                    "file_path": _validated(lambda: contained_file_path(file_path)),
                    "line": max(0, line),
                },
            ),
        )

    return mcp
