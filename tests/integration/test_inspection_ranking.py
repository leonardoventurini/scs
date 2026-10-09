"""Capped inspection spends its evidence budget on declarations before imports."""

from pathlib import Path
from typing import cast

import pytest

from scs.graph.native import NativeGraph
from scs.indexing.jobs import IngestionJobStore
from scs.providers.base import EmbeddingProvider, ProviderMetadata
from scs.services.routes import SCSServiceRoutes


@pytest.mark.asyncio
async def test_inspection_ranks_declarations_before_opaque_ids(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    graph = NativeGraph(
        database_path=tmp_path / "graph.db",
        vector_path=tmp_path / "vectors.usearch",
        provider_metadata_path=tmp_path / "provider.json",
        provider=ProviderMetadata("test", "structural", 2, False, "not used"),
    )
    repo_id = graph.get_or_create_repo_sync(str(repo))
    nodes = [
        {
            "id": f"{prefix}-{index:03d}", "type": kind,
            "name": f"symbol_{index}", "repo_id": repo_id,
            "metadata": {"file_path": "sample.py", "start_line": index},
        }
        for prefix, kind, count in [("a", "import", 150), ("z", "function", 12)]
        for index in range(count)
    ]
    graph.batch_upsert_nodes_sync(list(reversed(nodes)))
    jobs = IngestionJobStore(tmp_path / "jobs.db")
    routes = SCSServiceRoutes(
        graph=lambda: graph, jobs=lambda: jobs,
        embeddings=lambda: cast(EmbeddingProvider, object()),
    )
    params = {"repo_path": str(repo), "file_path": "sample.py", "node_limit": 5}

    first = await routes.inspect_file(params)
    second = await routes.inspect_file(params)

    assert [node["id"] for node in first["nodes"]] == [f"z-{index:03d}" for index in range(5)]
    assert first == second
    assert first["nodes_truncated"] is True
    assert first["edges_truncated"] is False

    full = await routes.inspect_file({**params, "node_limit": len(nodes)})

    assert {node["id"] for node in full["nodes"]} == {node["id"] for node in nodes}
    assert full["nodes_truncated"] is False
