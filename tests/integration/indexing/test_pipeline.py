from __future__ import annotations

import inspect
from pathlib import Path

from scs.indexing.pipeline import IngestionPipeline

from conftest import FakeEmbeddings, FakeGraph, FakeParser


def test_failed_parse_does_not_record_hash(repository: Path) -> None:
    source = repository / "broken.py"
    source.write_text("PARSE_ERROR\n")
    graph = FakeGraph()

    result = IngestionPipeline(graph=graph, parser=FakeParser()).ingest(repository)

    assert result.files_failed == 1
    assert graph.get_all_ingested_files_sync(str(repository.resolve())) == {}


def test_incremental_change_removes_stale_nodes(repository: Path) -> None:
    source = repository / "main.py"
    source.write_text("def old():\n    pass\n")
    graph = FakeGraph()
    pipeline = IngestionPipeline(graph=graph, parser=FakeParser())
    pipeline.ingest(repository)
    assert any(node["name"] == "old" for node in graph.nodes.values())

    source.write_text("def new():\n    pass\n")
    result = pipeline.ingest_files(repository, [source])

    assert result.files_changed == 1
    assert not any(node["name"] == "old" for node in graph.nodes.values())
    assert any(node["name"] == "new" for node in graph.nodes.values())


def test_deleted_file_cascades_nodes_edges_and_vectors(repository: Path) -> None:
    source = repository / "main.py"
    source.write_text("def run():\n    pass\n")
    graph = FakeGraph()
    pipeline = IngestionPipeline(graph=graph, parser=FakeParser(), embeddings=FakeEmbeddings())
    pipeline.ingest(repository)
    source.unlink()

    result = pipeline.ingest(repository)

    assert result.files_deleted == 1
    assert graph.nodes == {}
    assert graph.edges == []
    assert graph.embeddings == {}


def test_vectors_flush_before_hash_commit(repository: Path) -> None:
    source = repository / "main.py"
    source.write_text("def run():\n    pass\n")
    graph = FakeGraph()

    IngestionPipeline(graph=graph, parser=FakeParser(), embeddings=FakeEmbeddings()).ingest(repository)

    assert graph.flushes == 1
    assert graph.hashes[str(repository.resolve())]["main.py"]


def test_embeddings_consume_only_parser_owned_entity_text(repository: Path) -> None:
    """Retired remote enrichment cannot alter the durable vector input."""

    source = repository / "main.py"
    source.write_text("def run():\n    pass\n")
    graph = FakeGraph()
    embeddings = FakeEmbeddings()

    IngestionPipeline(
        graph=graph,
        parser=FakeParser(),
        embeddings=embeddings,
    ).ingest(repository)

    assert "summarizer" not in inspect.signature(IngestionPipeline).parameters
    assert embeddings.document_inputs == [
        "file: main",
        "function main.run ",
    ]
