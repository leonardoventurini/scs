from __future__ import annotations

import re
import tempfile
from pathlib import Path

from hypothesis import given, settings, strategies as st

from scs.graph.models import Edge, NodeType, RelationshipType
from scs.indexing.parser.base import ParsedEdge, ParsedEntity
from scs.indexing.pipeline import IngestionPipeline


class Parser:
    def supported_extensions(self):
        return frozenset({".py"})

    def parse(self, source: str, file_path: str):
        module = file_path.removesuffix(".py")
        entities = [ParsedEntity(NodeType.FILE, file_path, module, 0, 0, raw_text=source)]
        edges = []
        for name in re.findall(r"def ([a-z]+)", source):
            qualified = f"{module}.{name}"
            entities.append(ParsedEntity(NodeType.FUNCTION, name, qualified, 0, 0, raw_text=name))
            edges.append(ParsedEdge(module, qualified, RelationshipType.CONTAINS))
        return entities, edges


class Graph:
    def __init__(self):
        self.repos = {}
        self.nodes = {}
        self.edges = []
        self.hashes = {}

    def get_or_create_repo_sync(self, path):
        return self.repos.setdefault(path, 1)

    def resolve_repo_id_sync(self, path):
        return self.repos.get(path)

    def get_ingestion_stats_sync(self):
        return {path: {} for path in self.repos}

    def get_all_ingested_files_sync(self, path):
        return dict(self.hashes.get(path, {}))

    def get_file_paths_for_repo_sync(self, path):
        return sorted(self.hashes.get(path, {}))

    def get_node_ids_for_file_sync(self, path, rel):
        return [key for key, node in self.nodes.items() if node["metadata"]["file_path"] == rel]

    def batch_upsert_nodes_sync(self, nodes):
        self.nodes.update({node["id"]: node for node in nodes})
        return len(nodes)

    def batch_upsert_edges_sync(self, edges):
        identities = {
            (edge["source_id"], edge["target_id"], edge["relationship"]): edge
            for edge in self.edges
        }
        identities.update(
            {
                (edge["source_id"], edge["target_id"], edge["relationship"]): edge
                for edge in edges
            }
        )
        self.edges = list(identities.values())
        return len(edges)

    def get_edges_sync(self, node_id, *, direction="both"):
        return [
            Edge.model_validate({"id": "fake", **edge})
            for edge in self.edges
            if direction == "incoming" and edge["target_id"] == node_id
        ]

    def batch_upsert_embeddings_sync(self, values):
        return len(values)

    def flush_vector_index_sync(self):
        return True

    def reopened_vectors_contain_sync(self, node_ids):
        return True

    def reopened_vectors_absent_sync(self, node_ids):
        return True

    def delete_nodes_sync(self, node_ids: list[str]) -> int:
        return sum(self.delete_node_sync(node_id) for node_id in node_ids)

    def delete_node_sync(self, key):
        deleted = self.nodes.pop(key, None) is not None
        self.edges = [
            edge
            for edge in self.edges
            if edge["source_id"] != key and edge["target_id"] != key
        ]
        return deleted

    def resolve_node_id_by_qualified_name_sync(self, path, qualified_name):
        for key, node in self.nodes.items():
            if node["metadata"].get("qualified_name") == qualified_name:
                return key
        return None

    def remove_file_graph_and_vector_sync(self, path, rel):
        for key in self.get_node_ids_for_file_sync(path, rel):
            self.nodes.pop(key)
        self.edges = [edge for edge in self.edges if edge["source_id"] in self.nodes and edge["target_id"] in self.nodes]
        return 1

    def delete_ingestion_records_batch_sync(self, path, rel_paths):
        for rel in rel_paths:
            self.hashes.setdefault(path, {}).pop(rel, None)
        return len(rel_paths)

    def acknowledge_ingested_files_batch_sync(self, records):
        for record in records:
            self.hashes.setdefault(record["repo_path"], {})[record["rel_path"]] = record["content_hash"]

    def delete_ingested_file_sync(self, path, rel):
        self.hashes.setdefault(path, {}).pop(rel, None)
        return self.remove_file_graph_and_vector_sync(path, rel)

    def delete_ingestion_record_sync(self, path, rel):
        return self.hashes.setdefault(path, {}).pop(rel, None) is not None

    def upsert_ingested_file_sync(self, **kwargs):
        self.hashes.setdefault(kwargs["repo_path"], {})[kwargs["rel_path"]] = kwargs["content_hash"]


identifier = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8).filter(str.isidentifier)


@settings(max_examples=30, deadline=None)
@given(initial=st.lists(identifier, unique=True, max_size=8), final=st.lists(identifier, unique=True, max_size=8))
def test_full_and_incremental_ingestion_converge(initial: list[str], final: list[str]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary) / "repo"
        (repo / ".git").mkdir(parents=True)
        source = repo / "main.py"
        source.write_text("\n".join(f"def {name}(): pass" for name in initial))
        incremental = Graph()
        IngestionPipeline(graph=incremental, parser=Parser()).ingest(repo)

        source.write_text("\n".join(f"def {name}(): pass" for name in final))
        IngestionPipeline(graph=incremental, parser=Parser()).ingest_files(repo, [source])
        full = Graph()
        IngestionPipeline(graph=full, parser=Parser()).ingest(repo)

        assert incremental.nodes == full.nodes
        assert incremental.edges == full.edges
        assert incremental.hashes == full.hashes
