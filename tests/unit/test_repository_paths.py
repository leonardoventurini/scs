from __future__ import annotations

from pathlib import Path

import pytest

from scs.indexing import repository_paths


class Graph:
    def __init__(
        self,
        stats: dict[str, dict[str, object]],
        *,
        failed_deletions: frozenset[str] = frozenset(),
    ) -> None:
        self._stats = stats
        self._failed_deletions = failed_deletions
        self.deletions: list[str] = []

    def get_ingestion_stats_sync(self) -> dict[str, dict[str, object]]:
        return self._stats

    def delete_repo_sync(self, repo_path: str) -> object:
        self.deletions.append(repo_path)
        if repo_path in self._failed_deletions:
            raise OSError("synthetic deletion failure")
        return {"deleted": True}


def test_find_indexed_parent_returns_nearest_parent_and_allows_exact_repo(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    package = root / "packages"
    nested = package / "tool"
    sibling = tmp_path / "project-other"
    for path in (nested, sibling):
        path.mkdir(parents=True)

    parent = repository_paths.find_indexed_parent_repo(
        nested,
        [str(root), str(package), str(nested), str(sibling)],
    )

    assert parent == str(package.resolve())
    assert repository_paths.find_indexed_parent_repo(root, [str(root)]) is None
    assert repository_paths.find_indexed_parent_repo(sibling, [str(root)]) is None


def test_ingestion_policy_rejects_home_and_nested_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    parent = tmp_path / "parent"
    nested = parent / "nested"
    for path in (home, nested):
        path.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    graph = Graph({str(parent.resolve()): {"file_count": 1}})

    with pytest.raises(ValueError, match="home directory"):
        repository_paths.assert_ingestable_repo_path(graph, home)

    with pytest.raises(ValueError, match="parent repository is already indexed"):
        repository_paths.assert_ingestable_repo_path(graph, nested)

    repository_paths.assert_ingestable_repo_path(graph, parent)


def test_deduplication_keeps_largest_and_counts_only_successful_deletes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stats = {
        "/Repo": {"file_count": 10},
        "/REPO": {"file_count": 5},
        "/repo": {"file_count": "invalid"},
        "/other": {"file_count": 2},
    }
    graph = Graph(stats, failed_deletions=frozenset({"/repo"}))
    monkeypatch.setattr(
        repository_paths,
        "canonicalize_repo_path",
        lambda path: str(path).lower(),
    )

    result = repository_paths.deduplicate_repo_entries(graph)

    assert result["groups_found"] == 1
    assert result["duplicates_removed"] == 1
    assert graph.deletions == ["/REPO", "/repo"]
    assert result["details"] == [
        {
            "canonical": "/repo",
            "kept": "/Repo",
            "removed": ["/REPO", "/repo"],
        }
    ]


def test_deduplication_of_empty_graph_is_a_noop() -> None:
    graph = Graph({})

    assert repository_paths.deduplicate_repo_entries(graph) == {
        "groups_found": 0,
        "duplicates_removed": 0,
        "details": [],
    }
    assert graph.deletions == []


def test_true_case_lookup_falls_back_when_parent_cannot_be_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"

    def deny_listing(_path: Path) -> list[str]:
        raise PermissionError("denied")

    monkeypatch.setattr(repository_paths.os, "listdir", deny_listing)

    assert repository_paths._true_case_child(parent, "Child") == parent / "Child"
