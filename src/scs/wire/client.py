"""Timeout-bounded client for the local SCSWire control plane."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from pathlib import Path

from scs.wire.framing import read_frame, write_frame
from scs.wire.models import WireErrorResponse, WireRequest, WireResponse

DEFAULT_CALL_TIMEOUT_SECONDS = 5.0


class SCSWireError(RuntimeError):
    """Raised when SCSWire returns a typed application error."""


class SCSClient:
    """Make finite calls to one independently managed SCS daemon."""

    def __init__(
        self,
        socket_path: Path,
        *,
        timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._socket_path: Path = socket_path
        self._timeout_seconds: float = timeout_seconds

    async def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Call one method and close the connection after its finite response."""

        return await asyncio.wait_for(
            self._call(method, params or {}),
            timeout=self._timeout_seconds,
        )

    async def _call(
        self,
        method: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        reader, writer = await asyncio.open_unix_connection(self._socket_path)
        request = WireRequest(id=uuid.uuid4().hex, method=method, params=params)
        try:
            await write_frame(writer, request.model_dump(mode="json"))
            envelope = await read_frame(reader)
            if envelope.get("kind") == "error":
                response = WireErrorResponse.model_validate(envelope)
                raise SCSWireError(
                    f"{response.error.code.value}: {response.error.message}"
                )
            response = WireResponse.model_validate(envelope)
            if response.id != request.id:
                raise SCSWireError("SCSWire response id does not match request")
            return response.result
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()


class SCSConnection:
    """Persistent SCSWire connection used as a daemon client lease."""

    def __init__(self, socket_path: Path) -> None:
        self._socket_path: Path = socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock: asyncio.Lock = asyncio.Lock()

    async def connect(self) -> dict[str, object]:
        """Connect and attach this socket as one live daemon client."""

        if self._writer is not None:
            raise RuntimeError("SCSWire connection is already open")
        self._reader, self._writer = await asyncio.open_unix_connection(
            self._socket_path
        )
        try:
            return await self.call("system.client.attach")
        except BaseException:
            await self.close()
            raise

    async def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Make one serialized request without releasing the client lease."""

        reader = self._reader
        writer = self._writer
        if reader is None or writer is None:
            raise RuntimeError("SCSWire connection is not open")
        async with self._lock:
            request = WireRequest(
                id=uuid.uuid4().hex,
                method=method,
                params=params or {},
            )
            await write_frame(writer, request.model_dump(mode="json"))
            envelope = await read_frame(reader)
            if envelope.get("kind") == "error":
                response = WireErrorResponse.model_validate(envelope)
                raise SCSWireError(
                    f"{response.error.code.value}: {response.error.message}"
                )
            response = WireResponse.model_validate(envelope)
            if response.id != request.id:
                raise SCSWireError("SCSWire response id does not match request")
            return response.result

    async def close(self) -> None:
        """Close the socket; the daemon observes this as lease release."""

        writer, self._writer = self._writer, None
        self._reader = None
        if writer is None:
            return
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()

    async def __aenter__(self) -> "SCSConnection":
        await self.connect()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()
