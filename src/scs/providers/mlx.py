"""Lazy MLX embedding adapter with bounded serialized model access."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable, Iterable, Sequence
from typing import Protocol, SupportsFloat, SupportsIndex, cast, runtime_checkable

from scs.config import DEFAULT_LOCAL_EMBEDDING_DIMENSION, DEFAULT_LOCAL_EMBEDDING_MODEL
from scs.providers.base import ProviderMetadata, ProviderUnavailableError


class _EncodingModel(Protocol):
    """Minimal synchronous model contract used behind the MLX adapter."""

    def encode(self, texts: list[str]) -> object:
        """Encode text into a two-dimensional numeric result."""

        ...


class _ModelRegistryModule(Protocol):
    """Typed surface of the optional MLX model registry package."""

    from_registry: Callable[[str], _EncodingModel]


@runtime_checkable
class _ArrayLike(Protocol):
    """Array result that can expose plain Python values."""

    def tolist(self) -> object:
        """Return the array as nested Python values."""

        ...


class MLXEmbeddingProvider:
    """Load and serialize an MLX embedding model only when first requested."""

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_LOCAL_EMBEDDING_MODEL,
        dimension: int = DEFAULT_LOCAL_EMBEDDING_DIMENSION,
        batch_size: int = 32,
        loader: Callable[[str], _EncodingModel] | None = None,
    ) -> None:
        self._model_name: str = model_name
        self._dimension: int = dimension
        self._batch_size: int = batch_size
        self._loader: Callable[[str], _EncodingModel] | None = loader
        self._model: _EncodingModel | None = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._unavailable_reason: str | None = None

    @property
    def metadata(self) -> ProviderMetadata:
        """Report configured identity and any observed load failure."""

        return ProviderMetadata(
            provider="mlx",
            model=self._model_name,
            dimension=self._dimension,
            available=self._unavailable_reason is None,
            reason=self._unavailable_reason,
        )

    def _load(self) -> _EncodingModel:
        if self._model is not None:
            return self._model
        try:
            if self._loader is not None:
                self._model = self._loader(self._model_name)
            else:
                module = cast(
                    _ModelRegistryModule,
                    cast(object, importlib.import_module("mlx_embedding_models")),
                )
                self._model = module.from_registry(self._model_name)
        except (ImportError, OSError, RuntimeError) as exc:
            self._unavailable_reason = str(exc)
            raise ProviderUnavailableError(
                f"MLX embedding provider is unavailable: {exc}"
            ) from exc
        return self._model

    @staticmethod
    def _component(value: object) -> float:
        """Convert every scalar shape historically accepted by ``float``."""

        # The cast is static-only: providers may return Decimal, Fraction,
        # NumPy, or custom scalar objects implementing Python's numeric hooks.
        return float(
            cast(str | bytes | bytearray | SupportsFloat | SupportsIndex, value)
        )

    @staticmethod
    def _rows(value: object) -> list[list[float]]:
        """Normalize array-like model output without importing NumPy eagerly."""

        if isinstance(value, _ArrayLike):
            value = value.tolist()
        if not isinstance(value, list):
            raise TypeError("Embedding model returned a non-sequence result")
        rows = cast(list[object], value)
        normalized: list[list[float]] = []
        for row in rows:
            if not isinstance(row, Iterable):
                raise TypeError("Embedding model returned a non-sequence row")
            normalized.append(
                [
                    MLXEmbeddingProvider._component(component)
                    for component in cast(Iterable[object], row)
                ]
            )
        return normalized

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed documents in bounded batches under one shared model lock."""

        if not texts:
            return []
        rows: list[list[float]] = []
        async with self._lock:
            model = await asyncio.to_thread(self._load)
            for offset in range(0, len(texts), self._batch_size):
                batch = [
                    f"search_document: {text}"
                    for text in texts[offset : offset + self._batch_size]
                ]
                encoded = await asyncio.to_thread(model.encode, batch)
                rows.extend(self._rows(encoded))
        self._validate_dimensions(rows)
        return rows

    async def embed_query(self, text: str) -> list[float]:
        """Embed one search query using asymmetric retrieval prefixing."""

        rows = await self.embed_documents_as("search_query", [text])
        return rows[0]

    async def embed_documents_as(
        self, task: str, texts: Sequence[str]
    ) -> list[list[float]]:
        """Embed texts using an explicit Nomic task prefix."""

        async with self._lock:
            model = await asyncio.to_thread(self._load)
            encoded = await asyncio.to_thread(
                model.encode, [f"{task}: {text}" for text in texts]
            )
        rows = self._rows(encoded)
        self._validate_dimensions(rows)
        return rows

    def _validate_dimensions(self, rows: Sequence[Sequence[float]]) -> None:
        for row in rows:
            if len(row) != self._dimension:
                raise ValueError(
                    f"Embedding dimension mismatch: expected {self._dimension}, got {len(row)}"
                )
