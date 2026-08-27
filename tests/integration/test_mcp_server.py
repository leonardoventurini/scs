"""Integration checks across FastMCP dispatch and the public SCS gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
import socket

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp.exceptions import ToolError

from scs.mcp.http import MCPHTTPServer
from scs.mcp.inventory import MOVED_TO_SCS_TOOLS
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


@dataclass(slots=True)
class StaticGateway:
    """Return one exact SCSWire payload to exercise MCP output variants."""

    response: dict[str, object]

    async def call(
        self, method: str, params: dict[str, object] | None = None
    ) -> dict[str, object]:
        assert method == "lsp.references"
        del params
        return self.response


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

    assert {name for name, _, _ in cases} == MOVED_TO_SCS_TOOLS


async def test_search_dispatches_through_public_service_gateway(tmp_path) -> None:
    gateway = RecordingGateway()
    result = await build_mcp(gateway).call_tool(
        "search_code",
        {"query": "router contract", "repo_path": str(tmp_path), "limit": 999},
    )

    assert result[1]["query"] == "retained"
    assert result[1]["results"] == []
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
    assert len(tools.tools) == 10
    assert all(tool.annotations is not None for tool in tools.tools)
    for tool in tools.tools:
        annotations = tool.annotations
        assert annotations is not None
        if tool.name in {"ingest_project", "ingest_files"}:
            assert (
                annotations.readOnlyHint,
                annotations.destructiveHint,
                annotations.openWorldHint,
            ) == (False, True, False)
        else:
            assert (
                annotations.readOnlyHint,
                annotations.destructiveHint,
                annotations.idempotentHint,
                annotations.openWorldHint,
            ) == (True, False, True, False)
        assert tool.outputSchema is not None
        if tool.name != "find_references":
            assert set(tool.outputSchema["properties"]) == EXPECTED_OUTPUT_FIELDS[tool.name]
        assert tool.outputSchema.get("additionalProperties") is not True
    references = next(tool for tool in tools.tools if tool.name == "find_references")
    assert set(references.inputSchema["properties"]) == {"file_path", "line"}
    assert references.outputSchema is not None
    reference_result_schema = references.outputSchema["properties"]["result"]
    assert reference_result_schema.get("oneOf")
    assert reference_result_schema["discriminator"]["propertyName"] == "available"


@pytest.mark.parametrize(
    "response",
    [
        {
            "available": True,
            "source": "index",
            "symbol": {"id": "symbol"},
            "references": [{"id": "reference"}],
        },
        {
            "available": False,
            "source": "index",
            "file_path": "/repo/missing.py",
            "reason": "no indexed symbol exists at this position",
            "language_server_configured": False,
        },
    ],
)
async def test_streamable_http_returns_both_reference_variants(
    response: dict[str, object],
    tmp_path,
) -> None:
    source = tmp_path / "module.py"
    source.write_text("def referenced():\n    return 1\n", encoding="utf-8")
    server = MCPHTTPServer(build_mcp(StaticGateway(response)), port=0)
    await server.start()
    host, port = server.address
    try:
        async with streamable_http_client(f"http://{host}:{port}/mcp") as streams:
            read_stream, write_stream, _ = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "find_references", {"file_path": str(source), "line": 1}
                )
    finally:
        await server.stop()

    assert result.isError is False
    assert result.structuredContent == {"result": response}


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

    assert result[1]["status"] == "empty"
