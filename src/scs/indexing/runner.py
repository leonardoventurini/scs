"""Non-blocking worker for SCS's durable explicit indexing queue."""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Protocol, cast

from scs.indexing.jobs import IngestionJob, IngestionJobStore, job_to_dict
from scs.indexing.pipeline import IngestionPipeline
from scs.indexing.repository_paths import assert_not_user_home_repo
from scs.providers.base import EventSink, NullEventSink

PipelineFactory = Callable[[IngestionJob], IngestionPipeline]


class DeletableGraph(Protocol):
    """Graph operation needed by the destructive queue job."""

    def delete_repo_sync(self, repo_path: str) -> object: ...


GraphResolver = Callable[[IngestionJob], DeletableGraph]
CompletionHandler = Callable[[IngestionJob], None]


class IngestionJobRunner:
    """Drain durable jobs in the background and publish transport-neutral events."""

    def __init__(
        self,
        *,
        store: IngestionJobStore,
        graph_for_job: GraphResolver | None = None,
        graph: DeletableGraph | None = None,
        on_started: CompletionHandler | None = None,
        on_completed: CompletionHandler | None = None,
        pipeline_factory: PipelineFactory,
        event_sink: EventSink | None = None,
        poll_interval_seconds: float = 1.0,
        lease_seconds: float = 300.0,
    ) -> None:
        self._store: IngestionJobStore = store
        if (graph_for_job is None) == (graph is None):
            raise ValueError("provide exactly one graph resolver or graph")
        self._graph_for_job: GraphResolver = (
            graph_for_job
            if graph_for_job is not None
            else lambda _job: cast(DeletableGraph, graph)
        )
        self._pipeline_factory: PipelineFactory = pipeline_factory
        self._on_started: CompletionHandler | None = on_started
        self._on_completed: CompletionHandler | None = on_completed
        self._events: EventSink = event_sink or NullEventSink()
        self._poll_interval_seconds: float = poll_interval_seconds
        self._lease_seconds: float = lease_seconds
        self._owner: str = f"{socket.gethostname()}:{os.getpid()}:{id(self)}"
        self._stop: asyncio.Event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Return immediately after scheduling stale-lease recovery and work."""

        if self._task is not None:
            return
        self._stop = asyncio.Event()
        reclaimed = await asyncio.to_thread(
            self._store.reclaim_stale_running,
            lease_owner=self._owner,
            reclaim_other_owners=True,
        )
        for job in reclaimed:
            await self._publish(job)
        self._task = asyncio.create_task(self._loop(), name="scs-indexing-jobs")

    async def stop(self) -> None:
        """Stop claiming work and wait for the active durable boundary."""

        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await task

    async def run_once(self) -> bool:
        """Claim and execute at most one job."""

        job = await asyncio.to_thread(
            self._store.claim_next,
            lease_owner=self._owner,
            lease_seconds=self._lease_seconds,
        )
        if job is None:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(job.id))
        try:
            if self._on_started is not None:
                await asyncio.to_thread(self._on_started, job)
            await self._publish(job)
            result = await self._execute(job)
            current = await asyncio.to_thread(self._store.get, job.id)
            if current is not None and current.status == "cancelling":
                final = await asyncio.to_thread(self._store.mark_cancelled, job.id)
            else:
                if self._on_completed is not None:
                    await asyncio.to_thread(self._on_completed, job)
                final = await asyncio.to_thread(
                    self._store.complete, job.id, result=result
                )
            await self._publish(final)
        except Exception as exc:
            final = await asyncio.to_thread(
                self._store.fail_or_retry, job.id, error=str(exc)
            )
            await self._publish(final)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def _loop(self) -> None:
        while not self._stop.is_set():
            if await self.run_once():
                continue
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._poll_interval_seconds
                )
            except TimeoutError:
                pass

    async def _heartbeat(self, job_id: str) -> None:
        interval = max(1.0, min(30.0, self._lease_seconds / 3))
        while True:
            await asyncio.sleep(interval)
            await asyncio.to_thread(
                self._store.heartbeat,
                job_id,
                lease_owner=self._owner,
                lease_seconds=self._lease_seconds,
            )

    async def _execute(self, job: IngestionJob) -> dict[str, object]:
        repo = Path(job.repo_path)
        if job.mode != "drop_index":
            assert_not_user_home_repo(repo)
        pipeline = self._pipeline_factory(job)
        if job.mode == "files":
            result = await asyncio.to_thread(
                pipeline.ingest_files,
                repo,
                [
                    Path(path)
                    for path in cast(Sequence[str], job.payload.get("file_paths", []))
                ],
                cast(Sequence[str], job.payload.get("deleted_paths", [])),
            )
        elif job.mode == "cleanup":
            result = await asyncio.to_thread(pipeline.cleanup_stale_files, repo)
        elif job.mode == "full":
            result = await asyncio.to_thread(
                pipeline.ingest,
                repo,
                force=False,
            )
        elif job.mode == "force_full":
            snapshot, first_execution = await self._force_snapshot_for_job(
                job, pipeline, repo
            )
            def acknowledge_snapshot_batch(rel_paths: list[str]) -> None:
                self._store.acknowledge_force_full_snapshot_files(
                    job.id, rel_paths=rel_paths
                )

            result = await asyncio.to_thread(
                pipeline.ingest,
                repo,
                force=first_execution,
                force_snapshot=snapshot,
                on_force_batch_acknowledged=acknowledge_snapshot_batch,
            )
        elif job.mode == "drop_index":
            graph = self._graph_for_job(job)
            await asyncio.to_thread(graph.delete_repo_sync, job.repo_path)
            return {"repo_deleted": True}
        else:
            raise ValueError(f"Unsupported indexing job mode: {job.mode}")
        serialized = asdict(result)
        degraded = serialized.get("semantic_degraded_reason")
        # A pipeline may retain structural work without acknowledging hashes
        # after an embedding outage. Treat that as a retryable job failure so
        # the daemon never publishes semantic-ready for partial vectors.
        if isinstance(degraded, str) and degraded:
            raise RuntimeError(degraded)
        return serialized

    async def _force_snapshot_for_job(
        self,
        job: IngestionJob,
        pipeline: IngestionPipeline,
        repo: Path,
    ) -> tuple[list[Mapping[str, object]], bool]:
        """Return only this durable force job's still-pending frozen files."""

        raw_snapshot = job.payload.get("force_full_snapshot")
        first_execution = raw_snapshot is None
        if raw_snapshot is None:
            records = await asyncio.to_thread(
                pipeline.create_force_full_snapshot, repo
            )
            job = await asyncio.to_thread(
                self._store.install_force_full_snapshot,
                job.id,
                files=records,
            )
            raw_snapshot = job.payload.get("force_full_snapshot")
        if not isinstance(raw_snapshot, dict):
            raise RuntimeError("Force-full job snapshot is invalid")
        snapshot_object = cast(dict[str, object], raw_snapshot)
        raw_files = snapshot_object.get("files")
        if not isinstance(raw_files, list):
            raise RuntimeError("Force-full job snapshot files are invalid")
        files = cast(list[object], raw_files)
        if not all(
            isinstance(record, dict) for record in files
        ):
            raise RuntimeError("Force-full job snapshot files are invalid")
        if not first_execution:
            acknowledged = await asyncio.to_thread(
                pipeline.acknowledged_force_snapshot_paths,
                repo,
                [cast(Mapping[str, object], record) for record in files],
            )
            if acknowledged:
                job = await asyncio.to_thread(
                    self._store.acknowledge_force_full_snapshot_files,
                    job.id,
                    rel_paths=acknowledged,
                )
                raw_snapshot = job.payload.get("force_full_snapshot")
                if not isinstance(raw_snapshot, dict):
                    raise RuntimeError("Force-full job snapshot is invalid")
                refreshed_snapshot = cast(dict[str, object], raw_snapshot)
                files = cast(list[object], refreshed_snapshot.get("files", []))
        pending = [
            cast(Mapping[str, object], typed_record)
            for typed_record in (
                cast(dict[str, object], record) for record in files
            )
            if not bool(typed_record.get("acknowledged", False))
        ]
        # A process can die after persisting its manifest but before the
        # pipeline invalidates matching hashes.  Reapply force only while no
        # snapshot target has been acknowledged; once even one batch is
        # durable, native hashes and snapshot state become the retry boundary.
        first_execution = first_execution or (
            job.attempts == 0 and len(pending) == len(files)
        )
        return pending, first_execution

    async def _publish(self, job: IngestionJob) -> None:
        await self._events.publish("indexing_job", job_to_dict(job))
