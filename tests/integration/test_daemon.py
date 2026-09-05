"""SCSWire daemon integration tests over a real Unix socket."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from scs.wire.client import SCSClient, SCSConnection
from scs.models import PROTOCOL_VERSION
from scs.wire.framing import read_frame, write_frame
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


@pytest.mark.asyncio
async def test_attached_client_connection_owns_a_live_lease(
    short_runtime_path: Path,
) -> None:
    socket_path = short_runtime_path / "scs.sock"
    router = Router()
    counts: list[int] = []

    @router.method("system.client.attach")
    async def attach(_params: dict[str, object]) -> dict[str, object]:
        return {"attached": True}

    server = WireServer(
        router,
        socket_path=socket_path,
        client_count_changed=lambda count: _record_count(counts, count),
    )
    await server.start()
    connection = SCSConnection(socket_path)
    try:
        assert await connection.connect() == {"attached": True}
        assert counts == [1]
    finally:
        await connection.close()
        for _ in range(20):
            if counts == [1, 0]:
                break
            await asyncio.sleep(0.01)
        await server.stop()

    assert counts == [1, 0]


async def _record_count(counts: list[int], count: int) -> None:
    counts.append(count)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_state", ["idle", "partial_header", "partial_body", "attached"]
)
async def test_shutdown_closes_waiting_clients(
    short_runtime_path: Path,
    client_state: str,
) -> None:
    socket_path = short_runtime_path / "scs.sock"
    router = Router()
    counts: list[int] = []

    @router.method("system.client.attach")
    async def attach(_params: dict[str, object]) -> dict[str, object]:
        return {"attached": True}

    server = WireServer(
        router,
        socket_path=socket_path,
        client_count_changed=lambda count: _record_count(counts, count),
    )
    await server.start()
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        # A response establishes that the server has accepted this connection.
        await write_frame(
            writer,
            {
                "id": "ready",
                "version": PROTOCOL_VERSION,
                "kind": "request",
                "method": "system.client.attach"
                if client_state == "attached"
                else "health",
                "params": {},
            },
        )
        await read_frame(reader)
        if client_state == "partial_header":
            writer.write(b"\x00")
        elif client_state == "partial_body":
            writer.write((100).to_bytes(4, "big") + b"{")
        await writer.drain()

        stop_task = asyncio.create_task(server.stop())
        try:
            await asyncio.wait_for(asyncio.shield(stop_task), timeout=1)
            try:
                assert await asyncio.wait_for(reader.read(), timeout=1) == b""
            except ConnectionResetError:
                # Linux may reset a socket closed with unread partial-frame
                # bytes. Both reset and EOF prove this incomplete request closed;
                # idle and attached clients still require a graceful EOF.
                if client_state not in {"partial_header", "partial_body"}:
                    raise
            assert not socket_path.exists()
            assert counts == ([1, 0] if client_state == "attached" else [])
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            await asyncio.wait_for(stop_task, timeout=1)
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()
        await server.stop()


@pytest.mark.asyncio
async def test_shutdown_drains_dispatched_request_without_reading_next_request(
    short_runtime_path: Path,
) -> None:
    socket_path = short_runtime_path / "scs.sock"
    router = Router()
    entered = asyncio.Event()
    release = asyncio.Event()
    requests: list[dict[str, object]] = []

    @router.method("blocked")
    async def blocked(params: dict[str, object]) -> dict[str, object]:
        requests.append(params)
        entered.set()
        await release.wait()
        return {"finished": True}

    server = WireServer(router, socket_path=socket_path)
    await server.start()
    reader, writer = await asyncio.open_unix_connection(socket_path)
    stop_task: asyncio.Task[None] | None = None
    try:
        for index in range(2):
            await write_frame(
                writer,
                {
                    "id": str(index),
                    "version": PROTOCOL_VERSION,
                    "kind": "request",
                    "method": "blocked",
                    "params": {"index": index},
                },
            )
        await asyncio.wait_for(entered.wait(), timeout=1)
        stop_task = asyncio.create_task(server.stop())
        await asyncio.sleep(0)
        assert not stop_task.done()
        release.set()
        response = await asyncio.wait_for(read_frame(reader), timeout=1)
        assert response["result"] == {"finished": True}
        assert await asyncio.wait_for(reader.read(), timeout=1) == b""
        await asyncio.wait_for(asyncio.shield(stop_task), timeout=1)
        assert requests == [{"index": 0}]
        assert not socket_path.exists()
    finally:
        release.set()
        writer.close()
        await writer.wait_closed()
        if stop_task is not None:
            await asyncio.wait_for(stop_task, timeout=1)
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "probe_error", [PermissionError, TimeoutError, ConnectionResetError, OSError]
)
async def test_uncertain_socket_probe_preserves_occupant(
    short_runtime_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_error: type[OSError],
) -> None:
    socket_path = short_runtime_path / "scs.sock"
    occupant = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    occupant.bind(str(socket_path))
    occupant.close()
    identity = socket_path.stat().st_ino

    async def fail_probe(
        _path: Path,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise probe_error("probe failed")

    monkeypatch.setattr(asyncio, "open_unix_connection", fail_probe)
    server = WireServer(Router(), socket_path=socket_path)
    try:
        with pytest.raises(RuntimeError, match="cannot determine"):
            await server.start()
        assert socket_path.stat().st_ino == identity
    finally:
        await server.stop()
