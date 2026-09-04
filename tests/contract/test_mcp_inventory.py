"""Exact contract for the SCS MCP surface."""

import pytest

from scs.mcp.inventory import MCP_TOOL_NAMES
from scs.mcp.server import build_mcp


class _UnusedGateway:
    async def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        raise AssertionError(f"unexpected service call: {method} {params}")


def test_inventory_contains_only_code_intelligence_tools() -> None:
    assert MCP_TOOL_NAMES == {
        "search_code",
        "graph_context",
        "get_related",
        "list_symbols",
        "inspect_file",
        "find_references",
        "regression_risk_report",
        "ingest_project",
        "ingest_files",
        "get_graph_stats",
    }


@pytest.mark.asyncio
async def test_runtime_inventory_matches_exact_allowlist() -> None:
    tools = await build_mcp(_UnusedGateway()).list_tools()

    assert {tool.name for tool in tools} == MCP_TOOL_NAMES


@pytest.mark.asyncio
async def test_model_facing_inventory_excludes_operational_diagnostics() -> None:
    names = {tool.name for tool in await build_mcp(_UnusedGateway()).list_tools()}

    assert not any(name.startswith("scs_") for name in names)
