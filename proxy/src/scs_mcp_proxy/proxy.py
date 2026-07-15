"""MCP reverse-proxy with bounded queueing across daemon restarts.

Owns the public MCP port permanently and forwards each request to the daemon's
internal MCP port. When the daemon is briefly unreachable — the common case
during a hot-reload — the proxy retries the *initial connection* for a bounded
window and transparently replays the queued request once the upstream returns.

What this proxy can and cannot do:

* **Pre-connect failures** (the typical restart blackout) are replayed. The
  request never touched the backend, so replaying it has the same semantics
  as the client retrying itself — only without the user-visible error.
* **Post-connect failures** surface as ``503`` (before response headers) or
  a truncated response body (after headers). We do not replay a request that
  may have been partially executed, since we cannot know whether the dying
  backend committed a side effect.
* **MCP session invalidation** — when the new daemon comes up it generates
  fresh session IDs. If the client sends an old session ID the upstream will
  reject it; the proxy forwards that rejection verbatim so the client can
  re-``initialize`` the standard MCP way.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from aiohttp import web

from scs_mcp_proxy.discovery import DiscoveryPublisher, ServiceIdentityPublisher

logger = logging.getLogger(__name__)


# Hop-by-hop headers that must not be copied across the proxy boundary, per
# RFC 7230 §6.1. ``host`` and ``content-length`` are also stripped because
# aiohttp recomputes them from the outgoing request.
_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)

_DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024
_STREAM_CHUNK_BYTES = 8192
_DEFAULT_DISCOVERY_PATH = (
    Path.home() / "Library" / "Application Support" / "SCS" / "mcp.json"
)


@dataclass(frozen=True)
class ProxyConfig:
    """Immutable configuration for :class:`ProxyServer`.

    ``wait_for_upstream_seconds`` bounds how long the proxy will hold a client
    request while retrying the initial upstream connection. Default matches
    the observed ~2.5s daemon-restart blackout with headroom.
    """

    public_host: str = "127.0.0.1"
    public_port: int = 28463
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 28465
    wait_for_upstream_seconds: float = 5.0
    retry_interval_seconds: float = 0.1
    max_body_bytes: int = _DEFAULT_MAX_BODY_BYTES
    response_sock_read_seconds: float = 300.0
    connect_timeout_seconds: float = 2.0
    discovery_path: Path | None = _DEFAULT_DISCOVERY_PATH
    identity_path: Path | None = None

    @property
    def upstream_base(self) -> str:
        """Return the upstream origin without a trailing slash."""
        return f"http://{self.upstream_host}:{self.upstream_port}"


class _UpstreamUnreachable(RuntimeError):
    """Raised when the upstream stays unreachable past the wait window."""


def _filter_headers(headers: object) -> list[tuple[str, str]]:
    """Drop hop-by-hop headers before copying between client and upstream."""
    return [(k, v) for k, v in headers.items() if k.lower() not in _HOP_HEADERS]


def _decode_jsonrpc_request(body: bytes) -> dict[str, Any] | None:
    """Best-effort JSON-RPC request decoder for MCP compatibility shims."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _is_mcp_initialize(payload: dict[str, Any]) -> bool:
    """Return true when the decoded body is an MCP initialize request."""
    if payload.get("jsonrpc") != "2.0":
        return False
    return str(payload.get("method") or "").strip() == "initialize"


def _mcp_initialize_error_response(
    request_id: object | None, message: str
) -> web.Response:
    """Return a JSON-RPC error envelope clients can decode during initialize."""
    return web.json_response(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32000,
                "message": message,
            },
        },
        status=503,
        headers={"Retry-After": "2"},
    )


