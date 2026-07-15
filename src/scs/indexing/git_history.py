"""Read-only Git provenance ingestion for commit and contributor nodes."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from scs.graph.models import NodeType, RelationshipType

GIT_TIMEOUT_SECONDS = 60
FIELD_SEPARATOR = "\x1f"
RECORD_SEPARATOR = "\x1e"


class ProvenanceGraph(Protocol):
    """Batch operations required for source-control provenance."""

    def get_or_create_repo_sync(self, path: str) -> int: ...
    def batch_upsert_nodes_sync(self, nodes: list[dict[str, object]]) -> int: ...
    def batch_upsert_edges_sync(self, edges: list[dict[str, object]]) -> int: ...


@dataclass(frozen=True, slots=True)
class GitIngestionResult:
    """Counts persisted by one Git history scan."""

    commits_created: int
    contributors_created: int
    edges_created: int


def _identity(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()[:32]


class GitHistoryIngester:
    """Read Git history through subprocesses without mutating the repository."""

    def __init__(self, graph: ProvenanceGraph) -> None:
        self._graph = graph

    def ingest(self, repo_path: Path, *, limit: int = 1_000) -> GitIngestionResult:
        """Persist commits, contributors, authorship, and modified-file provenance."""

        root = repo_path.expanduser().resolve()
        repo_id = self._graph.get_or_create_repo_sync(str(root))
        format_value = f"%H{FIELD_SEPARATOR}%an{FIELD_SEPARATOR}%ae{FIELD_SEPARATOR}%aI{FIELD_SEPARATOR}%s{RECORD_SEPARATOR}"
        process = subprocess.run(
            ["git", "log", f"--max-count={limit}", f"--format={format_value}", "--name-only"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=True,
        )
        nodes: list[dict[str, object]] = []
        edges: list[dict[str, object]] = []
        contributors: set[str] = set()
        for record in process.stdout.split(RECORD_SEPARATOR):
            header, _, paths_text = record.strip().partition("\n")
            fields = header.split(FIELD_SEPARATOR)
            if len(fields) != 5:
                continue
            sha, author, email, authored_at, subject = fields
            commit_id = _identity("commit", sha)
            contributor_id = _identity("contributor", email.lower())
            nodes.append(
                {
                    "id": commit_id,
                    "type": NodeType.COMMIT.value,
                    "name": sha[:12],
                    "content": subject,
                    "metadata": {"sha": sha, "authored_at": authored_at},
                    "repo_id": repo_id,
                }
            )
            if contributor_id not in contributors:
                contributors.add(contributor_id)
                nodes.append(
                    {
                        "id": contributor_id,
                        "type": NodeType.CONTRIBUTOR.value,
                        "name": author,
                        "content": "",
                        "metadata": {"email": email},
                        "repo_id": repo_id,
                    }
                )
            edges.append(
                {
                    "source_id": commit_id,
                    "target_id": contributor_id,
                    "relationship": RelationshipType.AUTHORED_BY.value,
                    "weight": 1.0,
                }
            )
            for rel_path in filter(None, paths_text.splitlines()):
                file_id = _identity("file", f"{root}:{rel_path}")
                edges.append(
                    {
                        "source_id": commit_id,
                        "target_id": file_id,
                        "relationship": RelationshipType.MODIFIES.value,
                        "weight": 1.0,
                    }
                )
        return GitIngestionResult(
            commits_created=self._graph.batch_upsert_nodes_sync(nodes) if nodes else 0,
            contributors_created=len(contributors),
            edges_created=self._graph.batch_upsert_edges_sync(edges) if edges else 0,
        )
