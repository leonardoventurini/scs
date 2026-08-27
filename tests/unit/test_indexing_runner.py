from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from scs.indexing.jobs import IngestionJobStore
from scs.indexing.runner import IngestionJobRunner


@dataclass
class Result:
    files_changed: int = 1


class Pipeline:
    def ingest(self, repo: Path, *, force: bool = False) -> Result:
        return Result()


class ForceRecordingPipeline(Pipeline):
    """Capture force invalidation flags passed to a durable retry."""

    def __init__(self, force_flags: list[bool]) -> None:
        self.force_flags = force_flags

    def create_force_full_snapshot(self, repo: Path) -> list[dict[str, object]]:
        return [
            {
                "rel_path": "source.py",
                "content_hash": "f" * 64,
                "language": "python",
                "byte_size": 1,
            }
        ]

    def acknowledged_force_snapshot_paths(self, repo: Path, snapshot: object) -> list[str]:
        del repo, snapshot
        return []

    def ingest(
        self,
        repo: Path,
        *,
        force: bool = False,
        force_snapshot: object = None,
        on_force_batch_acknowledged: Callable[[list[str]], None] | None = None,
    ) -> Result:
        self.force_flags.append(force)
        if on_force_batch_acknowledged is not None:
            on_force_batch_acknowledged(["source.py"])
        return Result()


class ReconciledForcePipeline(ForceRecordingPipeline):
    """Simulate a hash committed before the queue snapshot mirror was updated."""

    def acknowledged_force_snapshot_paths(self, repo: Path, snapshot: object) -> list[str]:
        del repo, snapshot
        return ["source.py"]


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    async def publish(self, event: str, payload) -> None:
        self.events.append((event, payload))


