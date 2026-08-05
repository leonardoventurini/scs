"""Canonical path normalization for repository paths.

On macOS (case-insensitive APFS/HFS+), ``Path.resolve()`` preserves whatever
casing the caller used — the same directory typed as ``/Users/Leo/Repo`` and
``/users/leo/repo`` resolves to two different strings. This creates duplicate
entries in the knowledge graph's ``ingested_files`` table and orphaned node IDs
(SHA-256 hashed from ``repo_path:rel_path:...``).

The fix is a single ``canonicalize_repo_path()`` chokepoint that walks each
path component and matches it against the actual directory listing to recover
the true filesystem casing.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class RepositoryGraph(Protocol):
    """Graph read required by repository path policy."""

    def get_ingestion_stats_sync(self) -> dict[str, dict[str, object]]: ...


class RepositoryCleanupGraph(RepositoryGraph, Protocol):
    """Graph operations required by duplicate repository cleanup."""

    def delete_repo_sync(self, repo_path: str) -> object: ...


def canonicalize_repo_path(path: str | Path) -> str:
    """Resolve a path to its canonical filesystem representation.

    On macOS (case-insensitive APFS/HFS+), ``Path.resolve()`` preserves
    whatever casing the caller used. This walks each path component and
    matches against the actual directory listing to get the true filesystem
    casing, preventing the same directory from producing different string
    keys in the knowledge graph.

    Steps:
        1. ``Path(path).resolve()`` — collapse symlinks, ``.``, ``..``, make absolute.
        2. Walk each component from root to leaf, listing the parent directory
           to find the entry whose lowercased name matches the component.
        3. Fall back to the ``resolve()`` result if any component lookup fails
           (permissions, deleted directory, non-macOS filesystem).

    Returns:
        Canonicalized absolute path string.
    """
    resolved = Path(path).resolve()

    # Fast path: if we can't even list the root, just return resolve().
    try:
        parts = resolved.parts  # ('/', 'Users', 'leo', 'Repos', ...)
    except Exception:
        return str(resolved)

    # Build the canonical path component by component.
    canonical = Path(parts[0])  # Root: '/' on Unix, 'C:\\' on Windows
    for component in parts[1:]:
        canonical = _true_case_child(canonical, component)

    return str(canonical)


def _true_case_child(parent: Path, component: str) -> Path:
    """Find the true-cased child entry matching *component* in *parent*.

    Uses a case-insensitive comparison against ``os.listdir(parent)`` to
    find the real filename on disk. Falls back to appending the original
    component if the listing fails or no match is found.
    """
    try:
        entries = os.listdir(parent)
    except OSError:
        # Permission denied, path doesn't exist, etc.
        return parent / component

    lower = component.lower()
    for entry in entries:
        if entry.lower() == lower:
            return parent / entry

    # No match — the component might have been deleted between resolve()
    # and our listdir(). Just use what resolve() gave us.
    return parent / component


def find_indexed_parent_repo(
    repo_path: str | Path,
    indexed_repo_paths: list[str] | set[str] | tuple[str, ...],
) -> str | None:
    """Return the nearest indexed repo that contains ``repo_path``.

    Re-ingesting the same repo is allowed. Ingesting a nested project inside
    an already-indexed parent is not, because it creates duplicate repo scopes
    and duplicate nodes for the same files.
    """
    candidate = Path(canonicalize_repo_path(repo_path))
    nearest_parent: Path | None = None

    for existing_raw in indexed_repo_paths:
        existing = Path(canonicalize_repo_path(existing_raw))
        if candidate == existing:
            continue

        try:
            candidate.relative_to(existing)
        except ValueError:
            continue

        if nearest_parent is None or len(existing.parts) > len(nearest_parent.parts):
            nearest_parent = existing

    return str(nearest_parent) if nearest_parent else None


def find_indexed_parent_repo_for_graph(
    graph: RepositoryGraph, repo_path: str | Path
) -> str | None:
    """Return the indexed parent repo for ``repo_path`` using graph stats."""
    stats = graph.get_ingestion_stats_sync()
    return find_indexed_parent_repo(repo_path, set(stats.keys()))


def is_user_home_repo_path(repo_path: str | Path) -> bool:
    """Return whether ``repo_path`` resolves to the current user's home directory."""
    candidate = Path(canonicalize_repo_path(repo_path))
    home = Path(canonicalize_repo_path(Path.home()))
    return candidate == home


def assert_not_user_home_repo(repo_path: str | Path) -> None:
    """Raise if ``repo_path`` is the user's home directory."""
    if is_user_home_repo_path(repo_path):
        raise ValueError(
            f"Cannot ingest the user's home directory: {canonicalize_repo_path(repo_path)}. "
            "Choose a specific repository folder instead."
        )


def assert_not_nested_under_indexed_repo(
    graph: RepositoryGraph, repo_path: str | Path
) -> None:
    """Raise if ``repo_path`` is inside an already-indexed parent repo."""
    parent = find_indexed_parent_repo_for_graph(graph, repo_path)
    if parent:
        raise ValueError(
            f"Cannot ingest nested repository {canonicalize_repo_path(repo_path)}; "
            f"parent repository is already indexed: {parent}"
        )


def assert_ingestable_repo_path(graph: RepositoryGraph, repo_path: str | Path) -> None:
    """Raise if ``repo_path`` violates repository ingestion safety policy."""
    assert_not_user_home_repo(repo_path)
    assert_not_nested_under_indexed_repo(graph, repo_path)


def deduplicate_repo_entries(graph: RepositoryCleanupGraph) -> dict[str, object]:
    """Find and merge duplicate repo entries caused by non-canonical paths.

    Queries all distinct ``repo_path`` values from the ingested_files table,
    groups them by their canonical form, and for each group with duplicates
    keeps the entry with the most files and deletes the others.

    Args:
        graph: A ``KnowledgeGraph`` instance with sync methods.

    Returns:
        Summary dict with ``groups_found``, ``duplicates_removed``, and
        ``details`` listing each merged group.
    """
    stats = graph.get_ingestion_stats_sync()
    if not stats:
        return {"groups_found": 0, "duplicates_removed": 0, "details": []}

    # Group repo_path strings by their canonical form.
    canonical_groups: dict[str, list[tuple[str, dict[str, object]]]] = {}
    for repo_path, info in stats.items():
        canonical = canonicalize_repo_path(repo_path)
        canonical_groups.setdefault(canonical, []).append((repo_path, info))

    duplicates_removed = 0
    details: list[dict[str, object]] = []

    for canonical, group in canonical_groups.items():
        if len(group) <= 1:
            continue

        # Keep the entry with the most files; delete the rest.
        def file_count(item: tuple[str, dict[str, object]]) -> int:
            count = item[1].get("file_count", 0)
            return count if isinstance(count, int) else 0

        group.sort(key=file_count, reverse=True)
        keeper = group[0]
        victims = group[1:]

        for victim_path, _victim_info in victims:
            try:
                graph.delete_repo_sync(victim_path)
                duplicates_removed += 1
            except Exception:
                logger.exception(
                    "Failed to delete duplicate repo entry: %s", victim_path
                )

        details.append(
            {
                "canonical": canonical,
                "kept": keeper[0],
                "removed": [v[0] for v in victims],
            }
        )

    return {
        "groups_found": len([g for g in canonical_groups.values() if len(g) > 1]),
        "duplicates_removed": duplicates_removed,
        "details": details,
    }
