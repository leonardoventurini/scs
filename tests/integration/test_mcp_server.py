"""Integration checks across FastMCP dispatch and the public SCS gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import override

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from scs.mcp.inventory import MCP_TOOL_NAMES
from scs.mcp.observability import ToolRecorder
from scs.mcp.server import build_mcp

pytestmark = pytest.mark.asyncio

ROUTE_OUTPUTS: dict[str, dict[str, object]] = {
    "knowledge.query": {
        "goal": "find parser", "repo_path": "/repo",
        "routing": {"playbook": "DISCOVER"},
        "evidence": {"symbols": [], "files": [], "relationships": [],
                     "references": [], "test_targets": []},
        "trace": [], "complete": True, "truncated": False,
        "degraded_stages": [], "timings": {"classification_ms": 0.0,
        "execution_ms": 0.0, "total_ms": 0.0},
    },
    "repository.ingest_files": {"accepted": True, "job": {"id": "job-1"}},
    "repository.index": {"accepted": True, "job": {"id": "job-2"}},
    "repository.drop_index": {
        "accepted": True,
        "already_absent": False,
        "job": {"id": "job-3"},
    },
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
        "structural_search_ready": False,
        "semantic_search_ready": False,
        "semantic_search_unavailable_reason": "disabled in test",
        "active_job": None,
        "latest_job": None,
        "retry_after_ms": None,
        "wait": None,
    },
}

EXPECTED_OUTPUT_FIELDS: dict[str, set[str]] = {
    "query_code": {
        "goal", "repo_path", "routing", "evidence", "trace", "complete",
        "truncated", "degraded_stages", "timings",
    },
    "ingest_files": {"accepted", "job"},
    "ingest_project": {"accepted", "job"},
    "delete_repository": {"accepted", "already_absent", "job"},
    "get_graph_stats": {
        "repo_path", "status", "total_nodes", "nodes_by_type",
        "embedding_count", "vector_index_count", "vector_index_scope",
        "ingestion_stats", "database_size_bytes", "vector_available",
        "vector_unavailable_reason", "structural_search_ready",
        "semantic_search_ready", "semantic_search_unavailable_reason",
        "active_job", "latest_job", "retry_after_ms", "wait",
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


async def test_every_retained_tool_dispatches_to_its_public_route(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("def retained():\n    return True\n", encoding="utf-8")
    repo = str(tmp_path.resolve())
    source_path = str(source.resolve())
    cases: list[tuple[str, dict[str, object], tuple[str, dict[str, object] | None]]] = [
        (
            "query_code",
            {"goal": "find parser", "repo_path": repo},
            (
                "knowledge.query",
                {"goal": "find parser", "repo_path": repo, "mode": "balanced",
                 "node_type": None, "symbol_name": None, "node_ids": [],
                 "file_paths": [], "source_position": None, "limit": 10},
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
            "delete_repository",
            {"repo_path": repo},
            ("repository.drop_index", {"repo_path": repo}),
        ),
        (
            "get_graph_stats",
            {"repo_path": repo},
            (
                "knowledge.stats",
                {"repo_path": repo, "wait_job_id": None,
                 "wait_timeout_seconds": 0.0},
            ),
        ),
    ]
    gateway = RecordingGateway()
    mcp = build_mcp(gateway)

    for name, arguments, expected in cases:
        await mcp.call_tool(name, arguments)
        assert gateway.calls[-1] == expected

    assert {name for name, _, _ in cases} == MCP_TOOL_NAMES


async def test_explicit_project_ingestion_is_acknowledged_without_waiting(
    tmp_path: Path,
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


async def test_repository_deletion_serializes_an_already_absent_result(
    tmp_path: Path,
) -> None:
    class AlreadyAbsentGateway(RecordingGateway):
        @override
        async def call(
            self,
            method: str,
            params: dict[str, object] | None = None,
        ) -> dict[str, object]:
            self.calls.append((method, params))
            return {"accepted": True, "already_absent": True, "job": None}

    gateway = AlreadyAbsentGateway()
    result = await build_mcp(gateway).call_tool(
        "delete_repository",
        {"repo_path": str(tmp_path)},
    )

    assert result.structured_content == {
        "accepted": True,
        "already_absent": True,
        "job": None,
    }
    assert gateway.calls == [
        ("repository.drop_index", {"repo_path": str(tmp_path.resolve())})
    ]


@pytest.mark.parametrize(
    "retired_name",
    [
        "search_knowledge", "test_coverage_map", "scs_diagnostics_snapshot",
        "ingest_git_history",
    ],
)
async def test_representative_retired_tools_are_unavailable(retired_name: str) -> None:
    with pytest.raises(ToolError, match=f"Unknown tool: {retired_name}"):
        await build_mcp(RecordingGateway()).call_tool(retired_name, {})


async def test_empty_repository_scope_is_rejected() -> None:
    with pytest.raises(ToolError, match="String should have at least 1 character"):
        await build_mcp(RecordingGateway()).call_tool(
            "query_code", {"goal": "find scope", "repo_path": ""}
        )


async def test_mcp_application_lists_exact_inventory() -> None:
    tools = await build_mcp(RecordingGateway()).list_tools()

    assert {tool.name for tool in tools} == MCP_TOOL_NAMES
    assert len(tools) == 5
    assert all(tool.annotations is not None for tool in tools)
    for tool in tools:
        annotations = tool.annotations
        assert annotations is not None
        if tool.name == "delete_repository":
            assert (
                annotations.read_only_hint,
                annotations.destructive_hint,
                annotations.idempotent_hint,
                annotations.open_world_hint,
            ) == (False, True, True, False)
        elif tool.name in {"ingest_project", "ingest_files"}:
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
        assert set(tool.output_schema["properties"]) == EXPECTED_OUTPUT_FIELDS[tool.name]
        assert tool.output_schema.get("additionalProperties") is not True


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


async def test_source_aliases_preserve_identity_in_mcp_forwarding(tmp_path: Path) -> None:
    target = tmp_path / "source.py"
    target.write_text("value = 1\n", encoding="utf-8")
    alias = tmp_path / "alias.py"
    alias.symlink_to(target.name)
    gateway = RecordingGateway()
    mcp = build_mcp(gateway)

    await mcp.call_tool(
        "ingest_files",
        {"repo_path": str(tmp_path), "file_paths": [str(target), str(alias)]},
    )

    assert gateway.calls[-1][1] == {
        "repo_path": str(tmp_path.resolve()),
        "file_paths": [str(target), str(alias)],
        "deleted_paths": [],
    }
