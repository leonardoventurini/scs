from __future__ import annotations

import asyncio
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
