from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import scs.indexing.pipeline as pipeline_module
from scs.graph.models import NodeType, RelationshipType
from scs.indexing.parser.base import ParsedEdge, ParsedEntity
from scs.indexing.pipeline import IngestionPipeline
from scs.providers.base import ProviderMetadata, ProviderUnavailableError

from conftest import FakeEmbeddings, FakeGraph, FakeParser


class UnavailableEmbeddings:
    """Simulate an optional provider outage after structural persistence begins."""

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata("test", "unavailable", 2, False, "offline")

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        del texts
        raise ProviderUnavailableError("synthetic OMLX outage")

    async def embed_query(self, text: str) -> list[float]:
        del text
        raise ProviderUnavailableError("synthetic OMLX outage")


class FailsSecondBatchEmbeddings(FakeEmbeddings):
    """Fail exactly once after an earlier batch has been acknowledged."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.fail_second_batch = True

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail_second_batch and self.calls == 2:
            raise ProviderUnavailableError("synthetic second-batch outage")
        return await super().embed_documents(texts)


class MutatesSourceEmbeddings(FakeEmbeddings):
    """Change a source after parsing to exercise the pre-acknowledgement guard."""

    def __init__(self, source: Path) -> None:
        super().__init__()
        self._source = source

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = await super().embed_documents(texts)
        self._source.write_text("def changed_during_embedding():\n    pass\n")
        return vectors


class CrossFileCallParser:
    """Create a caller→callee edge whose files are intentionally split."""

    def supported_extensions(self) -> frozenset[str]:
        return frozenset({".py"})

    def parse(
        self, source: str, file_path: str
    ) -> tuple[list[ParsedEntity], list[ParsedEdge]]:
        del source
        module = file_path.removesuffix(".py")
        qualified = f"{module}.{module}"
        entities = [
            ParsedEntity(NodeType.FILE, file_path, module, 0, 1, raw_text=file_path),
            ParsedEntity(
                NodeType.FUNCTION, module, qualified, 1, 1, raw_text=qualified
            ),
        ]
        edges = [ParsedEdge(module, qualified, RelationshipType.CONTAINS)]
        if module == "caller":
            edges.append(ParsedEdge(qualified, "callee.callee", RelationshipType.CALLS))
        return entities, edges


class CrossBatchRetryParser:
    """Make the first batch own a relationship to the second batch's node."""

    def supported_extensions(self) -> frozenset[str]:
        return frozenset({".py"})

    def parse(
        self, source: str, file_path: str
    ) -> tuple[list[ParsedEntity], list[ParsedEdge]]:
        del source
        module = file_path.removesuffix(".py")
        qualified = f"{module}.{module}"
        entities = [
            ParsedEntity(NodeType.FILE, file_path, module, 0, 1, raw_text=file_path),
            ParsedEntity(
                NodeType.FUNCTION, module, qualified, 1, 1, raw_text=qualified
            ),
        ]
        edges = [ParsedEdge(module, qualified, RelationshipType.CONTAINS)]
        if module == "a_source":
            edges.append(
                ParsedEdge(qualified, "b_target.b_target", RelationshipType.CALLS)
            )
        return entities, edges


def test_failed_parse_does_not_record_hash(repository: Path) -> None:
    source = repository / "broken.py"
    source.write_text("PARSE_ERROR\n")
    graph = FakeGraph()

    result = IngestionPipeline(graph=graph, parser=FakeParser()).ingest(repository)

    assert result.files_failed == 1
    assert graph.get_all_ingested_files_sync(str(repository.resolve())) == {}


