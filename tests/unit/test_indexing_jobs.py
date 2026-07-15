from __future__ import annotations

from pathlib import Path

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
    first = store.enqueue(repo_path="/repo", mode="files", reason="watch", payload={"file_paths": ["a.py"]})
    second = store.enqueue(repo_path="/repo", mode="files", reason="watch", payload={"file_paths": ["b.py"]})

    assert first.id == second.id
    assert second.payload["file_paths"] == ["a.py", "b.py"]
