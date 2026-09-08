"""Optional semantic enrichment providers for SCS."""

from scs.providers.base import (
    EmbeddingProvider,
    EventSink,
    NullEventSink,
    ProviderMetadata,
    ProviderUnavailableError,
    RankedDocument,
    RerankerMetadata,
    RerankingProvider,
)

__all__ = [
    "EmbeddingProvider",
    "EventSink",
    "NullEventSink",
    "ProviderMetadata",
    "ProviderUnavailableError",
    "RankedDocument",
    "RerankerMetadata",
    "RerankingProvider",
]
