"""Live stdio-MCP-to-SCSWire lazy-daemon convergence."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from scs.mcp.inventory import MOVED_TO_SCS_TOOLS


@pytest.mark.asyncio
async def test_two_stdio_bridges_share_daemon_until_final_disconnect(
    tmp_path: Path,
) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="scs-mcp-", dir="/tmp"))
    environment = {
        **os.environ,
        "SCS_HOME": str(tmp_path / "home"),
        "SCS_RUNTIME_DIR": str(runtime),
        "SCS_LOG_DIR": str(tmp_path / "logs"),
        "SCS_MODEL_CACHE": str(tmp_path / "models"),
        "SCS_EMBEDDING_DIMENSION": "2",
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "scs.cli", "mcp"],
        env=environment,
        cwd=tmp_path,
    )

    try:
        async with stdio_client(parameters) as first_streams:
            async with ClientSession(*first_streams) as first:
                await first.initialize()
                first_tools = await first.list_tools()
                identity_path = runtime / "daemon-service.json"
                first_identity = json.loads(identity_path.read_text(encoding="utf-8"))

                async with stdio_client(parameters) as second_streams:
                    async with ClientSession(*second_streams) as second:
                        await second.initialize()
                        result = await second.call_tool("get_graph_stats", {})
                        second_identity = json.loads(
                            identity_path.read_text(encoding="utf-8")
                        )
                        assert (
                            second_identity["generation"]
                            == first_identity["generation"]
                        )
                        assert result.isError is False

                result = await first.call_tool("get_graph_stats", {})
                assert result.isError is False
                assert {tool.name for tool in first_tools.tools} == MOVED_TO_SCS_TOOLS

        for _ in range(100):
            if not identity_path.exists():
                break
            await asyncio.sleep(0.05)

        assert not identity_path.exists()
        assert not (runtime / "scs.sock").exists()
    finally:
        shutil.rmtree(runtime, ignore_errors=True)
