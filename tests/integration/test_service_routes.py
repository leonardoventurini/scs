"""Live SCSWire coverage for every method consumed by the MCP gateway."""

from __future__ import annotations

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
        "knowledge.composite.regression_risk",
        "knowledge.graph_context",
        "knowledge.inspect_file",
        "knowledge.nodes.list",
        "knowledge.related",
        "knowledge.search",
        "knowledge.stats",
        "lsp.references",
        "repository.index",
        "repository.ingest_files",
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


@pytest.mark.asyncio
async def test_every_mcp_gateway_method_is_a_live_public_route(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "sample.py"
    source.write_text("def production_symbol():\n    return 1\n", encoding="utf-8")
    test_source = repository / "tests" / "test_sample.py"
    test_source.parent.mkdir()
    test_source.write_text(
        "def test_production_symbol():\n    assert True\n", encoding="utf-8"
    )
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
                "id": "import-production",
                "type": "import",
                "name": "production_symbol",
                "content": "",
                "metadata": {
                    "file_path": "sample.py",
                    "start_line": 0,
                    "end_line": 0,
                },
                "repo_id": repo_id,
            },
            {
                "id": "file-test",
                "type": "file",
                "name": "tests/test_sample.py",
                "content": test_source.read_text(encoding="utf-8"),
                "metadata": {
                    "file_path": "tests/test_sample.py",
                    "start_line": 0,
                    "end_line": 2,
                },
                "repo_id": repo_id,
            },
            {
                "id": "symbol-test",
                "type": "function",
                "name": "test_production_symbol",
                "content": "def test_production_symbol():",
                "metadata": {
                    "file_path": "tests/test_sample.py",
                    "start_line": 0,
                    "end_line": 1,
                },
                "repo_id": repo_id,
            },
        ]
        graph.batch_upsert_nodes_sync(nodes)
        graph.batch_upsert_edges_sync(
            [
                {
                    "source_id": "file-production",
                    "target_id": "symbol-production",
                    "relationship": "contains",
                },
                {
                    "source_id": "file-production",
                    "target_id": "import-production",
                    "relationship": "contains",
                },
                {
                    "source_id": "file-test",
                    "target_id": "symbol-test",
                    "relationship": "contains",
                },
                {
                    "source_id": "symbol-test",
                    "target_id": "symbol-production",
                    "relationship": "references",
                },
            ]
        )
        for relative, path in (
            ("sample.py", source),
            ("tests/test_sample.py", test_source),
        ):
            graph.upsert_ingested_file_sync(
                file_id=f"record-{relative}",
                repo_path=repo_path,
                rel_path=relative,
                content_hash=f"hash-{relative}",
                byte_size=path.stat().st_size,
                language="python",
            )

        params_by_method: dict[str, dict[str, object]] = {
            "repository.index": {"repo_path": repo_path},
            "repository.ingest_files": {
                "repo_path": repo_path,
                "file_paths": [str(source)],
                "deleted_paths": [],
            },
            "knowledge.search": {"query": "production_symbol", "repo_path": repo_path},
            "knowledge.related": {
                "node_id": "symbol-production",
                "depth": 1,
                "repo_path": repo_path,
            },
            "knowledge.graph_context": {
                "query": "production_symbol",
                "repo_path": repo_path,
            },
            "knowledge.nodes.list": {"node_type": "function", "repo_path": repo_path},
            "knowledge.stats": {"repo_path": repo_path},
            "knowledge.inspect_file": {
                "repo_path": repo_path,
                "file_path": "sample.py",
            },
            "knowledge.composite.regression_risk": {
                "file_paths": [str(source)],
                "repo_path": repo_path,
            },
            "lsp.references": {"file_path": str(source), "line": 1},
        }
        assert params_by_method.keys() == MCP_GATEWAY_METHODS

        client = SCSClient(settings.paths.runtime / "scs.sock")
        results = {
            method: await client.call(method, params)
            for method, params in params_by_method.items()
        }

        assert "production_symbol" in {
            item["name"] for item in results["knowledge.search"]["results"]
        }
        assert results["knowledge.related"]["matches"][0]["id"] == "symbol-production"
        assert results["knowledge.stats"]["repo_path"] == repo_path
        assert results["knowledge.stats"]["total_nodes"] == 5
        assert results["knowledge.stats"]["semantic_search_ready"] is False
        assert (
            results["knowledge.stats"]["semantic_search_unavailable_reason"]
            == "disabled in test"
        )
        assert results["knowledge.inspect_file"]["nodes"]
        assert results["knowledge.inspect_file"]["nodes_truncated"] is False
        assert results["knowledge.inspect_file"]["edges_truncated"] is False
        assert results["lsp.references"]["available"] is True
        assert results["lsp.references"]["symbol"]["id"] == "symbol-production"
        risk = results["knowledge.composite.regression_risk"]
        assert {node["id"] for node in risk["dependents"]} == {"symbol-test"}
        assert {node["id"] for node in risk["test_dependents"]} == {"symbol-test"}
        related_by_name = await client.call(
            "knowledge.related",
            {"symbol_name": "production_symbol", "repo_path": repo_path},
        )
        assert [node["id"] for node in related_by_name["matches"]] == [
            "symbol-production"
        ]
        both_context = await client.call(
            "knowledge.graph_context",
            {"query": "production_symbol", "repo_path": repo_path, "direction": "both"},
        )
        assert both_context["direction"] == "both"
        assert any(
            item["node"]["id"] == "file-production"
            for item in both_context["context"]
        )
        bounded_file = await client.call(
            "knowledge.inspect_file",
            {
                "repo_path": repo_path,
                "file_path": "sample.py",
                "node_limit": 1,
                "edge_limit": 1,
            },
        )
        assert len(bounded_file["nodes"]) == 1
        assert sum(len(values) for values in bounded_file["edges"].values()) == 1
        assert bounded_file["nodes_truncated"] is True
        assert bounded_file["edges_truncated"] is True

        unindexed = tmp_path / "unindexed"
        unindexed.mkdir()
        unindexed_path = str(unindexed.resolve())
        scoped_empty = await client.call(
            "knowledge.search",
            {"query": "production_symbol", "repo_path": unindexed_path},
        )
        assert scoped_empty["results"] == []
        assert scoped_empty["retrieval_mode"] == "none"
        listing_empty = await client.call(
            "knowledge.nodes.list",
            {"node_type": "function", "repo_path": unindexed_path},
        )
        assert listing_empty["nodes"] == []
        stats_empty = await client.call(
            "knowledge.stats", {"repo_path": unindexed_path}
        )
        assert stats_empty["status"] == "empty"
        assert stats_empty["total_nodes"] == 0
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
