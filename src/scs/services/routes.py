"""Transport-neutral implementations of SCS's public code-intelligence routes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import cast

from scs.graph.models import Edge, Node, NodeType
from scs.graph.native import NativeGraph
from scs.indexing.jobs import IngestionJobStore, job_to_dict
from scs.indexing.repository_paths import canonicalize_repo_path
from scs.providers.base import EmbeddingProvider, ProviderUnavailableError

SYMBOL_NODE_TYPES = frozenset(
    {
        NodeType.CLASS,
        NodeType.FUNCTION,
        NodeType.METHOD,
        NodeType.VARIABLE,
        NodeType.CONSTANT,
        NodeType.TYPE_ALIAS,
    }
)
TEST_PATH_MARKERS = ("tests/", "test/", "test_", "_test.", ".test.", "Tests.swift")


def _node_dict(node: Node) -> dict[str, object]:
    return node.model_dump(mode="json")


def _edge_dict(edge: Edge) -> dict[str, object]:
    return edge.model_dump(mode="json")


def _node_type(value: object) -> NodeType | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("node_type must be a string")
    try:
        return NodeType(value)
    except ValueError as error:
        raise ValueError(f"unsupported node_type: {value}") from error


def _integer(
    params: dict[str, object], key: str, default: int, *, minimum: int = 0
) -> int:
    value = params.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return max(minimum, value)


def _string(
    params: dict[str, object], key: str, *, required: bool = False
) -> str | None:
    value = params.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _string_list(
    params: dict[str, object], key: str, *, default: bool = True
) -> list[str]:
    """Validate an untrusted route parameter as a concrete string list."""

    value = params.get(key, []) if default else params.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list of strings")
    items = cast(list[object], value)
    if not all(isinstance(item, str) for item in items):
        raise ValueError(f"{key} must be a list of strings")
    return [item for item in items if isinstance(item, str)]


def _dict_list(value: object, *, key: str) -> list[dict[str, object]]:
    """Narrow a service response field before downstream composition."""

    if not isinstance(value, list):
        raise TypeError(f"{key} must be a list of objects")
    items = cast(list[object], value)
    if not all(isinstance(item, dict) for item in items):
        raise TypeError(f"{key} must be a list of objects")
    return [cast(dict[str, object], item) for item in items]


def _integer_metadata(value: object, *, key: str, default: int) -> int:
    """Convert graph metadata that originated outside Python's type boundary."""

    if value is None:
        return default
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        raise TypeError(f"{key} must be numeric")
    return int(value)


