"""Lifecycle for SCS's internal Streamable HTTP MCP endpoint."""

from __future__ import annotations

import asyncio
import socket

import uvicorn
from mcp.server.fastmcp import FastMCP


class MCPHTTPServer:
    """Run one localhost-only MCP endpoint without owning public discovery."""

    def __init__(
        self, app: FastMCP, *, host: str = "127.0.0.1", port: int = 28465
    ) -> None:
        self._app = app
        self._host = host
        self._port = port
        self._socket: socket.socket | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def address(self) -> tuple[str, int]:
        """Return the bound internal address after startup."""

        if self._socket is None:
            raise RuntimeError("SCS MCP HTTP server is not started")
        host, port = self._socket.getsockname()[:2]
        return str(host), int(port)

    async def start(self) -> None:
        """Bind the configured localhost port and start serving in the background."""

        if self._task is not None:
            raise RuntimeError("SCS MCP HTTP server is already started")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self._host, self._port))
            listener.listen(socket.SOMAXCONN)
            listener.setblocking(False)
        except OSError as error:
            listener.close()
            raise RuntimeError(
                f"SCS MCP internal port is unavailable: {self._host}:{self._port}"
            ) from error
        server = uvicorn.Server(
            uvicorn.Config(
                self._app.streamable_http_app(),
                host=self._host,
                port=self._port,
                log_level="warning",
                lifespan="on",
            )
        )
        self._socket = listener
        self._server = server
        self._task = asyncio.create_task(
            server.serve(sockets=[listener]),
            name="scs-mcp-http",
        )
        for _ in range(100):
            if server.started:
                return
            if self._task.done():
                await self._task
                raise RuntimeError("SCS MCP HTTP server stopped during startup")
            await asyncio.sleep(0.01)
        await self.stop()
        raise RuntimeError("SCS MCP HTTP server did not become ready")

    async def stop(self) -> None:
        """Stop the internal endpoint and release its listening socket."""

        server, task = self._server, self._task
        self._server = None
        self._task = None
        if server is not None:
            server.should_exit = True
        if task is not None:
            await task
        listener, self._socket = self._socket, None
        if listener is not None:
            listener.close()
