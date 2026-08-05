from __future__ import annotations

from pathlib import Path

import pytest

from scs.indexing.jobs import IngestionJobStore


def test_queue_replays_stale_running_job(tmp_path: Path) -> None:
    store = IngestionJobStore(tmp_path / "jobs.db")
    queued = store.enqueue(repo_path="/repo", mode="full", reason="explicit", max_attempts=3)
    claimed = store.claim_next(lease_owner="dead", lease_seconds=-1)
    assert claimed is not None and claimed.id == queued.id

    recovered = store.reclaim_stale_running(lease_owner="new", reclaim_other_owners=True)

    assert recovered[0].status == "queued"
    assert store.claim_next(lease_owner="new") is not None


def test_queue_merges_incremental_paths(tmp_path: Path) -> None:
    store = IngestionJobStore(tmp_path / "jobs.db")
    first = store.enqueue(
        repo_path="/repo",
        mode="files",
        reason="watch",
        payload={"file_paths": ["a.py"]},
    )
    second = store.enqueue(
        repo_path="/repo",
        mode="files",
        reason="watch",
        payload={"file_paths": ["b.py"]},
    )

    assert first.id == second.id
    assert second.payload["file_paths"] == ["a.py", "b.py"]


def test_failed_running_job_requeues_and_releases_lease(tmp_path: Path) -> None:
    """A transient failure must leave a claimable durable retry."""

    store = IngestionJobStore(tmp_path / "jobs.db")
    queued = store.enqueue(
        repo_path="/repo", mode="full", reason="explicit", max_attempts=2
    )
    claimed = store.claim_next(lease_owner="worker")
    assert claimed is not None and claimed.id == queued.id

    retried = store.fail_or_retry(claimed.id, error="transient parse failure")

    assert retried.status == "queued"
    assert retried.phase == "queued"
    assert retried.attempts == 1
    assert retried.lease_owner is None
    assert retried.lease_expires_at is None
    assert retried.error == "transient parse failure"
    assert store.claim_next(lease_owner="replacement") is not None


def test_exhausted_running_job_becomes_terminal_failure(tmp_path: Path) -> None:
    store = IngestionJobStore(tmp_path / "jobs.db")
    queued = store.enqueue(
        repo_path="/repo", mode="full", reason="explicit", max_attempts=1
    )
    claimed = store.claim_next(lease_owner="worker")
    assert claimed is not None and claimed.id == queued.id

    failed = store.fail_or_retry(claimed.id, error="permanent parse failure")

    assert failed.status == "failed"
    assert failed.phase == "failed"
    assert failed.attempts == 1
    assert failed.finished_at is not None
    assert failed.lease_owner is None
    assert store.claim_next(lease_owner="replacement") is None


def test_failed_job_merges_into_queued_followup(tmp_path: Path) -> None:
    """Watcher changes arriving during a full job survive its failed attempt."""

    store = IngestionJobStore(tmp_path / "jobs.db")
    running = store.enqueue(
        repo_path="/repo", mode="full", reason="explicit", max_attempts=3
    )
    assert store.claim_next(lease_owner="worker") is not None
    followup = store.enqueue(
        repo_path="/repo",
        mode="files",
        reason="watch",
        payload={"file_paths": ["changed.py"]},
    )

    merged = store.fail_or_retry(running.id, error="worker stopped")

    assert merged.id == followup.id
    assert merged.mode == "full"
    assert merged.payload == {}
    original = store.get(running.id)
    assert original is not None
    assert original.status == "cancelled"
    assert original.attempts == 1
    assert original.finished_at is not None
    assert original.error == "worker stopped; merged into queued follow-up job"


def test_cancel_request_preserves_terminal_state_and_cancels_active_jobs(
    tmp_path: Path,
) -> None:
    store = IngestionJobStore(tmp_path / "jobs.db")
    queued = store.enqueue(repo_path="/queued", mode="full", reason="explicit")
    cancelled = store.request_cancel(queued.id)
    assert cancelled.status == "cancelled"
    assert cancelled.finished_at is not None

    running = store.enqueue(repo_path="/running", mode="full", reason="explicit")
    assert store.claim_next(lease_owner="worker") is not None
    cancelling = store.request_cancel(running.id)
    assert cancelling.status == "cancelling"

    completed = store.complete(running.id, result={"files_changed": 1})
    assert completed.status == "cancelled"
    assert completed.result == {"files_changed": 1}

    assert store.request_cancel(completed.id).status == "cancelled"


@pytest.mark.parametrize("operation", ["complete", "fail_or_retry", "request_cancel"])
def test_job_transition_rejects_unknown_identifier(
    tmp_path: Path, operation: str
) -> None:
    store = IngestionJobStore(tmp_path / "jobs.db")

    with pytest.raises(KeyError, match="missing"):
        if operation == "complete":
            store.complete("missing")
        elif operation == "fail_or_retry":
            store.fail_or_retry("missing", error="failure")
        else:
            store.request_cancel("missing")


def test_corrupt_job_database_is_quarantined_and_rebuilt(tmp_path: Path) -> None:
    database = tmp_path / "jobs.db"
    original_family = {
        database: b"not a sqlite database",
        Path(f"{database}-wal"): b"stale wal",
        Path(f"{database}-shm"): b"stale shm",
    }
    for path, content in original_family.items():
        path.write_bytes(content)

    store = IngestionJobStore(database)

    assert store.list_recent() == []
    for original, content in original_family.items():
        quarantined = list(tmp_path.glob(f"{original.name}.corrupt-*"))
        assert len(quarantined) == 1
        assert quarantined[0].stat().st_size > 0
        if original == database:
            assert quarantined[0].read_bytes() == content
