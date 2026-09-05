"""Repeated parser identities retain each source occurrence and its embedding."""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import override

import pytest

from scs.graph.models import Node, NodeType
from scs.graph.native import NativeGraph
from scs.indexing.parser.native import NativeParser
from scs.indexing.pipeline import IngestionPipeline
from scs.providers.base import ProviderMetadata

from occurrence_fixtures import CollisionCategory, collision_fixture, repeated_source


class OccurrenceEmbeddings:
    """Distinguish provider outputs even when import embedding texts coincide."""

    def __init__(self) -> None:
        self.document_inputs: list[str] = []

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata("test", "occurrences", 2)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        offset = len(self.document_inputs)
        self.document_inputs.extend(texts)
        return [[float(offset + index + 1), 1.0] for index in range(len(texts))]

    async def embed_query(self, text: str) -> list[float]:
        return [float(len(text)), 1.0]


class RecordingNativeGraph(NativeGraph):
    """Capture vector associations while exercising authoritative native writes."""

    embedding_pairs: list[tuple[str, list[float]]]
    deletion_batches: tuple[tuple[str, ...], ...] = ()

    @override
    def delete_nodes_sync(self, node_ids: list[str]) -> int:
        self.deletion_batches += (tuple(node_ids),)
        return super().delete_nodes_sync(node_ids)

    @override
    def batch_upsert_embeddings_sync(
        self, embeddings: list[tuple[str, list[float]]]
    ) -> int:
        self.embedding_pairs = embeddings
        return super().batch_upsert_embeddings_sync(embeddings)


def occurrence_ids(nodes: list[Node], kind: NodeType) -> list[str]:
    occurrences = sorted(
        (node for node in nodes if node.type == kind),
        key=lambda node: int(str(node.metadata["start_line"])),
    )
    return [node.id for node in occurrences]


@pytest.mark.parametrize(
    ("extension", "kind"), [("ts", NodeType.IMPORT), ("css", NodeType.VARIABLE)]
)
def test_native_occurrences_preserve_vectors_identity_and_incremental_cleanup(
    repository: Path,
    tmp_path: Path,
    extension: str,
    kind: NodeType,
) -> None:
    count = 4
    source = repository / f"repeated.{extension}"
    original = repeated_source(extension, count)
    source.write_text(original, encoding="utf-8")
    parser = NativeParser()
    entities, _ = parser.parse(original, source.name)
    repeated = [entity for entity in entities if entity.kind == kind]
    assert len(repeated) == count
    assert len({entity.qualified_name for entity in repeated}) == 1
    embeddings = OccurrenceEmbeddings()
    graph = RecordingNativeGraph(
        database_path=tmp_path / "graph.db",
        vector_path=tmp_path / "vectors.usearch",
        provider_metadata_path=tmp_path / "provider.json",
        provider=embeddings.metadata,
    )
    pipeline = IngestionPipeline(graph=graph, parser=parser, embeddings=embeddings)

    result = pipeline.ingest(repository)

    assert result.files_failed == 0
    assert result.entities_created == result.embeddings_created == len(entities)
    nodes = graph.list_nodes_sync()
    assert len(nodes) == len(entities)
    assert embeddings.document_inputs == [entity.embed_text() for entity in entities]
    assert len({node_id for node_id, _ in graph.embedding_pairs}) == len(entities)
    for entity, (node_id, vector) in zip(entities, graph.embedding_pairs, strict=True):
        node = graph.get_node_sync(node_id)
        assert node is not None
        assert node.content == entity.raw_text
        assert node.metadata["start_line"] == entity.start_line
        assert embeddings.document_inputs[int(vector[0]) - 1] == entity.embed_text()
    ids = occurrence_ids(nodes, kind)
    assert graph.reopened_vectors_contain_sync([node.id for node in nodes])
    identities = Counter((entity.kind, entity.qualified_name) for entity in entities)
    for entity in entities:
        if (
            identities[(entity.kind, entity.qualified_name)] == 1
            or entity == repeated[0]
        ):
            legacy_identity = (
                f"{repository.resolve()}:{source.name}:{entity.kind.value}:"
                f"{entity.qualified_name}"
            )
            legacy_id = hashlib.sha256(legacy_identity.encode()).hexdigest()[:32]
            assert graph.get_node_sync(legacy_id) is not None

    assert graph.deletion_batches == ()
    forced = pipeline.ingest(repository, force=True)
    assert graph.deletion_batches == (tuple(sorted(node.id for node in nodes)),)
    assert forced.files_failed == 0
    assert occurrence_ids(graph.list_nodes_sync(), kind) == ids

    source.write_text("\n\n" + original, encoding="utf-8")
    shifted = pipeline.ingest_files(repository, [source])
    assert shifted.files_changed == 1 and shifted.files_failed == 0
    assert occurrence_ids(graph.list_nodes_sync(), kind) == ids

    source.write_text(repeated_source(extension, count - 1), encoding="utf-8")
    removed = pipeline.ingest_files(repository, [source])
    assert removed.files_changed == 1 and removed.files_failed == 0
    assert occurrence_ids(graph.list_nodes_sync(), kind) == ids[:-1]
    assert graph.get_node_sync(ids[-1]) is None
    assert graph.reopened_vectors_absent_sync([ids[-1]])
    assert graph.reopened_vectors_contain_sync(ids[:-1])


@pytest.mark.parametrize(
    "same_line", [False, True], ids=["multiple-lines", "same-line"]
)
@pytest.mark.parametrize(
    "category", ["type_value_imports", "selector_properties", "cross_kind"]
)
def test_anonymized_source_collision_patterns_keep_embedding_associations(
    repository: Path,
    tmp_path: Path,
    category: CollisionCategory,
    same_line: bool,
) -> None:
    fixture = collision_fixture(category, same_line=same_line)
    source = repository / fixture.path
    source.write_text(fixture.source, encoding="utf-8")
    parser = NativeParser()
    entities, _ = parser.parse(fixture.source, fixture.path)
    shared = [
        entity for entity in entities if entity.qualified_name == fixture.shared_name
    ]
    assert len(shared) == fixture.occurrences
    if same_line:
        assert len({entity.start_line for entity in shared}) == 1
    if category == "cross_kind":
        assert len({entity.kind for entity in shared}) == fixture.occurrences
    embeddings = OccurrenceEmbeddings()
    graph = RecordingNativeGraph(
        database_path=tmp_path / "graph.db",
        vector_path=tmp_path / "vectors.usearch",
        provider_metadata_path=tmp_path / "provider.json",
        provider=embeddings.metadata,
    )

    result = IngestionPipeline(
        graph=graph,
        parser=parser,
        embeddings=embeddings,
    ).ingest(repository)

    assert result.files_failed == 0
    assert result.entities_created == result.embeddings_created == len(entities)
    assert len(graph.list_nodes_sync()) == len(entities)
    node_ids = [node_id for node_id, _ in graph.embedding_pairs]
    assert len(set(node_ids)) == len(entities)
    assert graph.reopened_vectors_contain_sync(node_ids)
    for entity, (node_id, vector) in zip(entities, graph.embedding_pairs, strict=True):
        node = graph.get_node_sync(node_id)
        assert node is not None
        assert node.type == entity.kind
        assert node.metadata["qualified_name"] == entity.qualified_name
        assert node.metadata["start_line"] == entity.start_line
        assert node.content == entity.raw_text
        assert embeddings.document_inputs[int(vector[0]) - 1] == entity.embed_text()
