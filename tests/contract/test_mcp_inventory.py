"""Exact accepted disposition for the former 45-tool External product MCP surface."""

from scs.mcp.inventory import (
    FORMER_EXTERNAL_PRODUCT_TOOL_COUNT,
    MOVED_TO_SCS_TOOLS,
    RETIRED_TOOLS,
)


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

