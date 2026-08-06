"""Exact accepted disposition for the former 45-tool External product MCP surface."""

import pytest

from scs.mcp.inventory import (
    FORMER_EXTERNAL_PRODUCT_TOOL_COUNT,
    MOVED_TO_SCS_TOOLS,
    RETIRED_TOOLS,
)
from scs.mcp.server import build_mcp

ESSENTIAL_TOOLS = frozenset(
    {
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
)

EXPECTED_RETIRED_TOOLS = frozenset(
    {
        "patch_knowledge_graph",
        "build_knowledge_graph",
        "get_graph_build_status",
        "inspect_graph_namespace",
        "delete_graph_namespace",
        "update_node",
        "delete_nodes",
        "sync_vocabulary",
        "get_vocabulary",
        "edit_file",
        "style_token_audit",
        "render_html_snapshot",
        "recording_session_list",
        "recording_session_get",
        "recording_asset_list",
        "recording_asset_get",
        "recording_asset_probe",
        "recording_asset_quarantine",
        "ingest_git_history",
        "inspect_graph_quality",
        "sample_nodes",
        "test_coverage_map",
        "consistency_check",
        "contract_check",
        "get_symbols_overview",
        "find_symbol",
        "get_symbol_info",
        "search_knowledge",
        "get_node_detail",
        "scs_diagnostics_snapshot",
        "scs_mcp_health",
        "scs_recent_failures",
        "scs_index_health",
        "scs_dev_doctor",
        "scs_test_recommendations",
    }
)


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
    assert MOVED_TO_SCS_TOOLS == ESSENTIAL_TOOLS
    assert RETIRED_TOOLS == EXPECTED_RETIRED_TOOLS


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
async def test_model_facing_diagnostics_are_retired() -> None:
    names = {tool.name for tool in await build_mcp(_UnusedGateway()).list_tools()}

    assert not any(name.startswith("scs_") for name in names)
    assert {
        "scs_diagnostics_snapshot",
        "scs_mcp_health",
        "scs_recent_failures",
        "scs_index_health",
        "scs_dev_doctor",
        "scs_test_recommendations",
    } <= RETIRED_TOOLS
