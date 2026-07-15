"""Live MCP-to-SCSWire-to-daemon convergence over Streamable HTTP."""

from __future__ import annotations

import tempfile
import shutil
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from scs.config import SCSSettings
from scs.main import SCSDaemon
from scs.mcp.inventory import MOVED_TO_SCS_TOOLS


@pytest.mark.asyncio
async def test_live_mcp_health_crosses_the_public_service_boundary(tmp_path: Path) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="scs-mcp-e2e-", dir="/tmp"))
    settings = SCSSettings(
        home=tmp_path / "home",
        model_cache=tmp_path / "models",
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
        mcp_internal_port=0,
    )
    daemon = SCSDaemon(settings)
    await daemon.start()
    host, port = daemon.mcp_address
    try:
        async with streamable_http_client(f"http://{host}:{port}/mcp") as streams:
            read_stream, write_stream, _session_id = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("scs_mcp_health", {})
    finally:
        await daemon.stop()
        shutil.rmtree(runtime, ignore_errors=True)

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["service"] == "scs"
    assert result.structuredContent["ready"] is True
    assert {tool.name for tool in tools.tools} == MOVED_TO_SCS_TOOLS
