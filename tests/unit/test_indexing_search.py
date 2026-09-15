from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from scs.graph.models import Node, NodeType, SearchResult
from scs.indexing.search import CodeSearchService
from scs.providers.base import (
    ProviderMetadata,
    ProviderUnavailableError,
    RankedDocument,
    RerankerMetadata,
)


def node(identity: str, *, content: str = "") -> Node:
    return Node(
        id=identity,
        type=NodeType.FUNCTION,
        name=identity,
        content=content,
        metadata={
            "qualified_name": f"package.{identity}",
            "file_path": f"src/{identity}.py",
            "signature": "() -> None",
        },
    )


class Graph:
    def __init__(self) -> None:
        self.lexical_limit = 0
        self.semantic_limit = 0

    def search_by_name_sync(self, query, *, node_type, limit, repo_id):
        del query, node_type, repo_id
        self.lexical_limit = limit
        return [node("lexical"), node("shared")]

    def search_by_vector_sync(self, vector, *, node_type, limit, repo_id):
        del vector, node_type, repo_id
        self.semantic_limit = limit
        return [
            SearchResult(node=node("semantic"), distance=0.1),
            SearchResult(node=node("shared"), distance=0.2),
        ]


class Provider:
    metadata = ProviderMetadata("fake", "v1", 2)

    async def embed_query(self, text):
        del text
        return [1.0, 0.0]

    async def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]


class Reranker:
    metadata = RerankerMetadata("fake", "reranker")

    def __init__(self) -> None:
        self.documents: list[str] = []

    async def rerank(
        self, query: str, documents: Sequence[str], *, limit: int
    ) -> list[RankedDocument]:
        del query
        self.documents = list(documents)
        assert limit == 2
        return [RankedDocument(index=2, score=0.9), RankedDocument(index=0, score=0.5)]


class UnavailableReranker:
    metadata = RerankerMetadata("fake", "unavailable", False, "disabled")

    async def rerank(
        self, query: str, documents: Sequence[str], *, limit: int
    ) -> list[RankedDocument]:
        del query, documents, limit
        raise ProviderUnavailableError("disabled")


class HangingReranker:
    metadata = RerankerMetadata("fake", "hanging")

    async def rerank(
        self, query: str, documents: Sequence[str], *, limit: int
    ) -> list[RankedDocument]:
        del query, documents, limit
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_hybrid_search_fuses_semantic_and_lexical_results() -> None:
    graph = Graph()

    response = await CodeSearchService(graph, Provider()).search("run", limit=3)

    assert response.semantic_available
    assert response.retrieval_mode == "hybrid"
    assert [match.node.id for match in response.matches] == [
        "shared",
        "lexical",
        "semantic",
    ]
    assert response.matches[0].semantic_distance == 0.2
    assert response.matches[1].semantic_distance is None
    assert graph.lexical_limit == graph.semantic_limit == 20


@pytest.mark.asyncio
async def test_hybrid_search_reranks_bounded_fused_candidates() -> None:
    reranker = Reranker()

    response = await CodeSearchService(Graph(), Provider(), reranker).search(
        "run", limit=2
    )

    assert response.retrieval_mode == "hybrid_reranked"
    assert [match.node.id for match in response.matches] == ["semantic", "shared"]
    assert [match.rerank_score for match in response.matches] == [0.9, 0.5]
    assert reranker.documents == [
        "function package.shared () -> None src/shared.py",
        "function package.lexical () -> None src/lexical.py",
        "function package.semantic () -> None src/semantic.py",
    ]


@pytest.mark.asyncio
async def test_reranker_failure_returns_deterministic_fused_results() -> None:
    response = await CodeSearchService(
        Graph(), Provider(), UnavailableReranker()
    ).search("run", limit=2)

    assert response.retrieval_mode == "hybrid"
    assert [match.node.id for match in response.matches] == ["shared", "lexical"]
    assert response.degraded_reason == "disabled"


@pytest.mark.asyncio
async def test_search_degrades_to_lexical_without_provider() -> None:
    response = await CodeSearchService(Graph(), None).search("run")

    assert not response.semantic_available
    assert response.retrieval_mode == "lexical"
    assert [match.node.id for match in response.matches] == ["lexical", "shared"]


@pytest.mark.asyncio
async def test_candidate_generation_obeys_global_result_ceiling() -> None:
    graph = Graph()

    await CodeSearchService(graph, Provider()).search("run", limit=200)

    assert graph.lexical_limit == graph.semantic_limit == 200


@pytest.mark.asyncio
async def test_fast_search_skips_configured_reranker() -> None:
    reranker = Reranker()

    response = await CodeSearchService(Graph(), Provider(), reranker).search(
        "run", limit=2, search_mode="fast"
    )

    assert response.retrieval_mode == "hybrid"
    assert [match.node.id for match in response.matches] == ["shared", "lexical"]
    assert response.reranker_applied is False
    assert reranker.documents == []


@pytest.mark.asyncio
async def test_balanced_search_times_out_to_deterministic_fused_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scs.indexing.search.BALANCED_RERANK_TIMEOUT_SECONDS", 0.001)

    response = await CodeSearchService(Graph(), Provider(), HangingReranker()).search(
        "run", limit=2, search_mode="balanced"
    )

    assert response.retrieval_mode == "hybrid"
    assert [match.node.id for match in response.matches] == ["shared", "lexical"]
    assert response.reranker_applied is False
    assert response.degraded_stage == "rerank"
    assert response.timed_out is True
    assert response.degraded_reason == "reranking timed out"


@pytest.mark.asyncio
async def test_default_search_preserves_thorough_reranking() -> None:
    response = await CodeSearchService(Graph(), Provider(), Reranker()).search(
        "run", limit=2
    )

    assert response.retrieval_mode == "hybrid_reranked"
    assert response.reranker_applied is True
    assert response.timed_out is False


@pytest.mark.asyncio
async def test_multi_query_search_deduplicates_matches_and_tracks_evidence() -> None:
    reranker = Reranker()

    response = await CodeSearchService(Graph(), Provider(), reranker).search(
        "run", queries=["execute", "invoke"], limit=2
    )

    assert response.queries == ("run", "execute", "invoke")
    assert [match.node.id for match in response.matches] == ["semantic", "shared"]
    assert response.matches[0].matched_query_indexes == (0, 1, 2)
    assert response.matches[1].matched_query_indexes == (0, 1, 2)
    assert len(reranker.documents) == 3


@pytest.mark.asyncio
async def test_multi_query_search_removes_exact_duplicate_queries() -> None:
    response = await CodeSearchService(Graph(), Provider()).search(
        "run", queries=["run", "execute", "execute"]
    )

    assert response.queries == ("run", "execute")
    assert response.matches[0].matched_query_indexes == (0, 1)


@pytest.mark.asyncio
async def test_search_reports_nonnegative_stage_timings() -> None:
    response = await CodeSearchService(Graph(), Provider(), Reranker()).search(
        "run", limit=2
    )

    assert response.timings.lexical_ms >= 0
    assert response.timings.embedding_ms >= 0
    assert response.timings.vector_ms >= 0
    assert response.timings.rerank_ms >= 0
    assert response.timings.total_ms >= 0
