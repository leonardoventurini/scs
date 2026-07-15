"""End-to-end tests for the MCP reverse proxy.

Every test stands up a real aiohttp upstream on a free port and points a live
:class:`ProxyServer` at it, so the proxy's reconnection and replay behaviour
is exercised the same way it runs in production.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import json
from pathlib import Path
from collections.abc import AsyncIterator, Callable

import aiohttp
import pytest
from aiohttp import web

from scs_mcp_proxy.proxy import ProxyConfig, ProxyServer


def _free_port() -> int:
    """Return an ephemeral TCP port that is not currently bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Upstream:
    """Lightweight aiohttp server that tests drive directly."""

    def __init__(
        self,
        port: int,
        handlers: Callable[..., object] | list[tuple[str, str, Callable]],
    ):
        self._port = port
        self._handlers = handlers
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        """Bind the configured handlers on the chosen port."""
        app = web.Application()
        for method, path, handler in self._handlers:
            app.router.add_route(method, path, handler)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host="127.0.0.1", port=self._port)
        await self._site.start()

    async def stop(self) -> None:
        """Shut down the upstream so the proxy sees a connection refused."""
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


@pytest.fixture
async def proxy_pair(
    tmp_path: Path,
) -> AsyncIterator[
    Callable[[list[tuple[str, str, Callable]], ProxyConfig | None], "ProxyContext"]
]:
    """Yield a factory for building matched proxy+upstream pairs.

    The factory accepts an upstream handler list and an optional
    :class:`ProxyConfig`, returns a running :class:`ProxyContext`, and is
    responsible for tearing everything down at test exit.
    """
    contexts: list[ProxyContext] = []

    async def _factory(
        handlers: list[tuple[str, str, Callable]],
        config: ProxyConfig | None = None,
    ) -> "ProxyContext":
        upstream_port = _free_port()
        public_port = _free_port()
        resolved_config = config or ProxyConfig(
            public_port=public_port,
            upstream_port=upstream_port,
            wait_for_upstream_seconds=2.0,
            retry_interval_seconds=0.05,
            connect_timeout_seconds=1.0,
        )
        # Tests need the fixed ports even when the caller supplied a config
        # without knowing what's free.
        resolved_config = ProxyConfig(
            **{
                **resolved_config.__dict__,
                "public_port": public_port,
                "upstream_port": upstream_port,
                "discovery_path": tmp_path / f"mcp-{public_port}.json",
            }
        )
        ctx = ProxyContext(resolved_config, handlers)
        await ctx.start()
        contexts.append(ctx)
        return ctx

    yield _factory

    for ctx in contexts:
        await ctx.stop()


class ProxyContext:
    """Bundle of proxy + upstream handles returned by the factory fixture."""

    def __init__(
        self,
        config: ProxyConfig,
        handlers: list[tuple[str, str, Callable]],
    ) -> None:
        self.config = config
        self.upstream = _Upstream(config.upstream_port, handlers)
        self.proxy = ProxyServer(config)
        self.client: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        await self.upstream.start()
        await self.proxy.start()
        self.client = aiohttp.ClientSession()

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None
        await self.proxy.stop()
        await self.upstream.stop()

    @property
    def url(self) -> str:
        return f"http://{self.config.public_host}:{self.config.public_port}"


async def test_passthrough_preserves_status_headers_and_body(proxy_pair) -> None:
    """Happy path — the proxy forwards method, headers, body, and status."""
    seen_requests: list[tuple[str, bytes, dict[str, str]]] = []

    async def handler(request: web.Request) -> web.Response:
        body = await request.read()
        seen_requests.append((request.method, body, dict(request.headers)))
        return web.json_response({"echo": body.decode()}, headers={"X-Tool": "scs"})

    ctx = await proxy_pair([("POST", "/mcp", handler)])

    assert ctx.client is not None
    async with ctx.client.post(
        f"{ctx.url}/mcp",
        data=b'{"jsonrpc":"2.0","method":"tools/list","id":1}',
        headers={"Content-Type": "application/json", "X-Session": "abc"},
    ) as resp:
        assert resp.status == 200
        assert resp.headers["X-Tool"] == "scs"
        payload = await resp.json()
        assert payload == {"echo": '{"jsonrpc":"2.0","method":"tools/list","id":1}'}

    assert len(seen_requests) == 1
    method, body, headers = seen_requests[0]
    assert method == "POST"
    assert body == b'{"jsonrpc":"2.0","method":"tools/list","id":1}'
    assert headers.get("X-Session") == "abc"


