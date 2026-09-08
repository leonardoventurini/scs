"""Strict loopback adapter for the oMLX document-reranking endpoint."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from ipaddress import ip_address
from math import isfinite
from typing import Final, cast
from urllib.parse import urlsplit

import aiohttp

from scs.providers.base import (
    ProviderUnavailableError,
    RankedDocument,
    RerankerMetadata,
)

RERANK_PATH: Final[str] = "rerank"
REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0
RerankerRequest = Callable[[dict[str, object]], Awaitable[object]]


class OMLXRerankingProvider:
    """Rerank bounded text candidates through an explicitly configured oMLX."""

    def __init__(
        self,
        *,
        base_url: str,
        model_name: str,
        request: RerankerRequest | None = None,
    ) -> None:
        self._base_url: str = self._validated_base_url(base_url)
        self._model_name: str = model_name
        self._request: RerankerRequest | None = request
        self._unavailable_reason: str | None = None

    @property
    def metadata(self) -> RerankerMetadata:
        """Report configured identity and the most recent availability state."""

        return RerankerMetadata(
            provider="omlx",
            model=self._model_name,
            available=self._unavailable_reason is None,
            reason=self._unavailable_reason,
        )

    @property
    def endpoint(self) -> str:
        """Return the configured local reranking endpoint."""

        return f"{self._base_url}/{RERANK_PATH}"

    async def rerank(
        self, query: str, documents: Sequence[str], *, limit: int
    ) -> list[RankedDocument]:
        """Return oMLX's relevance ordering as validated original positions."""

        if limit < 1:
            raise ValueError("reranking limit must be positive")
        if not documents:
            return []

        result_count = min(limit, len(documents))
        payload: dict[str, object] = {
            "model": self._model_name,
            "query": query,
            "documents": list(documents),
            "top_n": result_count,
            "return_documents": False,
        }
        try:
            response = await self._post(payload)
            results = self._parse_results(
                response,
                document_count=len(documents),
                expected_count=result_count,
            )
            self._unavailable_reason = None
            return results
        except (
            aiohttp.ClientError,
            OSError,
            TimeoutError,
            TypeError,
            ValueError,
        ) as exc:
            self._unavailable_reason = str(exc)
            raise ProviderUnavailableError(
                f"oMLX reranking provider is unavailable: {exc}"
            ) from exc

    async def _post(self, payload: dict[str, object]) -> object:
        request = self._request
        if request is not None:
            return await request(payload)

        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.endpoint, json=payload) as response:
                response.raise_for_status()
                return cast(object, await response.json(content_type=None))

    @staticmethod
    def _parse_results(
        response: object, *, document_count: int, expected_count: int
    ) -> list[RankedDocument]:
        if not isinstance(response, Mapping):
            raise ValueError("reranking response must be an object")
        raw_results = cast(Mapping[str, object], response).get("results")
        if not isinstance(raw_results, list):
            raise ValueError("reranking response count does not match request")
        result_items = cast(list[object], raw_results)
        if len(result_items) != expected_count:
            raise ValueError("reranking response count does not match request")

        seen: set[int] = set()
        results: list[RankedDocument] = []
        previous_score = float("inf")
        for item in result_items:
            if not isinstance(item, Mapping):
                raise ValueError("reranking results must contain objects")
            record = cast(Mapping[str, object], item)
            index = record.get("index")
            raw_score = record.get("relevance_score")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < document_count
                or index in seen
            ):
                raise ValueError("reranking indexes must be unique and in range")
            if isinstance(raw_score, bool) or not isinstance(raw_score, int | float):
                raise ValueError("reranking scores must be numeric")
            score = float(raw_score)
            if not isfinite(score):
                raise ValueError("reranking scores must be finite")
            if score > previous_score:
                raise ValueError("reranking results must be ordered by score")

            seen.add(index)
            results.append(RankedDocument(index=index, score=score))
            previous_score = score
        return results

    @staticmethod
    def _validated_base_url(value: str) -> str:
        """Keep source-bearing reranking requests on the local machine."""

        parsed = urlsplit(value)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError("oMLX reranking base URL must be absolute HTTP")
        host = parsed.hostname.lower()
        try:
            is_loopback = ip_address(host).is_loopback
        except ValueError:
            is_loopback = host == "localhost"
        if not is_loopback:
            raise ValueError("oMLX reranking base URL must use a loopback host")
        return value.rstrip("/")
