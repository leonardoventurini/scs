from __future__ import annotations

from pathlib import Path

import pytest

from scs.indexing.pipeline import IngestionPipeline

from indexing.conftest import FakeEmbeddings, FakeGraph, FakeParser


@pytest.mark.parametrize("boundary", ["nodes", "edges", "embeddings", "flush"])
def test_fault_before_completion_never_records_hash(tmp_path: Path, boundary: str) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "main.py").write_text("def run():\n    pass\n")
    graph = FakeGraph(fail_at=boundary)

    with pytest.raises(OSError, match=boundary):
        IngestionPipeline(graph=graph, parser=FakeParser(), embeddings=FakeEmbeddings()).ingest(repo)

    assert graph.hashes.get(str(repo.resolve()), {}) == {}


def test_retry_after_torn_boundary_converges(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "main.py").write_text("def run():\n    pass\n")
    graph = FakeGraph(fail_at="flush")
    pipeline = IngestionPipeline(graph=graph, parser=FakeParser(), embeddings=FakeEmbeddings())
    with pytest.raises(OSError):
        pipeline.ingest(repo)

    graph.fail_at = None
    result = pipeline.ingest(repo)

    assert result.files_changed == 1
    assert graph.hashes[str(repo.resolve())]["main.py"]
