"""Hybrid code search with truthful semantic degradation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from scs.graph.models import Node, NodeType, SearchResult
from scs.providers.base import EmbeddingProvider, ProviderUnavailableError


class SearchGraph(Protocol):
    """Graph queries required by the hybrid search service."""

    def search_by_name_sync(
        self, query: str, *, node_type: NodeType | None, limit: int, repo_id: int | None
    ) -> list[Node]: ...
    def search_by_vector_sync(
        self,
        vector: list[float],
        *,
        node_type: NodeType | None,
        limit: int,
        repo_id: int | None,
    ) -> list[SearchResult]: ...


@dataclass(frozen=True, slots=True)
class CodeSearchResponse:
    """Search results plus an explicit semantic availability signal."""

    nodes: list[Node]
    semantic_available: bool
    degraded_reason: str | None = None


class CodeSearchService:
    """Prefer semantic matches and always retain lexical code search."""

    def __init__(
        self, graph: SearchGraph, embeddings: EmbeddingProvider | None
    ) -> None:
        self._graph: SearchGraph = graph
        self._embeddings: EmbeddingProvider | None = embeddings

    async def search(
        self,
        query: str,
        *,
        node_type: NodeType | None = None,
        limit: int = 20,
        repo_id: int | None = None,
    ) -> CodeSearchResponse:
        """Return deduplicated semantic then lexical code-only matches."""

        lexical = await asyncio.to_thread(
            self._graph.search_by_name_sync,
            query,
            node_type=node_type,
            limit=limit,
            repo_id=repo_id,
        )
        provider = self._embeddings
        if provider is None:
            return CodeSearchResponse(lexical, False, "embedding provider is not configured")
        try:
            vector = await provider.embed_query(query)
            semantic = await asyncio.to_thread(
                self._graph.search_by_vector_sync,
                vector,
                node_type=node_type,
                limit=limit,
                repo_id=repo_id,
            )
        except (ProviderUnavailableError, OSError, RuntimeError) as exc:
            return CodeSearchResponse(lexical, False, str(exc))
        ordered = [match.node for match in semantic]
        known = {node.id for node in ordered}
        ordered.extend(node for node in lexical if node.id not in known)
        return CodeSearchResponse(ordered[:limit], True)
