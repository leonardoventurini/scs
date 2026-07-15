"""Async Unix-domain SCSWire server with conservative socket ownership."""

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
import uuid
from pathlib import Path

from pydantic import ValidationError

from scs.models import PROTOCOL_VERSION
from scs.wire.framing import FrameError, read_frame, write_frame
from scs.wire.models import (
    ErrorCode,
    WireError,
    WireErrorResponse,
    WireRequest,
    WireResponse,
)
from scs.wire.router import Router

SOCKET_MODE = 0o600
RUNTIME_DIRECTORY_MODE = 0o700
OWNERSHIP_PROBE_TIMEOUT_SECONDS = 0.5


class WireServer:
    """Serve finite SCSWire requests without exposing router internals."""

    def __init__(
        self,
        router: Router,
        *,
        socket_path: Path | None = None,
    ) -> None:
        self._router = router
        self._socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._client_tasks: set[asyncio.Task[object]] = set()

    @property
    def socket_path(self) -> Path:
        """Return the configured local control socket path."""

        if self._socket_path is None:
            raise RuntimeError("SCSWire server has no socket path")
        return self._socket_path

    async def start(self) -> None:
        """Bind the local socket after rejecting unsafe or live occupants."""

        if self._server is not None:
            raise RuntimeError("SCSWire server is already started")
        path = self.socket_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=RUNTIME_DIRECTORY_MODE)
        os.chmod(path.parent, RUNTIME_DIRECTORY_MODE)
        await self._prepare_socket_path(path)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_client,
                path=path,
            )
        except OSError as error:
            raise RuntimeError(f"cannot bind SCSWire socket: {path}") from error
        os.chmod(path, SOCKET_MODE)
        metadata = path.lstat()
        self._socket_identity = (metadata.st_dev, metadata.st_ino)

    async def stop(self) -> None:
        """Stop accepting work, drain handlers, and remove only our socket."""

        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        tasks = tuple(self._client_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._remove_owned_socket()

    async def dispatch_envelope(
        self,
        envelope: dict[str, object],
    ) -> dict[str, object]:
        """Validate and dispatch one request envelope into a public response."""

        request_id = envelope.get("id")
        safe_id = request_id if isinstance(request_id, str) else ""
        version = envelope.get("version")
        if version != PROTOCOL_VERSION:
            return self._error_response(
                safe_id,
                ErrorCode.INCOMPATIBLE_PROTOCOL,
                f"unsupported SCSWire protocol version: {version!r}",
            )
        try:
            request = WireRequest.model_validate(envelope)
        except ValidationError as error:
            return self._error_response(safe_id, ErrorCode.BAD_REQUEST, str(error))
        dispatch = await self._router.dispatch(request.method, request.params)
        if dispatch.error is not None:
            return WireErrorResponse(
                id=request.id,
                error=dispatch.error,
            ).model_dump(mode="json")
        return WireResponse(
            id=request.id,
            result=dispatch.value or {},
        ).model_dump(mode="json")

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        try:
            while True:
                try:
                    envelope = await read_frame(reader)
                except FrameError:
                    break
                response = await self.dispatch_envelope(envelope)
                await write_frame(writer, response)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            if task is not None:
                self._client_tasks.discard(task)

    async def _prepare_socket_path(self, path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(f"refusing symlink at SCSWire socket path: {path}")
        if not stat.S_ISSOCK(metadata.st_mode):
            raise RuntimeError(f"refusing non-socket at SCSWire socket path: {path}")
        if metadata.st_uid != os.getuid():
            raise RuntimeError(f"refusing socket owned by another user: {path}")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(path),
                timeout=OWNERSHIP_PROBE_TIMEOUT_SECONDS,
            )
        except (ConnectionError, OSError, TimeoutError):
            path.unlink()
            return
        request_id = f"ownership-{uuid.uuid4().hex}"
        try:
            await asyncio.wait_for(
                write_frame(
                    writer,
                    {
                        "kind": "request",
                        "id": request_id,
                        "version": PROTOCOL_VERSION,
                        "method": "system.health",
                        "params": {},
                    },
                ),
                timeout=OWNERSHIP_PROBE_TIMEOUT_SECONDS,
            )
            response = await asyncio.wait_for(
                read_frame(reader),
                timeout=OWNERSHIP_PROBE_TIMEOUT_SECONDS,
            )
            is_scswire = (
                response.get("id") == request_id
                and response.get("version") == PROTOCOL_VERSION
                and response.get("kind") in {"response", "error"}
            )
        except (ConnectionError, OSError, TimeoutError, FrameError):
            is_scswire = False
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
        if is_scswire:
            raise RuntimeError(f"SCSWire server is already active: {path}")
        path.unlink()

    def _remove_owned_socket(self) -> None:
        path = self._socket_path
        identity = self._socket_identity
        self._socket_identity = None
        if path is None or identity is None:
            return
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == identity:
            path.unlink()

    @staticmethod
    def _error_response(
        request_id: str,
        code: ErrorCode,
        message: str,
    ) -> dict[str, object]:
        return WireErrorResponse(
            id=request_id,
            error=WireError(code=code, message=message),
        ).model_dump(mode="json")
