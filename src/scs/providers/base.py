"""Provider ports that keep indexing independent from model implementations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Protocol, runtime_checkable


class ProviderUnavailableError(RuntimeError):
    """Raised when optional enrichment is requested but unavailable."""


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """Durable identity required to interpret persisted semantic vectors."""

    provider: str
    model: str
    dimension: int
    available: bool = True
    reason: str | None = None

    def to_dict(self) -> dict[str, str | int | bool | None]:
        """Return a JSON-serializable persistence representation."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class RerankerMetadata:
    """Runtime identity and availability for optional result reranking."""

    provider: str
    model: str
    available: bool = True
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RankedDocument:
    """One reranker score associated with its original document position."""

    index: int
    score: float


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Asynchronous, bounded provider for document and query embeddings."""

    @property
    def metadata(self) -> ProviderMetadata:
        """Describe the vector representation produced by this provider."""

        ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed code documents in input order."""

        ...

    async def embed_query(self, text: str) -> list[float]:
        """Embed a semantic search query."""

        ...


@runtime_checkable
class RerankingProvider(Protocol):
    """Asynchronous provider for bounded query-document relevance ordering."""

    @property
    def metadata(self) -> RerankerMetadata:
        """Describe the configured reranker and its observed availability."""

        ...

    async def rerank(
        self, query: str, documents: Sequence[str], *, limit: int
    ) -> list[RankedDocument]:
        """Rank document positions by their relevance to one query."""

        ...


@runtime_checkable
class EventSink(Protocol):
    """Non-blocking output port for durable job and indexing progress events."""

    async def publish(self, event: str, payload: Mapping[str, object]) -> None:
        """Publish an event without granting the indexer transport ownership."""

        ...


class NullEventSink:
    """Default event sink for standalone and test use."""

    async def publish(self, event: str, payload: Mapping[str, object]) -> None:
        """Discard an event intentionally."""

        del event, payload
