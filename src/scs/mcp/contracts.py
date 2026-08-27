"""Structured result contracts for the public SCS MCP tools."""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal, TypeAlias, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class SearchCodeOutput(TypedDict):
    """Stable top-level shape returned by semantic and lexical code search."""

    query: str
    results: list[dict[str, object]]
    neighbors: list[dict[str, object]]
    total: int
    retrieval_mode: str


class RelatedOutput(TypedDict):
    """Stable top-level shape returned by bounded relationship traversal."""

    symbol_name: str | None
    node_id: str | None
    matches: list[dict[str, object]]
    related: list[dict[str, object]]


class GraphContextOutput(TypedDict):
    """Stable top-level shape returned by search-seeded graph context."""

    query: str
    direction: str
    seeds: list[dict[str, object]]
    context: list[dict[str, object]]


class ListSymbolsOutput(TypedDict):
    """Stable top-level shape returned by paginated symbol inventory."""

    nodes: list[dict[str, object]]
    total: int
    limit: int
    offset: int


class IngestionOutput(TypedDict):
    """Acknowledgement returned after durable ingestion work is queued."""

    accepted: bool
    job: dict[str, object]


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
    semantic_search_ready: bool
    semantic_search_unavailable_reason: str | None


class InspectFileOutput(TypedDict):
    """Stable indexed entities and edges associated with one source file."""

    repo_path: str
    file_path: str
    nodes: list[dict[str, object]]
    edges: dict[str, list[dict[str, object]]]
    nodes_truncated: bool
    edges_truncated: bool


class RegressionRiskOutput(TypedDict):
    """Stable dependent-symbol blast radius for a set of changed files."""

    file_paths: list[str]
    affected_node_ids: list[str]
    dependents: list[dict[str, object]]
    test_dependents: list[dict[str, object]]


class _ReferenceOutput(BaseModel):
    """Reject extra fields so each public reference-result variant stays exact."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    source: str


class AvailableReferencesOutput(_ReferenceOutput):
    """References resolved from SCS's indexed structural graph."""

    available: Literal[True]
    symbol: dict[str, object]
    references: list[dict[str, object]]


class UnavailableReferencesOutput(_ReferenceOutput):
    """Typed explanation for a source position without an indexed symbol."""

    available: Literal[False]
    file_path: str
    reason: str
    language_server_configured: bool


ReferencesOutput: TypeAlias = Annotated[
    AvailableReferencesOutput | UnavailableReferencesOutput,
    Field(discriminator="available"),
]
