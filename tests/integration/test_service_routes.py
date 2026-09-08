"""Live SCSWire coverage for every method consumed by the MCP gateway."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import cast

import pytest

from scs.config import SCSSettings
from scs.indexing.jobs import IngestionJobStore
from scs.indexing.parser.native import NativeParser
from scs.indexing.pipeline import IngestionPipeline
from scs.main import SCSDaemon
from scs.providers.base import ProviderMetadata, ProviderUnavailableError
from scs.storage.registry import ProjectStoreRegistry
from scs.wire.client import SCSClient, SCSConnection

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


@pytest.mark.asyncio
async def test_startup_restores_watchers_without_opening_every_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    runtime = Path(tempfile.mkdtemp(prefix="scs-lazy-start-", dir="/tmp"))
    settings = SCSSettings(
        home=tmp_path / "home",
        model_cache=tmp_path / "models",
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
        embedding_dimension=2,
        auto_reindex_enabled=False,
    )
    registry = ProjectStoreRegistry(
        home=settings.paths.home,
        provider=UnavailableEmbeddings().metadata,
    )
    registry.ensure_graph(repository)
    registry.flush()

    def unexpected_open(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("startup eagerly opened a project graph")

    monkeypatch.setattr(ProjectStoreRegistry, "lookup_graph", unexpected_open)
    daemon = SCSDaemon(settings)

    try:
        await daemon.start()
        assert (runtime / "scs.sock").exists()
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_references_lazily_open_the_registered_project_graph(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "sample.py"
    source.write_text("def indexed_symbol():\n    return 1\n", encoding="utf-8")
    runtime = Path(tempfile.mkdtemp(prefix="scs-lazy-references-", dir="/tmp"))
    settings = SCSSettings(
        home=tmp_path / "home",
        model_cache=tmp_path / "models",
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
        embedding_dimension=2,
        auto_reindex_enabled=False,
    )
    registry = ProjectStoreRegistry(
        home=settings.paths.home,
        provider=UnavailableEmbeddings().metadata,
    )
    _record, graph = registry.ensure_graph(repository)
    pipeline = IngestionPipeline(graph=graph, parser=NativeParser())

    await asyncio.to_thread(pipeline.ingest, repository)
    registry.flush()
    # Model a prior daemon generation that released every native store handle.
    del pipeline, graph, registry

    daemon = SCSDaemon(settings)
    await daemon.start()

    try:
        result = await SCSClient(runtime / "scs.sock").call(
            "lsp.references",
            {"file_path": str(source), "line": 0},
        )

        assert result["available"] is True
        symbol = cast(dict[str, object], result["symbol"])
        assert symbol["name"] == "indexed_symbol"

        unavailable = await SCSClient(runtime / "scs.sock").call(
            "lsp.references",
            {"file_path": str(source), "line": 999},
        )

        assert unavailable == {
            "available": False,
            "source": "index",
            "file_path": str(source),
            "reason": "no indexed symbol exists at this position",
            "language_server_configured": False,
        }
    finally:
        await daemon.stop()


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
        reranking_model=None,
    )
    daemon = SCSDaemon(settings)
    await daemon.start()
    try:
        daemon._embeddings = UnavailableEmbeddings()
        repo_path = str(repository.resolve())
        _record, graph = daemon._require_stores().ensure_graph(repo_path)
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
        full_search = results["knowledge.search"]["results"][0]
        assert "created_at" in full_search
        compact_search = await client.call(
            "knowledge.search",
            {
                "query": "production_symbol",
                "repo_path": repo_path,
                "result_detail": "compact",
            },
        )
        assert compact_search["results"]
        assert set(compact_search["results"][0]) == {
            "content",
            "distance",
            "end_line",
            "file_path",
            "id",
            "name",
            "qualified_name",
            "signature",
            "start_line",
            "type",
        }
        assert len(json.dumps(compact_search)) < len(
            json.dumps(results["knowledge.search"])
        )
        assert results["knowledge.related"]["matches"][0]["id"] == "symbol-production"
        assert results["knowledge.stats"]["repo_path"] == repo_path
        assert results["knowledge.stats"]["total_nodes"] == 5
        assert results["knowledge.stats"]["vector_index_scope"] == "project"
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
async def test_final_attached_client_requests_daemon_shutdown(tmp_path: Path) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="scs-client-lease-", dir="/tmp"))
    settings = SCSSettings(
        home=tmp_path / "home",
        model_cache=tmp_path / "models",
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
        embedding_dimension=2,
    )

    daemon = SCSDaemon(settings)
    await daemon.start()
    connection = SCSConnection(runtime / "scs.sock")
    identity = await connection.connect()

    assert identity["attached"] is True
    assert identity["generation"]
    await connection.close()
    await asyncio.wait_for(daemon.wait_for_shutdown_request(), timeout=2)
    await daemon.stop()


@pytest.mark.asyncio
async def test_final_client_defers_shutdown_until_durable_jobs_are_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long indexing remains observable after its submitting client exits."""

    class ActiveThenIdleJobs:
        def __init__(self) -> None:
            self.checks = 0

        def has_active(self) -> bool:
            self.checks += 1
            return self.checks == 1

    runtime = Path(tempfile.mkdtemp(prefix="scs-active-job-", dir="/tmp"))
    settings = SCSSettings(
        home=tmp_path / "home",
        model_cache=tmp_path / "models",
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
        embedding_dimension=2,
    )
    monkeypatch.setattr("scs.main.CLIENT_HANDOFF_SECONDS", 0.01)
    daemon = SCSDaemon(settings)
    await daemon.start()
    jobs = ActiveThenIdleJobs()
    daemon._jobs = cast(IngestionJobStore, jobs)
    connection = SCSConnection(runtime / "scs.sock")
    await connection.connect()

    try:
        await connection.close()
        await asyncio.wait_for(daemon.wait_for_shutdown_request(), timeout=1)

        assert jobs.checks >= 2
        assert (runtime / "scs.sock").exists()
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_unattached_startup_grace_keeps_active_jobs_observable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered durable work must not leave a live lock without a socket."""

    class ActiveThenIdleJobs:
        def __init__(self) -> None:
            self.checks = 0

        def has_active(self) -> bool:
            self.checks += 1
            return self.checks == 1

    runtime = Path(tempfile.mkdtemp(prefix="scs-unattached-job-", dir="/tmp"))
    settings = SCSSettings(
        home=tmp_path / "home",
        model_cache=tmp_path / "models",
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
        embedding_dimension=2,
    )
    monkeypatch.setattr("scs.main.UNATTACHED_STARTUP_GRACE_SECONDS", 0.01)
    monkeypatch.setattr("scs.main.CLIENT_HANDOFF_SECONDS", 0.01)
    daemon = SCSDaemon(settings)
    await daemon.start()
    jobs = ActiveThenIdleJobs()
    daemon._jobs = cast(IngestionJobStore, jobs)
    daemon.arm_startup_grace()

    try:
        await asyncio.sleep(0.015)
        assert jobs.checks == 1
        assert (runtime / "scs.sock").exists()
        await asyncio.wait_for(daemon.wait_for_shutdown_request(), timeout=1)
        assert jobs.checks >= 2
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_upgrade_shutdown_requests_cancellation_before_exit(
    tmp_path: Path,
) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="scs-cancel-shutdown-", dir="/tmp"))
    settings = SCSSettings(
        home=tmp_path / "home",
        model_cache=tmp_path / "models",
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
        embedding_dimension=2,
    )
    daemon = SCSDaemon(settings)
    await daemon.start()
    original_jobs = daemon._jobs
    calls: list[bool] = []

    class Jobs:
        def request_cancel_all(self) -> list[object]:
            calls.append(True)
            return [object(), object()]

    daemon._jobs = cast(IngestionJobStore, Jobs())

    try:
        response = await SCSClient(runtime / "scs.sock").call(
            "system.shutdown",
            {"generation": daemon._generation, "cancel_active": True},
        )

        assert response["accepted"] is True
        assert response["cancelled_jobs"] == 2
        assert calls == [True]
        await asyncio.wait_for(daemon.wait_for_shutdown_request(), timeout=1)
    finally:
        daemon._jobs = original_jobs
        await daemon.stop()
