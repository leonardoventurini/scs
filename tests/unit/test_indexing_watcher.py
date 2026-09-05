from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from scs.indexing.watcher import (
    GIT_STATUS_COMMAND,
    GitFingerprint,
    RepositoryWatcher,
    git_fingerprint,
)


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


def _edit_with_new_timestamp(path: Path, text: str) -> None:
    previous = path.stat()
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))


@pytest.mark.parametrize("state", ["modified", "untracked", "staged", "renamed"])
@pytest.mark.parametrize("name", ["space name.txt", "line\nname.txt", "quote\"name.txt"])
def test_repeated_edits_change_fingerprint_without_status_transition(
    tmp_path: Path, state: str, name: str
) -> None:
    repo = _repository(tmp_path)
    path = repo / name
    if state == "untracked":
        path.write_text("one\n", encoding="utf-8")
    else:
        _git(repo, "mv", "tracked.txt", name)
        if state != "renamed":
            _git(repo, "commit", "-qm", "rename")
    _edit_with_new_timestamp(path, "two\n")
    if state == "staged":
        _git(repo, "add", "--", name)
        _edit_with_new_timestamp(path, "six\n")
    status_command = ("git", "status", "--porcelain=v1", "-z")
    before_status = subprocess.check_output(status_command, cwd=repo)
    before = git_fingerprint(repo, timeout_seconds=2.0)

    _edit_with_new_timestamp(path, "ten\n")

    assert subprocess.check_output(status_command, cwd=repo) == before_status
    after = git_fingerprint(repo, timeout_seconds=2.0)
    assert before is not None and after is not None
    assert after != before
    assert git_fingerprint(repo, timeout_seconds=2.0) == after


def test_rename_source_is_not_parsed_as_another_status_record(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    source = "?? misleading\nsource.txt"
    _git(repo, "mv", "tracked.txt", source)
    _git(repo, "commit", "-qm", "source")
    _git(repo, "mv", source, "destination.txt")
    following = repo / "untracked.txt"
    following.write_text("one\n", encoding="utf-8")
    before = git_fingerprint(repo, timeout_seconds=2.0)

    _edit_with_new_timestamp(following, "two\n")

    assert git_fingerprint(repo, timeout_seconds=2.0) != before


@pytest.mark.parametrize("nested", [False, True])
def test_fingerprint_does_not_follow_external_symlink_targets(
    tmp_path: Path, nested: bool
) -> None:
    repo = _repository(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    target = external / "file.txt"
    target.write_text("one\n", encoding="utf-8")
    link = repo / "link"
    if nested:
        link.mkdir()
        (link / target.name).write_text("one\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "nested")
        (link / target.name).unlink()
        link.rmdir()
        link.symlink_to(external, target_is_directory=True)
    else:
        link.symlink_to(target)
    before = git_fingerprint(repo, timeout_seconds=2.0)

    _edit_with_new_timestamp(target, "two\n")

    assert before is not None
    assert git_fingerprint(repo, timeout_seconds=2.0) == before


def _mock_status(monkeypatch: pytest.MonkeyPatch, status: bytes) -> None:
    def run(command: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        output = status if command == GIT_STATUS_COMMAND else b"head"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr=b"")

    monkeypatch.setattr("scs.indexing.watcher.subprocess.run", run)


@pytest.mark.parametrize("code", [b"R ", b" R", b"C ", b" C"])
def test_rename_and_copy_records_consume_both_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: bytes
) -> None:
    source = tmp_path / "?? source"
    source.write_text("one\n", encoding="utf-8")
    _mock_status(monkeypatch, code + b" destination\0?? source\0")
    before = git_fingerprint(tmp_path, timeout_seconds=2.0)

    _edit_with_new_timestamp(source, "two\n")

    assert before is not None
    assert git_fingerprint(tmp_path, timeout_seconds=2.0) != before


@pytest.mark.parametrize(
    "status", [b"M\0", b"R  destination\0", b"?? ../outside\0", b"?? /\0", b"?? .\0"]
)
def test_invalid_status_paths_preserve_the_previous_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: bytes
) -> None:
    _mock_status(monkeypatch, status)

    assert git_fingerprint(tmp_path, timeout_seconds=2.0) is None


def test_metadata_permission_failure_is_an_unavailable_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_status(monkeypatch, b"?? unreadable\0")

    def denied(_path: Path) -> os.stat_result:
        raise PermissionError("metadata denied")

    monkeypatch.setattr(Path, "lstat", denied)

    assert git_fingerprint(tmp_path, timeout_seconds=2.0) is None


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
