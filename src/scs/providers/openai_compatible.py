"""Local OpenAI-compatible embeddings provider used for OMLX."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from math import isfinite
from typing import Final, cast

import aiohttp

from scs.providers.base import ProviderMetadata, ProviderUnavailableError

EMBEDDINGS_PATH: Final[str] = "embeddings"
DOCUMENT_PREFIX: Final[str] = "search_document"
QUERY_PREFIX: Final[str] = "search_query"
REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0
ProviderRequest = Callable[[dict[str, object]], Awaitable[object]]


class OpenAICompatibleEmbeddingProvider:
    """Embed parser-owned text through a strictly validated local API boundary."""

    def __init__(
        self,
        *,
        base_url: str,
        model_name: str,
        dimension: int,
        batch_size: int = 32,
        request: ProviderRequest | None = None,
    ) -> None:
        self._base_url: str = base_url.rstrip("/")
        self._model_name: str = model_name
        self._dimension: int = dimension
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._batch_size: int = batch_size
        self._request: ProviderRequest | None = request
        self._unavailable_reason: str | None = None

    @property
    def metadata(self) -> ProviderMetadata:
        """Expose the durable identity that makes vectors interpretable."""

        return ProviderMetadata(
            provider="omlx-openai-compatible",
            model=self._model_name,
            dimension=self._dimension,
            available=self._unavailable_reason is None,
            reason=self._unavailable_reason,
        )

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed documents using the retrieval-document instruction prefix."""

        return await self._embed(DOCUMENT_PREFIX, texts)

    async def embed_query(self, text: str) -> list[float]:
        """Embed one query using the matching retrieval-query instruction prefix."""

        vectors = await self._embed(QUERY_PREFIX, [text])
        return vectors[0]

    async def _embed(self, prefix: str, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            vectors: list[list[float]] = []
            for offset in range(0, len(texts), self._batch_size):
                inputs = [
                    f"{prefix}: {text}"
                    for text in texts[offset : offset + self._batch_size]
                ]
                payload: dict[str, object] = {
                    "model": self._model_name,
                    "input": inputs,
                }
                response = await self._post(payload)
                vectors.extend(
                    self._parse_embeddings(response, expected_count=len(inputs))
                )
            self._unavailable_reason = None
            return vectors
        except (aiohttp.ClientError, OSError, TimeoutError, TypeError, ValueError) as exc:
            self._unavailable_reason = str(exc)
            raise ProviderUnavailableError(
                f"OpenAI-compatible embedding provider is unavailable: {exc}"
            ) from exc

    async def _post(self, payload: dict[str, object]) -> object:
        request = self._request
        if request is not None:
            return await request(payload)
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self._base_url}/{EMBEDDINGS_PATH}", json=payload
            ) as response:
                response.raise_for_status()
                return cast(object, await response.json(content_type=None))

    def _parse_embeddings(
        self, response: object, *, expected_count: int
    ) -> list[list[float]]:
        if not isinstance(response, Mapping):
            raise ValueError("embedding response must be an object")
        response_object = cast(Mapping[str, object], response)
        raw_data = response_object.get("data")
        if not isinstance(raw_data, list):
            raise ValueError("embedding response count does not match request")
        data = cast(list[object], raw_data)
        if len(data) != expected_count:
            raise ValueError("embedding response count does not match request")
        ordered: list[list[float] | None] = [None] * expected_count
        for item in data:
            if not isinstance(item, Mapping):
                raise ValueError("embedding response data must contain objects")
            record = cast(Mapping[str, object], item)
            index = record.get("index")
            raw_embedding = record.get("embedding")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < expected_count
                or ordered[index] is not None
            ):
                raise ValueError("embedding response indexes must be unique and contiguous")
            if not isinstance(raw_embedding, list):
                raise ValueError("embedding response entry must contain an embedding list")
            vector = [
                self._component(value) for value in cast(list[object], raw_embedding)
            ]
            if len(vector) != self._dimension:
                raise ValueError(
                    "embedding dimension mismatch: "
                    f"expected {self._dimension}, got {len(vector)}"
                )
            ordered[index] = vector
        if any(vector is None for vector in ordered):
            raise ValueError("embedding response indexes must be contiguous")
        return [cast(list[float], vector) for vector in ordered]

    @staticmethod
    def _component(value: object) -> float:
        """Reject non-numeric JSON values before they reach the vector store."""

        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("embedding components must be numeric")
        try:
            component = float(value)
        except OverflowError as exc:
            raise ValueError("embedding components must be finite") from exc
        if not isfinite(component):
            raise ValueError("embedding components must be finite")
        return component