async def test_proxy_request_trace_logs_at_debug(
    proxy_pair, caplog: pytest.LogCaptureFixture
) -> None:
    """High-frequency MCP traffic should not flood the default INFO dashboard."""
    caplog.set_level(logging.INFO, logger="scs_mcp_proxy.proxy")

    async def handler(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    ctx = await proxy_pair([("POST", "/mcp", handler)])

    assert ctx.client is not None
    async with ctx.client.post(f"{ctx.url}/mcp", data=b"{}") as resp:
        assert resp.status == 200

    assert "Proxy request received" not in caplog.text


async def test_health_endpoint_returns_proxy_metadata(proxy_pair) -> None:
    """The proxy health endpoint is served locally, without upstream forwarding."""

    async def handler(_: web.Request) -> web.Response:  # pragma: no cover
        return web.Response(text="not reached")

    ctx = await proxy_pair([("GET", "/health", handler)])

    assert ctx.client is not None
    async with ctx.client.get(f"{ctx.url}/health") as resp:
        assert resp.status == 200
        payload = await resp.json()

    assert payload == {
        "status": "ok",
        "service": "scs-mcp-proxy",
        "public": {"host": "127.0.0.1", "port": ctx.config.public_port},
        "upstream": {"host": "127.0.0.1", "port": ctx.config.upstream_port},
    }


async def test_proxy_atomically_publishes_and_removes_owned_discovery(
    proxy_pair,
) -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    ctx = await proxy_pair([("POST", "/mcp", handler)])
    discovery_path = ctx.config.discovery_path
    assert discovery_path is not None
    payload = json.loads(discovery_path.read_text(encoding="utf-8"))

    assert payload["service"] == "scs"
    assert payload["url"] == f"{ctx.url}/mcp"
    assert payload["generation"]
    assert not list(discovery_path.parent.glob(f".{discovery_path.name}.*.tmp"))

    await ctx.proxy.stop()
    assert not discovery_path.exists()


async def test_proxy_preserves_newer_discovery_generation(proxy_pair) -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    ctx = await proxy_pair([("POST", "/mcp", handler)])
    discovery_path = ctx.config.discovery_path
    assert discovery_path is not None
    discovery_path.write_text(
        json.dumps(
            {"service": "scs", "generation": "new-owner", "url": "http://new/mcp"}
        ),
        encoding="utf-8",
    )

    await ctx.proxy.stop()

    assert (
        json.loads(discovery_path.read_text(encoding="utf-8"))["generation"]
        == "new-owner"
    )


async def test_replays_request_across_upstream_restart(proxy_pair) -> None:
    """Simulate an daemon hot-reload: upstream dies, then comes back."""
    call_count = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal call_count
        call_count += 1
        body = await request.read()
        return web.json_response({"received": body.decode(), "call": call_count})

    ctx = await proxy_pair([("POST", "/mcp", handler)])

    await ctx.upstream.stop()

    async def delayed_restart() -> None:
        await asyncio.sleep(0.5)
        await ctx.upstream.start()

    restart_task = asyncio.create_task(delayed_restart())

    assert ctx.client is not None
    async with ctx.client.post(f"{ctx.url}/mcp", data=b'{"hello":"world"}') as resp:
        assert resp.status == 200
        payload = await resp.json()
        assert payload == {"received": '{"hello":"world"}', "call": 1}

    await restart_task
    assert call_count == 1


async def test_returns_503_when_upstream_never_recovers(proxy_pair) -> None:
    """Upstream stays dead past the wait window — client gets a clean 503."""

    async def handler(_: web.Request) -> web.Response:  # pragma: no cover
        return web.Response(text="never reached")

    ctx = await proxy_pair(
        [("POST", "/mcp", handler)],
        ProxyConfig(wait_for_upstream_seconds=0.2, retry_interval_seconds=0.05),
    )
    await ctx.upstream.stop()

    assert ctx.client is not None
    async with ctx.client.post(f"{ctx.url}/mcp", data=b'{"jsonrpc":"2.0"}') as resp:
        assert resp.status == 503
        assert resp.headers.get("Retry-After") == "2"
        payload = await resp.json()
        assert payload["error"]["type"] == "upstream_unavailable"


async def test_initialize_request_gets_jsonrpc_error_when_upstream_unavailable(
    proxy_pair,
) -> None:
    """MCP initialize should stay JSON-RPC decodable when the upstream is down."""

    async def handler(_: web.Request) -> web.Response:  # pragma: no cover
        return web.Response(text="never reached")

    ctx = await proxy_pair(
        [("POST", "/mcp", handler)],
        ProxyConfig(wait_for_upstream_seconds=0.2, retry_interval_seconds=0.05),
    )
    await ctx.upstream.stop()

    assert ctx.client is not None
    async with ctx.client.post(
        f"{ctx.url}/mcp",
        data=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"codex"}}}',
        headers={"Content-Type": "application/json"},
    ) as resp:
        assert resp.status == 503
        payload = await resp.json()

    assert payload == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": -32000,
            "message": "SCS MCP upstream is unavailable; retry initialization shortly.",
        },
    }


