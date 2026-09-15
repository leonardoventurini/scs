"""Live SCSWire coverage for every method consumed by the MCP gateway."""

from __future__ import annotations

import asyncio
import json
import shutil
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
from scs.wire.client import SCSClient, SCSConnection, SCSWireError

JOB_COMPLETION_TIMEOUT_SECONDS = 10.0
JOB_POLL_INTERVAL_SECONDS = 0.05

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
        "repository.drop_index",
    }
)


async def _wait_for_job(
    client: SCSClient,
    *,
    repo_path: str,
    job_id: object,
) -> dict[str, object]:
    """Wait for one durable job and fail with its terminal state."""

    observed: dict[str, object] | None = None
    try:
        async with asyncio.timeout(JOB_COMPLETION_TIMEOUT_SECONDS):
            while True:
                jobs = cast(
                    list[dict[str, object]],
                    (await client.call("jobs.recent", {"repo_path": repo_path}))[
                        "jobs"
                    ],
                )
                observed = next((job for job in jobs if job["id"] == job_id), None)
                if observed is not None:
                    assert observed["status"] not in {"failed", "cancelled"}, (
                        f"Durable job terminated: {observed}"
                    )
                    if observed["status"] == "completed":
                        return observed
                await asyncio.sleep(JOB_POLL_INTERVAL_SECONDS)
    except TimeoutError:
        pytest.fail(f"Durable job {job_id} did not complete: {observed}")


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


class ImmediateEmbeddings:
    """Keep lifecycle tests independent from external embedding providers."""

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata("test", "immediate", 2)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0] for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        del text
        return [0.0, 1.0]


@pytest.mark.asyncio
async def test_repository_deletion_is_durable_and_preserves_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scs.main.OpenAICompatibleEmbeddingProvider",
        lambda **_kwargs: ImmediateEmbeddings(),
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "module.py"
    source.write_text("def retained_source():\n    return 42\n", encoding="utf-8")
    source_before = (
        source.read_bytes(),
        source.stat().st_mode,
        source.stat().st_mtime_ns,
    )
    runtime = Path(tempfile.mkdtemp(prefix="scs-delete-durable-", dir="/tmp"))
    settings = SCSSettings(
        home=tmp_path / "home",
        model_cache=tmp_path / "models",
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
        embedding_dimension=2,
    )
    repo_path = str(repository.resolve())
    daemon = SCSDaemon(settings)

    try:
        await daemon.start()
        client = SCSClient(runtime / "scs.sock")
        index_acknowledgement = await client.call(
            "repository.index", {"repo_path": repo_path}
        )
        index_job = cast(dict[str, object], index_acknowledgement["job"])
        await _wait_for_job(client, repo_path=repo_path, job_id=index_job["id"])

        record = daemon._require_stores().catalog.lookup(repo_path)
        assert record is not None
        store_path = settings.paths.home / "projects" / record.store_id
        assert store_path.exists()
        assert repo_path in daemon._watchers

        deletion = await client.call("repository.drop_index", {"repo_path": repo_path})

        assert deletion["accepted"] is True
        assert deletion["already_absent"] is False
        deletion_job = cast(dict[str, object], deletion["job"])
        await _wait_for_job(client, repo_path=repo_path, job_id=deletion_job["id"])

        assert daemon._require_stores().catalog.lookup(repo_path) is None
        assert not store_path.exists()
        assert repo_path not in daemon._watchers
        assert (
            source.read_bytes(),
            source.stat().st_mode,
            source.stat().st_mtime_ns,
        ) == (source_before)
        deleted_stats = await client.call("knowledge.stats", {"repo_path": repo_path})
        assert deleted_stats["status"] == "empty"
        assert deleted_stats["total_nodes"] == 0
        assert deleted_stats["embedding_count"] == 0
        assert deleted_stats["ingestion_stats"] == {}

        await daemon.stop()
        daemon = SCSDaemon(settings)
        await daemon.start()
        client = SCSClient(runtime / "scs.sock")

        assert daemon._require_stores().catalog.lookup(repo_path) is None
        assert repo_path not in daemon._watchers
        assert not store_path.exists()
        assert (await client.call("knowledge.stats", {"repo_path": repo_path}))[
            "status"
        ] == "empty"
    finally:
        await daemon.stop()
        shutil.rmtree(runtime, ignore_errors=True)


