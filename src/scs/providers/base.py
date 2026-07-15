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
class FileSummarizer(Protocol):
    """Optional provider that summarizes code files without owning storage."""

    @property
    def provider_name(self) -> str:
        """Return a stable provider/model identity for provenance."""

        ...

    async def summarize_files(self, files: Mapping[str, str]) -> dict[str, str]:
        """Return summaries keyed by the supplied repository-relative paths."""

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
