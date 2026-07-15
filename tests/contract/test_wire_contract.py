"""SCSWire framing, routing, and compatibility contracts."""

from __future__ import annotations

import asyncio
import struct

import pytest

from scs.wire.framing import MAX_FRAME_BYTES, FrameError, read_frame, write_frame
from scs.wire.models import ErrorCode, WireRequest
from scs.wire.router import Router
from scs.wire.server import WireServer


class MemoryWriter:
    """Minimal drainable writer used to inspect encoded frames."""

    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None


@pytest.mark.asyncio
async def test_frame_round_trip() -> None:
    writer = MemoryWriter()
    await write_frame(writer, {"kind": "request", "id": "1", "value": "ok"})
    reader = asyncio.StreamReader()
    reader.feed_data(bytes(writer.data))
    reader.feed_eof()
    assert await read_frame(reader) == {"kind": "request", "id": "1", "value": "ok"}


@pytest.mark.asyncio
async def test_oversized_frame_is_rejected_before_body_read() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack(">I", MAX_FRAME_BYTES + 1))
    reader.feed_eof()
    with pytest.raises(FrameError, match="exceeds"):
        await read_frame(reader)


@pytest.mark.asyncio
async def test_malformed_json_is_typed_frame_error() -> None:
    reader = asyncio.StreamReader()
    body = b"{not-json"
    reader.feed_data(struct.pack(">I", len(body)) + body)
    reader.feed_eof()
    with pytest.raises(FrameError, match="JSON"):
        await read_frame(reader)


@pytest.mark.asyncio
async def test_unknown_method_returns_typed_error() -> None:
    result = await Router().dispatch("missing.method", {})
    assert result.error is not None
    assert result.error.code.value == "unknown_method"


@pytest.mark.asyncio
async def test_invalid_params_return_bad_request() -> None:
    router = Router()

    @router.method("math.increment")
    async def increment(params: dict[str, object]) -> dict[str, object]:
        value = params["value"]
        if not isinstance(value, int):
            raise ValueError("value must be an integer")
        return {"value": value + 1}

    result = await router.dispatch("math.increment", {"value": "wrong"})
    assert result.error is not None
    assert result.error.code.value == "bad_request"


@pytest.mark.asyncio
async def test_incompatible_protocol_is_rejected_before_dispatch() -> None:
    dispatched = False
    router = Router()

    @router.method("health")
    async def health(params: dict[str, object]) -> dict[str, object]:
        nonlocal dispatched
        dispatched = True
        return {"ready": True}

    server = WireServer(router)
    request = WireRequest(id="version-check", method="health").model_dump()
    request["version"] = 999
    response = await server.dispatch_envelope(request)
    assert response["kind"] == "error"
    assert response["error"]["code"] == ErrorCode.INCOMPATIBLE_PROTOCOL
    assert dispatched is False
