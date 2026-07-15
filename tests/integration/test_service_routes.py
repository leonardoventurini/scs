"""Live SCSWire coverage for every method consumed by the MCP gateway."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from scs.config import SCSSettings
from scs.main import SCSDaemon
from scs.mcp.http import MCPHTTPServer
from scs.providers.base import ProviderMetadata, ProviderUnavailableError
from scs.service import ProcessLock
from scs.wire.client import SCSClient

MCP_GATEWAY_METHODS = frozenset(
    {
        "diagnostics.dev_doctor",
        "diagnostics.index_health",
        "diagnostics.recent_failures",
        "diagnostics.snapshot",
        "diagnostics.test_recommendations",
        "knowledge.composite.consistency_check",
        "knowledge.composite.contract_check",
        "knowledge.composite.regression_risk",
        "knowledge.composite.test_coverage",
        "knowledge.graph_context",
        "knowledge.inspect",
        "knowledge.inspect_file",
        "knowledge.nodes.get",
        "knowledge.nodes.list",
        "knowledge.related",
        "knowledge.sample",
        "knowledge.search",
        "knowledge.stats",
        "lsp.find_symbol",
        "lsp.hover",
        "lsp.references",
        "lsp.symbols",
        "repository.index",
        "repository.ingest_files",
        "repository.ingest_git_history",
        "system.health",
    }
)


class UnavailableEmbeddings:
    """Force deterministic lexical retrieval without loading a model."""

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata("test", "unavailable", 2, False, "disabled in test")

    async def embed_documents(self, texts: object) -> list[list[float]]:
        del texts
        raise ProviderUnavailableError("disabled in test")

    async def embed_query(self, text: str) -> list[float]:
        del text
        raise ProviderUnavailableError("disabled in test")


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "GIT_AUTHOR_NAME": "SCS Test",
            "GIT_AUTHOR_EMAIL": "scs@example.test",
            "GIT_COMMITTER_NAME": "SCS Test",
            "GIT_COMMITTER_EMAIL": "scs@example.test",
        },
    )


@pytest.mark.asyncio
async def test_every_mcp_gateway_method_is_a_live_public_route(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "sample.py"
    source.write_text("def production_symbol():\n    return 1\n", encoding="utf-8")
    test_source = repository / "tests" / "test_sample.py"
    test_source.parent.mkdir()
    test_source.write_text("def test_production_symbol():\n    assert True\n", encoding="utf-8")
    _git(repository, "init", "-q")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "initial indexed source")

    settings = SCSSettings(
        home=tmp_path / "home",
        model_cache=tmp_path / "models",
        runtime_dir=Path(tempfile.mkdtemp(prefix="scs-routes-", dir="/tmp")),
        log_dir=tmp_path / "logs",
        embedding_dimension=2,
        mcp_internal_port=0,
    )
    daemon = SCSDaemon(settings)
    await daemon.start()
    try:
        daemon._embeddings = UnavailableEmbeddings()
        graph = daemon._require_graph()
        repo_path = str(repository.resolve())
        repo_id = graph.get_or_create_repo_sync(repo_path)
        nodes = [
            {
                "id": "file-production",
                "type": "file",
                "name": "sample.py",
                "content": source.read_text(encoding="utf-8"),
                "metadata": {"file_path": "sample.py", "start_line": 0, "end_line": 2},
                "repo_id": repo_id,
            },
            {
                "id": "symbol-production",
                "type": "function",
                "name": "production_symbol",
                "content": "def production_symbol():",
                "metadata": {
                    "file_path": "sample.py",
                    "start_line": 0,
                    "end_line": 1,
                    "signature": "() -> int",
                },
                "repo_id": repo_id,
            },
            {
                "id": "file-test",
                "type": "file",
                "name": "tests/test_sample.py",
                "content": test_source.read_text(encoding="utf-8"),
                "metadata": {"file_path": "tests/test_sample.py", "start_line": 0, "end_line": 2},
                "repo_id": repo_id,
            },
            {
                "id": "symbol-test",
                "type": "function",
                "name": "test_production_symbol",
                "content": "def test_production_symbol():",
                "metadata": {"file_path": "tests/test_sample.py", "start_line": 0, "end_line": 1},
                "repo_id": repo_id,
            },
        ]
        graph.batch_upsert_nodes_sync(nodes)
        graph.batch_upsert_edges_sync(
            [
                {"source_id": "file-production", "target_id": "symbol-production", "relationship": "contains"},
                {"source_id": "file-test", "target_id": "symbol-test", "relationship": "contains"},
                {"source_id": "symbol-test", "target_id": "symbol-production", "relationship": "references"},
            ]
        )
        for relative, path in (("sample.py", source), ("tests/test_sample.py", test_source)):
            graph.upsert_ingested_file_sync(
                file_id=f"record-{relative}",
                repo_path=repo_path,
                rel_path=relative,
                content_hash=f"hash-{relative}",
                byte_size=path.stat().st_size,
                language="python",
            )

        params_by_method: dict[str, dict[str, object]] = {
            "system.health": {},
            "repository.index": {"repo_path": repo_path},
            "repository.ingest_files": {"repo_path": repo_path, "file_paths": [str(source)], "deleted_paths": []},
            "repository.ingest_git_history": {"repo_path": repo_path},
            "knowledge.search": {"query": "production_symbol", "repo_path": repo_path},
            "knowledge.related": {"symbol_name": "production_symbol", "depth": 1},
            "knowledge.graph_context": {"query": "production_symbol", "repo_path": repo_path},
            "knowledge.nodes.list": {"node_type": "function", "repo_path": repo_path},
            "knowledge.nodes.get": {"node_id": "symbol-production", "include_edges": True},
            "knowledge.stats": {},
            "knowledge.inspect": {"repo_path": repo_path},
            "knowledge.sample": {"node_type": "function", "repo_path": repo_path},
            "knowledge.inspect_file": {"repo_path": repo_path, "file_path": "sample.py"},
            "knowledge.composite.test_coverage": {"node_type": "function", "repo_path": repo_path},
            "knowledge.composite.regression_risk": {"file_paths": [str(source)], "repo_path": repo_path},
            "knowledge.composite.consistency_check": {"file_path": str(source), "repo_path": repo_path},
            "knowledge.composite.contract_check": {"symbol_name": "production_symbol", "repo_path": repo_path},
            "lsp.symbols": {"file_path": str(source)},
            "lsp.find_symbol": {"name": "production_symbol", "file_path": str(source)},
            "lsp.references": {"file_path": str(source), "line": 0, "column": 4},
            "lsp.hover": {"file_path": str(source), "line": 0, "column": 4},
            "diagnostics.snapshot": {"include_logs": False},
            "diagnostics.recent_failures": {"limit": 10},
            "diagnostics.index_health": {"repo_path": repo_path, "include_quality": True},
            "diagnostics.dev_doctor": {"repo_path": repo_path},
            "diagnostics.test_recommendations": {"changed_files": ["src/scs/services/routes.py"]},
        }
        assert params_by_method.keys() == MCP_GATEWAY_METHODS

        client = SCSClient(settings.paths.runtime / "scs.sock")
        results = {
            method: await client.call(method, params)
            for method, params in params_by_method.items()
        }

        assert results["system.health"]["ready"] is True
        assert "production_symbol" in {
            item["name"] for item in results["knowledge.search"]["results"]
        }
        assert results["knowledge.nodes.get"]["node"]["id"] == "symbol-production"
        assert results["knowledge.inspect_file"]["nodes"]
        assert results["knowledge.composite.test_coverage"]["covered"]
        assert results["lsp.symbols"]["available"] is True
        assert results["lsp.hover"]["contents"] == "() -> int"
        assert results["repository.ingest_git_history"]["commits_created"] >= 1
        assert results["diagnostics.snapshot"]["status"] == "healthy"
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_internal_mcp_failure_unwinds_wire_and_process_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="scs-unwind-", dir="/tmp"))
    settings = SCSSettings(
        home=tmp_path / "home",
        model_cache=tmp_path / "models",
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
        embedding_dimension=2,
        mcp_internal_port=0,
    )

    async def fail_start(_server: MCPHTTPServer) -> None:
        raise RuntimeError("synthetic MCP startup failure")

    monkeypatch.setattr(MCPHTTPServer, "start", fail_start)
    daemon = SCSDaemon(settings)

    with pytest.raises(RuntimeError, match="synthetic MCP startup failure"):
        await daemon.start()

    assert not (runtime / "scs.sock").exists()
    assert daemon._server is None
    assert daemon._graph is None
    assert daemon._jobs is None
    ownership = ProcessLock(settings.paths.home / ".daemon.lock")
    ownership.acquire()
    ownership.release()


@pytest.mark.asyncio
async def test_daemon_stops_internal_mcp_before_scswire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="scs-stop-order-", dir="/tmp"))
    settings = SCSSettings(
        home=tmp_path / "home",
        model_cache=tmp_path / "models",
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
        embedding_dimension=2,
        mcp_internal_port=0,
    )
    daemon = SCSDaemon(settings)
    await daemon.start()
    assert daemon._mcp_server is not None
    assert daemon._server is not None
    mcp_server = daemon._mcp_server
    wire_server = daemon._server
    original_mcp_stop = mcp_server.stop
    original_wire_stop = wire_server.stop
    stop_order: list[str] = []

    async def stop_mcp() -> None:
        stop_order.append("mcp")
        await original_mcp_stop()

    async def stop_wire() -> None:
        stop_order.append("wire")
        await original_wire_stop()

    monkeypatch.setattr(mcp_server, "stop", stop_mcp)
    monkeypatch.setattr(wire_server, "stop", stop_wire)

    await daemon.stop()

    assert stop_order[:2] == ["mcp", "wire"]
