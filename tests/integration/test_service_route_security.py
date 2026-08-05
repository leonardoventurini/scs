from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from scs.config import SCSSettings
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
        settings=SCSSettings(
            home=tmp_path / "home",
            model_cache=tmp_path / "models",
            runtime_dir=tmp_path / "runtime",
            log_dir=tmp_path / "logs",
        ),
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