async def _connect_with_retry(
    session: aiohttp.ClientSession,
    *,
    method: str,
    url: str,
    headers: list[tuple[str, str]],
    body: bytes,
    config: ProxyConfig,
    clock: Callable[[], float],
) -> aiohttp.ClientResponse:
    """Open the upstream request, retrying pre-connect failures.

    Returns the live :class:`aiohttp.ClientResponse` once headers come back.
    ``aiohttp.ClientConnectorError`` / :class:`ConnectionResetError` raised
    before the response headers arrive indicate the upstream was not yet
    serving; we retry until ``wait_for_upstream_seconds`` elapses.
    """
    deadline = clock() + config.wait_for_upstream_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            return await session.request(
                method=method,
                url=url,
                headers=headers,
                data=body,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(
                    total=None,
                    sock_connect=config.connect_timeout_seconds,
                    sock_read=config.response_sock_read_seconds,
                ),
            )
        except (aiohttp.ClientConnectorError, ConnectionResetError) as exc:
            now = clock()
            if now >= deadline:
                raise _UpstreamUnreachable(
                    f"Upstream MCP server at {url} unreachable after "
                    f"{config.wait_for_upstream_seconds:.1f}s"
                ) from exc
            logger.debug(
                "Upstream unreachable (attempt %d), retrying in %.2fs: %s",
                attempt,
                config.retry_interval_seconds,
                exc,
            )
            await asyncio.sleep(config.retry_interval_seconds)


def _make_handler(
    config: ProxyConfig,
    session_provider: Callable[[], aiohttp.ClientSession],
    clock: Callable[[], float] | None = None,
) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    """Build a request handler bound to the given config and client session."""
    resolve_clock = clock or (lambda: asyncio.get_running_loop().time())

    async def handler(request: web.Request) -> web.StreamResponse:
        body = await request.content.read()
        if len(body) > config.max_body_bytes:
            return web.json_response(
                {
                    "error": {
                        "message": (
                            f"Request body exceeds proxy limit of "
                            f"{config.max_body_bytes} bytes"
                        ),
                        "type": "payload_too_large",
                    }
                },
                status=413,
            )

        upstream_url = f"{config.upstream_base}{request.rel_url}"
        headers = _filter_headers(request.headers)
        logger.debug("Proxy request received: %s %s", request.method, request.rel_url)

        try:
            upstream_resp = await _connect_with_retry(
                session_provider(),
                method=request.method,
                url=upstream_url,
                headers=headers,
                body=body,
                config=config,
                clock=resolve_clock,
            )
        except _UpstreamUnreachable as exc:
            logger.warning(str(exc))
            decoded_request = (
                _decode_jsonrpc_request(body) if request.path == "/mcp" else None
            )
            if decoded_request is not None and _is_mcp_initialize(decoded_request):
                logger.info(
                    "Returning MCP-compatible initialize error for %s", request.rel_url
                )
                return _mcp_initialize_error_response(
                    decoded_request.get("id"),
                    "SCS MCP upstream is unavailable; retry initialization shortly.",
                )
            return web.json_response(
                {
                    "error": {
                        "message": str(exc),
                        "type": "upstream_unavailable",
                    }
                },
                status=503,
                headers={"Retry-After": "2"},
            )
        except aiohttp.ClientError as exc:
            logger.warning("Upstream request failed pre-response: %s", exc)
            return web.json_response(
                {
                    "error": {
                        "message": f"Upstream request failed: {exc}",
                        "type": "upstream_error",
                    }
                },
                status=502,
            )

        response = web.StreamResponse(
            status=upstream_resp.status,
            reason=upstream_resp.reason,
            headers=_filter_headers(upstream_resp.headers),
        )
        await response.prepare(request)

        try:
            async for chunk in upstream_resp.content.iter_chunked(_STREAM_CHUNK_BYTES):
                await response.write(chunk)
            await response.write_eof()
        except (aiohttp.ClientError, ConnectionResetError) as exc:
            # Backend died after we committed response headers. The best we
            # can do is close the connection; the client will see a truncated
            # response (SSE stream cut short or partial body). Intentionally
            # not replaying — we cannot tell whether the dying backend
            # committed a side effect mid-request.
            logger.warning("Upstream connection dropped mid-response: %s", exc)
        finally:
            upstream_resp.release()

        return response

    return handler


