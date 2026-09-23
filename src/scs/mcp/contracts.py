"""Structured result contracts for the public SCS MCP tools."""

from __future__ import annotations

from typing import Literal, TypedDict


class IngestionOutput(TypedDict):
    """Acknowledgement returned after durable ingestion work is queued."""

    accepted: bool
    job: dict[str, object]


class RepositoryDeletionOutput(TypedDict):
    """Acknowledgement or idempotent no-op for repository deletion."""

    accepted: bool
    already_absent: bool
    job: dict[str, object] | None


class JobWaitOutput(TypedDict):
    """Outcome of bounded observation for one repository-scoped job."""

    outcome: Literal["terminal", "timeout", "not_found"]
    job: dict[str, object] | None


class GraphStatsOutput(TypedDict):
    """Stable readiness and storage statistics for an optional repository scope."""

    repo_path: str | None
    status: str
    total_nodes: int
    nodes_by_type: dict[str, int]
    embedding_count: int
    vector_index_count: int
    vector_index_scope: str
    ingestion_stats: dict[str, dict[str, object]]
    database_size_bytes: int
    vector_available: bool
    vector_unavailable_reason: str | None
    structural_search_ready: bool
    semantic_search_ready: bool
    semantic_search_unavailable_reason: str | None
    active_job: dict[str, object] | None
    latest_job: dict[str, object] | None
    retry_after_ms: int | None
    wait: JobWaitOutput | None
