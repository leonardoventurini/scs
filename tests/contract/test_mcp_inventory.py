"""Exact accepted disposition for the former 45-tool External product MCP surface."""

import pytest

from scs.mcp.inventory import (
    FORMER_EXTERNAL_PRODUCT_TOOL_COUNT,
    MOVED_TO_SCS_TOOLS,
    RETIRED_TOOLS,
)
from scs.mcp.server import build_mcp


class _UnusedGateway:
    async def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        raise AssertionError(f"unexpected service call: {method} {params}")


def test_every_former_tool_has_exactly_one_disposition() -> None:
    assert MOVED_TO_SCS_TOOLS.isdisjoint(RETIRED_TOOLS)
    assert len(MOVED_TO_SCS_TOOLS | RETIRED_TOOLS) == FORMER_EXTERNAL_PRODUCT_TOOL_COUNT


def test_scs_inventory_is_read_only_for_repository_source() -> None:
    assert "edit_file" in RETIRED_TOOLS
    assert "style_token_audit" in RETIRED_TOOLS
    assert "edit_file" not in MOVED_TO_SCS_TOOLS


def test_external-product_product_tools_are_retired() -> None:
    assert "render_html_snapshot" in RETIRED_TOOLS
    assert "recording_session_list" in RETIRED_TOOLS
    assert "recording_asset_quarantine" in RETIRED_TOOLS


@pytest.mark.asyncio
async def test_runtime_inventory_matches_exact_accepted_allowlist() -> None:
    tools = await build_mcp(_UnusedGateway()).list_tools()

    assert {tool.name for tool in tools} == MOVED_TO_SCS_TOOLS
    assert not ({tool.name for tool in tools} & RETIRED_TOOLS)


@pytest.mark.asyncio
async def test_diagnostics_use_scs_identity_only() -> None:
    names = {tool.name for tool in await build_mcp(_UnusedGateway()).list_tools()}

    assert {name for name in names if name.startswith("scs_")} == {
        "scs_diagnostics_snapshot",
        "scs_mcp_health",
        "scs_recent_failures",
        "scs_index_health",
        "scs_dev_doctor",
        "scs_test_recommendations",
    }
    assert not any(name.startswith("external-product_") for name in names)
