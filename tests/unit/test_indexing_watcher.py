from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from scs.indexing.watcher import GitFingerprint, RepositoryWatcher, git_fingerprint


class Jobs:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, object]] = []

    def enqueue(self, **kwargs: object) -> object:
        self.enqueued.append(kwargs)
        return None


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ("git", *args),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "SCS Test")
    _git(repo, "config", "user.email", "scs@example.invalid")
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    return repo


def test_git_fingerprint_covers_all_visible_worktree_transitions(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    clean = git_fingerprint(repo, timeout_seconds=2.0)

    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    modified = git_fingerprint(repo, timeout_seconds=2.0)
    _git(repo, "add", "tracked.txt")
    staged = git_fingerprint(repo, timeout_seconds=2.0)
    (repo / "untracked.txt").write_text("new\n", encoding="utf-8")
    untracked = git_fingerprint(repo, timeout_seconds=2.0)
    (repo / "tracked.txt").unlink()
    deleted = git_fingerprint(repo, timeout_seconds=2.0)
    (repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    ignored_change = git_fingerprint(repo, timeout_seconds=2.0)

    assert all(
        fingerprint is not None
        for fingerprint in (clean, modified, staged, untracked, deleted)
    )
    assert len({clean, modified, staged, untracked, deleted}) == 5
    assert ignored_change == deleted


def test_git_fingerprint_changes_after_commit(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    before = git_fingerprint(repo, timeout_seconds=2.0)
    (repo / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "update")

    assert git_fingerprint(repo, timeout_seconds=2.0) != before


@pytest.mark.asyncio
async def test_startup_reconciles_then_identical_state_stays_quiet(
    tmp_path: Path,
) -> None:
    jobs = Jobs()
    watcher = RepositoryWatcher(
        jobs=jobs,
        repo_path=tmp_path,
        store_id="store",
        store_generation="generation",
        fingerprint=lambda _path, _timeout: GitFingerprint("same"),
        active_interval_seconds=0.01,
        idle_interval_seconds=0.02,
        debounce_seconds=0,
    )

    await watcher.start()
    await watcher.observe_once()
    await watcher.observe_once()
    await watcher.stop()

    assert len(jobs.enqueued) == 1
    assert jobs.enqueued[0]["mode"] == "full"
    assert jobs.enqueued[0]["reason"] == "git-poller-startup"


@pytest.mark.asyncio
async def test_changed_state_debounces_and_resets_adaptive_interval(
    tmp_path: Path,
) -> None:
    observations: Iterator[GitFingerprint | None] = iter(
        [GitFingerprint("a"), GitFingerprint("a"), GitFingerprint("b")]
    )
    jobs = Jobs()
    watcher = RepositoryWatcher(
        jobs=jobs,
        repo_path=tmp_path,
        store_id="store",
        store_generation="generation",
        fingerprint=lambda _path, _timeout: next(observations),
        active_interval_seconds=2.0,
        idle_interval_seconds=30.0,
        debounce_seconds=0,
    )

    await watcher.reconcile_startup()
    await watcher.observe_once()
    await watcher.observe_once()
    assert watcher.current_interval_seconds == 4.0
    await watcher.observe_once()
    await watcher.flush_pending()

    assert watcher.current_interval_seconds == 2.0
    assert [job["reason"] for job in jobs.enqueued] == [
        "git-poller-startup",
        "git-poller-change",
    ]


@pytest.mark.asyncio
async def test_git_failure_preserves_baseline_and_backs_off(tmp_path: Path) -> None:
    observations: Iterator[GitFingerprint | None] = iter(
        [GitFingerprint("a"), None, GitFingerprint("b")]
    )
    jobs = Jobs()
    watcher = RepositoryWatcher(
        jobs=jobs,
        repo_path=tmp_path,
        store_id="store",
        store_generation="generation",
        fingerprint=lambda _path, _timeout: next(observations),
        active_interval_seconds=2.0,
        idle_interval_seconds=30.0,
        debounce_seconds=0,
    )

    await watcher.observe_once()
    await watcher.observe_once()
    assert watcher.current_interval_seconds == 4.0
    await watcher.observe_once()
    await watcher.flush_pending()

    assert watcher.current_interval_seconds == 2.0
    assert jobs.enqueued[0]["reason"] == "git-poller-change"
