from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scs.indexing import discovery


def test_build_file_entry_enforces_repository_and_ignore_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    accepted = repo / "src" / "main.py"
    accepted.parent.mkdir()
    accepted.write_text("print('accepted')\n", encoding="utf-8")
    ignored = repo / "ignored.py"
    ignored.write_text("print('ignored')\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    unsupported = repo / "notes.txt"
    unsupported.write_text("notes", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    skipped = repo / "node_modules" / "package.py"
    skipped.parent.mkdir()
    skipped.write_text("dependency = True\n", encoding="utf-8")
    monkeypatch.setattr(discovery, "_git_check_ignored_paths", lambda *_: None)

    entry = discovery.build_file_entry(accepted, repo, frozenset({".py"}))

    assert entry is not None
    assert entry.rel_path == "src/main.py"
    assert entry.language == "python"
    assert entry.byte_size == accepted.stat().st_size
    assert entry.content_hash == hashlib.sha256(accepted.read_bytes()).hexdigest()
    assert discovery.build_file_entry(ignored, repo, frozenset({".py"})) is None
    assert discovery.build_file_entry(unsupported, repo, frozenset({".py"})) is None
    assert discovery.build_file_entry(outside, repo, frozenset({".py"})) is None
    assert discovery.build_file_entry(skipped, repo, frozenset({".py"})) is None
    assert discovery.build_file_entry(repo / "missing.py", repo) is None


def test_discover_git_fast_path_rechecks_every_candidate_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    source = repo / "src" / "main.py"
    source.parent.mkdir(parents=True)
    source.write_text("print('main')\n", encoding="utf-8")
    dependency = repo / "node_modules" / "package.py"
    dependency.parent.mkdir()
    dependency.write_text("dependency = True\n", encoding="utf-8")
    notes = repo / "notes.txt"
    notes.write_text("notes", encoding="utf-8")
    monkeypatch.setattr(
        discovery,
        "_list_git_non_ignored_paths",
        lambda _repo: [
            "src/main.py",
            "node_modules/package.py",
            "notes.txt",
            "missing.py",
        ],
    )

    entries = discovery.discover(repo, extensions=frozenset({".py"}))

    assert [entry.rel_path for entry in entries] == ["src/main.py"]


def test_discover_falls_back_to_root_gitignore_when_git_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "keep.py").write_text("keep = True\n", encoding="utf-8")
    (repo / "ignored.py").write_text("ignored = True\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    dependency = repo / "node_modules" / "dependency.py"
    dependency.parent.mkdir()
    dependency.write_text("dependency = True\n", encoding="utf-8")
    monkeypatch.setattr(discovery, "_list_git_non_ignored_paths", lambda _repo: None)
    monkeypatch.setattr(discovery, "_git_check_ignored_paths", lambda *_: None)

    entries = discovery.discover(repo, extensions=frozenset({".py"}))

    assert [entry.rel_path for entry in entries] == ["keep.py"]


def test_discover_skips_unreadable_candidate_without_aborting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bad = repo / "bad.py"
    good = repo / "good.py"
    bad.write_text("bad = True\n", encoding="utf-8")
    good.write_text("good = True\n", encoding="utf-8")
    monkeypatch.setattr(
        discovery,
        "_list_git_non_ignored_paths",
        lambda _repo: ["bad.py", "good.py"],
    )
    real_compute_hash = discovery._compute_hash

    def fail_one_hash(path: Path) -> str:
        if path == bad:
            raise PermissionError("denied")
        return real_compute_hash(path)

    monkeypatch.setattr(discovery, "_compute_hash", fail_one_hash)

    entries = discovery.discover(repo, extensions=frozenset({".py"}))

    assert [entry.rel_path for entry in entries] == ["good.py"]


def test_git_ignore_lookup_failure_uses_fallback_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise OSError("git unavailable")

    monkeypatch.setattr(discovery.subprocess, "run", unavailable)
    fallback = discovery._load_gitignore_spec(repo)

    assert discovery._resolve_ignored_paths(
        repo, ["ignored.py", "keep.py"], fallback
    ) == {"ignored.py"}
