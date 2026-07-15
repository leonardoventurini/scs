"""SCSWire daemon integration tests over a real Unix socket."""

from __future__ import annotations

import asyncio
import shutil
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from scs.wire.client import SCSClient
from scs.wire.router import Router
from scs.wire.server import WireServer


@pytest.fixture
def short_runtime_path() -> Iterator[Path]:
    """Provide a macOS-compatible path below the AF_UNIX byte limit."""

    root = Path(tempfile.mkdtemp(prefix="scs-", dir="/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_client_server_round_trip_and_clean_shutdown(
    short_runtime_path: Path,
) -> None:
    socket_path = short_runtime_path / "runtime" / "scs.sock"
    router = Router()

    @router.method("health")
    async def health(params: dict[str, object]) -> dict[str, object]:
        return {"ready": True}

    server = WireServer(router, socket_path=socket_path)
    await server.start()
    assert socket_path.exists()
    assert socket_path.stat().st_mode & 0o777 == 0o600

    client = SCSClient(socket_path)
    assert await client.call("health") == {"ready": True}

    await server.stop()
    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_duplicate_live_server_is_refused(short_runtime_path: Path) -> None:
    socket_path = short_runtime_path / "scs.sock"
    first = WireServer(Router(), socket_path=socket_path)
    second = WireServer(Router(), socket_path=socket_path)
    await first.start()
    try:
        with pytest.raises(RuntimeError, match="already active"):
            await second.start()
    finally:
        await first.stop()


@pytest.mark.asyncio
async def test_stale_same_user_socket_is_reclaimed(short_runtime_path: Path) -> None:
    socket_path = short_runtime_path / "scs.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()

    server = WireServer(Router(), socket_path=socket_path)
    await server.start()
    try:
        assert socket_path.is_socket()
    finally:
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("occupant", ["file", "symlink"])
async def test_unsafe_socket_path_occupants_are_refused(
    short_runtime_path: Path,
    occupant: str,
) -> None:
    socket_path = short_runtime_path / "scs.sock"
    if occupant == "file":
        socket_path.write_text("not a socket", encoding="utf-8")
    else:
        target = short_runtime_path / "target"
        target.write_text("not a socket", encoding="utf-8")
        socket_path.symlink_to(target)

    with pytest.raises(RuntimeError, match="refusing"):
        await WireServer(Router(), socket_path=socket_path).start()

    assert socket_path.exists()


@pytest.mark.asyncio
async def test_shutdown_preserves_replacement_socket_generation(
    short_runtime_path: Path,
) -> None:
    socket_path = short_runtime_path / "scs.sock"
    server = WireServer(Router(), socket_path=socket_path)
    await server.start()
    socket_path.unlink()
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement.bind(str(socket_path))
    replacement.listen()
    try:
        await server.stop()
        assert socket_path.is_socket()
    finally:
        replacement.close()
        socket_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_live_foreign_socket_is_never_unlinked(
    short_runtime_path: Path,
) -> None:
    socket_path = short_runtime_path / "scs.sock"

    async def ignore_scswire_probe(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await reader.read(1024)
        writer.write(b"\x00\x00\x00\x01x")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    foreign = await asyncio.start_unix_server(ignore_scswire_probe, path=socket_path)
    identity = socket_path.stat().st_ino
    try:
        with pytest.raises(RuntimeError, match="live foreign peer"):
            await WireServer(Router(), socket_path=socket_path).start()
        assert socket_path.stat().st_ino == identity
    finally:
        foreign.close()
        await foreign.wait_closed()
        socket_path.unlink(missing_ok=True)
