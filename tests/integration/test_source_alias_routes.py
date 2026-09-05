"""Native indexed alias identities survive service-layer source lookup."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

from scs.graph.native import NativeGraph
from scs.indexing.jobs import IngestionJobStore
from scs.indexing.parser.native import NativeParser
from scs.indexing.pipeline import IngestionPipeline
from scs.providers.base import EmbeddingProvider, ProviderMetadata
from scs.services.routes import SCSServiceRoutes


@pytest.mark.asyncio
async def test_indexed_aliases_remain_distinct_in_inspection_risk_and_references(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.py"
    source.write_text("def synthetic_symbol():\n    return 1\n", encoding="utf-8")
    alias = repo / "alias.py"
    alias.symlink_to(source.name)
    graph = NativeGraph(
        database_path=tmp_path / "graph.db",
        vector_path=tmp_path / "vectors.usearch",
        provider_metadata_path=tmp_path / "provider.json",
        provider=ProviderMetadata("test", "structural", 2, False, "not used"),
    )
    pipeline = IngestionPipeline(graph=graph, parser=NativeParser())
    await asyncio.to_thread(pipeline.ingest, repo)
    jobs = IngestionJobStore(tmp_path / "jobs.db")
    routes = SCSServiceRoutes(
        graph=lambda: graph,
        jobs=lambda: jobs,
        embeddings=lambda: cast(EmbeddingProvider, object()),
    )
    alias_ids = set(graph.get_node_ids_for_file_sync(str(repo), alias.name))
    source_ids = set(graph.get_node_ids_for_file_sync(str(repo), source.name))
    assert alias_ids and source_ids and alias_ids.isdisjoint(source_ids)
    inspected = await routes.inspect_file(
        {"repo_path": str(repo), "file_path": alias.name}
    )
    assert {node["id"] for node in inspected["nodes"]} == alias_ids
    risk = await routes.composite_regression_risk(
        {"repo_path": str(repo), "file_paths": [str(alias)]}
    )
    assert set(risk["affected_node_ids"]) == alias_ids
    references = await routes.lsp_references({"file_path": str(alias), "line": 0})
    assert references["available"] is True
    assert references["symbol"]["id"] in alias_ids
