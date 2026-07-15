"""Headless MCP host exposing only SCS code-intelligence operations."""

from __future__ import annotations

from pathlib import Path

from scs.mcp.gateway import ServiceGateway
from scs.mcp.inventory import MOVED_TO_SCS_TOOLS
from scs.mcp.observability import ObservedFastMCP, ToolRecorder
from scs.mcp.paths import (
    canonical_repo_path,
    contained_deleted_path,
    contained_file_path,
)

MAX_RESULTS = 200
MAX_TRAVERSAL_DEPTH = 3


def _limit(value: int) -> int:
    return max(1, min(value, MAX_RESULTS))


def build_mcp(
    gateway: ServiceGateway,
    *,
    recorder: ToolRecorder | None = None,
) -> ObservedFastMCP:
    """Build an isolated MCP application over SCS's public service contract."""

    mcp = ObservedFastMCP("scs", recorder=recorder or ToolRecorder())

    @mcp.tool()
    async def search_code(
        query: str,
        node_type: str | None = None,
        limit: int = 10,
        repo_path: str | None = None,
    ) -> dict[str, object]:
        """Search indexed code using semantic and lexical retrieval."""
        return await gateway.call(
            "knowledge.search",
            {
                "query": query,
                "node_type": node_type,
                "limit": _limit(limit),
                "repo_path": canonical_repo_path(repo_path),
            },
        )

    @mcp.tool()
    async def get_related(
        symbol_name: str,
        depth: int = 2,
        relationship: str | None = None,
        direction: str = "outgoing",
    ) -> dict[str, object]:
        """Traverse relationships around an indexed code symbol."""
        return await gateway.call(
            "knowledge.related",
            {
                "symbol_name": symbol_name,
                "depth": max(1, min(depth, MAX_TRAVERSAL_DEPTH)),
                "relationship": relationship,
                "direction": direction,
            },
        )

    @mcp.tool()
    async def graph_context(
        query: str,
        node_type: str | None = None,
        vector_limit: int = 5,
        hop_limit: int = 2,
        repo_path: str | None = None,
    ) -> dict[str, object]:
        """Combine code search seeds with bounded graph traversal."""
        return await gateway.call(
            "knowledge.graph_context",
            {
                "query": query,
                "node_type": node_type,
                "vector_limit": _limit(vector_limit),
                "hop_limit": max(1, min(hop_limit, MAX_TRAVERSAL_DEPTH)),
                "repo_path": canonical_repo_path(repo_path),
            },
        )

    @mcp.tool()
    async def list_symbols(
        node_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        repo_path: str | None = None,
    ) -> dict[str, object]:
        """List indexed code symbols with stable pagination."""
        return await gateway.call(
            "knowledge.nodes.list",
            {
                "node_type": node_type,
                "limit": _limit(limit),
                "offset": max(0, offset),
                "repo_path": canonical_repo_path(repo_path),
            },
        )

    @mcp.tool()
    async def ingest_files(
        repo_path: str,
        file_paths: list[str] | None = None,
        deleted_paths: list[str] | None = None,
    ) -> dict[str, object]:
        """Queue explicit changed and deleted source files for indexing."""
        repo = canonical_repo_path(repo_path)
        assert repo is not None
        files = [contained_file_path(path, repo) for path in (file_paths or [])]
        deleted = [contained_deleted_path(path) for path in (deleted_paths or [])]
        if not files and not deleted:
            raise ValueError("at least one changed or deleted file is required")
        return await gateway.call(
            "repository.ingest_files",
            {"repo_path": repo, "file_paths": files, "deleted_paths": deleted},
        )

    @mcp.tool()
    async def ingest_git_history(repo_path: str) -> dict[str, object]:
        """Queue read-only ingestion of repository commit provenance."""
        return await gateway.call(
            "repository.ingest_git_history",
            {"repo_path": canonical_repo_path(repo_path)},
        )

    @mcp.tool()
    async def ingest_project(repo_path: str) -> dict[str, object]:
        """Queue an explicit full indexing pass for one repository."""
        return await gateway.call(
            "repository.index", {"repo_path": canonical_repo_path(repo_path)}
        )

    @mcp.tool()
    async def get_graph_stats() -> dict[str, object]:
        """Return code graph and repository ingestion statistics."""
        return await gateway.call("knowledge.stats")

    @mcp.tool()
    async def inspect_graph_quality(repo_path: str | None = None) -> dict[str, object]:
        """Report code graph coverage and freshness."""
        return await gateway.call(
            "knowledge.inspect", {"repo_path": canonical_repo_path(repo_path)}
        )

    @mcp.tool()
    async def sample_nodes(
        node_type: str = "",
        summary_status: str = "",
        file_path: str = "",
        limit: int = 10,
        repo_path: str | None = None,
    ) -> dict[str, object]:
        """Sample indexed code nodes for parser and index inspection."""
        return await gateway.call(
            "knowledge.sample",
            {
                "node_type": node_type or None,
                "summary_status": summary_status or None,
                "file_path": file_path or None,
                "limit": _limit(limit),
                "repo_path": canonical_repo_path(repo_path),
            },
        )

    @mcp.tool()
    async def inspect_file(repo_path: str, file_path: str) -> dict[str, object]:
        """Inspect indexed entities and edges for one repository file."""
        repo = canonical_repo_path(repo_path)
        assert repo is not None
        source = contained_file_path(file_path, repo)
        return await gateway.call(
            "knowledge.inspect_file",
            {"repo_path": repo, "file_path": str(Path(source).relative_to(repo))},
        )

    @mcp.tool()
    async def test_coverage_map(
        node_type: str = "function", limit: int = 50, repo_path: str | None = None
    ) -> dict[str, object]:
        """Estimate structural test coverage from code graph references."""
        return await gateway.call(
            "knowledge.composite.test_coverage",
            {
                "node_type": node_type,
                "limit": _limit(limit),
                "repo_path": canonical_repo_path(repo_path),
            },
        )

    @mcp.tool()
    async def regression_risk_report(
        file_paths: list[str], repo_path: str | None = None
    ) -> dict[str, object]:
        """Estimate dependent and test blast radius for changed source files."""
        repo = canonical_repo_path(repo_path)
        paths = (
            [contained_file_path(path, repo) for path in file_paths]
            if repo
            else file_paths
        )
        return await gateway.call(
            "knowledge.composite.regression_risk",
            {"file_paths": paths, "repo_path": repo},
        )

    @mcp.tool()
    async def consistency_check(
        file_path: str, repo_path: str | None = None
    ) -> dict[str, object]:
        """Compare one source file's symbols with neighboring conventions."""
        repo = canonical_repo_path(repo_path)
        path = contained_file_path(file_path, repo) if repo else file_path
        return await gateway.call(
            "knowledge.composite.consistency_check",
            {"file_path": path, "repo_path": repo},
        )

    @mcp.tool()
    async def contract_check(
        symbol_name: str, repo_path: str | None = None
    ) -> dict[str, object]:
        """Show incoming code relationships that form a symbol contract."""
        return await gateway.call(
            "knowledge.composite.contract_check",
            {"symbol_name": symbol_name, "repo_path": canonical_repo_path(repo_path)},
        )

    @mcp.tool()
    async def get_symbols_overview(file_path: str) -> dict[str, object]:
        """Return read-only LSP symbols for a source file."""
        return await gateway.call(
            "lsp.symbols", {"file_path": contained_file_path(file_path)}
        )

    @mcp.tool()
    async def find_symbol(name: str, file_path: str | None = None) -> dict[str, object]:
        """Find indexed or live read-only definitions for a symbol."""
        return await gateway.call(
            "lsp.find_symbol",
            {
                "name": name,
                "file_path": contained_file_path(file_path) if file_path else None,
            },
        )

    @mcp.tool()
    async def find_references(
        file_path: str, line: int, column: int
    ) -> dict[str, object]:
        """Find read-only LSP references at a source position."""
        return await gateway.call(
            "lsp.references",
            {
                "file_path": contained_file_path(file_path),
                "line": max(0, line),
                "column": max(0, column),
            },
        )

    @mcp.tool()
    async def get_symbol_info(
        file_path: str, line: int, column: int
    ) -> dict[str, object]:
        """Return read-only LSP hover information for a source position."""
        return await gateway.call(
            "lsp.hover",
            {
                "file_path": contained_file_path(file_path),
                "line": max(0, line),
                "column": max(0, column),
            },
        )

    @mcp.tool()
    async def search_knowledge(
        query: str,
        node_type: str | None = None,
        limit: int = 10,
        include_neighbors: bool = False,
        repo_path: str | None = None,
    ) -> dict[str, object]:
        """Search only code and repository-provenance nodes."""
        return await gateway.call(
            "knowledge.search",
            {
                "query": query,
                "node_type": node_type,
                "limit": _limit(limit),
                "include_neighbors": include_neighbors,
                "repo_path": canonical_repo_path(repo_path),
                "data_scope": "code_and_provenance",
            },
        )

    @mcp.tool()
    async def get_node_detail(node_id: str) -> dict[str, object]:
        """Return one indexed code or repository-provenance node."""
        return await gateway.call(
            "knowledge.nodes.get",
            {
                "node_id": node_id,
                "include_edges": True,
                "data_scope": "code_and_provenance",
            },
        )

    @mcp.tool()
    async def scs_diagnostics_snapshot(include_logs: bool = False) -> dict[str, object]:
        """Return a broad read-only SCS runtime snapshot."""
        snapshot = await gateway.call(
            "diagnostics.snapshot", {"include_logs": include_logs}
        )
        return {**snapshot, "mcp": mcp.recorder.snapshot()}

    @mcp.tool()
    async def scs_mcp_health() -> dict[str, object]:
        """Check the SCS daemon plus MCP ownership and inventory."""
        health = await gateway.call("system.health")
        return {**health, "mcp_tools": sorted(MOVED_TO_SCS_TOOLS)}

    @mcp.tool()
    async def scs_recent_failures(limit: int = 50) -> dict[str, object]:
        """Return recent classified SCS runtime failures."""
        return await gateway.call(
            "diagnostics.recent_failures", {"limit": _limit(limit)}
        )

    @mcp.tool()
    async def scs_index_health(
        repo_path: str | None = None, include_quality: bool = False
    ) -> dict[str, object]:
        """Report code-index and optional repository readiness."""
        return await gateway.call(
            "diagnostics.index_health",
            {
                "repo_path": canonical_repo_path(repo_path),
                "include_quality": include_quality,
            },
        )

    @mcp.tool()
    async def scs_dev_doctor(repo_path: str | None = None) -> dict[str, object]:
        """Check SCS development prerequisites and owned paths."""
        return await gateway.call(
            "diagnostics.dev_doctor", {"repo_path": canonical_repo_path(repo_path)}
        )

    @mcp.tool()
    async def scs_test_recommendations(
        changed_files: list[str] | None = None,
    ) -> dict[str, object]:
        """Recommend targeted SCS validation for changed files."""
        return await gateway.call(
            "diagnostics.test_recommendations", {"changed_files": changed_files or []}
        )

    return mcp
