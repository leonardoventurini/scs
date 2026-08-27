"""Transport-neutral implementations of SCS's public code-intelligence routes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import cast

from scs.graph.models import Edge, Node, NodeType, RelationshipType
from scs.graph.native import NativeGraph
from scs.indexing.jobs import IngestionJobStore, job_to_dict
from scs.indexing.repository_paths import canonicalize_repo_path
from scs.providers.base import EmbeddingProvider, ProviderUnavailableError

GraphForRepository = Callable[[str], NativeGraph | None]
BindingForRepository = Callable[[str], tuple[str, str] | None]

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
DEPENDENCY_RELATIONSHIPS = frozenset(
    {
        RelationshipType.CALLS,
        RelationshipType.IMPORTS,
        RelationshipType.INHERITS,
        RelationshipType.IMPLEMENTS,
        RelationshipType.REFERENCES,
    }
)
DEFAULT_INSPECT_NODE_LIMIT = 50
DEFAULT_INSPECT_EDGE_LIMIT = 100
MAX_INSPECT_NODE_LIMIT = 200
MAX_INSPECT_EDGE_LIMIT = 500


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
        graph_for_repository: GraphForRepository | None = None,
        binding_for_repository: BindingForRepository | None = None,
    ) -> None:
        self._graph: Callable[[], NativeGraph] = graph
        self._jobs: Callable[[], IngestionJobStore] = jobs
        self._embeddings: Callable[[], EmbeddingProvider] = embeddings
        self._graph_for_repository: GraphForRepository | None = graph_for_repository
        self._binding_for_repository: BindingForRepository | None = (
            binding_for_repository
        )

    def _read_graph(self, repo_path: object) -> NativeGraph | None:
        """Resolve an existing project graph without registering a repository."""

        if repo_path is None or self._graph_for_repository is None:
            try:
                return self._graph()
            except RuntimeError:
                return None
        if not isinstance(repo_path, str):
            raise ValueError("repo_path must be a string")
        return self._graph_for_repository(canonicalize_repo_path(repo_path))

    def _repo_id(self, repo_path: object) -> int | None:
        if repo_path is None:
            return None
        if not isinstance(repo_path, str):
            raise ValueError("repo_path must be a string")
        graph = self._read_graph(repo_path)
        if graph is None:
            return None
        return graph.resolve_repo_id_sync(canonicalize_repo_path(repo_path))

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
        graph = self._read_graph(repo_path)
        if graph is None:
            return {
                "query": query,
                "results": [],
                "neighbors": [],
                "total": 0,
                "retrieval_mode": "none",
            }
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
        node_type = _node_type(params.get("node_type", NodeType.FUNCTION.value))
        if node_type not in SYMBOL_NODE_TYPES:
            raise ValueError("node_type must identify a code symbol")
        limit = min(200, _integer(params, "limit", 50, minimum=1))
        offset = _integer(params, "offset", 0)
        repo_path = params.get("repo_path")
        graph = self._read_graph(repo_path)
        if graph is None:
            return {"nodes": [], "total": 0, "limit": limit, "offset": offset}
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
        repo_path = _string(params, "repo_path")
        graph = self._read_graph(repo_path)
        if graph is None:
            return {
                "repo_path": canonicalize_repo_path(repo_path) if repo_path else None,
                "status": "empty",
                "total_nodes": 0,
                "nodes_by_type": {},
                "embedding_count": 0,
                "vector_index_count": 0,
                "vector_index_scope": "project",
                "ingestion_stats": {},
                "database_size_bytes": 0,
                "vector_available": False,
                "vector_unavailable_reason": "repository is not indexed",
                "semantic_search_ready": False,
                "semantic_search_unavailable_reason": "repository is not indexed",
            }
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
        embedding_count = total_nodes - without_embeddings
        provider_metadata = self._embeddings().metadata
        semantic_search_ready = (
            provider_metadata.available
            and graph.vector_state.available
            and embedding_count > 0
        )
        if semantic_search_ready:
            semantic_search_unavailable_reason: str | None = None
        elif not provider_metadata.available:
            semantic_search_unavailable_reason = provider_metadata.reason
        elif not graph.vector_state.available:
            semantic_search_unavailable_reason = graph.vector_state.reason
        else:
            semantic_search_unavailable_reason = (
                "no indexed embeddings are available for this scope"
            )
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
            "embedding_count": embedding_count,
            "vector_index_count": vector_index_count,
            "vector_index_scope": "global",
            "ingestion_stats": ingestion,
            "database_size_bytes": graph.database_path.stat().st_size
            if graph.database_path.exists()
            else 0,
            "vector_available": graph.vector_state.available,
            "vector_unavailable_reason": graph.vector_state.reason,
            "semantic_search_ready": semantic_search_ready,
            "semantic_search_unavailable_reason": semantic_search_unavailable_reason,
        }

    async def related(self, params: dict[str, object]) -> dict[str, object]:
        symbol = _string(params, "symbol_name")
        node_id = _string(params, "node_id")
        if (symbol is None) == (node_id is None):
            raise ValueError("exactly one of symbol_name or node_id is required")
        repo_path = params.get("repo_path")
        graph = self._read_graph(repo_path)
        if graph is None:
            return {"symbol_name": symbol, "node_id": node_id, "matches": [], "related": []}
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
            matches_by_type = await asyncio.gather(
                *(
                    asyncio.to_thread(
                        graph.search_by_name_sync,
                        symbol,
                        node_type=node_type,
                        limit=20,
                        repo_id=repo_id,
                    )
                    for node_type in SYMBOL_NODE_TYPES
                )
            )
            matches = sorted(
                {node.id: node for nodes in matches_by_type for node in nodes}.values(),
                key=lambda node: (node.name.casefold() != symbol.casefold(), node.name, node.id),
            )
            # An exact declaration is the deterministic answer to a symbol lookup.
            # Retaining substring matches only when no exact symbol exists preserves
            # discovery without letting a similarly named test/helper dilute lookup.
            exact_matches = [
                node for node in matches if node.name.casefold() == symbol.casefold()
            ]
            matches = (exact_matches or matches)[:20]
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
        direction = _string(params, "direction") or "both"
        graph = self._read_graph(params.get("repo_path"))
        if graph is None:
            return {"query": seeds["query"], "direction": direction, "seeds": [], "context": []}
        if direction not in {"outgoing", "incoming", "both"}:
            raise ValueError("direction must be outgoing, incoming, or both")
        directions = ("outgoing", "incoming") if direction == "both" else (direction,)
        context: list[dict[str, object]] = []
        seen: set[str] = set()
        seed_results = _dict_list(seeds.get("results"), key="results")
        for seed in seed_results:
            for active_direction in directions:
                traversal = await asyncio.to_thread(
                    graph.traverse_sync,
                    str(seed["id"]),
                    depth=min(3, _integer(params, "hop_limit", 2, minimum=1)),
                    direction=active_direction,
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
                        context.append({**item, "direction": active_direction})
        return {
            "query": seeds["query"],
            "direction": direction,
            "seeds": seed_results,
            "context": context,
        }

    async def inspect_file(self, params: dict[str, object]) -> dict[str, object]:
        repo_path = _string(params, "repo_path", required=True)
        file_path = _string(params, "file_path", required=True)
        assert repo_path is not None and file_path is not None
        graph = self._read_graph(repo_path)
        if graph is None:
            return {
                "repo_path": canonicalize_repo_path(repo_path),
                "file_path": Path(file_path).as_posix(),
                "nodes": [],
                "edges": {},
                "nodes_truncated": False,
                "edges_truncated": False,
            }
        node_ids = sorted(await asyncio.to_thread(
            graph.get_node_ids_for_file_sync,
            canonicalize_repo_path(repo_path),
            Path(file_path).as_posix(),
        ))
        node_limit = min(
            MAX_INSPECT_NODE_LIMIT,
            _integer(
                params,
                "node_limit",
                DEFAULT_INSPECT_NODE_LIMIT,
                minimum=1,
            ),
        )
        edge_limit = min(
            MAX_INSPECT_EDGE_LIMIT,
            _integer(
                params,
                "edge_limit",
                DEFAULT_INSPECT_EDGE_LIMIT,
                minimum=1,
            ),
        )
        selected_node_ids = node_ids[:node_limit]
        nodes = [
            node
            for node_id in selected_node_ids
            if (node := await asyncio.to_thread(graph.get_node_sync, node_id))
            is not None
        ]
        all_edges = (
            await asyncio.to_thread(graph.batch_get_edges_sync, selected_node_ids)
            if selected_node_ids
            else {}
        )
        ordered_edges = [
            (node_id, edge)
            for node_id in sorted(all_edges)
            for edge in sorted(
                all_edges[node_id],
                key=lambda edge: (edge.source_id, edge.target_id, edge.relationship, edge.id),
            )
        ]
        limited_edges = ordered_edges[:edge_limit]
        edges: dict[str, list[Edge]] = {}
        for node_id, edge in limited_edges:
            edges.setdefault(node_id, []).append(edge)
        return {
            "repo_path": canonicalize_repo_path(repo_path),
            "file_path": Path(file_path).as_posix(),
            "nodes": [_node_dict(node) for node in nodes],
            "edges": {
                node_id: [_edge_dict(edge) for edge in values]
                for node_id, values in edges.items()
            },
            "nodes_truncated": len(node_ids) > len(selected_node_ids),
            "edges_truncated": len(ordered_edges) > len(limited_edges),
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
        binding = (
            self._binding_for_repository(canonical)
            if self._binding_for_repository is not None
            else None
        )
        if self._binding_for_repository is not None and binding is None:
            raise ValueError("repository does not have an indexed project store")
        job = await asyncio.to_thread(
            self._jobs().enqueue,
            repo_path=canonical,
            store_id=binding[0] if binding is not None else None,
            store_generation=binding[1] if binding is not None else None,
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
        graph = self._read_graph(repo_path)
        if graph is None:
            return {"file_paths": raw_paths, "affected_node_ids": [], "dependents": [], "test_dependents": []}
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
        affected_id_set = set(affected_ids)
        dependent_ids = sorted(
            {
                edge.source_id
                for values in edges.values()
                for edge in values
                if (
                    edge.target_id in affected_id_set
                    and edge.source_id not in affected_id_set
                    and edge.relationship in DEPENDENCY_RELATIONSHIPS
                )
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
