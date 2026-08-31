from __future__ import annotations

import re
from pathlib import Path

import pytest

from scs.graph.models import Edge, NodeType, RelationshipType
from scs.indexing.parser.base import ParsedEdge, ParsedEntity


class FakeParser:
    def supported_extensions(self) -> frozenset[str]:
        return frozenset({".py"})

    def parse(self, source: str, file_path: str) -> tuple[list[ParsedEntity], list[ParsedEdge]]:
        if "PARSE_ERROR" in source:
            raise ValueError("synthetic parse failure")
        module = file_path.removesuffix(".py").replace("/", ".")
        entities = [ParsedEntity(NodeType.FILE, file_path, module, 0, max(0, source.count("\n")), raw_text=source)]
        edges: list[ParsedEdge] = []
        for line_number, name in enumerate(re.findall(r"^def\s+([A-Za-z_]\w*)", source, re.MULTILINE), start=1):
            qualified = f"{module}.{name}"
            entities.append(ParsedEntity(NodeType.FUNCTION, name, qualified, line_number, line_number, raw_text=f"def {name}"))
            edges.append(ParsedEdge(module, qualified, RelationshipType.CONTAINS))
        return entities, edges


class FakeGraph:
    def __init__(self, fail_at: str | None = None) -> None:
        self.repos: dict[str, int] = {}
        self.nodes: dict[str, dict[str, object]] = {}
        self.edges: list[dict[str, object]] = []
        self.embeddings: dict[str, list[float]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.fail_at = fail_at
        self.flushes = 0
        self.qualified_name_resolutions = 0

    def _fail(self, boundary: str) -> None:
        if self.fail_at == boundary:
            raise OSError(f"synthetic {boundary} persistence fault")

    def get_or_create_repo_sync(self, path: str) -> int:
        return self.repos.setdefault(path, len(self.repos) + 1)

    def resolve_repo_id_sync(self, path: str) -> int | None:
        return self.repos.get(path)

    def get_ingestion_stats_sync(self) -> dict[str, dict[str, object]]:
        return {path: {"file_count": len(self.hashes.get(path, {}))} for path in self.repos}

    def get_all_ingested_files_sync(self, repo_path: str) -> dict[str, str]:
        return dict(self.hashes.get(repo_path, {}))

    def get_file_paths_for_repo_sync(self, repo_path: str) -> list[str]:
        return sorted({str(node["metadata"]["file_path"]) for node in self.nodes.values() if node.get("repo_id") == self.repos.get(repo_path)})

    def get_node_ids_for_file_sync(self, repo_path: str, rel_path: str) -> list[str]:
        repo_id = self.repos.get(repo_path)
        return [node_id for node_id, node in self.nodes.items() if node.get("repo_id") == repo_id and node["metadata"].get("file_path") == rel_path]

    def resolve_node_id_by_qualified_name_sync(
        self, repo_path: str, qualified_name: str
    ) -> str | None:
        self.qualified_name_resolutions += 1
        repo_id = self.repos.get(repo_path)
        return next(
            (
                node_id
                for node_id, node in self.nodes.items()
                if node.get("repo_id") == repo_id
                and node["metadata"].get("qualified_name") == qualified_name
            ),
            None,
        )

    def batch_upsert_nodes_sync(self, nodes: list[dict[str, object]]) -> int:
        self._fail("nodes")
        for node in nodes:
            self.nodes[str(node["id"])] = node
        return len(nodes)

    def batch_upsert_edges_sync(self, edges: list[dict[str, object]]) -> int:
        self._fail("edges")
        by_identity = {
            (str(edge["source_id"]), str(edge["target_id"]), str(edge["relationship"])): edge
            for edge in self.edges
        }
        for edge in edges:
            by_identity[
                (str(edge["source_id"]), str(edge["target_id"]), str(edge["relationship"]))
            ] = edge
        self.edges = list(by_identity.values())
        return len(edges)

    def get_edges_sync(self, node_id: str, *, direction: str = "both") -> list[Edge]:
        return [
            Edge.model_validate({"id": "fake", **edge})
            for edge in self.edges
            if (direction == "both" and node_id in {edge["source_id"], edge["target_id"]})
            or (direction == "incoming" and edge["target_id"] == node_id)
            or (direction == "outgoing" and edge["source_id"] == node_id)
        ]

    def batch_upsert_embeddings_sync(self, embeddings: list[tuple[str, list[float]]]) -> int:
        self._fail("embeddings")
        self.embeddings.update(embeddings)
        return len(embeddings)

    def flush_vector_index_sync(self) -> bool:
        self._fail("flush")
        self.flushes += 1
        return True

    def reopened_vectors_contain_sync(self, node_ids: list[str]) -> bool:
        self._fail("reopen")
        if self.fail_at == "reopen_missing":
            return False
        return all(node_id in self.embeddings for node_id in node_ids)

    def reopened_vectors_absent_sync(self, node_ids: list[str]) -> bool:
        self._fail("reopen")
        if self.fail_at == "reopen_present":
            return False
        return all(node_id not in self.embeddings for node_id in node_ids)

    def delete_node_sync(self, node_id: str) -> bool:
        deleted = self.nodes.pop(node_id, None) is not None
        self.embeddings.pop(node_id, None)
        self.edges = [
            edge
            for edge in self.edges
            if edge["source_id"] != node_id and edge["target_id"] != node_id
        ]
        return deleted

    def delete_ingested_file_sync(self, repo_path: str, rel_path: str) -> int:
        self.hashes.setdefault(repo_path, {}).pop(rel_path, None)
        return self.remove_file_graph_and_vector_sync(repo_path, rel_path)

    def remove_file_graph_and_vector_sync(self, repo_path: str, rel_path: str) -> int:
        node_ids = self.get_node_ids_for_file_sync(repo_path, rel_path)
        for node_id in node_ids:
            self.nodes.pop(node_id, None)
            self.embeddings.pop(node_id, None)
        self.edges = [edge for edge in self.edges if edge["source_id"] not in node_ids and edge["target_id"] not in node_ids]
        return len(node_ids)

    def delete_ingestion_records_batch_sync(
        self, repo_path: str, rel_paths: list[str]
    ) -> int:
        records = self.hashes.setdefault(repo_path, {})
        for rel_path in rel_paths:
            records.pop(rel_path, None)
        return len(rel_paths)

    def acknowledge_ingested_files_batch_sync(self, files: list[dict[str, object]]) -> None:
        self._fail("hash")
        committed = [
            (str(file["repo_path"]), str(file["rel_path"]), str(file["content_hash"]))
            for file in files
        ]
        for repo_path, rel_path, content_hash in committed:
            self.hashes.setdefault(repo_path, {})[rel_path] = content_hash

    def delete_repo_sync(self, repo_path: str) -> object:
        for rel_path in list(self.hashes.get(repo_path, {})):
            self.delete_ingested_file_sync(repo_path, rel_path)
        return {"deleted": True}


class FakeEmbeddings:
    def __init__(self) -> None:
        self.document_inputs: list[str] = []

    @property
    def metadata(self):
        from scs.providers.base import ProviderMetadata
        return ProviderMetadata("fake", "deterministic", 2)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_inputs.extend(texts)
        return [[float(len(text)), 1.0] for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return [float(len(text)), 1.0]


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo
