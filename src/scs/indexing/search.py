"""Deterministic hybrid code retrieval with optional bounded reranking."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import perf_counter
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
MAX_SEARCH_QUERIES: Final[int] = 5
BALANCED_RERANK_TIMEOUT_SECONDS: Final[float] = 5.0
THOROUGH_RERANK_TIMEOUT_SECONDS: Final[float] = 30.0
SearchMode = Literal["fast", "balanced", "thorough"]
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
    matched_query_indexes: tuple[int, ...] = (0,)


@dataclass(frozen=True, slots=True)
class SearchTimings:
    """Wall-clock milliseconds spent in each material retrieval stage."""

    lexical_ms: float = 0.0
    embedding_ms: float = 0.0
    vector_ms: float = 0.0
    rerank_ms: float = 0.0
    total_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class CodeSearchResponse:
    """Ranked search matches plus truthful enrichment availability."""

    matches: list[CodeSearchMatch]
    semantic_available: bool
    retrieval_mode: RetrievalMode
    degraded_reason: str | None = None
    queries: tuple[str, ...] = ()
    reranker_applied: bool = False
    degraded_stage: Literal["semantic", "rerank"] | None = None
    timed_out: bool = False
    timings: SearchTimings = SearchTimings()

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
    matched_query_indexes: set[int] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _QueryResult:
    """Candidate evidence and timings produced for one query angle."""

    matches: list[CodeSearchMatch]
    semantic_available: bool
    retrieval_mode: RetrievalMode
    degraded_reason: str | None
    lexical_ms: float
    embedding_ms: float
    vector_ms: float


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
        queries: list[str] | None = None,
        search_mode: SearchMode = "thorough",
    ) -> CodeSearchResponse:
        """Return bounded fused matches and fail open around model enrichment."""

        started = perf_counter()
        effective_queries = self._effective_queries(query, queries)
        if search_mode not in {"fast", "balanced", "thorough"}:
            raise ValueError("search_mode must be fast, balanced, or thorough")

        result_limit = max(1, min(limit, MAX_SEARCH_RESULTS))
        candidate_limit = min(
            MAX_SEARCH_RESULTS,
            max(MINIMUM_CANDIDATE_COUNT, result_limit * CANDIDATE_MULTIPLIER),
        )
        query_results = await asyncio.gather(
            *(
                self._retrieve_query(
                    active_query,
                    query_index=query_index,
                    node_type=node_type,
                    limit=candidate_limit,
                    repo_id=repo_id,
                )
                for query_index, active_query in enumerate(effective_queries)
            )
        )
        fused = self._merge_query_matches(query_results)
        semantic_available = all(item.semantic_available for item in query_results)
        degraded_reason = next(
            (item.degraded_reason for item in query_results if item.degraded_reason),
            None,
        )
        mode = self._retrieval_mode(
            semantic=any(
                "semantic" in item.retrieval_mode or "hybrid" in item.retrieval_mode
                for item in query_results
            ),
            lexical=any(
                "lexical" in item.retrieval_mode or "hybrid" in item.retrieval_mode
                for item in query_results
            ),
        )
        lexical_ms = sum(item.lexical_ms for item in query_results)
        embedding_ms = sum(item.embedding_ms for item in query_results)
        vector_ms = sum(item.vector_ms for item in query_results)
        if not fused:
            return self._response(
                [],
                semantic_available,
                "none",
                degraded_reason,
                queries=effective_queries,
                degraded_stage="semantic" if degraded_reason else None,
                lexical_ms=lexical_ms,
                embedding_ms=embedding_ms,
                vector_ms=vector_ms,
                started=started,
            )

        reranker = self._reranker
        if reranker is None or search_mode == "fast":
            return self._response(
                fused[:result_limit],
                semantic_available,
                mode,
                degraded_reason,
                queries=effective_queries,
                degraded_stage="semantic" if degraded_reason else None,
                lexical_ms=lexical_ms,
                embedding_ms=embedding_ms,
                vector_ms=vector_ms,
                started=started,
            )

        rerank_started = perf_counter()
        try:
            timeout_seconds = (
                BALANCED_RERANK_TIMEOUT_SECONDS
                if search_mode == "balanced"
                else THOROUGH_RERANK_TIMEOUT_SECONDS
            )
            async with asyncio.timeout(timeout_seconds):
                ranked = await reranker.rerank(
                    "\n".join(effective_queries),
                    [self._rerank_document(match.node) for match in fused],
                    limit=result_limit,
                )
        except TimeoutError:
            return self._response(
                fused[:result_limit],
                semantic_available,
                mode,
                "reranking timed out",
                queries=effective_queries,
                degraded_stage="rerank",
                timed_out=True,
                lexical_ms=lexical_ms,
                embedding_ms=embedding_ms,
                vector_ms=vector_ms,
                rerank_ms=self._elapsed_ms(rerank_started),
                started=started,
            )
        except (ProviderUnavailableError, OSError, RuntimeError, ValueError) as exc:
            return self._response(
                fused[:result_limit],
                semantic_available,
                mode,
                str(exc),
                queries=effective_queries,
                degraded_stage="rerank",
                lexical_ms=lexical_ms,
                embedding_ms=embedding_ms,
                vector_ms=vector_ms,
                rerank_ms=self._elapsed_ms(rerank_started),
                started=started,
            )

        reranked = [
            CodeSearchMatch(
                node=fused[result.index].node,
                fusion_score=fused[result.index].fusion_score,
                semantic_distance=fused[result.index].semantic_distance,
                rerank_score=result.score,
                matched_query_indexes=fused[result.index].matched_query_indexes,
            )
            for result in ranked
        ]
        reranked_modes: dict[RetrievalMode, RetrievalMode] = {
            "hybrid": "hybrid_reranked",
            "semantic": "semantic_reranked",
            "lexical": "lexical_reranked",
        }
        reranked_mode = reranked_modes[mode]
        return self._response(
            reranked,
            semantic_available,
            reranked_mode,
            degraded_reason,
            queries=effective_queries,
            reranker_applied=True,
            degraded_stage="semantic" if degraded_reason else None,
            lexical_ms=lexical_ms,
            embedding_ms=embedding_ms,
            vector_ms=vector_ms,
            rerank_ms=self._elapsed_ms(rerank_started),
            started=started,
        )

    async def _retrieve_query(
        self,
        query: str,
        *,
        query_index: int,
        node_type: NodeType | None,
        limit: int,
        repo_id: int | None,
    ) -> _QueryResult:
        lexical_task = asyncio.create_task(
            self._lexical_candidates(
                query,
                node_type=node_type,
                limit=limit,
                repo_id=repo_id,
            )
        )
        semantic_task = asyncio.create_task(
            self._semantic_candidates(
                query, node_type=node_type, limit=limit, repo_id=repo_id
            )
        )
        lexical_result, semantic_result = await asyncio.gather(
            lexical_task, semantic_task
        )
        lexical, lexical_ms = lexical_result
        semantic, available, reason, embedding_ms, vector_ms = semantic_result
        mode = self._retrieval_mode(semantic=bool(semantic), lexical=bool(lexical))

        return _QueryResult(
            matches=self._fuse(semantic, lexical, query_index=query_index),
            semantic_available=available,
            retrieval_mode=mode,
            degraded_reason=reason,
            lexical_ms=lexical_ms,
            embedding_ms=embedding_ms,
            vector_ms=vector_ms,
        )

    async def _lexical_candidates(
        self,
        query: str,
        *,
        node_type: NodeType | None,
        limit: int,
        repo_id: int | None,
    ) -> tuple[list[Node], float]:
        started = perf_counter()
        matches = await asyncio.to_thread(
            self._graph.search_by_name_sync,
            query,
            node_type=node_type,
            limit=limit,
            repo_id=repo_id,
        )
        return matches, self._elapsed_ms(started)

    async def _semantic_candidates(
        self,
        query: str,
        *,
        node_type: NodeType | None,
        limit: int,
        repo_id: int | None,
    ) -> tuple[list[SearchResult], bool, str | None, float, float]:
        provider = self._embeddings
        if provider is None:
            return [], False, "embedding provider is not configured", 0.0, 0.0
        embedding_started = perf_counter()
        try:
            vector = await provider.embed_query(query)
        except (ProviderUnavailableError, OSError, RuntimeError, ValueError) as exc:
            return [], False, str(exc), self._elapsed_ms(embedding_started), 0.0

        embedding_ms = self._elapsed_ms(embedding_started)
        vector_started = perf_counter()
        try:
            matches = await asyncio.to_thread(
                self._graph.search_by_vector_sync,
                vector,
                node_type=node_type,
                limit=limit,
                repo_id=repo_id,
            )
        except (ProviderUnavailableError, OSError, RuntimeError, ValueError) as exc:
            return [], False, str(exc), embedding_ms, self._elapsed_ms(vector_started)
        return matches, True, None, embedding_ms, self._elapsed_ms(vector_started)

    @staticmethod
    def _fuse(
        semantic: list[SearchResult], lexical: list[Node], *, query_index: int = 0
    ) -> list[CodeSearchMatch]:
        candidates: dict[str, _Candidate] = {}
        for rank, match in enumerate(semantic, start=1):
            candidate = candidates.setdefault(match.node.id, _Candidate(match.node))
            candidate.matched_query_indexes.add(query_index)
            candidate.fusion_score += 1.0 / (RECIPROCAL_RANK_CONSTANT + rank)
            candidate.semantic_distance = match.distance
            candidate.semantic = True
        for rank, node in enumerate(lexical, start=1):
            candidate = candidates.setdefault(node.id, _Candidate(node))
            candidate.matched_query_indexes.add(query_index)
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
                matched_query_indexes=tuple(sorted(candidate.matched_query_indexes)),
            )
            for candidate in ordered
        ]

    @staticmethod
    def _merge_query_matches(results: list[_QueryResult]) -> list[CodeSearchMatch]:
        candidates: dict[str, _Candidate] = {}
        for result in results:
            for match in result.matches:
                candidate = candidates.setdefault(match.node.id, _Candidate(match.node))
                candidate.fusion_score += match.fusion_score
                if match.semantic_distance is not None and (
                    candidate.semantic_distance is None
                    or match.semantic_distance < candidate.semantic_distance
                ):
                    candidate.semantic_distance = match.semantic_distance
                candidate.matched_query_indexes.update(match.matched_query_indexes)

        ordered = sorted(
            candidates.values(),
            key=lambda candidate: (-candidate.fusion_score, candidate.node.id),
        )
        return [
            CodeSearchMatch(
                node=candidate.node,
                fusion_score=candidate.fusion_score,
                semantic_distance=candidate.semantic_distance,
                matched_query_indexes=tuple(sorted(candidate.matched_query_indexes)),
            )
            for candidate in ordered
        ]

    @staticmethod
    def _effective_queries(query: str, queries: list[str] | None) -> tuple[str, ...]:
        requested = (query, *(queries or ()))
        if len(requested) > MAX_SEARCH_QUERIES:
            raise ValueError(f"search accepts at most {MAX_SEARCH_QUERIES} queries")
        if any(not item.strip() for item in requested):
            raise ValueError("search queries cannot be blank")

        return tuple(dict.fromkeys(requested))

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return (perf_counter() - started) * 1_000

    @classmethod
    def _response(
        cls,
        matches: list[CodeSearchMatch],
        semantic_available: bool,
        retrieval_mode: RetrievalMode,
        degraded_reason: str | None,
        *,
        queries: tuple[str, ...],
        reranker_applied: bool = False,
        degraded_stage: Literal["semantic", "rerank"] | None = None,
        timed_out: bool = False,
        lexical_ms: float = 0.0,
        embedding_ms: float = 0.0,
        vector_ms: float = 0.0,
        rerank_ms: float = 0.0,
        started: float,
    ) -> CodeSearchResponse:
        return CodeSearchResponse(
            matches=matches,
            semantic_available=semantic_available,
            retrieval_mode=retrieval_mode,
            degraded_reason=degraded_reason,
            queries=queries,
            reranker_applied=reranker_applied,
            degraded_stage=degraded_stage,
            timed_out=timed_out,
            timings=SearchTimings(
                lexical_ms=lexical_ms,
                embedding_ms=embedding_ms,
                vector_ms=vector_ms,
                rerank_ms=rerank_ms,
                total_ms=cls._elapsed_ms(started),
            ),
        )

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
            for part in (
                node.type.value,
                qualified_name or node.name,
                signature,
                file_path,
            )
            if part
        )
        content = node.content.strip()
        document = f"{structural}\n{content}" if content else structural
        return document[:MAX_RERANK_DOCUMENT_CHARS]