@pytest.mark.asyncio
async def test_runner_completes_only_after_pipeline_returns(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = IngestionJobStore(tmp_path / "jobs.db")
    job = store.enqueue(repo_path=str(repo), mode="full", reason="explicit")
    sink = Sink()
    runner = IngestionJobRunner(
        store=store,
        graph=object(),
        pipeline_factory=lambda _: Pipeline(),
        event_sink=sink,
    )

    assert await runner.run_once()

    assert store.get(job.id).status == "completed"
    assert sink.events[-1][1]["status"] == "completed"


@pytest.mark.asyncio
async def test_runner_publishes_store_readiness_before_job_completion(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = IngestionJobStore(tmp_path / "jobs.db")
    job = store.enqueue(repo_path=str(repo), mode="full", reason="explicit")
    completed: list[str] = []
    runner = IngestionJobRunner(
        store=store,
        graph=object(),
        pipeline_factory=lambda _: Pipeline(),
        on_completed=lambda indexed: completed.append(indexed.id),
    )

    assert await runner.run_once()

    assert completed == [job.id]
    assert store.get(job.id).status == "completed"


@pytest.mark.asyncio
async def test_runner_marks_store_stale_before_executing_pipeline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = IngestionJobStore(tmp_path / "jobs.db")
    job = store.enqueue(repo_path=str(repo), mode="full", reason="explicit")
    transitions: list[str] = []
    runner = IngestionJobRunner(
        store=store,
        graph=object(),
        pipeline_factory=lambda _: Pipeline(),
        on_started=lambda indexed: transitions.append(f"started:{indexed.id}"),
        on_completed=lambda indexed: transitions.append(f"completed:{indexed.id}"),
    )

    assert await runner.run_once()

    assert transitions == [f"started:{job.id}", f"completed:{job.id}"]


@pytest.mark.asyncio
async def test_force_full_retry_preserves_acknowledged_hash_checkpoints(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = IngestionJobStore(tmp_path / "jobs.db")
    job = store.enqueue(repo_path=str(repo), mode="force_full", reason="explicit")
    store.install_force_full_snapshot(
        job.id,
        files=[
            {
                "rel_path": "source.py",
                "content_hash": "f" * 64,
                "language": "python",
                "byte_size": 1,
            }
        ],
    )
    flags: list[bool] = []
    runner = IngestionJobRunner(
        store=store,
        graph=object(),
        pipeline_factory=lambda _: ForceRecordingPipeline(flags),
    )

    # The runner receives a durable retry row after a prior execution failed.
    running = store.claim_next(lease_owner="first")
    assert running is not None
    retried = store.fail_or_retry(running.id, error="provider unavailable")
    assert retried.status == "queued"

    assert await runner.run_once()

    assert flags == [False]


@pytest.mark.asyncio
async def test_force_full_first_execution_freezes_a_snapshot_and_invalidates_hashes(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = IngestionJobStore(tmp_path / "jobs.db")
    job = store.enqueue(repo_path=str(repo), mode="force_full", reason="explicit")
    flags: list[bool] = []
    runner = IngestionJobRunner(
        store=store,
        graph=object(),
        pipeline_factory=lambda _: ForceRecordingPipeline(flags),
    )

    assert await runner.run_once()

    completed = store.get(job.id)
    assert completed is not None
    assert flags == [True]
    snapshot = completed.payload["force_full_snapshot"]
    assert snapshot == {
        "store_generation": None,
        "store_id": None,
        "files": [
            {
                "acknowledged": True,
                "byte_size": 1,
                "content_hash": "f" * 64,
                "language": "python",
                "rel_path": "source.py",
            }
        ]
    }


@pytest.mark.asyncio
async def test_force_retry_reconciles_hash_checkpoint_before_selecting_pending_work(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = IngestionJobStore(tmp_path / "jobs.db")
    job = store.enqueue(repo_path=str(repo), mode="force_full", reason="explicit")
    store.install_force_full_snapshot(
        job.id,
        files=[
            {
                "rel_path": "source.py",
                "content_hash": "f" * 64,
                "language": "python",
                "byte_size": 1,
            }
        ],
    )
    flags: list[bool] = []
    runner = IngestionJobRunner(
        store=store,
        graph=object(),
        pipeline_factory=lambda _: ReconciledForcePipeline(flags),
    )

    assert await runner.run_once()

    completed = store.get(job.id)
    assert completed is not None and completed.status == "completed"
    snapshot = completed.payload["force_full_snapshot"]
    assert snapshot["files"][0]["acknowledged"] is True
    assert flags == [False]


@pytest.mark.asyncio
async def test_reclaimed_unstarted_force_snapshot_still_invalidates_hashes(
    tmp_path: Path,
) -> None:
    """A crash after snapshot persistence cannot turn force into a no-op."""

    repo = tmp_path / "repo"
    repo.mkdir()
    store = IngestionJobStore(tmp_path / "jobs.db")
    job = store.enqueue(repo_path=str(repo), mode="force_full", reason="explicit")
    store.install_force_full_snapshot(
        job.id,
        files=[
            {
                "rel_path": "source.py",
                "content_hash": "f" * 64,
                "language": "python",
                "byte_size": 1,
            }
        ],
    )
    assert store.claim_next(lease_owner="dead", lease_seconds=-1) is not None
    store.reclaim_stale_running(lease_owner="replacement", reclaim_other_owners=True)
    flags: list[bool] = []
    runner = IngestionJobRunner(
        store=store,
        graph=object(),
        pipeline_factory=lambda _: ForceRecordingPipeline(flags),
    )

    assert await runner.run_once()

    assert flags == [True]


@pytest.mark.asyncio
async def test_start_returns_while_background_job_is_active(tmp_path: Path) -> None:
    store = IngestionJobStore(tmp_path / "jobs.db")
    runner = IngestionJobRunner(
        store=store,
        graph=object(),
        pipeline_factory=lambda _: Pipeline(),
        poll_interval_seconds=0.01,
    )

    await asyncio.wait_for(runner.start(), timeout=0.1)
    await runner.stop()