def test_plain_text_fallback_creates_one_searchable_file_node(repository: Path) -> None:
    dockerfile = repository / "Dockerfile"
    dockerfile.write_text("FROM python:3.13\nRUN useradd app\n", encoding="utf-8")
    graph = FakeGraph()

    result = IngestionPipeline(graph=graph, parser=FakeParser()).ingest(repository)

    nodes = [
        node
        for node in graph.nodes.values()
        if node["metadata"].get("file_path") == "Dockerfile"
    ]
    assert result.files_failed == 0
    assert len(nodes) == 1
    assert nodes[0]["type"] == NodeType.FILE.value
    assert nodes[0]["name"] == "Dockerfile"
    assert nodes[0]["content"] == "FROM python:3.13\nRUN useradd app\n"
    assert nodes[0]["metadata"]["language"] == "text"


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
    pipeline = IngestionPipeline(
        graph=graph, parser=FakeParser(), embeddings=FakeEmbeddings()
    )
    pipeline.ingest(repository)
    source.unlink()

    result = pipeline.ingest(repository)

    assert result.files_deleted == 1
    assert graph.nodes == {}
    assert graph.edges == []
    assert graph.embeddings == {}


def test_deletion_reopen_failure_preserves_ingestion_checkpoint(
    repository: Path,
) -> None:
    """A deleted hash survives until a fresh sidecar handle confirms removal."""

    source = repository / "main.py"
    source.write_text("def run():\n    pass\n")
    graph = FakeGraph()
    pipeline = IngestionPipeline(
        graph=graph,
        parser=FakeParser(),
        embeddings=FakeEmbeddings(),
    )
    pipeline.ingest(repository)
    source.unlink()
    graph.fail_at = "reopen_present"

    with pytest.raises(RuntimeError, match="retains a deleted file"):
        pipeline.ingest(repository)

    assert graph.hashes[str(repository.resolve())]["main.py"]


def test_vectors_flush_before_hash_commit(repository: Path) -> None:
    source = repository / "main.py"
    source.write_text("def run():\n    pass\n")
    graph = FakeGraph()

    IngestionPipeline(
        graph=graph, parser=FakeParser(), embeddings=FakeEmbeddings()
    ).ingest(repository)

    assert graph.flushes == 1
    assert graph.hashes[str(repository.resolve())]["main.py"]


def test_missing_vector_after_reopen_does_not_acknowledge_hash(
    repository: Path,
) -> None:
    """A fresh sidecar handle is the semantic durability oracle."""

    source = repository / "main.py"
    source.write_text("def run():\n    pass\n")
    graph = FakeGraph(fail_at="reopen_missing")

    result = IngestionPipeline(
        graph=graph,
        parser=FakeParser(),
        embeddings=FakeEmbeddings(),
    ).ingest(repository)

    assert result.semantic_degraded_reason == (
        "Reopened vector sidecar is missing an acknowledged batch vector"
    )
    assert graph.hashes.get(str(repository.resolve()), {}) == {}


def test_embedding_outage_preserves_structure_but_does_not_acknowledge_hash(
    repository: Path,
) -> None:
    source = repository / "main.py"
    source.write_text("def run():\n    pass\n")
    graph = FakeGraph()

    result = IngestionPipeline(
        graph=graph,
        parser=FakeParser(),
        embeddings=UnavailableEmbeddings(),
    ).ingest(repository)

    assert result.semantic_degraded_reason == "synthetic OMLX outage"
    assert graph.nodes
    assert graph.edges
    assert graph.embeddings == {}
    assert graph.hashes.get(str(repository.resolve()), {}) == {}


def test_second_batch_failure_retries_only_unacknowledged_files(
    repository: Path, monkeypatch
) -> None:
    """Given batch two fails, retrying must not call OMLX for batch one again."""

    for name in ("a.py", "b.py"):
        (repository / name).write_text("def run():\n    pass\n")
    monkeypatch.setattr(pipeline_module, "INGESTION_BATCH_MAX_FILES", 1)
    graph = FakeGraph()
    embeddings = FailsSecondBatchEmbeddings()
    pipeline = IngestionPipeline(
        graph=graph, parser=FakeParser(), embeddings=embeddings
    )

    first = pipeline.ingest(repository)

    assert first.files_failed == 1
    assert sorted(graph.hashes[str(repository.resolve())]) == ["a.py"]
    first_call_inputs = list(embeddings.document_inputs)
    embeddings.fail_second_batch = False
    retry = pipeline.ingest(repository)

    assert retry.files_changed == 1
    assert sorted(graph.hashes[str(repository.resolve())]) == ["a.py", "b.py"]
    assert embeddings.document_inputs[len(first_call_inputs) :] == [
        "file: b",
        "function b.run ",
    ]