class SCSServiceRoutes:
    """Implement every finite method consumed by the SCS MCP gateway."""

    def __init__(
        self,
        *,
        graph: Callable[[], NativeGraph],
        jobs: Callable[[], IngestionJobStore],
        embeddings: Callable[[], EmbeddingProvider],
    ) -> None:
        self._graph: Callable[[], NativeGraph] = graph
        self._jobs: Callable[[], IngestionJobStore] = jobs
        self._embeddings: Callable[[], EmbeddingProvider] = embeddings

    def _repo_id(self, repo_path: object) -> int | None:
        if repo_path is None:
            return None
        if not isinstance(repo_path, str):
            raise ValueError("repo_path must be a string")
        return self._graph().resolve_repo_id_sync(canonicalize_repo_path(repo_path))

    async def search(self, params: dict[str, object]) -> dict[str, object]:
        query = _string(params, "query", required=True)
        assert query is not None
        limit = min(200, _integer(params, "limit", 10, minimum=1))
        node_type = _node_type(params.get("node_type"))
        repo_path = params.get("repo_path")
        repo_id = self._repo_id(repo_path)
        if repo_path is not None and repo_id is None:
            return {
                "query": query,
                "results": [],
                "neighbors": [],
                "total": 0,
                "retrieval_mode": "none",
            }
        graph = self._graph()
        results: list[dict[str, object]] = []
        mode = "lexical"
        try:
            vector = await self._embeddings().embed_query(query)
            semantic = await asyncio.to_thread(
                graph.search_by_vector_sync,
                vector,
                node_type=node_type,
                limit=limit,
                repo_id=repo_id,
            )
            results = [
                {**_node_dict(match.node), "distance": match.distance}
                for match in semantic
            ]
            mode = "semantic"
        except ProviderUnavailableError:
            pass
        if not results:
            lexical = await asyncio.to_thread(
                graph.search_by_name_sync,
                query,
                node_type=node_type,
                limit=limit,
                repo_id=repo_id,
            )
            results = [{**_node_dict(node), "distance": None} for node in lexical]
            mode = "lexical"
        neighbors: list[dict[str, object]] = []
        if bool(params.get("include_neighbors")):
            seen = {str(item["id"]) for item in results}
            for item in results:
                adjacent = await asyncio.to_thread(
                    graph.get_neighbors_sync, str(item["id"]), limit=20
                )
                for node in adjacent:
                    if node.id not in seen:
                        seen.add(node.id)
                        neighbors.append(_node_dict(node))
        return {
            "query": query,
            "results": results,
            "neighbors": neighbors,
            "total": len(results),
            "retrieval_mode": mode,
        }

    async def nodes_list(self, params: dict[str, object]) -> dict[str, object]:
        graph = self._graph()
        node_type = _node_type(params.get("node_type", NodeType.FUNCTION.value))
        if node_type not in SYMBOL_NODE_TYPES:
            raise ValueError("node_type must identify a code symbol")
        limit = min(200, _integer(params, "limit", 50, minimum=1))
        offset = _integer(params, "offset", 0)
        repo_path = params.get("repo_path")
        repo_id = self._repo_id(repo_path)
        if repo_path is not None and repo_id is None:
            return {"nodes": [], "total": 0, "limit": limit, "offset": offset}
        nodes, total = await asyncio.gather(
            asyncio.to_thread(
                graph.list_nodes_sync,
                node_type=node_type,
                limit=limit,
                offset=offset,
                repo_id=repo_id,
            ),
            asyncio.to_thread(
                graph.count_nodes_sync, node_type=node_type, repo_id=repo_id
            ),
        )
        return {
            "nodes": [_node_dict(node) for node in nodes],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def stats(self, params: dict[str, object]) -> dict[str, object]:
        graph = self._graph()
        repo_path = _string(params, "repo_path")
        repo_id = self._repo_id(repo_path)
        if repo_path is not None and repo_id is None:
            counts: dict[str, int] = {}
            without_embeddings = 0
            vector_index_count, all_ingestion = await asyncio.gather(
                asyncio.to_thread(graph.count_embeddings_sync),
                asyncio.to_thread(graph.get_ingestion_stats_sync),
            )
        else:
            (
                counts,
                without_embeddings,
                vector_index_count,
                all_ingestion,
            ) = await asyncio.gather(
                asyncio.to_thread(graph.count_nodes_by_type_sync, repo_id),
                asyncio.to_thread(
                    graph.count_nodes_without_embeddings_sync, repo_id=repo_id
                ),
                asyncio.to_thread(graph.count_embeddings_sync),
                asyncio.to_thread(graph.get_ingestion_stats_sync),
            )
        total_nodes = sum(counts.values())
        canonical_repo = canonicalize_repo_path(repo_path) if repo_path else None
        ingestion = (
            {canonical_repo: all_ingestion.get(canonical_repo, {})}
            if canonical_repo is not None
            else all_ingestion
        )
        return {
            "repo_path": canonical_repo,
            "status": "ready" if total_nodes else "empty",
            "total_nodes": total_nodes,
            "nodes_by_type": counts,
            "embedding_count": total_nodes - without_embeddings,
            "vector_index_count": vector_index_count,
            "vector_index_scope": "global",
            "ingestion_stats": ingestion,
            "database_size_bytes": graph.database_path.stat().st_size
            if graph.database_path.exists()
            else 0,
            "vector_available": graph.vector_state.available,
            "vector_unavailable_reason": graph.vector_state.reason,
        }

    async def related(self, params: dict[str, object]) -> dict[str, object]:
        symbol = _string(params, "symbol_name")
        node_id = _string(params, "node_id")
        if (symbol is None) == (node_id is None):
            raise ValueError("exactly one of symbol_name or node_id is required")
        graph = self._graph()
        repo_path = params.get("repo_path")
        repo_id = self._repo_id(repo_path)
        direction = _string(params, "direction") or "outgoing"
        if direction not in {"outgoing", "incoming", "both"}:
            raise ValueError("direction must be outgoing, incoming, or both")
        if repo_path is not None and repo_id is None:
            matches = []
        elif node_id is not None:
            node = await asyncio.to_thread(graph.get_node_sync, node_id)
            matches: list[Node] = [
                node
                for node in [node]
                if node is not None and (repo_path is None or node.repo_id == repo_id)
            ]
        else:
            assert symbol is not None
            matches = await asyncio.to_thread(
                graph.search_by_name_sync, symbol, limit=20, repo_id=repo_id
            )
        if not matches:
            return {
                "symbol_name": symbol,
                "node_id": node_id,
                "matches": [],
                "related": [],
            }
        depth = min(3, _integer(params, "depth", 2, minimum=1))
        relationship = _string(params, "relationship")
        related: list[dict[str, object]] = []
        directions = ("outgoing", "incoming") if direction == "both" else (direction,)
        for match in matches:
            for active_direction in directions:
                traversal = await asyncio.to_thread(
                    graph.traverse_sync,
                    match.id,
                    depth=depth,
                    relationship=relationship,
                    direction=active_direction,
                )
                related.extend(
                    {**item, "direction": active_direction, "seed_id": match.id}
                    for item in traversal
                )
        return {
            "symbol_name": symbol,
            "node_id": node_id,
            "matches": [_node_dict(node) for node in matches],
            "related": related,
        }

    async def graph_context(self, params: dict[str, object]) -> dict[str, object]:
        search_params = {
            **params,
            "limit": params.get("vector_limit", 5),
            "include_neighbors": False,
        }
        seeds = await self.search(search_params)
        graph = self._graph()
        context: list[dict[str, object]] = []
        seen: set[str] = set()
        seed_results = _dict_list(seeds.get("results"), key="results")
        for seed in seed_results:
            traversal = await asyncio.to_thread(
                graph.traverse_sync,
                str(seed["id"]),
                depth=min(3, _integer(params, "hop_limit", 2, minimum=1)),
            )
            for item in traversal:
                raw_node = item.get("node", item)
                node = (
                    cast(dict[str, object], raw_node)
                    if isinstance(raw_node, dict)
                    else None
                )
                key = str(node.get("id", "")) if node is not None else repr(item)
                if key not in seen:
                    seen.add(key)
                    context.append(item)
        return {"query": seeds["query"], "seeds": seed_results, "context": context}

    async def inspect_file(self, params: dict[str, object]) -> dict[str, object]:
        repo_path = _string(params, "repo_path", required=True)
        file_path = _string(params, "file_path", required=True)
        assert repo_path is not None and file_path is not None
        graph = self._graph()
        node_ids = await asyncio.to_thread(
            graph.get_node_ids_for_file_sync,
            canonicalize_repo_path(repo_path),
            Path(file_path).as_posix(),
        )
        nodes = [
            node
            for node_id in node_ids
            if (node := await asyncio.to_thread(graph.get_node_sync, node_id))
            is not None
        ]
        edges = (
            await asyncio.to_thread(graph.batch_get_edges_sync, node_ids)
            if node_ids
            else {}
        )
        return {
            "repo_path": canonicalize_repo_path(repo_path),
            "file_path": Path(file_path).as_posix(),
            "nodes": [_node_dict(node) for node in nodes],
            "edges": {
                node_id: [_edge_dict(edge) for edge in values]
                for node_id, values in edges.items()
            },
        }

    async def ingest_files(self, params: dict[str, object]) -> dict[str, object]:
        repo_path = _string(params, "repo_path", required=True)
        assert repo_path is not None
        canonical = canonicalize_repo_path(repo_path)
        raw_files = _string_list(params, "file_paths")
        raw_deleted = _string_list(params, "deleted_paths")
        root = Path(canonical)
        files: list[str] = []
        for raw_path in raw_files:
            resolved = Path(raw_path).expanduser().resolve(strict=True)
            if not resolved.is_file() or not resolved.is_relative_to(root):
                raise ValueError(f"source path escapes repository: {resolved}")
            files.append(str(resolved))
        deleted: list[str] = []
        for raw_path in raw_deleted:
            candidate = Path(raw_path)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError(
                    f"deleted path must remain repository-relative: {raw_path}"
                )
            deleted.append(candidate.as_posix())
        if not files and not deleted:
            raise ValueError("at least one changed or deleted file is required")
        job = await asyncio.to_thread(
            self._jobs().enqueue,
            repo_path=canonical,
            mode="files",
            reason="explicit_files",
            payload={"file_paths": files, "deleted_paths": deleted},
        )
        return {"accepted": True, "job": job_to_dict(job)}

    async def composite_regression_risk(
        self, params: dict[str, object]
    ) -> dict[str, object]:
        raw_paths = _string_list(params, "file_paths", default=False)
        repo_path = _string(params, "repo_path", required=True)
        assert repo_path is not None
        root = Path(canonicalize_repo_path(repo_path))
        graph = self._graph()
        affected_ids: list[str] = []
        for raw_path in raw_paths:
            path = Path(raw_path)
            resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
            if not resolved.is_relative_to(root):
                raise ValueError(f"source path escapes repository: {resolved}")
            rel_path = resolved.relative_to(root).as_posix()
            affected_ids.extend(
                await asyncio.to_thread(
                    graph.get_node_ids_for_file_sync, str(root), rel_path
                )
            )
        edges = (
            await asyncio.to_thread(graph.batch_get_edges_sync, affected_ids)
            if affected_ids
            else {}
        )
        dependent_ids = sorted(
            {
                edge.source_id
                for values in edges.values()
                for edge in values
                if edge.target_id in affected_ids
            }
        )
        dependents = [
            node
            for node_id in dependent_ids
            if (node := await asyncio.to_thread(graph.get_node_sync, node_id))
            is not None
        ]
        return {
            "file_paths": raw_paths,
            "affected_node_ids": affected_ids,
            "dependents": [_node_dict(node) for node in dependents],
            "test_dependents": [
                _node_dict(node)
                for node in dependents
                if any(
                    marker in str(node.metadata.get("file_path", ""))
                    for marker in TEST_PATH_MARKERS
                )
            ],
        }

    async def lsp_references(self, params: dict[str, object]) -> dict[str, object]:
        node = await self._node_at_position(params)
        if node is None:
            return self._lsp_unavailable(
                str(params.get("file_path", "")),
                "no indexed symbol exists at this position",
            )
        edges = await asyncio.to_thread(
            self._graph().get_edges_sync, node.id, direction="incoming"
        )
        references = [
            related
            for edge in edges
            if (
                related := await asyncio.to_thread(
                    self._graph().get_node_sync, edge.source_id
                )
            )
            is not None
        ]
        return {
            "available": True,
            "source": "index",
            "symbol": _node_dict(node),
            "references": [_node_dict(item) for item in references],
        }

    def _indexed_location(self, file_path: Path) -> tuple[str | None, str]:
        for repo_path in self._graph().get_ingestion_stats_sync():
            root = Path(repo_path)
            if file_path.is_relative_to(root):
                return repo_path, file_path.relative_to(root).as_posix()
        return None, file_path.name

    async def _node_at_position(self, params: dict[str, object]) -> Node | None:
        file_path = _string(params, "file_path", required=True)
        assert file_path is not None
        line = _integer(params, "line", 0)
        repo_path, rel_path = self._indexed_location(
            Path(file_path).resolve(strict=True)
        )
        if repo_path is None:
            return None
        node_ids = await asyncio.to_thread(
            self._graph().get_node_ids_for_file_sync, repo_path, rel_path
        )
        candidates = [
            node
            for node_id in node_ids
            if (node := await asyncio.to_thread(self._graph().get_node_sync, node_id))
            is not None
        ]
        containing = [
            node
            for node in candidates
            if _integer_metadata(
                node.metadata.get("start_line"), key="start_line", default=-1
            )
            <= line
            <= _integer_metadata(
                node.metadata.get("end_line"), key="end_line", default=-1
            )
        ]
        return min(
            containing,
            key=lambda node: (
                _integer_metadata(
                    node.metadata.get("end_line"), key="end_line", default=line
                )
                - _integer_metadata(
                    node.metadata.get("start_line"), key="start_line", default=line
                )
            ),
            default=None,
        )

    @staticmethod
    def _lsp_unavailable(file_path: str, reason: str) -> dict[str, object]:
        return {
            "available": False,
            "source": "index",
            "file_path": file_path,
            "reason": reason,
            "language_server_configured": False,
        }
