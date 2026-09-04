"""Public SCS service gateway used by the MCP transport adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from scs.wire.client import SCSConnection


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
        del timeout_seconds
        self._connection: SCSConnection = SCSConnection(socket_path)

    async def connect(self) -> dict[str, object]:
        """Open the bridge-owned daemon lease."""

        return await self._connection.connect()

    async def close(self) -> None:
        """Release the bridge-owned daemon lease."""

        await self._connection.close()

    async def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Forward one finite operation through SCSWire."""

        return await self._connection.call(method, params)
