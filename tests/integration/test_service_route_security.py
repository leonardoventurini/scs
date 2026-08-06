from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from scs.graph.native import NativeGraph
from scs.indexing.jobs import IngestionJobStore
from scs.providers.base import EmbeddingProvider
from scs.services.routes import SCSServiceRoutes


def build_routes(tmp_path: Path, jobs: IngestionJobStore) -> SCSServiceRoutes:
    """Build routes whose unused dependencies fail if validation reaches them."""

    def unused_graph() -> NativeGraph:
        return cast(NativeGraph, object())

    def unused_embeddings() -> EmbeddingProvider:
        return cast(EmbeddingProvider, object())

    return SCSServiceRoutes(
        graph=unused_graph,
        jobs=lambda: jobs,
        embeddings=unused_embeddings,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_changes",
    [
        {"file_paths": []},
        {"deleted_paths": ["../outside.py"]},
        {"deleted_paths": ["/absolute/outside.py"]},
    ],
)
async def test_ingest_files_rejects_invalid_changes_before_queueing(
    tmp_path: Path, invalid_changes: dict[str, object]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    jobs = IngestionJobStore(tmp_path / "jobs.db")
    routes = build_routes(tmp_path, jobs)

    with pytest.raises(ValueError):
        await routes.ingest_files({"repo_path": str(repo), **invalid_changes})

    assert jobs.list_recent() == []


@pytest.mark.asyncio
async def test_ingest_files_rejects_existing_source_outside_repository(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    jobs = IngestionJobStore(tmp_path / "jobs.db")
    routes = build_routes(tmp_path, jobs)

    with pytest.raises(ValueError, match="escapes repository"):
        await routes.ingest_files(
            {"repo_path": str(repo), "file_paths": [str(outside)]}
        )

    assert jobs.list_recent() == []


@pytest.mark.asyncio
async def test_ingest_files_enqueues_only_normalized_valid_changes(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "src" / "main.py"
    source.parent.mkdir()
    source.write_text("print('indexed')\n", encoding="utf-8")
    jobs = IngestionJobStore(tmp_path / "jobs.db")
    routes = build_routes(tmp_path, jobs)

    response = await routes.ingest_files(
        {
            "repo_path": str(repo),
            "file_paths": [str(source)],
            "deleted_paths": ["src/deleted.py"],
        }
    )

    assert response["accepted"] is True
    queued = jobs.list_recent()
    assert len(queued) == 1
    assert queued[0].repo_path == str(repo.resolve())
    assert queued[0].payload == {
        "file_paths": [str(source.resolve())],
        "deleted_paths": ["src/deleted.py"],
    }


@pytest.mark.asyncio
async def test_regression_risk_rejects_repository_escape_before_graph_reads(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    jobs = IngestionJobStore(tmp_path / "jobs.db")
    routes = build_routes(tmp_path, jobs)

    with pytest.raises(ValueError, match="escapes repository"):
        await routes.composite_regression_risk(
            {"repo_path": str(repo), "file_paths": [str(outside)]}
        )


@pytest.mark.asyncio
async def test_regression_risk_requires_repository_scope_before_graph_reads(
    tmp_path: Path,
) -> None:
    jobs = IngestionJobStore(tmp_path / "jobs.db")
    routes = build_routes(tmp_path, jobs)

    with pytest.raises(ValueError, match="repo_path must be a non-empty string"):
        await routes.composite_regression_risk({"file_paths": ["src/main.py"]})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selectors",
    [
        {},
        {"symbol_name": ""},
        {"node_id": ""},
        {"symbol_name": "target", "node_id": "node-1"},
    ],
)
async def test_related_requires_exactly_one_selector_before_graph_reads(
    tmp_path: Path, selectors: dict[str, object]
) -> None:
    jobs = IngestionJobStore(tmp_path / "jobs.db")
    routes = build_routes(tmp_path, jobs)

    with pytest.raises(ValueError, match="exactly one|non-empty"):
        await routes.related(selectors)


@pytest.mark.asyncio
async def test_related_validates_direction_before_a_missing_seed(
    tmp_path: Path,
) -> None:
    jobs = IngestionJobStore(tmp_path / "jobs.db")
    routes = build_routes(tmp_path, jobs)

    with pytest.raises(ValueError, match="direction must be"):
        await routes.related({"node_id": "missing", "direction": "sideways"})


@pytest.mark.asyncio
async def test_related_does_not_read_nodes_for_an_unindexed_repository(
    tmp_path: Path,
) -> None:
    class UnindexedGraph:
        def resolve_repo_id_sync(self, repo_path: str) -> None:
            del repo_path
            return None

        def get_node_sync(self, node_id: str) -> None:
            raise AssertionError(f"unexpected unscoped node read: {node_id}")

    repo = tmp_path / "unindexed"
    repo.mkdir()
    jobs = IngestionJobStore(tmp_path / "jobs.db")
    routes = SCSServiceRoutes(
        graph=lambda: cast(NativeGraph, UnindexedGraph()),
        jobs=lambda: jobs,
        embeddings=lambda: cast(EmbeddingProvider, object()),
    )

    result = await routes.related({"node_id": "unscoped-node", "repo_path": str(repo)})

    assert result["matches"] == []
    assert result["related"] == []


@pytest.mark.asyncio
async def test_symbol_listing_rejects_non_symbol_types_before_graph_reads(
    tmp_path: Path,
) -> None:
    jobs = IngestionJobStore(tmp_path / "jobs.db")
    routes = build_routes(tmp_path, jobs)

    with pytest.raises(ValueError, match="code symbol"):
        await routes.nodes_list({"node_type": "file"})
