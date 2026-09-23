"""Security invariants for MCP repository and LSP inputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scs.mcp.server import build_mcp


@dataclass(slots=True)
class RecordingGateway:
    calls: list[tuple[str, dict[str, object] | None]] = field(default_factory=list)

    async def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.calls.append((method, params))
        return {"accepted": True}


@dataclass(slots=True)
class DeleteGateway(RecordingGateway):
    async def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.calls.append((method, params))
        return {"accepted": True, "already_absent": True, "job": None}


@pytest.mark.asyncio
async def test_incremental_ingestion_rejects_source_escape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    gateway = RecordingGateway()

    with pytest.raises(Exception, match="escapes repository"):
        await build_mcp(gateway).call_tool(
            "ingest_files",
            {"repo_path": str(repo), "file_paths": [str(outside)]},
        )

    assert gateway.calls == []


@pytest.mark.asyncio
async def test_incremental_ingestion_rejects_deleted_path_escape(
    tmp_path: Path,
) -> None:
    gateway = RecordingGateway()

    with pytest.raises(Exception, match="repository-relative"):
        await build_mcp(gateway).call_tool(
            "ingest_files",
            {"repo_path": str(tmp_path), "deleted_paths": ["../outside.py"]},
        )

    assert gateway.calls == []


@pytest.mark.asyncio
async def test_repository_deletion_accepts_a_missing_source_directory(
    tmp_path: Path,
) -> None:
    missing_repository = tmp_path / "removed-repository"
    gateway = DeleteGateway()

    result = await build_mcp(gateway).call_tool(
        "delete_repository",
        {"repo_path": str(missing_repository)},
    )

    assert result.is_error is False
    assert gateway.calls == [
        (
            "repository.drop_index",
            {"repo_path": str(missing_repository.resolve())},
        )
    ]


@pytest.mark.asyncio
async def test_repository_deletion_rejects_an_empty_path() -> None:
    gateway = DeleteGateway()

    with pytest.raises(Exception, match="repo_path must be a non-empty string"):
        await build_mcp(gateway).call_tool(
            "delete_repository",
            {"repo_path": ""},
        )

    assert gateway.calls == []


@pytest.mark.asyncio
async def test_query_rejects_missing_source_position_path(tmp_path: Path) -> None:
    """Missing sources fail at the MCP boundary with a durable public error."""
    gateway = RecordingGateway()
    missing = tmp_path / "missing.py"

    with pytest.raises(Exception, match="source path does not exist"):
        await build_mcp(gateway).call_tool(
            "query_code",
            {"goal": "find references", "repo_path": str(tmp_path),
             "source_position": {"file_path": str(missing), "line": 0}},
        )

    assert gateway.calls == []


def test_mcp_runtime_contains_no_source_mutation_lsp_operations() -> None:
    source = Path(__file__).parents[2] / "src" / "scs" / "mcp"
    runtime = "\n".join(
        path.read_text(encoding="utf-8") for path in source.rglob("*.py")
    )

    assert "workspace/applyEdit" not in runtime
    assert "workspace/executeCommand" not in runtime
    assert "write_text(" not in runtime
    assert 'open("w' not in runtime
