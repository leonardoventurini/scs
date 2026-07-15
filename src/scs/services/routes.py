"""Transport-neutral implementations of SCS's public code-intelligence routes."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from scs.config import SCSSettings
from scs.graph.models import Edge, Node, NodeType
from scs.graph.native import NativeGraph
from scs.indexing.git_history import GitHistoryIngester
from scs.indexing.jobs import IngestionJobStore, job_to_dict
from scs.indexing.repository_paths import canonicalize_repo_path
from scs.providers.base import EmbeddingProvider, ProviderUnavailableError

TESTABLE_NODE_TYPES = frozenset({NodeType.CLASS, NodeType.FUNCTION, NodeType.METHOD})
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
MAX_SCAN_NODES = 2_000


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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


def _integer(params: dict[str, object], key: str, default: int, *, minimum: int = 0) -> int:
    value = params.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return max(minimum, value)


def _string(params: dict[str, object], key: str, *, required: bool = False) -> str | None:
    value = params.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value):
        raise ValueError(f"{key} must be a non-empty string")
    return value


class SCSServiceRoutes:
    """Implement every finite method consumed by the SCS MCP gateway."""

    def __init__(
        self,
        *,
        graph: Callable[[], NativeGraph],
        jobs: Callable[[], IngestionJobStore],
        embeddings: Callable[[], EmbeddingProvider],
        settings: SCSSettings,
    ) -> None:
        self._graph = graph
        self._jobs = jobs
        self._embeddings = embeddings
        self._settings = settings

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
        repo_id = self._repo_id(params.get("repo_path"))
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
        node_type = _node_type(params.get("node_type"))
        limit = min(200, _integer(params, "limit", 50, minimum=1))
        offset = _integer(params, "offset", 0)
        repo_id = self._repo_id(params.get("repo_path"))
        nodes, total = await asyncio.gather(
            asyncio.to_thread(
                graph.list_nodes_sync,
                node_type=node_type,
                limit=limit,
                offset=offset,
                repo_id=repo_id,
            ),
            asyncio.to_thread(graph.count_nodes_sync, node_type=node_type, repo_id=repo_id),
        )
        return {
            "nodes": [_node_dict(node) for node in nodes],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def node_get(self, params: dict[str, object]) -> dict[str, object]:
        node_id = _string(params, "node_id", required=True)
        assert node_id is not None
        graph = self._graph()
        node = await asyncio.to_thread(graph.get_node_sync, node_id)
        if node is None:
            return {"found": False, "node": None, "edges": []}
        edges = (
            await asyncio.to_thread(graph.get_edges_sync, node_id)
            if bool(params.get("include_edges", False))
            else []
        )
        return {
            "found": True,
            "node": _node_dict(node),
            "edges": [_edge_dict(edge) for edge in edges],
        }

    async def stats(self, _params: dict[str, object]) -> dict[str, object]:
        graph = self._graph()
        counts, embeddings, ingestion = await asyncio.gather(
            asyncio.to_thread(graph.count_nodes_by_type_sync),
            asyncio.to_thread(graph.count_embeddings_sync),
            asyncio.to_thread(graph.get_ingestion_stats_sync),
        )
        return {
            "total_nodes": sum(counts.values()),
            "nodes_by_type": counts,
            "embedding_count": embeddings,
            "ingestion_stats": ingestion,
            "database_size_bytes": graph.database_path.stat().st_size
            if graph.database_path.exists()
            else 0,
            "vector_available": graph.vector_state.available,
            "vector_unavailable_reason": graph.vector_state.reason,
        }

    async def related(self, params: dict[str, object]) -> dict[str, object]:
        symbol = _string(params, "symbol_name", required=True)
        assert symbol is not None
        graph = self._graph()
        matches = await asyncio.to_thread(graph.search_by_name_sync, symbol, limit=20)
        if not matches:
            return {"symbol_name": symbol, "matches": [], "related": []}
        direction = _string(params, "direction") or "outgoing"
        if direction not in {"outgoing", "incoming", "both"}:
            raise ValueError("direction must be outgoing, incoming, or both")
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
        for seed in seeds["results"]:
            assert isinstance(seed, dict)
            traversal = await asyncio.to_thread(
                graph.traverse_sync,
                str(seed["id"]),
                depth=min(3, _integer(params, "hop_limit", 2, minimum=1)),
            )
            for item in traversal:
                key = str(item.get("node", item).get("id", "")) if isinstance(item.get("node", item), dict) else repr(item)
                if key not in seen:
                    seen.add(key)
                    context.append(item)
        return {"query": seeds["query"], "seeds": seeds["results"], "context": context}

    async def inspect(self, params: dict[str, object]) -> dict[str, object]:
        repo_id = self._repo_id(params.get("repo_path"))
        graph = self._graph()
        counts, without_embeddings = await asyncio.gather(
            asyncio.to_thread(graph.count_nodes_by_type_sync, repo_id),
            asyncio.to_thread(graph.count_embeddings_sync),
        )
        total = sum(counts.values())
        return {
            "total_nodes": total,
            "nodes_by_type": counts,
            "embedding_count": without_embeddings,
            "has_files": counts.get(NodeType.FILE.value, 0) > 0,
            "quality_status": "ready" if total else "empty",
        }

    async def sample(self, params: dict[str, object]) -> dict[str, object]:
        listing = await self.nodes_list({**params, "offset": 0})
        file_path = _string(params, "file_path")
        summary_status = _string(params, "summary_status")
        nodes = list(listing["nodes"])
        if file_path:
            nodes = [
                node
                for node in nodes
                if isinstance(node, dict)
                and node.get("metadata", {}).get("file_path") == file_path
            ]
        if summary_status:
            nodes = [
                node
                for node in nodes
                if isinstance(node, dict)
                and ("fresh" if node.get("metadata", {}).get("summary") else "missing")
                == summary_status
            ]
        return {"nodes": nodes, "total": len(nodes)}

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
            if (node := await asyncio.to_thread(graph.get_node_sync, node_id)) is not None
        ]
        edges = await asyncio.to_thread(graph.batch_get_edges_sync, node_ids) if node_ids else {}
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
        raw_files = params.get("file_paths", [])
        raw_deleted = params.get("deleted_paths", [])
        if not isinstance(raw_files, list) or not all(isinstance(path, str) for path in raw_files):
            raise ValueError("file_paths must be a list of strings")
        if not isinstance(raw_deleted, list) or not all(isinstance(path, str) for path in raw_deleted):
            raise ValueError("deleted_paths must be a list of strings")
        root = Path(canonical)
        files = []
        for raw_path in raw_files:
            resolved = Path(raw_path).expanduser().resolve(strict=True)
            if not resolved.is_file() or not resolved.is_relative_to(root):
                raise ValueError(f"source path escapes repository: {resolved}")
            files.append(str(resolved))
        deleted = []
        for raw_path in raw_deleted:
            candidate = Path(raw_path)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError(f"deleted path must remain repository-relative: {raw_path}")
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

    async def ingest_git_history(self, params: dict[str, object]) -> dict[str, object]:
        repo_path = _string(params, "repo_path", required=True)
        assert repo_path is not None
        root = Path(canonicalize_repo_path(repo_path))
        if not (root / ".git").exists():
            raise ValueError(f"repository is not a Git checkout: {root}")
        result = await asyncio.to_thread(GitHistoryIngester(self._graph()).ingest, root)
        return {
            "accepted": True,
            "repo_path": str(root),
            "commits_created": result.commits_created,
            "contributors_created": result.contributors_created,
            "edges_created": result.edges_created,
        }

    async def composite_test_coverage(self, params: dict[str, object]) -> dict[str, object]:
        requested_type = _node_type(params.get("node_type", NodeType.FUNCTION.value))
        if requested_type not in TESTABLE_NODE_TYPES:
            return {"covered": [], "uncovered": [], "coverage_percentage": 0.0, "total_test_files": 0}
        repo_id = self._repo_id(params.get("repo_path"))
        graph = self._graph()
        files = await asyncio.to_thread(graph.list_nodes_sync, node_type=NodeType.FILE, limit=MAX_SCAN_NODES, repo_id=repo_id)
        test_files = {
            node.id
            for node in files
            if any(marker in str(node.metadata.get("file_path", node.name)) for marker in TEST_PATH_MARKERS)
        }
        symbols = await asyncio.to_thread(
            graph.list_nodes_sync,
            node_type=requested_type,
            limit=min(200, _integer(params, "limit", 50, minimum=1)),
            repo_id=repo_id,
        )
        edge_map = await asyncio.to_thread(graph.batch_get_edges_sync, [node.id for node in symbols])
        covered: list[dict[str, object]] = []
        uncovered: list[dict[str, object]] = []
        for symbol in symbols:
            incoming = [edge for edge in edge_map.get(symbol.id, []) if edge.target_id == symbol.id]
            source_ids = [edge.source_id for edge in incoming]
            source_nodes = [
                node
                for source_id in source_ids
                if (node := await asyncio.to_thread(graph.get_node_sync, source_id)) is not None
            ]
            target = covered if any(node.id in test_files or any(marker in str(node.metadata.get("file_path", "")) for marker in TEST_PATH_MARKERS) for node in source_nodes) else uncovered
            target.append(_node_dict(symbol))
        total = len(symbols)
        return {
            "covered": covered,
            "uncovered": uncovered,
            "coverage_percentage": (len(covered) / total * 100.0) if total else 0.0,
            "total_test_files": len(test_files),
        }

    async def composite_contract_check(self, params: dict[str, object]) -> dict[str, object]:
        symbol = _string(params, "symbol_name", required=True)
        assert symbol is not None
        matches = await asyncio.to_thread(
            self._graph().search_by_name_sync,
            symbol,
            limit=50,
            repo_id=self._repo_id(params.get("repo_path")),
        )
        edge_map = await asyncio.to_thread(self._graph().batch_get_edges_sync, [node.id for node in matches])
        return {
            "symbol_name": symbol,
            "contracts": [
                {"node": _node_dict(node), "incoming_edges": [_edge_dict(edge) for edge in edge_map.get(node.id, []) if edge.target_id == node.id]}
                for node in matches
            ],
        }

    async def composite_regression_risk(self, params: dict[str, object]) -> dict[str, object]:
        raw_paths = params.get("file_paths")
        if not isinstance(raw_paths, list) or not all(isinstance(path, str) for path in raw_paths):
            raise ValueError("file_paths must be a list of strings")
        repo_path = _string(params, "repo_path")
        graph = self._graph()
        affected_ids: list[str] = []
        for raw_path in raw_paths:
            path = Path(raw_path)
            if repo_path:
                root = Path(canonicalize_repo_path(repo_path))
                resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
                if not resolved.is_relative_to(root):
                    raise ValueError(f"source path escapes repository: {resolved}")
                rel_path = resolved.relative_to(root).as_posix()
                affected_ids.extend(await asyncio.to_thread(graph.get_node_ids_for_file_sync, str(root), rel_path))
        edges = await asyncio.to_thread(graph.batch_get_edges_sync, affected_ids) if affected_ids else {}
        dependent_ids = sorted({edge.source_id for values in edges.values() for edge in values if edge.target_id in affected_ids})
        dependents = [
            node
            for node_id in dependent_ids
            if (node := await asyncio.to_thread(graph.get_node_sync, node_id)) is not None
        ]
        return {
            "file_paths": raw_paths,
            "affected_node_ids": affected_ids,
            "dependents": [_node_dict(node) for node in dependents],
            "test_dependents": [_node_dict(node) for node in dependents if any(marker in str(node.metadata.get("file_path", "")) for marker in TEST_PATH_MARKERS)],
        }

    async def composite_consistency(self, params: dict[str, object]) -> dict[str, object]:
        file_path = _string(params, "file_path", required=True)
        assert file_path is not None
        repo_path = _string(params, "repo_path")
        if repo_path:
            root = Path(canonicalize_repo_path(repo_path))
            resolved = Path(file_path).resolve() if Path(file_path).is_absolute() else (root / file_path).resolve()
            rel_path = resolved.relative_to(root).as_posix()
            node_ids = await asyncio.to_thread(self._graph().get_node_ids_for_file_sync, str(root), rel_path)
            nodes = [node for node_id in node_ids if (node := await asyncio.to_thread(self._graph().get_node_sync, node_id)) is not None and node.type in SYMBOL_NODE_TYPES]
        else:
            nodes = []
        by_type: dict[str, int] = {}
        undocumented: list[str] = []
        for node in nodes:
            by_type[node.type.value] = by_type.get(node.type.value, 0) + 1
            if not node.metadata.get("docstring"):
                undocumented.append(node.name)
        return {"file_path": file_path, "symbol_counts": by_type, "undocumented_symbols": undocumented, "symbols": [_node_dict(node) for node in nodes]}

    async def lsp_symbols(self, params: dict[str, object]) -> dict[str, object]:
        file_path = _string(params, "file_path", required=True)
        assert file_path is not None
        resolved = Path(file_path).resolve(strict=True)
        repo_path, rel_path = self._indexed_location(resolved)
        if repo_path is None:
            return self._lsp_unavailable(file_path, "file is not part of an indexed repository")
        node_ids = await asyncio.to_thread(self._graph().get_node_ids_for_file_sync, repo_path, rel_path)
        nodes = [node for node_id in node_ids if (node := await asyncio.to_thread(self._graph().get_node_sync, node_id)) is not None and node.type != NodeType.FILE]
        return {"available": True, "source": "index", "file_path": str(resolved), "symbols": [_node_dict(node) for node in nodes]}

    async def lsp_find_symbol(self, params: dict[str, object]) -> dict[str, object]:
        name = _string(params, "name", required=True)
        assert name is not None
        nodes = await asyncio.to_thread(self._graph().search_by_name_sync, name, limit=100)
        return {"available": True, "source": "index", "name": name, "symbols": [_node_dict(node) for node in nodes]}

    async def lsp_references(self, params: dict[str, object]) -> dict[str, object]:
        node = await self._node_at_position(params)
        if node is None:
            return self._lsp_unavailable(str(params.get("file_path", "")), "no indexed symbol exists at this position")
        edges = await asyncio.to_thread(self._graph().get_edges_sync, node.id, direction="incoming")
        references = [
            related
            for edge in edges
            if (related := await asyncio.to_thread(self._graph().get_node_sync, edge.source_id)) is not None
        ]
        return {"available": True, "source": "index", "symbol": _node_dict(node), "references": [_node_dict(item) for item in references]}

    async def lsp_hover(self, params: dict[str, object]) -> dict[str, object]:
        node = await self._node_at_position(params)
        if node is None:
            return self._lsp_unavailable(str(params.get("file_path", "")), "no indexed symbol exists at this position")
        return {"available": True, "source": "index", "symbol": _node_dict(node), "contents": node.metadata.get("signature") or node.metadata.get("docstring") or node.content}

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
        repo_path, rel_path = self._indexed_location(Path(file_path).resolve(strict=True))
        if repo_path is None:
            return None
        node_ids = await asyncio.to_thread(self._graph().get_node_ids_for_file_sync, repo_path, rel_path)
        candidates = [node for node_id in node_ids if (node := await asyncio.to_thread(self._graph().get_node_sync, node_id)) is not None]
        containing = [node for node in candidates if int(node.metadata.get("start_line", -1)) <= line <= int(node.metadata.get("end_line", -1))]
        return min(containing, key=lambda node: int(node.metadata.get("end_line", line)) - int(node.metadata.get("start_line", line)), default=None)

    @staticmethod
    def _lsp_unavailable(file_path: str, reason: str) -> dict[str, object]:
        return {"available": False, "source": "index", "file_path": file_path, "reason": reason, "language_server_configured": False}

    async def diagnostics_snapshot(self, params: dict[str, object]) -> dict[str, object]:
        stats = await self.stats({})
        log = self._settings.paths.logs / "scs.log"
        recent = self._recent_failures(80) if bool(params.get("include_logs")) else []
        return {
            "status": "healthy",
            "generated_at": _now(),
            "storage": {"home": str(self._settings.paths.home), "database": str(self._settings.paths.database), "database_exists": self._settings.paths.database.exists()},
            "index": stats,
            "log": {"path": str(log), "exists": log.exists(), "size_bytes": log.stat().st_size if log.exists() else 0},
            "recent_failures": recent,
        }

    async def diagnostics_recent_failures(self, params: dict[str, object]) -> dict[str, object]:
        limit = min(200, _integer(params, "limit", 50, minimum=1))
        return {"failures": self._recent_failures(limit), "generated_at": _now()}

    def _recent_failures(self, limit: int) -> list[dict[str, object]]:
        log = self._settings.paths.logs / "scs.log"
        if not log.exists():
            return []
        lines = log.read_bytes()[-131_072:].decode("utf-8", errors="replace").splitlines()
        return [{"line": line} for line in lines if "error" in line.lower() or "traceback" in line.lower()][-limit:]

    async def diagnostics_index_health(self, params: dict[str, object]) -> dict[str, object]:
        repo_path = params.get("repo_path")
        quality = await self.inspect({"repo_path": repo_path})
        status = "healthy" if quality["total_nodes"] else "empty"
        result: dict[str, object] = {"status": status, "generated_at": _now(), "repo_path": repo_path}
        if bool(params.get("include_quality")):
            result["quality"] = quality
        return result

    async def diagnostics_dev_doctor(self, params: dict[str, object]) -> dict[str, object]:
        repo_path = _string(params, "repo_path")
        tools = [self._tool_version("git", "--version"), self._tool_version("uv", "--version"), self._tool_version("cargo", "--version")]
        findings: list[dict[str, str]] = []
        if repo_path and not Path(repo_path).is_dir():
            findings.append({"severity": "error", "message": f"repository directory does not exist: {repo_path}"})
        for tool in tools:
            if not tool["available"]:
                findings.append({"severity": "error", "message": f"required development tool is unavailable: {tool['binary']}"})
        return {"status": "healthy" if not findings else "degraded", "generated_at": _now(), "tools": tools, "findings": findings}

    @staticmethod
    def _tool_version(binary: str, argument: str) -> dict[str, object]:
        path = shutil.which(binary)
        if path is None:
            return {"binary": binary, "available": False}
        completed = subprocess.run([path, argument], capture_output=True, text=True, timeout=5, check=False)
        output = (completed.stdout or completed.stderr).strip().splitlines()
        return {"binary": binary, "available": True, "path": path, "version": output[0] if output else "", "returncode": completed.returncode}

    async def diagnostics_test_recommendations(self, params: dict[str, object]) -> dict[str, object]:
        raw_files = params.get("changed_files", [])
        if not isinstance(raw_files, list) or not all(isinstance(path, str) for path in raw_files):
            raise ValueError("changed_files must be a list of strings")
        commands: list[dict[str, str]] = []
        if any(path.startswith("src/scs/mcp/") or path.startswith("src/scs/services/") for path in raw_files):
            commands.append({"command": "uv run pytest tests/integration/test_service_routes.py tests/integration/test_mcp_server.py -v", "reason": "Public gateway or MCP transport changed."})
        if any(path.startswith("crates/") for path in raw_files):
            commands.append({"command": "cargo test --workspace", "reason": "Native graph implementation changed."})
        if any(path.startswith("src/scs/indexing/") for path in raw_files):
            commands.append({"command": "uv run pytest tests/integration/indexing tests/unit/test_indexing_runner.py -v", "reason": "Indexing behavior changed."})
        if not commands:
            commands.append({"command": "uv run pytest", "reason": "Default SCS regression suite."})
        return {"commands": commands}
