"""Deterministic hybrid code retrieval with optional bounded reranking."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from scs.graph.models import Node, NodeType, SearchResult
from scs.providers.base import (
    EmbeddingProvider,
    ProviderUnavailableError,
    RerankingProvider,
)

MAX_SEARCH_RESULTS: Final[int] = 200
MINIMUM_CANDIDATE_COUNT: Final[int] = 20
CANDIDATE_MULTIPLIER: Final[int] = 4
RECIPROCAL_RANK_CONSTANT: Final[int] = 60
MAX_RERANK_DOCUMENT_CHARS: Final[int] = 4_096
RetrievalMode = Literal[
    "none",
    "lexical",
    "semantic",
    "hybrid",
    "lexical_reranked",
    "semantic_reranked",
    "hybrid_reranked",
]


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
class CodeSearchMatch:
    """One fused search candidate with optional semantic and reranker scores."""

    node: Node
    fusion_score: float
    semantic_distance: float | None = None
    rerank_score: float | None = None


@dataclass(frozen=True, slots=True)
class CodeSearchResponse:
    """Ranked search matches plus truthful enrichment availability."""

    matches: list[CodeSearchMatch]
    semantic_available: bool
    retrieval_mode: RetrievalMode
    degraded_reason: str | None = None

    @property
    def nodes(self) -> list[Node]:
        """Retain the former node-only view for internal callers."""

        return [match.node for match in self.matches]


@dataclass(slots=True)
class _Candidate:
    """Mutable fusion state kept private until ordering is finalized."""

    node: Node
    fusion_score: float = 0.0
    semantic_distance: float | None = None
    semantic: bool = False
    lexical: bool = False


class CodeSearchService:
    """Fuse lexical and semantic candidates, then optionally rerank them."""

    def __init__(
        self,
        graph: SearchGraph,
        embeddings: EmbeddingProvider | None,
        reranker: RerankingProvider | None = None,
    ) -> None:
        self._graph: SearchGraph = graph
        self._embeddings: EmbeddingProvider | None = embeddings
        self._reranker: RerankingProvider | None = reranker

    async def search(
        self,
        query: str,
        *,
        node_type: NodeType | None = None,
        limit: int = 20,
        repo_id: int | None = None,
    ) -> CodeSearchResponse:
        """Return bounded fused matches and fail open around model enrichment."""

        result_limit = max(1, min(limit, MAX_SEARCH_RESULTS))
        candidate_limit = min(
            MAX_SEARCH_RESULTS,
            max(MINIMUM_CANDIDATE_COUNT, result_limit * CANDIDATE_MULTIPLIER),
        )
        lexical_task = asyncio.create_task(
            asyncio.to_thread(
                self._graph.search_by_name_sync,
                query,
                node_type=node_type,
                limit=candidate_limit,
                repo_id=repo_id,
            )
        )
        semantic_task = asyncio.create_task(
            self._semantic_candidates(
                query,
                node_type=node_type,
                limit=candidate_limit,
                repo_id=repo_id,
            )
        )
        lexical, semantic_result = await asyncio.gather(lexical_task, semantic_task)
        semantic, semantic_available, degraded_reason = semantic_result
        fused = self._fuse(semantic, lexical)
        mode = self._retrieval_mode(semantic=bool(semantic), lexical=bool(lexical))
        if not fused:
            return CodeSearchResponse([], semantic_available, "none", degraded_reason)

        reranker = self._reranker
        if reranker is None:
            return CodeSearchResponse(
                fused[:result_limit], semantic_available, mode, degraded_reason
            )
        try:
            ranked = await reranker.rerank(
                query,
                [self._rerank_document(match.node) for match in fused],
                limit=result_limit,
            )
        except (ProviderUnavailableError, OSError, RuntimeError, ValueError) as exc:
            return CodeSearchResponse(
                fused[:result_limit], semantic_available, mode, str(exc)
            )

        reranked = [
            CodeSearchMatch(
                node=fused[result.index].node,
                fusion_score=fused[result.index].fusion_score,
                semantic_distance=fused[result.index].semantic_distance,
                rerank_score=result.score,
            )
            for result in ranked
        ]
        reranked_modes: dict[RetrievalMode, RetrievalMode] = {
            "hybrid": "hybrid_reranked",
            "semantic": "semantic_reranked",
            "lexical": "lexical_reranked",
        }
        reranked_mode = reranked_modes[mode]
        return CodeSearchResponse(
            reranked, semantic_available, reranked_mode, degraded_reason
        )

    async def _semantic_candidates(
        self,
        query: str,
        *,
        node_type: NodeType | None,
        limit: int,
        repo_id: int | None,
    ) -> tuple[list[SearchResult], bool, str | None]:
        provider = self._embeddings
        if provider is None:
            return [], False, "embedding provider is not configured"
        try:
            vector = await provider.embed_query(query)
            matches = await asyncio.to_thread(
                self._graph.search_by_vector_sync,
                vector,
                node_type=node_type,
                limit=limit,
                repo_id=repo_id,
            )
        except (ProviderUnavailableError, OSError, RuntimeError, ValueError) as exc:
            return [], False, str(exc)
        return matches, True, None

    @staticmethod
    def _fuse(
        semantic: list[SearchResult], lexical: list[Node]
    ) -> list[CodeSearchMatch]:
        candidates: dict[str, _Candidate] = {}
        for rank, match in enumerate(semantic, start=1):
            candidate = candidates.setdefault(match.node.id, _Candidate(match.node))
            candidate.fusion_score += 1.0 / (RECIPROCAL_RANK_CONSTANT + rank)
            candidate.semantic_distance = match.distance
            candidate.semantic = True
        for rank, node in enumerate(lexical, start=1):
            candidate = candidates.setdefault(node.id, _Candidate(node))
            candidate.fusion_score += 1.0 / (RECIPROCAL_RANK_CONSTANT + rank)
            candidate.lexical = True

        ordered = sorted(
            candidates.values(),
            key=lambda candidate: (-candidate.fusion_score, candidate.node.id),
        )
        return [
            CodeSearchMatch(
                node=candidate.node,
                fusion_score=candidate.fusion_score,
                semantic_distance=candidate.semantic_distance,
            )
            for candidate in ordered
        ]

    @staticmethod
    def _retrieval_mode(*, semantic: bool, lexical: bool) -> RetrievalMode:
        if semantic and lexical:
            return "hybrid"
        if semantic:
            return "semantic"
        if lexical:
            return "lexical"
        return "none"

    @staticmethod
    def _rerank_document(node: Node) -> str:
        metadata = node.metadata
        qualified_name = metadata.get("qualified_name")
        signature = metadata.get("signature")
        file_path = metadata.get("file_path")
        structural = " ".join(
            str(part)
            for part in (node.type.value, qualified_name or node.name, signature, file_path)
            if part
        )
        content = node.content.strip()
        document = f"{structural}\n{content}" if content else structural
        return document[:MAX_RERANK_DOCUMENT_CHARS]
