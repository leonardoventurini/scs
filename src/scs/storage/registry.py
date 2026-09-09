"""Catalog-routed native graph handles for isolated project stores."""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scs.graph.native import NativeGraph
from scs.providers.base import ProviderMetadata
from scs.storage.catalog import CatalogRecord, ProjectStoreCatalog
from scs.storage.models import (
    StoreGeneration,
    StoreId,
    StoreState,
    canonical_repository_root,
    store_id_for_root,
    validate_store_generation,
    validate_store_id,
)
from scs.storage.paths import ProjectStorePaths, retired_project_store_path


@dataclass(frozen=True, slots=True)
class StoreBinding:
    """Immutable routing identity carried by a durable indexing job."""

    store_id: str
    generation: str


@dataclass(frozen=True, slots=True)
class RepositoryDeletionTarget:
    """SCS-owned state that may require durable repository cleanup."""

    store_id: str
    store_generation: str | None


class ProjectStoreRegistry:
    """Open only catalog-registered graph handles and flush them on shutdown."""

    def __init__(self, *, home: Path, provider: ProviderMetadata) -> None:
        self._home: Path = home
        self._provider: ProviderMetadata = provider
        self.catalog: ProjectStoreCatalog = ProjectStoreCatalog(home)
        self._graphs: dict[StoreBinding, NativeGraph] = {}

    def lookup_graph(self, root: str | Path) -> NativeGraph | None:
        """Resolve an existing graph without registering a root or creating paths."""

        record = self.catalog.lookup(root)
        if record is None or record.active_generation is None:
            return None
        paths = ProjectStorePaths.resolve(
            self._home, record.store_id, record.active_generation
        )
        if not paths.database.exists():
            return None
        return self._open(record, paths)

    def ensure_graph(self, root: str | Path) -> tuple[CatalogRecord, NativeGraph]:
        """Create one empty graph only for an explicit indexing request."""

        record = self.catalog.register(root)
        if record.active_generation is None:
            generation = StoreGeneration(f"g{uuid.uuid4().hex[:16]}")
            paths = ProjectStorePaths.resolve(self._home, record.store_id, generation)
            paths.ensure()
            graph = NativeGraph(
                database_path=paths.database,
                vector_path=paths.vector_index,
                provider_metadata_path=paths.provider_metadata,
                provider=self._provider,
            )
            canonical = canonical_repository_root(root)
            graph.get_or_create_repo_sync(canonical)
            record = self.catalog.activate(
                canonical,
                generation=generation,
                state=StoreState.SEMANTIC_STALE,
            )
            self._graphs[StoreBinding(str(record.store_id), str(generation))] = graph
            return record, graph
        paths = ProjectStorePaths.resolve(
            self._home, record.store_id, record.active_generation
        )
        return record, self._open(record, paths)

    def graph_for_binding(self, root: str, binding: StoreBinding) -> NativeGraph:
        """Reject a queued job whose recorded store no longer matches the catalog."""

        record = self.catalog.lookup(root)
        if (
            record is None
            or record.active_generation is None
            or binding.store_id != record.store_id
            or binding.generation != record.active_generation
        ):
            raise RuntimeError("project-store binding no longer matches the catalog")
        paths = ProjectStorePaths.resolve(
            self._home, record.store_id, record.active_generation
        )
        if not paths.database.exists():
            raise RuntimeError("project-store graph is missing for durable job")
        return self._open(record, paths)

    def mark_semantic_ready(self, root: str, binding: StoreBinding) -> CatalogRecord:
        """Publish semantic readiness for the generation completed by a job."""

        record = self.catalog.lookup(root)
        if record is None or binding.store_id != record.store_id:
            raise RuntimeError("project-store binding no longer matches the catalog")
        return self.catalog.update_state(
            root,
            expected_generation=StoreGeneration(binding.generation),
            state=StoreState.SEMANTIC_READY,
        )

    def records(self) -> list[CatalogRecord]:
        """Return registered stores for startup watcher restoration."""

        return self.catalog.list_records()

    def delete_repository(
        self,
        root: str | Path,
        *,
        store_id: str,
        store_generation: str | None,
        deletion_id: str,
    ) -> dict[str, object]:
        """Durably forget one repository and remove only its SCS-owned store.

        Store retirement precedes catalog removal. The deterministic tombstone
        lets a reclaimed job distinguish a completed graph/store retirement
        from work that still needs the native graph binding.
        """

        canonical = canonical_repository_root(root)
        safe_store_id = validate_store_id(StoreId(store_id))
        generation = (
            validate_store_generation(StoreGeneration(store_generation))
            if store_generation is not None
            else None
        )
        paths = ProjectStorePaths.resolve(
            self._home,
            safe_store_id,
            generation or StoreGeneration("deletion"),
        )
        tombstone = retired_project_store_path(
            self._home,
            safe_store_id,
            deletion_id,
        )
        record = self.catalog.lookup(canonical)
        binding_matches = (
            record is not None
            and generation is not None
            and record.store_id == safe_store_id
            and record.active_generation == generation
        )
        if record is not None and not binding_matches:
            self._remove_retired_stores(safe_store_id)
            return {"repo_deleted": False, "superseded": True}

        deletion_result: dict[str, object] = {
            "repo_deleted": record is not None or paths.store.exists(),
            "superseded": False,
        }
        if not tombstone.exists() and paths.store.exists():
            if binding_matches:
                assert generation is not None
                binding = StoreBinding(str(safe_store_id), str(generation))
                graph = self.graph_for_binding(canonical, binding)
                native_result = graph.delete_repo_sync(canonical)
                if isinstance(native_result, dict):
                    deletion_result.update(cast(dict[str, object], native_result))
                self._graphs.pop(binding, None)
                del graph
            paths.store.replace(tombstone)

        if binding_matches:
            assert generation is not None
            self.catalog.unregister(
                canonical,
                expected_store_id=safe_store_id,
                expected_generation=generation,
            )

        self._remove_retired_stores(safe_store_id)
        return deletion_result

    def deletion_target(
        self,
        root: str | Path,
    ) -> RepositoryDeletionTarget | None:
        """Resolve catalog, live-store, or tombstone state without source reads."""

        canonical = canonical_repository_root(root)
        record = self.catalog.lookup(canonical)
        store_id = record.store_id if record is not None else store_id_for_root(canonical)
        generation = record.active_generation if record is not None else None
        paths = ProjectStorePaths.resolve(
            self._home,
            store_id,
            generation or StoreGeneration("deletion"),
        )
        if record is None and not paths.store.exists() and not self._retired_stores(store_id):
            return None
        return RepositoryDeletionTarget(
            store_id=str(store_id),
            store_generation=str(generation) if generation is not None else None,
        )

    def flush(self) -> None:
        """Flush every cached sidecar before daemon ownership is released."""

        for graph in self._graphs.values():
            graph.flush_vector_index_sync()
        self._graphs.clear()

    def _open(self, record: CatalogRecord, paths: ProjectStorePaths) -> NativeGraph:
        generation = record.active_generation
        if generation is None:
            raise RuntimeError("cannot open an uninitialized project store")
        binding = StoreBinding(str(record.store_id), str(generation))
        graph = self._graphs.get(binding)
        if graph is None:
            graph = NativeGraph(
                database_path=paths.database,
                vector_path=paths.vector_index,
                provider_metadata_path=paths.provider_metadata,
                provider=self._provider,
            )
            self._graphs[binding] = graph
        return graph

    def _retired_stores(self, store_id: StoreId) -> list[Path]:
        projects = ProjectStorePaths.resolve(
            self._home,
            store_id,
            StoreGeneration("deletion"),
        ).projects
        if not projects.is_dir():
            return []
        prefix = f".deleted-{store_id}-"
        return sorted(
            path for path in projects.iterdir() if path.name.startswith(prefix)
        )

    def _remove_retired_stores(self, store_id: StoreId) -> None:
        for tombstone in self._retired_stores(store_id):
            shutil.rmtree(tombstone)
