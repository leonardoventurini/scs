from __future__ import annotations

from pathlib import Path

import pytest

from scs.indexing.watcher import RepositoryWatcher


class Graph:
    def __init__(self, indexed: bool) -> None:
        self.indexed = indexed

    def resolve_repo_id_sync(self, path: str) -> int | None:
        return 1 if self.indexed else None


class Jobs:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, object]] = []

    def enqueue(self, **kwargs: object) -> None:
        self.enqueued.append(kwargs)


@pytest.mark.asyncio
async def test_unindexed_repository_event_never_enqueues(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    source = repo / "main.py"
    source.write_text("print('x')")
    jobs = Jobs()
    watcher = RepositoryWatcher(
        graph=Graph(False),
        jobs=jobs,
        base_dir=tmp_path,
        supported_extensions=frozenset({".py"}),
        debounce_seconds=0,
    )

    await watcher.record({(2, str(source))})

    assert jobs.enqueued == []


@pytest.mark.asyncio
async def test_indexed_repository_event_enqueues_incremental_job(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    source = repo / "main.py"
    source.write_text("print('x')")
    jobs = Jobs()
    watcher = RepositoryWatcher(
        graph=Graph(True),
        jobs=jobs,
        base_dir=tmp_path,
        supported_extensions=frozenset({".py"}),
        debounce_seconds=0,
    )

    await watcher.record({(2, str(source))})
    await list(watcher._timers.values())[0]

    assert jobs.enqueued[0]["mode"] == "files"
    assert jobs.enqueued[0]["payload"] == {
        "file_paths": [str(source.resolve())],
        "deleted_paths": [],
    }
