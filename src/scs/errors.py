"""Cross-layer service errors with stable transport semantics."""

from __future__ import annotations


class ServiceBusyError(RuntimeError):
    """Raised for a finite operation that is safe to retry after a delay."""
