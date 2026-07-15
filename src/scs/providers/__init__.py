"""Optional semantic enrichment providers for SCS."""

from scs.providers.base import (
    EmbeddingProvider,
    EventSink,
    FileSummarizer,
    NullEventSink,
    ProviderMetadata,
    ProviderUnavailableError,
)

__all__ = [
    "EmbeddingProvider",
    "EventSink",
    "FileSummarizer",
    "NullEventSink",
    "ProviderMetadata",
    "ProviderUnavailableError",
]
