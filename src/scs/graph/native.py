"""Typed Python adapter over the independently built ``_scs_native`` module."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from scs.graph.models import Edge, Node, NodeType, SearchResult
from scs.providers.base import ProviderMetadata

PROVIDER_METADATA_SCHEMA_VERSION = 1
VECTOR_QUARANTINE_SUFFIX = ".quarantine"


class NativeModuleUnavailableError(RuntimeError):
    """Raised when the standalone SCS native extension is not installed."""


class _NativeGraphHandle(Protocol):
    """Native methods required by the authoritative Python pipeline."""

    def get_or_create_repo(self, path: str) -> int: ...
    def resolve_repo_id(self, path: str) -> int | None: ...
    def resolve_repo_path(self, repo_id: int) -> str | None: ...
    def get_file_node_map(self, repo_id: int) -> object: ...
    def batch_upsert_nodes(self, nodes_json: str) -> int: ...
    def batch_upsert_edges(self, edges_json: str) -> int: ...
    def batch_upsert_embeddings(self, embeddings_json: str) -> int: ...
    def flush_vector_index(self) -> bool: ...
    def get_all_ingested_files(self, repo_path: str) -> str: ...
    def get_file_paths_for_repo(self, repo_path: str) -> list[str]: ...
    def get_node_ids_for_file(self, repo_path: str, rel_path: str) -> list[str]: ...
    def delete_node(self, node_id: str) -> bool: ...
    def delete_ingested_file(self, repo_path: str, rel_path: str) -> None: ...
    def delete_ingestion_record(self, repo_path: str, rel_path: str) -> None: ...
    def upsert_ingested_file(self, **kwargs: object) -> None: ...
    def search_by_name(self, query: str, node_type: str | None, limit: int, repo_id: int | None) -> object: ...
    def search_by_vector(self, embedding: list[float], node_type: str | None, limit: int, repo_id: int | None) -> object: ...
    def get_node(self, node_id: str) -> object | None: ...
    def list_nodes(self, node_type: str | None, limit: int, offset: int, repo_id: int | None) -> object: ...
    def count_nodes(self, node_type: str | None, repo_id: int | None) -> int: ...
    def count_nodes_by_type(self, repo_id: int | None) -> object: ...
    def count_embeddings(self) -> int: ...
    def get_edges(self, node_id: str, relationship: str | None, direction: str) -> object: ...
    def batch_get_edges(self, node_ids: list[str], direction: str) -> object: ...
    def get_neighbors(self, node_id: str, relationship: str | None, direction: str, limit: int) -> object: ...
    def traverse(self, node_id: str, depth: int, relationship: str | None, direction: str) -> object: ...
    def delete_repo(self, repo_path: str) -> object: ...
    def get_ingestion_stats(self) -> object: ...


@dataclass(frozen=True, slots=True)
class VectorState:
    """Truthful vector readiness after provider metadata validation."""

    available: bool
    reason: str | None = None
    quarantined_path: Path | None = None


def _json_value(value: object) -> object:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _quarantine(path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = path.with_name(f"{path.name}{VECTOR_QUARANTINE_SUFFIX}-{stamp}")
    collision = 1
    while destination.exists():
        destination = path.with_name(
            f"{path.name}{VECTOR_QUARANTINE_SUFFIX}-{stamp}-{collision}"
        )
        collision += 1
    shutil.move(path, destination)
    return destination


class NativeGraph:
    """Synchronous native graph facade with asynchronous search conveniences."""

    def __init__(
        self,
        *,
        database_path: Path,
        vector_path: Path,
        provider_metadata_path: Path,
        provider: ProviderMetadata,
        native_handle: _NativeGraphHandle | None = None,
    ) -> None:
        self.database_path = database_path
        self.vector_path = vector_path
        self.provider_metadata_path = provider_metadata_path
        self.provider = provider
        self.vector_state = self._prepare_vector_state()
        self._inner = native_handle or self._open_native()
        if provider.available:
            _atomic_write_json(
                provider_metadata_path,
                {"schema_version": PROVIDER_METADATA_SCHEMA_VERSION, **provider.to_dict()},
            )

    def _prepare_vector_state(self) -> VectorState:
        if not self.provider.available:
            return VectorState(False, self.provider.reason or "embedding provider unavailable")
        if not self.vector_path.exists():
            return VectorState(True)
        try:
            persisted = json.loads(self.provider_metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            quarantined = _quarantine(self.vector_path)
            return VectorState(False, "vector provider metadata is missing or invalid", quarantined)
        expected = self.provider.to_dict()
        if any(persisted.get(key) != expected[key] for key in ("provider", "model", "dimension")):
            quarantined = _quarantine(self.vector_path)
            return VectorState(False, "vector provider metadata does not match active provider", quarantined)
        return VectorState(True)

    def _open_native(self) -> _NativeGraphHandle:
        try:
            module = importlib.import_module("_scs_native")
        except ImportError as exc:
            raise NativeModuleUnavailableError(
                "_scs_native is not installed; build the standalone SCS native extension"
            ) from exc
        return cast(_NativeGraphHandle, module.KnowledgeGraph(str(self.database_path), self.provider.dimension))

    def get_or_create_repo_sync(self, path: str) -> int:
        return self._inner.get_or_create_repo(path)

    def resolve_repo_id_sync(self, path: str) -> int | None:
        return self._inner.resolve_repo_id(path)

    def resolve_repo_path_sync(self, repo_id: int) -> str | None:
        return self._inner.resolve_repo_path(repo_id)

    def get_file_node_map_sync(self, repo_id: int) -> dict[str, str]:
        """Map repository-relative source paths to parser-created file nodes."""

        return cast(dict[str, str], _json_value(self._inner.get_file_node_map(repo_id)))

    def batch_upsert_nodes_sync(self, nodes: list[dict[str, object]]) -> int:
        return self._inner.batch_upsert_nodes(json.dumps(nodes, default=str))

    def batch_upsert_edges_sync(self, edges: list[dict[str, object]]) -> int:
        return self._inner.batch_upsert_edges(json.dumps(edges, default=str))

    def batch_upsert_embeddings_sync(self, embeddings: list[tuple[str, list[float]]]) -> int:
        return self._inner.batch_upsert_embeddings(json.dumps(embeddings))

    def flush_vector_index_sync(self) -> bool:
        return self._inner.flush_vector_index()

    def get_all_ingested_files_sync(self, repo_path: str) -> dict[str, str]:
        return cast(dict[str, str], _json_value(self._inner.get_all_ingested_files(repo_path)))

    def get_file_paths_for_repo_sync(self, repo_path: str) -> list[str]:
        return self._inner.get_file_paths_for_repo(repo_path)

    def get_node_ids_for_file_sync(self, repo_path: str, rel_path: str) -> list[str]:
        return self._inner.get_node_ids_for_file(repo_path, rel_path)

    def delete_node_sync(self, node_id: str) -> bool:
        return self._inner.delete_node(node_id)

    def delete_ingested_file_sync(self, repo_path: str, rel_path: str) -> int:
        self._inner.delete_ingested_file(repo_path, rel_path)
        return 1

    def delete_ingestion_record_sync(self, repo_path: str, rel_path: str) -> bool:
        self._inner.delete_ingestion_record(repo_path, rel_path)
        return True

    def upsert_ingested_file_sync(self, **kwargs: object) -> None:
        self._inner.upsert_ingested_file(**kwargs)

    def search_by_name_sync(
        self,
        query: str,
        *,
        node_type: NodeType | None = None,
        limit: int = 20,
        repo_id: int | None = None,
    ) -> list[Node]:
        raw = _json_value(
            self._inner.search_by_name(
                query,
                node_type.value if node_type else None,
                limit,
                repo_id,
            )
        )
        return [Node.model_validate(item) for item in cast(list[object], raw)]

    async def search_by_name(self, query: str, **kwargs: object) -> list[Node]:
        return await asyncio.to_thread(self.search_by_name_sync, query, **kwargs)

    def get_node_sync(self, node_id: str) -> Node | None:
        raw = self._inner.get_node(node_id)
        return Node.model_validate(_json_value(raw)) if raw is not None else None

    def list_nodes_sync(
        self,
        *,
        node_type: NodeType | None = None,
        limit: int = 100,
        offset: int = 0,
        repo_id: int | None = None,
    ) -> list[Node]:
        raw = _json_value(
            self._inner.list_nodes(
                node_type.value if node_type else None, limit, offset, repo_id
            )
        )
        return [Node.model_validate(item) for item in cast(list[object], raw)]

    def count_nodes_sync(
        self, *, node_type: NodeType | None = None, repo_id: int | None = None
    ) -> int:
        return self._inner.count_nodes(node_type.value if node_type else None, repo_id)

    def count_nodes_by_type_sync(self, repo_id: int | None = None) -> dict[str, int]:
        return cast(dict[str, int], _json_value(self._inner.count_nodes_by_type(repo_id)))

    def count_embeddings_sync(self) -> int:
        return self._inner.count_embeddings()

    def get_edges_sync(
        self,
        node_id: str,
        *,
        relationship: str | None = None,
        direction: str = "both",
    ) -> list[Edge]:
        raw = _json_value(self._inner.get_edges(node_id, relationship, direction))
        return [Edge.model_validate(item) for item in cast(list[object], raw)]

    def batch_get_edges_sync(
        self, node_ids: list[str], *, direction: str = "both"
    ) -> dict[str, list[Edge]]:
        raw = cast(
            dict[str, list[object]],
            _json_value(self._inner.batch_get_edges(node_ids, direction)),
        )
        return {
            node_id: [Edge.model_validate(item) for item in edges]
            for node_id, edges in raw.items()
        }

    def get_neighbors_sync(
        self,
        node_id: str,
        *,
        relationship: str | None = None,
        direction: str = "outgoing",
        limit: int = 50,
    ) -> list[Node]:
        raw = _json_value(
            self._inner.get_neighbors(node_id, relationship, direction, limit)
        )
        return [Node.model_validate(item) for item in cast(list[object], raw)]

    def traverse_sync(
        self,
        node_id: str,
        *,
        depth: int = 2,
        relationship: str | None = None,
        direction: str = "outgoing",
    ) -> list[dict[str, object]]:
        return cast(
            list[dict[str, object]],
            _json_value(self._inner.traverse(node_id, depth, relationship, direction)),
        )

    def search_by_vector_sync(
        self,
        vector: list[float],
        *,
        node_type: NodeType | None = None,
        limit: int = 20,
        repo_id: int | None = None,
    ) -> list[SearchResult]:
        if not self.vector_state.available:
            return []
        raw = _json_value(
            self._inner.search_by_vector(
                vector,
                node_type.value if node_type else None,
                limit,
                repo_id,
            )
        )
        return [SearchResult.model_validate(item) for item in cast(list[object], raw)]

    def delete_repo_sync(self, repo_path: str) -> object:
        return _json_value(self._inner.delete_repo(repo_path))

    def get_ingestion_stats_sync(self) -> dict[str, dict[str, object]]:
        return cast(dict[str, dict[str, object]], _json_value(self._inner.get_ingestion_stats()))
