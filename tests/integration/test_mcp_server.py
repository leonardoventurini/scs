"""Integration checks across FastMCP dispatch and the public SCS gateway."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from scs.mcp.inventory import MCP_TOOL_NAMES
from scs.mcp.observability import ToolRecorder
from scs.mcp.server import build_mcp

pytestmark = pytest.mark.asyncio


ROUTE_OUTPUTS: dict[str, dict[str, object]] = {
    "knowledge.search": {
        "query": "retained",
        "results": [],
        "neighbors": [],
        "total": 0,
        "retrieval_mode": "lexical",
    },
    "knowledge.related": {
        "symbol_name": None,
        "node_id": "node-1",
        "matches": [],
        "related": [],
    },
    "knowledge.graph_context": {
        "query": "retained",
        "direction": "both",
        "seeds": [],
        "context": [],
    },
    "knowledge.nodes.list": {"nodes": [], "total": 0, "limit": 50, "offset": 0},
    "repository.ingest_files": {"accepted": True, "job": {"id": "job-1"}},
    "repository.index": {"accepted": True, "job": {"id": "job-2"}},
    "knowledge.stats": {
        "repo_path": None,
        "status": "empty",
        "total_nodes": 0,
        "nodes_by_type": {},
        "embedding_count": 0,
        "vector_index_count": 0,
        "vector_index_scope": "global",
        "ingestion_stats": {},
        "database_size_bytes": 0,
        "vector_available": False,
        "vector_unavailable_reason": "disabled in test",
        "semantic_search_ready": False,
        "semantic_search_unavailable_reason": "disabled in test",
    },
    "knowledge.inspect_file": {
        "repo_path": "/repo",
        "file_path": "module.py",
        "nodes": [],
        "edges": {},
        "nodes_truncated": False,
        "edges_truncated": False,
    },
    "knowledge.composite.regression_risk": {
        "file_paths": [],
        "affected_node_ids": [],
        "dependents": [],
        "test_dependents": [],
    },
    "lsp.references": {
        "available": False,
        "source": "index",
        "file_path": "/repo/module.py",
        "reason": "not indexed",
        "language_server_configured": False,
    },
}

EXPECTED_OUTPUT_FIELDS: dict[str, set[str]] = {
    "search_code": {"query", "results", "neighbors", "total", "retrieval_mode"},
    "get_related": {"symbol_name", "node_id", "matches", "related"},
    "graph_context": {"query", "direction", "seeds", "context"},
    "list_symbols": {"nodes", "total", "limit", "offset"},
    "ingest_files": {"accepted", "job"},
    "ingest_project": {"accepted", "job"},
    "get_graph_stats": {
        "repo_path",
        "status",
        "total_nodes",
        "nodes_by_type",
        "embedding_count",
        "vector_index_count",
        "vector_index_scope",
        "ingestion_stats",
        "database_size_bytes",
        "vector_available",
        "vector_unavailable_reason",
        "semantic_search_ready",
        "semantic_search_unavailable_reason",
    },
    "inspect_file": {
        "repo_path",
        "file_path",
        "nodes",
        "edges",
        "nodes_truncated",
        "edges_truncated",
    },
    "regression_risk_report": {
        "file_paths",
        "affected_node_ids",
        "dependents",
        "test_dependents",
    },
}


@dataclass(slots=True)
class RecordingGateway:
    calls: list[tuple[str, dict[str, object] | None]] = field(default_factory=list)

    async def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.calls.append((method, params))
        return ROUTE_OUTPUTS[method]


async def test_every_retained_tool_dispatches_to_its_public_route(tmp_path) -> None:
    source = tmp_path / "module.py"
    source.write_text("def retained():\n    return True\n", encoding="utf-8")
    repo = str(tmp_path.resolve())
    source_path = str(source.resolve())
    cases: list[tuple[str, dict[str, object], tuple[str, dict[str, object] | None]]] = [
        (
            "search_code",
            {"query": "retained", "repo_path": repo},
            (
                "knowledge.search",
                {
                    "query": "retained",
                    "node_type": None,
                    "limit": 10,
                    "repo_path": repo,
                },
            ),
        ),
        (
            "get_related",
            {"node_id": "node-1", "repo_path": repo},
            (
                "knowledge.related",
                {
                    "symbol_name": None,
                    "node_id": "node-1",
                    "depth": 2,
                    "relationship": None,
                    "direction": "outgoing",
                    "repo_path": repo,
                },
            ),
        ),
        (
            "graph_context",
            {"query": "retained", "repo_path": repo},
            (
                "knowledge.graph_context",
                {
                    "query": "retained",
                    "node_type": None,
                    "vector_limit": 5,
                    "hop_limit": 2,
                    "direction": "both",
                    "repo_path": repo,
                },
            ),
        ),
        (
            "list_symbols",
            {"repo_path": repo},
            (
                "knowledge.nodes.list",
                {"node_type": "function", "limit": 50, "offset": 0, "repo_path": repo},
            ),
        ),
        (
            "ingest_files",
            {"repo_path": repo, "file_paths": [source_path]},
            (
                "repository.ingest_files",
                {"repo_path": repo, "file_paths": [source_path], "deleted_paths": []},
            ),
        ),
        (
            "ingest_project",
            {"repo_path": repo},
            ("repository.index", {"repo_path": repo}),
        ),
        (
            "get_graph_stats",
            {"repo_path": repo},
            ("knowledge.stats", {"repo_path": repo}),
        ),
        (
            "inspect_file",
            {"repo_path": repo, "file_path": source_path},
            (
                "knowledge.inspect_file",
                {
                    "repo_path": repo,
                    "file_path": "module.py",
                    "node_limit": 50,
                    "edge_limit": 100,
                },
            ),
        ),
        (
            "regression_risk_report",
            {"repo_path": repo, "file_paths": [source_path]},
            (
                "knowledge.composite.regression_risk",
                {"repo_path": repo, "file_paths": [source_path]},
            ),
        ),
        (
            "find_references",
            {"file_path": source_path, "line": 0},
            ("lsp.references", {"file_path": source_path, "line": 0}),
        ),
    ]
    gateway = RecordingGateway()
    mcp = build_mcp(gateway)

    for name, arguments, expected in cases:
        await mcp.call_tool(name, arguments)
        assert gateway.calls[-1] == expected

    assert {name for name, _, _ in cases} == MCP_TOOL_NAMES


async def test_search_dispatches_through_public_service_gateway(tmp_path) -> None:
    gateway = RecordingGateway()
    result = await build_mcp(gateway).call_tool(
        "search_code",
        {"query": "router contract", "repo_path": str(tmp_path), "limit": 999},
    )

    assert result.structured_content is not None
    assert result.structured_content["query"] == "retained"
    assert result.structured_content["results"] == []
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

    assert result.structured_content is not None
    assert result.structured_content["accepted"] is True
    assert gateway.calls == [
        ("repository.index", {"repo_path": str(tmp_path.resolve())})
    ]


@pytest.mark.parametrize(
    "retired_name",
    [
        "search_knowledge",
        "test_coverage_map",
        "scs_diagnostics_snapshot",
        "ingest_git_history",
    ],
)
async def test_representative_retired_tools_are_unavailable(retired_name: str) -> None:
    with pytest.raises(ToolError, match=f"Unknown tool: {retired_name}"):
        await build_mcp(RecordingGateway()).call_tool(retired_name, {})


async def test_empty_repository_scope_is_rejected() -> None:
    with pytest.raises(ToolError, match="repo_path must be a non-empty string"):
        await build_mcp(RecordingGateway()).call_tool(
            "search_code", {"query": "scope", "repo_path": ""}
        )


async def test_mcp_application_lists_exact_inventory() -> None:
    tools = await build_mcp(RecordingGateway()).list_tools()

    assert {tool.name for tool in tools} == MCP_TOOL_NAMES
    assert len(tools) == 10
    assert all(tool.annotations is not None for tool in tools)
    for tool in tools:
        annotations = tool.annotations
        assert annotations is not None
        if tool.name in {"ingest_project", "ingest_files"}:
            assert (
                annotations.read_only_hint,
                annotations.destructive_hint,
                annotations.open_world_hint,
            ) == (False, True, False)
        else:
            assert (
                annotations.read_only_hint,
                annotations.destructive_hint,
                annotations.idempotent_hint,
                annotations.open_world_hint,
            ) == (True, False, True, False)
        assert tool.output_schema is not None
        if tool.name != "find_references":
            assert set(tool.output_schema["properties"]) == EXPECTED_OUTPUT_FIELDS[tool.name]
        assert tool.output_schema.get("additionalProperties") is not True
    references = next(tool for tool in tools if tool.name == "find_references")
    assert set(references.input_schema["properties"]) == {"file_path", "line"}
    assert references.output_schema is not None
    reference_result_schema = references.output_schema["properties"]["result"]
    assert reference_result_schema.get("oneOf")
    assert reference_result_schema["discriminator"]["propertyName"] == "available"

async def test_observability_failure_is_fail_open() -> None:
    class BrokenRecorder(ToolRecorder):
        def record(self, event) -> None:
            raise RuntimeError("telemetry unavailable")

    gateway = RecordingGateway()
    result = await build_mcp(gateway, recorder=BrokenRecorder()).call_tool(
        "get_graph_stats",
        {},
    )

    assert result.structured_content is not None
    assert result.structured_content["status"] == "empty"
