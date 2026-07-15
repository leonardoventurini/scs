"""SCSWire daemon integration tests over a real Unix socket."""

from __future__ import annotations

import shutil
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
