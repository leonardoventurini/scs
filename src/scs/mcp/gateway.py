"""Public SCS service gateway used by the MCP transport adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from scs.wire.client import SCSClient


class ServiceGateway(Protocol):
    """Finite public service calls available to transport adapters."""

    async def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]: ...


class SCSWireGateway:
    """Adapt the public SCSWire client to the MCP service gateway."""

    def __init__(self, socket_path: Path, *, timeout_seconds: float = 20.0) -> None:
        self._client = SCSClient(socket_path, timeout_seconds=timeout_seconds)

    async def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Forward one finite operation through SCSWire."""

        return await self._client.call(method, params)
