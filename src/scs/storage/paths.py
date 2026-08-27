"""Contained filesystem paths for a single isolated project store."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from scs.paths import validate_scs_home
from scs.storage.models import (
    StoreGeneration,
    StoreId,
    validate_store_generation,
    validate_store_id,
)


class StorePathError(RuntimeError):
    """Raised when a project-store path would escape SCS-owned storage."""


def _resolved(path: Path) -> Path:
    """Resolve aliases without requiring that the final component exists."""

    return path.resolve(strict=False)


def _assert_contained(container: Path, candidate: Path) -> Path:
    """Return a resolved candidate only when it remains under its container."""

    resolved_container = _resolved(container)
    resolved_candidate = _resolved(candidate)
    try:
        resolved_candidate.relative_to(resolved_container)
    except ValueError as exc:
        raise StorePathError(
            f"Project store path {resolved_candidate} escapes {resolved_container}"
        ) from exc
    return resolved_candidate


def _ensure_private_directory(path: Path, *, container: Path) -> None:
    """Create one contained directory, rejecting symlink and mode aliases.

    This is intentionally used only by explicit creation flows. Read paths are
    pure values and never call it.
    """

    _assert_contained(container, path)
    path.mkdir(mode=0o700, exist_ok=True)
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise StorePathError(f"Project store directory is not a real directory: {path}")
    _assert_contained(container, path)
    os.chmod(path, 0o700)


@dataclass(frozen=True, slots=True)
class ProjectStorePaths:
    """All SCS-owned paths for one store generation.

    Constructing this type does not touch the filesystem. ``ensure`` is the
    explicit registration/indexing-only operation that creates its directories.
    """

    home: Path
    store_id: StoreId
    generation: StoreGeneration
    projects: Path
    store: Path
    generations: Path
    active: Path
    database: Path
    vector_index: Path
    provider_metadata: Path
    current_pointer: Path

    @classmethod
    def resolve(
        cls,
        home: Path,
        store_id: StoreId,
        generation: StoreGeneration,
    ) -> "ProjectStorePaths":
        """Build strictly contained store paths without creating any files."""

        safe_home = validate_scs_home(home)
        safe_store_id = validate_store_id(store_id)
        safe_generation = validate_store_generation(generation)
        projects = _assert_contained(safe_home, safe_home / "projects")
        store = _assert_contained(projects, projects / safe_store_id)
        generations = _assert_contained(store, store / "generations")
        active = _assert_contained(generations, generations / safe_generation)
        return cls(
            home=safe_home,
            store_id=safe_store_id,
            generation=safe_generation,
            projects=projects,
            store=store,
            generations=generations,
            active=active,
            database=_assert_contained(active, active / "index.db"),
            vector_index=_assert_contained(active, active / "index.usearch"),
            provider_metadata=_assert_contained(active, active / "provider.json"),
            current_pointer=_assert_contained(store, store / "CURRENT"),
        )

    def ensure(self) -> None:
        """Create this isolated store generation with private directory modes."""

        _ensure_private_directory(self.home, container=self.home)
        _ensure_private_directory(self.projects, container=self.home)
        _ensure_private_directory(self.store, container=self.projects)
        _ensure_private_directory(self.generations, container=self.store)
        _ensure_private_directory(self.active, container=self.generations)
