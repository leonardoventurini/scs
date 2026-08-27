"""Catalog-routed native graph handles for isolated project stores."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from scs.graph.native import NativeGraph
from scs.providers.base import ProviderMetadata
from scs.storage.catalog import CatalogRecord, ProjectStoreCatalog
from scs.storage.models import StoreGeneration, StoreState, canonical_repository_root
from scs.storage.paths import ProjectStorePaths


@dataclass(frozen=True, slots=True)
class StoreBinding:
    """Immutable routing identity carried by a durable indexing job."""

    store_id: str
    generation: str


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
