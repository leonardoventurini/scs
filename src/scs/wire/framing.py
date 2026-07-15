"""Bounded length-prefixed JSON framing for local SCSWire connections."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Mapping
from typing import Protocol

FRAME_HEADER_BYTES = 4
MAX_FRAME_BYTES = 16 * 1024 * 1024


class FrameError(ValueError):
    """Raised when a peer sends an invalid or unsafe SCSWire frame."""


class DrainableWriter(Protocol):
    """Small writer contract shared by asyncio and in-memory test writers."""

    def write(self, data: bytes) -> None:
        """Append bytes to the output stream."""

    async def drain(self) -> None:
        """Yield until buffered output is accepted by the transport."""


async def read_frame(reader: asyncio.StreamReader) -> dict[str, object]:
    """Read one bounded JSON object from a length-prefixed stream."""

    try:
        header = await reader.readexactly(FRAME_HEADER_BYTES)
    except asyncio.IncompleteReadError as error:
        raise FrameError("incomplete SCSWire frame header") from error

    (body_length,) = struct.unpack(">I", header)
    if body_length == 0:
        raise FrameError("SCSWire frame body cannot be empty")
    if body_length > MAX_FRAME_BYTES:
        raise FrameError(
            f"SCSWire frame size {body_length} exceeds {MAX_FRAME_BYTES} bytes"
        )

    try:
        body = await reader.readexactly(body_length)
    except asyncio.IncompleteReadError as error:
        raise FrameError("incomplete SCSWire frame body") from error

    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FrameError("SCSWire frame body is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise FrameError("SCSWire frame JSON must be an object")
    if not all(isinstance(key, str) for key in decoded):
        raise FrameError("SCSWire frame keys must be strings")
    return decoded


async def write_frame(
    writer: DrainableWriter,
    payload: Mapping[str, object],
) -> None:
    """Write one deterministic, bounded JSON object to a framed stream."""

    try:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise FrameError("SCSWire payload is not JSON serializable") from error
    if not body:
        raise FrameError("SCSWire frame body cannot be empty")
    if len(body) > MAX_FRAME_BYTES:
        raise FrameError(
            f"SCSWire frame size {len(body)} exceeds {MAX_FRAME_BYTES} bytes"
        )
    writer.write(struct.pack(">I", len(body)) + body)
    await writer.drain()
