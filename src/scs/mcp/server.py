"""Headless MCP host exposing only SCS code-intelligence operations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, TypeVar, cast

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from scs.mcp.contracts import (
    GraphStatsOutput,
    IngestionOutput,
    RepositoryDeletionOutput,
)
from scs.mcp.gateway import ServiceGateway
from scs.mcp.observability import ObservedMCPServer, ToolRecorder
from scs.mcp.paths import (
    canonical_repo_path,
    canonical_repository_identity,
    contained_deleted_path,
    contained_file_path,
)
from scs.orchestration.decision import SourcePosition
from scs.orchestration.query import QueryCodeOutput, QueryRequest

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
    async def query_code(
        goal: str,
        repo_path: str,
        mode: Literal["fast", "balanced", "thorough"] = "balanced",
        node_type: str | None = None,
        symbol_name: str | None = None,
        node_ids: list[str] | None = None,
        file_paths: list[str] | None = None,
        source_position: SourcePosition | None = None,
        limit: int = 10,
    ) -> QueryCodeOutput:
        """Investigate a code goal with one bounded, inspectable playbook."""

        request = _validated(
            lambda: QueryRequest(
                goal=goal,
                repo_path=repo_path,
                mode=mode,
                node_type=node_type,
                symbol_name=symbol_name,
                node_ids=node_ids or [],
                file_paths=file_paths or [],
                source_position=source_position,
                limit=limit,
            )
        )
        return cast(
            QueryCodeOutput,
            await gateway.call("knowledge.query", cast(dict[str, object], request.model_dump(mode="json"))),
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

    return mcp
