from __future__ import annotations

import pytest

from scs.graph.models import Node, NodeType, SearchResult
from scs.indexing.search import CodeSearchService
from scs.providers.base import ProviderMetadata


class Graph:
    def search_by_name_sync(self, query, *, node_type, limit, repo_id):
        return [Node(id="lexical", type=NodeType.FUNCTION, name=query)]

    def search_by_vector_sync(self, vector, *, node_type, limit, repo_id):
        return [SearchResult(node=Node(id="semantic", type=NodeType.FUNCTION, name="match"), distance=0.1)]


class Provider:
    metadata = ProviderMetadata("fake", "v1", 2)

    async def embed_query(self, text):
        return [1.0, 0.0]

    async def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]


@pytest.mark.asyncio
async def test_hybrid_search_prioritizes_semantic_results() -> None:
    response = await CodeSearchService(Graph(), Provider()).search("run")

    assert response.semantic_available
    assert [node.id for node in response.nodes] == ["semantic", "lexical"]


@pytest.mark.asyncio
async def test_search_degrades_to_lexical_without_provider() -> None:
    response = await CodeSearchService(Graph(), None).search("run")

    assert not response.semantic_available
    assert [node.id for node in response.nodes] == ["lexical"]