@pytest.mark.parametrize("source_state", ["moved", "removed"])
@pytest.mark.asyncio
async def test_repository_deletion_accepts_an_absent_source_and_repeats_as_a_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_state: str,
) -> None:
    monkeypatch.setattr(
        "scs.main.OpenAICompatibleEmbeddingProvider",
        lambda **_kwargs: ImmediateEmbeddings(),
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "module.py").write_text("value = 1\n", encoding="utf-8")
    runtime = Path(tempfile.mkdtemp(prefix="scs-delete-absent-", dir="/tmp"))
    settings = SCSSettings(
        home=tmp_path / "home",
        model_cache=tmp_path / "models",
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
        embedding_dimension=2,
    )
    daemon = SCSDaemon(settings)

    try:
        await daemon.start()
        client = SCSClient(runtime / "scs.sock")
        repo_path = str(repository.resolve())
        acknowledgement = await client.call(
            "repository.index", {"repo_path": repo_path}
        )
        job = cast(dict[str, object], acknowledgement["job"])
        await _wait_for_job(client, repo_path=repo_path, job_id=job["id"])

        if source_state == "moved":
            repository.rename(tmp_path / "moved-repository")
        else:
            shutil.rmtree(repository)
        assert not repository.exists()

        deletion = await client.call("repository.drop_index", {"repo_path": repo_path})
        assert deletion["accepted"] is True
        assert deletion["already_absent"] is False
        deletion_job = cast(dict[str, object], deletion["job"])
        await _wait_for_job(client, repo_path=repo_path, job_id=deletion_job["id"])

        repeated = await client.call("repository.drop_index", {"repo_path": repo_path})
        assert repeated == {
            "accepted": True,
            "already_absent": True,
            "job": None,
        }
    finally:
        await daemon.stop()
        shutil.rmtree(runtime, ignore_errors=True)


@pytest.mark.asyncio
async def test_active_repository_deletion_rejects_new_indexing_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scs.main.OpenAICompatibleEmbeddingProvider",
        lambda **_kwargs: ImmediateEmbeddings(),
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "module.py"
    source.write_text("value = 1\n", encoding="utf-8")
    runtime = Path(tempfile.mkdtemp(prefix="scs-delete-blocks-index-", dir="/tmp"))
    settings = SCSSettings(
        home=tmp_path / "home",
        model_cache=tmp_path / "models",
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
        embedding_dimension=2,
    )
    daemon = SCSDaemon(settings)

    try:
        await daemon.start()
        client = SCSClient(runtime / "scs.sock")
        repo_path = str(repository.resolve())
        acknowledgement = await client.call(
            "repository.index", {"repo_path": repo_path}
        )
        job = cast(dict[str, object], acknowledgement["job"])
        await _wait_for_job(client, repo_path=repo_path, job_id=job["id"])

        runner = daemon._runner
        assert runner is not None
        await runner.stop()
        deletion = await client.call("repository.drop_index", {"repo_path": repo_path})
        assert deletion["already_absent"] is False

        blocked_calls = (
            ("repository.index", {"repo_path": repo_path}),
            ("repository.reindex", {"repo_path": repo_path}),
            (
                "repository.ingest_files",
                {
                    "repo_path": repo_path,
                    "file_paths": [str(source)],
                    "deleted_paths": [],
                },
            ),
        )
        for method, params in blocked_calls:
            with pytest.raises(SCSWireError, match="deletion"):
                await client.call(method, params)
    finally:
        await daemon.stop()
        shutil.rmtree(runtime, ignore_errors=True)


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
            "repository.drop_index": {"repo_path": repo_path},
        }
        assert params_by_method.keys() == MCP_GATEWAY_METHODS

        client = SCSClient(settings.paths.runtime / "scs.sock")
        results = {
            method: await client.call(method, params)
            for method, params in params_by_method.items()
            if method != "repository.drop_index"
        }

        assert "production_symbol" in {
            item["name"] for item in results["knowledge.search"]["results"]
        }
        assert results["knowledge.search"]["queries"] == ["production_symbol"]
        assert results["knowledge.search"]["semantic_available"] is False
        assert results["knowledge.search"]["degraded_stage"] == "semantic"
        assert results["knowledge.search"]["timed_out"] is False
        assert set(results["knowledge.search"]["timings"]) == {
            "lexical_ms",
            "embedding_ms",
            "vector_ms",
            "rerank_ms",
            "total_ms",
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
        assert risk["total_dependents"] == 1
        assert risk["dependents_truncated"] is False
        assert risk["complete"] is True
        assert risk["test_targets"] == [
            {
                "file_path": "tests/test_sample.py",
                "evidence": [
                    {
                        "dependent_node_id": "symbol-test",
                        "affected_node_id": "symbol-production",
                        "relationship": "references",
                    }
                ],
            }
        ]
        assert all(value >= 0 for value in risk["timings"].values())
        related_by_name = await client.call(
            "knowledge.related",
            {"symbol_name": "production_symbol", "repo_path": repo_path},
        )
        assert [node["id"] for node in related_by_name["matches"]] == [
            "symbol-production"
        ]
        both_context = await client.call(
            "knowledge.graph_context",
            {
                "query": "production_symbol",
                "queries": ["sample helper"],
                "search_mode": "fast",
                "repo_path": repo_path,
                "direction": "both",
            },
        )
        assert both_context["direction"] == "both"
        assert both_context["search"]["queries"] == [
            "production_symbol",
            "sample helper",
        ]
        assert both_context["search"]["reranker_applied"] is False
        assert any(
            item["node"]["id"] == "file-production" for item in both_context["context"]
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
        assert scoped_empty["queries"] == ["production_symbol"]
        assert scoped_empty["degraded_reason"] == "repository is not indexed"
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

        # Deletion runs last because every other public route above reads the
        # project store that this lifecycle operation retires.
        results["repository.drop_index"] = await client.call(
            "repository.drop_index",
            params_by_method["repository.drop_index"],
        )
        assert results["repository.drop_index"]["accepted"] is True
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
