from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from scs.indexing.git_history import GitHistoryIngester


class Graph:
    def __init__(self) -> None:
        self.edges: list[dict[str, object]] = []

    def get_or_create_repo_sync(self, path: str) -> int:
        return 7

    def get_file_node_map_sync(self, repo_id: int) -> dict[str, str]:
        assert repo_id == 7
        return {"tracked.py": "parser-file-node"}

    def batch_upsert_nodes_sync(self, nodes: list[dict[str, object]]) -> int:
        return len(nodes)

    def batch_upsert_edges_sync(self, edges: list[dict[str, object]]) -> int:
        self.edges = edges
        return len(edges)


def test_modifies_edges_target_parser_created_file_nodes(tmp_path: Path) -> None:
    output = "\x1eabc123\x1fAda\x1fada@example.com\x1f2026-01-01T00:00:00Z\x1fchange\ntracked.py\nunindexed.py\n"
    graph = Graph()
    completed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout=output, stderr="")

    with patch("scs.indexing.git_history.subprocess.run", return_value=completed):
        GitHistoryIngester(graph).ingest(tmp_path)

    modifies = [edge for edge in graph.edges if edge["relationship"] == "modifies"]
    assert [edge["target_id"] for edge in modifies] == ["parser-file-node"]
