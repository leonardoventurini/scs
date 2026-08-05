from __future__ import annotations

import json
from decimal import Decimal

import pytest

from scs.providers.base import EmbeddingProvider, ProviderUnavailableError
from scs.providers.mlx import MLXEmbeddingProvider
from scs.providers.openai import OpenAIFileSummarizer


class FakeModel:
    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[float(index), 1.0] for index, _ in enumerate(texts)]


class DecimalModel:
    def encode(self, texts: list[str]) -> list[list[Decimal]]:
        return [[Decimal("1.25"), Decimal("2.5")] for _ in texts]


class FakeResponse:
    output_text = json.dumps({"a.py": "Defines the application entry point."})


class FakeResponses:
    def create(self, **kwargs: object) -> FakeResponse:
        return FakeResponse()


def test_provider_metadata_is_persistable() -> None:
    provider = MLXEmbeddingProvider(dimension=2, loader=lambda _: FakeModel())

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

    assert await provider.embed_documents(["one", "two"]) == [[0.0, 1.0], [0.0, 1.0]]
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
async def test_missing_openai_key_degrades_truthfully() -> None:
    summarizer = OpenAIFileSummarizer(api_key=None)

    with pytest.raises(ProviderUnavailableError, match="SCS_OPENAI_API_KEY"):
        await summarizer.summarize_files({"a.py": "print('a')"})


@pytest.mark.asyncio
async def test_openai_summarizer_filters_unknown_paths() -> None:
    summarizer = OpenAIFileSummarizer(api_key=None, responses=FakeResponses())

    assert await summarizer.summarize_files({"a.py": "print('a')"}) == {
        "a.py": "Defines the application entry point."
    }
