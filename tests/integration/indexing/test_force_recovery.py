"""Force recovery must distinguish old hashes from this job's completed work."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import scs.indexing.pipeline as pipeline_module
from scs.indexing.jobs import IngestionJobStore
from scs.indexing.pipeline import IngestionPipeline
from scs.indexing.runner import IngestionJobRunner
from scs.providers.base import ProviderUnavailableError

from conftest import FakeEmbeddings, FakeGraph, FakeParser


class RetryEmbeddings(FakeEmbeddings):
    def __init__(self) -> None:
        super().__init__()
        self.calls: int = 0
        self.fail_on_call: int | None = None

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise ProviderUnavailableError("injected provider outage")
        return await super().embed_documents(texts)


def write_sources(repository: Path, count: int) -> None:
    for index in range(count):
        (repository / f"module_{index}.py").write_text(
            f"def symbol_{index}():\n    return {index}\n", encoding="utf-8"
        )


@pytest.mark.asyncio
async def test_reclaimed_force_snapshot_rebuilds_preexisting_hashes(
    repository: Path, tmp_path: Path,
) -> None:
    write_sources(repository, 2)
    graph, embeddings = FakeGraph(), FakeEmbeddings()
    pipeline = IngestionPipeline(graph=graph, parser=FakeParser(), embeddings=embeddings)
    await asyncio.to_thread(pipeline.ingest, repository)
    embeddings.document_inputs.clear()
    store = IngestionJobStore(tmp_path / "jobs.db")
    job = store.enqueue(repo_path=str(repository), mode="force_full", reason="test")
    assert store.claim_next(lease_owner="dead", lease_seconds=-1) is not None
    store.install_force_full_snapshot(
        job.id, files=await asyncio.to_thread(pipeline.create_force_full_snapshot, repository)
    )
    store.reclaim_stale_running(lease_owner="replacement", reclaim_other_owners=True)
    runner = IngestionJobRunner(store=store, graph=graph, pipeline_factory=lambda _: pipeline)

    assert await runner.run_once()

    completed = store.get(job.id)
    assert completed is not None and completed.status == "completed"
    assert completed.result is not None and completed.result["files_changed"] == 2
    assert embeddings.document_inputs


@pytest.mark.asyncio
async def test_force_retry_skips_only_job_acknowledged_batches(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline_module, "INGESTION_BATCH_MAX_FILES", 1)
    write_sources(repository, 3)
    graph, embeddings = FakeGraph(), RetryEmbeddings()
    pipeline = IngestionPipeline(graph=graph, parser=FakeParser(), embeddings=embeddings)
    await asyncio.to_thread(pipeline.ingest, repository)
    embeddings.calls = 0
    embeddings.document_inputs.clear()
    embeddings.fail_on_call = 2
    store = IngestionJobStore(tmp_path / "jobs.db")
    job = store.enqueue(repo_path=str(repository), mode="force_full", reason="test")
    runner = IngestionJobRunner(store=store, graph=graph, pipeline_factory=lambda _: pipeline)

    assert await runner.run_once()
    retried = store.get(job.id)
    assert retried is not None and retried.status == "queued"
    embeddings.document_inputs.clear()
    embeddings.fail_on_call = None
    assert await runner.run_once()

    completed = store.get(job.id)
    assert completed is not None and completed.status == "completed"
    assert completed.result is not None and completed.result["files_changed"] == 2
    assert all("symbol_0" not in text for text in embeddings.document_inputs)
    assert any("symbol_1" in text for text in embeddings.document_inputs)
    assert any("symbol_2" in text for text in embeddings.document_inputs)


@pytest.mark.asyncio
async def test_force_retry_replays_native_commit_without_queue_acknowledgement(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_sources(repository, 1)
    graph, embeddings = FakeGraph(), FakeEmbeddings()
    pipeline = IngestionPipeline(graph=graph, parser=FakeParser(), embeddings=embeddings)
    await asyncio.to_thread(pipeline.ingest, repository)
    store = IngestionJobStore(tmp_path / "jobs.db")
    job = store.enqueue(repo_path=str(repository), mode="force_full", reason="test")
    runner = IngestionJobRunner(store=store, graph=graph, pipeline_factory=lambda _: pipeline)

    def fail_acknowledgement(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected failure before queue acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(store, "acknowledge_force_full_snapshot_files", fail_acknowledgement)
        assert await runner.run_once()
    retried = store.get(job.id)
    assert retried is not None and retried.status == "queued"
    embeddings.document_inputs.clear()

    assert await runner.run_once()

    completed = store.get(job.id)
    assert completed is not None and completed.status == "completed"
    assert completed.result is not None and completed.result["files_changed"] == 1
    assert embeddings.document_inputs