async def test_non_initialize_mcp_request_keeps_generic_timeout_error(
    proxy_pair,
) -> None:
    """Only initialize gets the JSON-RPC compatibility envelope."""

    async def handler(_: web.Request) -> web.Response:  # pragma: no cover
        return web.Response(text="never reached")

    ctx = await proxy_pair(
        [("POST", "/mcp", handler)],
        ProxyConfig(wait_for_upstream_seconds=0.2, retry_interval_seconds=0.05),
    )
    await ctx.upstream.stop()

    assert ctx.client is not None
    async with ctx.client.post(
        f"{ctx.url}/mcp",
        data=b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}',
        headers={"Content-Type": "application/json"},
    ) as resp:
        assert resp.status == 503
        payload = await resp.json()

    assert payload["error"]["type"] == "upstream_unavailable"


async def test_non_json_body_keeps_generic_timeout_error(proxy_pair) -> None:
    """Non-JSON requests should keep the proxy-local timeout envelope."""

    async def handler(_: web.Request) -> web.Response:  # pragma: no cover
        return web.Response(text="never reached")

    ctx = await proxy_pair(
        [("POST", "/mcp", handler)],
        ProxyConfig(wait_for_upstream_seconds=0.2, retry_interval_seconds=0.05),
    )
    await ctx.upstream.stop()

    assert ctx.client is not None
    async with ctx.client.post(
        f"{ctx.url}/mcp",
        data=b"not-json",
        headers={"Content-Type": "application/json"},
    ) as resp:
        assert resp.status == 503
        payload = await resp.json()

    assert payload["error"]["type"] == "upstream_unavailable"


async def test_streams_server_sent_events_back_to_client(proxy_pair) -> None:
    """SSE bodies must reach the client chunk-by-chunk without buffering."""

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        await response.prepare(request)
        for payload in ("first", "second", "third"):
            await response.write(f"data: {payload}\n\n".encode())
        await response.write_eof()
        return response

    ctx = await proxy_pair([("POST", "/mcp", handler)])

    assert ctx.client is not None
    async with ctx.client.post(f"{ctx.url}/mcp", data=b"{}") as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/event-stream"
        chunks: list[bytes] = []
        async for chunk in resp.content.iter_any():
            chunks.append(chunk)
    joined = b"".join(chunks).decode()
    assert "data: first" in joined
    assert "data: second" in joined
    assert "data: third" in joined


async def test_rejects_oversized_bodies_with_413(proxy_pair) -> None:
    """Proxy's body cap trips before the request ever touches the upstream."""

    async def handler(_: web.Request) -> web.Response:  # pragma: no cover
        return web.Response(text="should not be reached")

    ctx = await proxy_pair(
        [("POST", "/mcp", handler)],
        ProxyConfig(max_body_bytes=64),
    )
    oversize = b"x" * 128

    assert ctx.client is not None
    async with ctx.client.post(f"{ctx.url}/mcp", data=oversize) as resp:
        assert resp.status == 413
        payload = await resp.json()
        assert payload["error"]["type"] == "payload_too_large"


def test_filter_headers_drops_hop_by_hop_headers() -> None:
    """Unit-level check on the header filter used on both request legs."""
    from multidict import CIMultiDict

    from scs_mcp_proxy.proxy import _filter_headers

    filtered = _filter_headers(
        CIMultiDict(
            [
                ("Connection", "close"),
                ("Transfer-Encoding", "chunked"),
                ("Keep-Alive", "timeout=5"),
                ("Host", "example.com"),
                ("Content-Length", "42"),
                ("X-Business", "kept"),
                ("Authorization", "Bearer t"),
            ]
        )
    )

    keys = {name.lower() for name, _ in filtered}
    assert "connection" not in keys
    assert "transfer-encoding" not in keys
    assert "keep-alive" not in keys
    assert "host" not in keys
    assert "content-length" not in keys
    assert ("X-Business", "kept") in filtered
    assert ("Authorization", "Bearer t") in filtered


async def test_proxy_fails_closed_when_public_port_is_occupied(tmp_path: Path) -> None:
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen()
    port = occupied.getsockname()[1]
    server = ProxyServer(
        ProxyConfig(
            public_port=port,
            upstream_port=_free_port(),
            discovery_path=tmp_path / "mcp.json",
        )
    )

    try:
        with pytest.raises(RuntimeError, match="public port is unavailable"):
            await server.start()
    finally:
        occupied.close()

    assert not (tmp_path / "mcp.json").exists()
