from __future__ import annotations

import sqlite3
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


def test_active_work_tracks_nonterminal_queue_states(tmp_path: Path) -> None:
    store = IngestionJobStore(tmp_path / "jobs.db")
    queued = store.enqueue(repo_path="/repo", mode="full", reason="explicit")

    assert store.has_active() is True
    claimed = store.claim_next(lease_owner="worker")
    assert claimed is not None
    assert store.has_active() is True

    store.complete(queued.id)

    assert store.has_active() is False


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


def test_new_explicit_force_request_replaces_a_queued_force_snapshot(
    tmp_path: Path,
) -> None:
    """A new force request must rediscover rather than inherit stale targets."""

    store = IngestionJobStore(tmp_path / "jobs.db")
    first = store.enqueue(repo_path="/repo", mode="force_full", reason="explicit")
    store.install_force_full_snapshot(
        first.id,
        files=[
            {
                "rel_path": "old.py",
                "content_hash": "a" * 64,
                "language": "python",
                "byte_size": 1,
            }
        ],
    )

    second = store.enqueue(repo_path="/repo", mode="force_full", reason="explicit")

    assert second.id == first.id
    assert second.payload == {}


def test_explicit_job_persists_immutable_project_store_binding(tmp_path: Path) -> None:
    """A worker must receive the store identity selected at enqueue time."""

    store = IngestionJobStore(tmp_path / "jobs.db")

    job = store.enqueue(
        repo_path="/repo",
        store_id="a" * 64,
        store_generation="g00000001",
        mode="force_full",
        reason="explicit_reindex",
    )

    assert job.store_id == "a" * 64
    assert job.store_generation == "g00000001"
    snapshot = store.install_force_full_snapshot(
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
    assert snapshot.payload["force_full_snapshot"] == {
        "store_id": "a" * 64,
        "store_generation": "g00000001",
        "files": [
            {
                "rel_path": "source.py",
                "content_hash": "f" * 64,
                "language": "python",
                "byte_size": 1,
                "acknowledged": False,
            }
        ],
    }
    claimed = store.claim_next(lease_owner="worker")
    assert claimed is not None
    assert claimed.store_id == job.store_id
    assert claimed.store_generation == job.store_generation


def test_job_database_adds_store_binding_columns_without_losing_jobs(tmp_path: Path) -> None:
    """Pre-topology queues remain readable until cutover archives them."""

    database = tmp_path / "jobs.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            CREATE TABLE ingestion_jobs (
                id TEXT PRIMARY KEY, repo_path TEXT NOT NULL, mode TEXT NOT NULL,
                reason TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL,
                phase TEXT NOT NULL, current INTEGER NOT NULL, total INTEGER NOT NULL,
                message TEXT NOT NULL, attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL,
                lease_owner TEXT, lease_expires_at TEXT, error TEXT, result_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO ingestion_jobs VALUES
            ('legacy', '/repo', 'full', 'legacy', '{}', 'queued', 'queued', 0, 0,
             '', 0, 3, NULL, NULL, NULL, NULL, '2026-01-01T00:00:00Z',
             '2026-01-01T00:00:00Z', NULL)
            """
        )
        connection.commit()
    finally:
        connection.close()

    store = IngestionJobStore(database)
    legacy = store.get("legacy")

    assert legacy is not None
    assert legacy.store_id is None
    assert legacy.store_generation is None


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