def test_source_change_before_acknowledgement_leaves_batch_unacknowledged(
    repository: Path,
) -> None:
    """A source race must not acknowledge hashes for bytes that were not parsed."""

    source = repository / "main.py"
    source.write_text("def run():\n    pass\n")
    graph = FakeGraph()

    result = IngestionPipeline(
        graph=graph,
        parser=FakeParser(),
        embeddings=MutatesSourceEmbeddings(source),
    ).ingest(repository)

    assert result.files_failed == 1
    assert result.semantic_degraded_reason == (
        "Source changed while its embedding batch was in progress"
    )
    assert graph.hashes.get(str(repository.resolve()), {}) == {}


def test_force_snapshot_rejects_source_hash_drift_before_mutation(
    repository: Path,
) -> None:
    """A reclaimed force attempt cannot claim a newer edit as its old target."""

    source = repository / "main.py"
    source.write_text("def original():\n    pass\n")
    graph = FakeGraph()
    pipeline = IngestionPipeline(
        graph=graph, parser=FakeParser(), embeddings=FakeEmbeddings()
    )
    snapshot = pipeline.create_force_full_snapshot(repository)
    source.write_text("def newer():\n    pass\n")

    with pytest.raises(RuntimeError, match="Force-full snapshot source changed"):
        pipeline.ingest(repository, force_snapshot=snapshot)

    assert graph.nodes == {}
    assert graph.hashes.get(str(repository.resolve()), {}) == {}


def test_cross_batch_call_edge_survives_complete_file_embedding_batches(
    repository: Path, monkeypatch
) -> None:
    """Plan all structure before batching so a caller and callee stay connected."""

    (repository / "caller.py").write_text("caller")
    (repository / "callee.py").write_text("callee")
    monkeypatch.setattr(pipeline_module, "INGESTION_BATCH_MAX_FILES", 1)
    graph = FakeGraph()

    IngestionPipeline(
        graph=graph,
        parser=CrossFileCallParser(),
        embeddings=FakeEmbeddings(),
    ).ingest(repository)

    names_by_id = {node_id: str(node["name"]) for node_id, node in graph.nodes.items()}
    assert any(
        edge["relationship"] == RelationshipType.CALLS.value
        and names_by_id[str(edge["source_id"])] == "caller"
        and names_by_id[str(edge["target_id"])] == "callee"
        for edge in graph.edges
    )


def test_partial_batch_retry_preserves_edges_owned_by_acknowledged_files(
    repository: Path, monkeypatch
) -> None:
    """An acknowledged caller must retain its edge when its target retries."""

    (repository / "a_source.py").write_text("source")
    (repository / "b_target.py").write_text("target")
    monkeypatch.setattr(pipeline_module, "INGESTION_BATCH_MAX_FILES", 1)
    graph = FakeGraph()
    embeddings = FailsSecondBatchEmbeddings()
    pipeline = IngestionPipeline(
        graph=graph,
        parser=CrossBatchRetryParser(),
        embeddings=embeddings,
    )

    first = pipeline.ingest(repository)
    assert first.files_failed == 1
    embeddings.fail_second_batch = False
    pipeline.ingest(repository)

    names_by_id = {node_id: str(node["name"]) for node_id, node in graph.nodes.items()}
    assert any(
        edge["relationship"] == RelationshipType.CALLS.value
        and names_by_id[str(edge["source_id"])] == "a_source"
        and names_by_id[str(edge["target_id"])] == "b_target"
        for edge in graph.edges
    )


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
