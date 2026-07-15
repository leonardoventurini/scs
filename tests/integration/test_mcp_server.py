"""Integration checks across FastMCP dispatch and the public SCS gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
import socket

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from scs.mcp.http import MCPHTTPServer
from scs.mcp.inventory import MOVED_TO_SCS_TOOLS
from scs.mcp.observability import ToolRecorder
from scs.mcp.server import build_mcp

pytestmark = pytest.mark.asyncio


@dataclass(slots=True)
class RecordingGateway:
    calls: list[tuple[str, dict[str, object] | None]] = field(default_factory=list)

    async def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.calls.append((method, params))
        return {"service_method": method, "accepted": True}


async def test_search_dispatches_through_public_service_gateway(tmp_path) -> None:
    gateway = RecordingGateway()
    result = await build_mcp(gateway).call_tool(
        "search_code",
        {"query": "router contract", "repo_path": str(tmp_path), "limit": 999},
    )

    assert result[1] == {
        "service_method": "knowledge.search",
        "accepted": True,
    }
    assert gateway.calls == [
        (
            "knowledge.search",
            {
                "query": "router contract",
                "node_type": None,
                "limit": 200,
                "repo_path": str(tmp_path.resolve()),
            },
        )
    ]


async def test_explicit_project_ingestion_is_acknowledged_without_waiting(
    tmp_path,
) -> None:
    gateway = RecordingGateway()
    result = await build_mcp(gateway).call_tool(
        "ingest_project",
        {"repo_path": str(tmp_path)},
    )

    assert result[1]["accepted"] is True
    assert gateway.calls == [
        ("repository.index", {"repo_path": str(tmp_path.resolve())})
    ]


async def test_code_only_search_forces_data_scope(tmp_path) -> None:
    gateway = RecordingGateway()
    await build_mcp(gateway).call_tool(
        "search_knowledge",
        {"query": "semantic boundary", "repo_path": str(tmp_path)},
    )

    method, params = gateway.calls[0]
    assert method == "knowledge.search"
    assert params is not None
    assert params["data_scope"] == "code_and_provenance"


async def test_health_reports_exact_scs_inventory() -> None:
    gateway = RecordingGateway()
    result = await build_mcp(gateway).call_tool("scs_mcp_health", {})

    assert result[1]["service_method"] == "system.health"
    assert "search_code" in result[1]["mcp_tools"]


async def test_streamable_http_lists_exact_inventory_on_ephemeral_port() -> None:
    server = MCPHTTPServer(build_mcp(RecordingGateway()), port=0)
    await server.start()
    host, port = server.address
    try:
        async with streamable_http_client(f"http://{host}:{port}/mcp") as streams:
            read_stream, write_stream, _ = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
    finally:
        await server.stop()

    assert {tool.name for tool in tools.tools} == MOVED_TO_SCS_TOOLS


async def test_http_server_fails_closed_when_port_is_occupied() -> None:
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen()
    port = occupied.getsockname()[1]
    server = MCPHTTPServer(build_mcp(RecordingGateway()), port=port)

    try:
        with pytest.raises(RuntimeError, match="port is unavailable"):
            await server.start()
    finally:
        occupied.close()


async def test_observability_failure_is_fail_open() -> None:
    class BrokenRecorder(ToolRecorder):
        def record(self, event) -> None:
            raise RuntimeError("telemetry unavailable")

    gateway = RecordingGateway()
    result = await build_mcp(gateway, recorder=BrokenRecorder()).call_tool(
        "get_graph_stats",
        {},
    )

    assert result[1]["accepted"] is True