def build_app(
    config: ProxyConfig,
    *,
    session: aiohttp.ClientSession | None = None,
    clock: Callable[[], float] | None = None,
) -> web.Application:
    """Build the aiohttp application with all methods routed through the proxy.

    ``session`` lets tests inject a pre-built client; production code leaves
    it ``None`` so :class:`ProxyServer` manages the lifecycle.
    """
    app = web.Application(client_max_size=config.max_body_bytes)
    state: dict[str, aiohttp.ClientSession | None] = {"session": session}

    def _session_provider() -> aiohttp.ClientSession:
        resolved = state["session"]
        if resolved is None:
            raise RuntimeError("Proxy client session not initialised.")
        return resolved

    async def _on_startup(_app: web.Application) -> None:
        if state["session"] is None:
            # ``force_close=True`` disables connection pooling to the daemon.
            # MCP call volume is low and pooled sockets would otherwise go
            # stale across every daemon restart, forcing an error on the
            # first request after reconnect.
            state["session"] = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=64, force_close=True),
                auto_decompress=False,
            )

    async def _on_cleanup(_app: web.Application) -> None:
        owned = session is None
        resolved = state["session"]
        if owned and resolved is not None:
            await resolved.close()
            state["session"] = None

    async def _health(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "service": "scs-mcp-proxy",
                "public": {"host": config.public_host, "port": config.public_port},
                "upstream": {
                    "host": config.upstream_host,
                    "port": config.upstream_port,
                },
            }
        )

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/health", _health)
    app.router.add_route(
        "*", "/{tail:.*}", _make_handler(config, _session_provider, clock)
    )
    return app


class ProxyServer:
    """Convenience wrapper that binds, serves, and tears down the proxy.

    Usage::

        server = ProxyServer(ProxyConfig())
        await server.start()
        try:
            await server.wait_closed()
        finally:
            await server.stop()
    """

    def __init__(self, config: ProxyConfig) -> None:
        self._config = config
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._closed = asyncio.Event()
        self._generation = uuid.uuid4().hex
        self._discovery = (
            DiscoveryPublisher(config.discovery_path, generation=self._generation)
            if config.discovery_path is not None
            else None
        )
        identity_path = config.identity_path
        if identity_path is None and config.discovery_path is not None:
            identity_path = config.discovery_path.with_name("proxy-service.json")
        self._identity = (
            ServiceIdentityPublisher(
                identity_path,
                generation=self._generation,
                artifact_path=Path(__file__),
            )
            if identity_path is not None
            else None
        )

    @property
    def config(self) -> ProxyConfig:
        """Expose the immutable configuration this server was built with."""
        return self._config

    async def start(self) -> None:
        """Bind the public port and begin serving requests."""
        app = build_app(self._config)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(
            runner, host=self._config.public_host, port=self._config.public_port
        )
        try:
            await site.start()
        except OSError as error:
            await runner.cleanup()
            raise RuntimeError(
                "SCS MCP public port is unavailable: "
                f"{self._config.public_host}:{self._config.public_port}"
            ) from error
        self._runner = runner
        self._site = site
        try:
            if self._identity is not None:
                self._identity.publish()
            if self._discovery is not None:
                self._discovery.publish(
                    url=f"http://{self._config.public_host}:{self._config.public_port}/mcp"
                )
        except BaseException:
            if self._discovery is not None:
                self._discovery.remove_owned()
            if self._identity is not None:
                self._identity.remove_owned()
            await site.stop()
            await runner.cleanup()
            self._site = None
            self._runner = None
            raise
        logger.info(
            "MCP proxy listening on http://%s:%d -> http://%s:%d",
            self._config.public_host,
            self._config.public_port,
            self._config.upstream_host,
            self._config.upstream_port,
        )

    async def stop(self) -> None:
        """Stop the site and dispose of the app runner."""
        if self._discovery is not None:
            self._discovery.remove_owned()
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._identity is not None:
            self._identity.remove_owned()
        self._closed.set()

    async def wait_closed(self) -> None:
        """Block until :meth:`stop` has been called."""
        await self._closed.wait()
