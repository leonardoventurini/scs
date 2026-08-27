from __future__ import annotations

import json
from decimal import Decimal

import pytest

from scs.providers.base import EmbeddingProvider, ProviderUnavailableError
from scs.providers.mlx import MLXEmbeddingProvider
from scs.providers.openai_compatible import OpenAICompatibleEmbeddingProvider


class FakeModel:
    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[float(index), 1.0] for index, _ in enumerate(texts)]


class DecimalModel:
    def encode(self, texts: list[str]) -> list[list[Decimal]]:
        return [[Decimal("1.25"), Decimal("2.5")] for _ in texts]


def test_provider_metadata_is_persistable() -> None:
    provider = MLXEmbeddingProvider(
        model_name="nomic-ai/nomic-embed-text-v1.5",
        dimension=2,
        loader=lambda _: FakeModel(),
    )

    assert isinstance(provider, EmbeddingProvider)
    assert json.loads(json.dumps(provider.metadata.to_dict())) == {
        "available": True,
        "dimension": 2,
        "model": "nomic-ai/nomic-embed-text-v1.5",
        "provider": "mlx",
        "reason": None,
    }


@pytest.mark.asyncio
async def test_embedding_adapter_preserves_order_and_dimension() -> None:
    provider = MLXEmbeddingProvider(
        dimension=2, batch_size=1, loader=lambda _: FakeModel()
    )

    assert await provider.embed_documents(["one", "two"]) == [
        [0.0, 1.0],
        [0.0, 1.0],
    ]
    assert await provider.embed_query("query") == [0.0, 1.0]


@pytest.mark.asyncio
async def test_embedding_adapter_preserves_float_convertible_scalars() -> None:
    """Custom numeric scalars remain valid at the optional provider boundary."""

    provider = MLXEmbeddingProvider(dimension=2, loader=lambda _: DecimalModel())

    assert await provider.embed_query("query") == [1.25, 2.5]


@pytest.mark.asyncio
async def test_missing_optional_provider_degrades() -> None:
    provider = MLXEmbeddingProvider(
        loader=lambda _: (_ for _ in ()).throw(ImportError("missing"))
    )

    with pytest.raises(ProviderUnavailableError, match="unavailable"):
        await provider.embed_query("query")

    assert provider.metadata.available is False
    assert provider.metadata.reason == "missing"


@pytest.mark.asyncio
async def test_openai_compatible_provider_preserves_response_index_order() -> None:
    payloads: list[dict[str, object]] = []

    async def request(payload: dict[str, object]) -> object:
        payloads.append(payload)
        return {
            "data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]
        }

    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:10000/v1",
        model_name="test-embedding",
        dimension=2,
        request=request,
    )

    assert await provider.embed_documents(["first", "second"]) == [
        [1.0, 0.0],
        [0.0, 1.0],
    ]
    assert payloads == [
        {
            "model": "test-embedding",
            "input": ["search_document: first", "search_document: second"],
        }
    ]


@pytest.mark.asyncio
async def test_openai_compatible_provider_rejects_malformed_vectors() -> None:
    async def malformed(_payload: dict[str, object]) -> object:
        return {"data": [{"index": 0, "embedding": [1.0]}]}

    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:10000/v1",
        model_name="test-embedding",
        dimension=2,
        request=malformed,
    )

    with pytest.raises(ProviderUnavailableError, match="dimension mismatch"):
        await provider.embed_query("query")

    assert provider.metadata.available is False


@pytest.mark.asyncio
@pytest.mark.parametrize("component", [float("nan"), float("inf"), float("-inf")])
async def test_openai_compatible_provider_rejects_nonfinite_components(
    component: float,
) -> None:
    async def nonfinite(_payload: dict[str, object]) -> object:
        return {"data": [{"index": 0, "embedding": [component, 0.0]}]}

    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:10000/v1",
        model_name="test-embedding",
        dimension=2,
        request=nonfinite,
    )

    with pytest.raises(ProviderUnavailableError, match="finite"):
        await provider.embed_query("query")


@pytest.mark.asyncio
async def test_openai_compatible_provider_recovers_and_honors_batch_size() -> None:
    attempts = 0
    payloads: list[dict[str, object]] = []

    async def transient_then_healthy(payload: dict[str, object]) -> object:
        nonlocal attempts
        attempts += 1
        payloads.append(payload)
        if attempts == 1:
            raise OSError("OMLX is starting")
        inputs = payload["input"]
        assert isinstance(inputs, list)
        return {
            "data": [
                {"index": index, "embedding": [float(index), 1.0]}
                for index, _ in enumerate(inputs)
            ]
        }

    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:10000/v1",
        model_name="test-embedding",
        dimension=2,
        batch_size=1,
        request=transient_then_healthy,
    )

    with pytest.raises(ProviderUnavailableError, match="starting"):
        await provider.embed_query("first")
    assert provider.metadata.available is False
    assert await provider.embed_documents(["one", "two"]) == [[0.0, 1.0], [0.0, 1.0]]
    assert provider.metadata.available is True
    assert [payload["input"] for payload in payloads] == [
        ["search_query: first"],
        ["search_document: one"],
        ["search_document: two"],
    ]
