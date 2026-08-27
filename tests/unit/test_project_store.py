"""Regression coverage for isolated project-store catalog and path contracts."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from scs.storage import (
    ProjectStoreCatalog,
    ProjectStorePaths,
    StoreState,
    StoreGeneration,
    StoreId,
    StorePathError,
    store_id_for_root,
)


def test_lookup_does_not_create_catalog_or_project_store(tmp_path: Path) -> None:
    home = tmp_path / "scs-home"
    repository = tmp_path / "repository"
    repository.mkdir()
    catalog = ProjectStoreCatalog(home)

    assert catalog.lookup(repository) is None
    assert not home.exists()


def test_catalog_maps_canonical_root_to_one_stable_store_without_creating_it(
    tmp_path: Path,
) -> None:
    home = tmp_path / "scs-home"
    repository = tmp_path / "repository"
    repository.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(repository, target_is_directory=True)
    catalog = ProjectStoreCatalog(home)

    first = catalog.register(repository)
    second = catalog.register(alias)

    assert first == second
    assert first.store_id == store_id_for_root(repository)
    assert catalog.lookup(alias) == first
    assert not (home / "projects" / first.store_id).exists()


def test_project_paths_are_contained_and_created_only_explicitly(tmp_path: Path) -> None:
    home = tmp_path / "scs-home"
    repository = tmp_path / "repository"
    repository.mkdir()
    store_id = store_id_for_root(repository)
    paths = ProjectStorePaths.resolve(home, store_id, StoreGeneration("g00000001"))

    assert paths.active == home / "projects" / store_id / "generations" / "g00000001"
    assert not home.exists()
    paths.ensure()

    for directory in (home, paths.projects, paths.store, paths.generations, paths.active):
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("store_id", "generation"),
    [
        ("../outside", "g00000001"),
        ("f" * 64, "../outside"),
        ("F" * 64, "g00000001"),
        ("f" * 64, "g/escape"),
    ],
)
def test_project_paths_reject_escape_components(
    tmp_path: Path,
    store_id: str,
    generation: str,
) -> None:
    with pytest.raises((StorePathError, ValueError)):
        ProjectStorePaths.resolve(
            tmp_path / "scs-home",
            StoreId(store_id),
            StoreGeneration(generation),
        )


def test_project_paths_reject_existing_symlinked_store_directory(tmp_path: Path) -> None:
    home = tmp_path / "scs-home"
    repository = tmp_path / "repository"
    destination = tmp_path / "outside"
    repository.mkdir()
    destination.mkdir()
    store_id = store_id_for_root(repository)
    paths = ProjectStorePaths.resolve(home, store_id, StoreGeneration("g00000001"))
    paths.projects.mkdir(parents=True)
    paths.store.symlink_to(destination, target_is_directory=True)

    with pytest.raises(StorePathError, match="escapes|real directory"):
        paths.ensure()


def test_catalog_state_update_requires_the_active_generation(tmp_path: Path) -> None:
    home = tmp_path / "scs-home"
    repository = tmp_path / "repository"
    repository.mkdir()
    catalog = ProjectStoreCatalog(home)
    catalog.register(repository)
    catalog.activate(
        repository,
        generation=StoreGeneration("g00000001"),
        state=StoreState.SEMANTIC_STALE,
    )

    ready = catalog.update_state(
        repository,
        expected_generation=StoreGeneration("g00000001"),
        state=StoreState.SEMANTIC_READY,
    )

    assert ready.state is StoreState.SEMANTIC_READY
    with pytest.raises(RuntimeError, match="generation"):
        catalog.update_state(
            repository,
            expected_generation=StoreGeneration("g00000002"),
            state=StoreState.SEMANTIC_READY,
        )
