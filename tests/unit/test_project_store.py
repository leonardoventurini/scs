"""Regression coverage for isolated project-store catalog and path contracts."""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

import pytest

from scs.providers.base import ProviderMetadata
from scs.storage import (
    ProjectStoreCatalog,
    ProjectStorePaths,
    ProjectStoreRegistry,
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


def test_catalog_unregister_is_conditional_idempotent_and_isolated(
    tmp_path: Path,
) -> None:
    home = tmp_path / "scs-home"
    repository = tmp_path / "repository"
    sibling = tmp_path / "sibling"
    repository.mkdir()
    sibling.mkdir()
    catalog = ProjectStoreCatalog(home)
    record = catalog.register(repository)
    sibling_record = catalog.register(sibling)
    generation = StoreGeneration("g00000001")
    catalog.activate(
        repository,
        generation=generation,
        state=StoreState.SEMANTIC_STALE,
    )
    sibling_record = catalog.activate(
        sibling,
        generation=generation,
        state=StoreState.SEMANTIC_STALE,
    )

    assert (
        catalog.unregister(
            repository,
            expected_store_id=record.store_id,
            expected_generation=StoreGeneration("g00000002"),
        )
        is False
    )
    assert catalog.lookup(repository) is not None

    assert (
        catalog.unregister(
            repository,
            expected_store_id=record.store_id,
            expected_generation=generation,
        )
        is True
    )
    assert catalog.lookup(repository) is None
    retained_sibling = catalog.lookup(sibling)
    assert retained_sibling is not None
    assert retained_sibling.store_id == sibling_record.store_id

    assert (
        catalog.unregister(
            repository,
            expected_store_id=record.store_id,
            expected_generation=generation,
        )
        is False
    )


def test_catalog_unregister_removes_an_uninitialized_registration(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    catalog = ProjectStoreCatalog(tmp_path / "scs-home")
    record = catalog.register(repository)

    assert record.active_generation is None
    assert catalog.unregister(
        repository,
        expected_store_id=record.store_id,
        expected_generation=None,
    )
    assert catalog.lookup(repository) is None


def test_repository_retirement_preserves_source_and_sibling_store(
    tmp_path: Path,
) -> None:
    home = tmp_path / "scs-home"
    repository = tmp_path / "repository"
    sibling = tmp_path / "sibling"
    repository.mkdir()
    sibling.mkdir()
    source = repository / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    source_bytes = source.read_bytes()
    registry = ProjectStoreRegistry(
        home=home,
        provider=ProviderMetadata("test", "unavailable", 2, False, "disabled"),
    )
    record, _graph = registry.ensure_graph(repository)
    sibling_record, _sibling_graph = registry.ensure_graph(sibling)
    assert record.active_generation is not None
    assert sibling_record.active_generation is not None
    paths = ProjectStorePaths.resolve(home, record.store_id, record.active_generation)
    sibling_paths = ProjectStorePaths.resolve(
        home,
        sibling_record.store_id,
        sibling_record.active_generation,
    )

    registry.delete_repository(
        repository,
        store_id=str(record.store_id),
        store_generation=str(record.active_generation),
        deletion_id="ingest_123456789abc",
    )

    assert registry.catalog.lookup(repository) is None
    assert not paths.store.exists()
    assert set(paths.projects.iterdir()) == {sibling_paths.store}
    assert source.read_bytes() == source_bytes
    assert sibling_paths.store.is_dir()
    assert registry.catalog.lookup(sibling) == sibling_record


def test_repository_retirement_resumes_from_a_contained_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "scs-home"
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    registry = ProjectStoreRegistry(
        home=home,
        provider=ProviderMetadata("test", "unavailable", 2, False, "disabled"),
    )
    record, _graph = registry.ensure_graph(repository)
    assert record.active_generation is not None
    paths = ProjectStorePaths.resolve(home, record.store_id, record.active_generation)
    original_rmtree = shutil.rmtree
    removals = 0

    def fail_first_removal(path: Path) -> None:
        nonlocal removals
        removals += 1
        if removals == 1:
            raise OSError("synthetic tombstone removal failure")
        original_rmtree(path)

    monkeypatch.setattr("scs.storage.registry.shutil.rmtree", fail_first_removal)
    with pytest.raises(OSError, match="synthetic tombstone removal failure"):
        registry.delete_repository(
            repository,
            store_id=str(record.store_id),
            store_generation=str(record.active_generation),
            deletion_id="ingest_123456789abc",
        )

    tombstones = list(paths.projects.iterdir())
    assert len(tombstones) == 1
    assert tombstones[0].parent == paths.projects
    assert tombstones[0] != paths.store
    assert registry.catalog.lookup(repository) is None
    assert not paths.store.exists()
    assert source.read_text(encoding="utf-8") == "value = 1\n"

    registry.delete_repository(
        repository,
        store_id=str(record.store_id),
        store_generation=str(record.active_generation),
        deletion_id="ingest_123456789abc",
    )

    assert list(paths.projects.iterdir()) == []
    assert source.read_text(encoding="utf-8") == "value = 1\n"
