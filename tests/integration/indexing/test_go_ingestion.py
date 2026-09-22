"""End-to-end Go ingestion through the native parser and durable graph."""

from __future__ import annotations

from pathlib import Path

from scs.graph.models import NodeType
from scs.graph.native import NativeGraph
from scs.indexing.parser.native import NativeParser
from scs.indexing.pipeline import IngestionPipeline
from scs.providers.base import ProviderMetadata


def test_go_repository_is_durably_ingested_and_searchable(
    repository: Path, tmp_path: Path
) -> None:
    first = repository / "internal" / "model" / "entity.go"
    first.parent.mkdir(parents=True)
    first.write_text(
        """package model

type Entity struct { ID string }

func New() Entity { return Entity{} }
""",
        encoding="utf-8",
    )
    second = first.with_name("service_test.go")
    second.write_text(
        """package model

func Save(entity Entity) { New() }
""",
        encoding="utf-8",
    )
    provider = ProviderMetadata("test", "disabled", 2, False, "offline")
    graph = NativeGraph(
        database_path=tmp_path / "graph.db",
        vector_path=tmp_path / "vectors.usearch",
        provider_metadata_path=tmp_path / "provider.json",
        provider=provider,
    )

    result = IngestionPipeline(graph=graph, parser=NativeParser()).ingest(repository)

    assert result.files_discovered == 2
    assert result.files_failed == 0
    assert graph.get_all_ingested_files_sync(str(repository.resolve())).keys() == {
        "internal/model/entity.go",
        "internal/model/service_test.go",
    }
    modules = graph.list_nodes_sync(node_type=NodeType.MODULE)
    assert {node.metadata["qualified_name"] for node in modules} == {
        "internal.model.model"
    }
    functions = graph.search_by_name_sync("Save", node_type=NodeType.FUNCTION)
    assert [node.metadata["qualified_name"] for node in functions] == [
        "internal.model.model.Save"
    ]

    save = functions[0]
    outgoing = graph.get_edges_sync(save.id, direction="outgoing")
    target_names = {
        node.metadata["qualified_name"]
        for edge in outgoing
        if (node := graph.get_node_sync(edge.target_id)) is not None
    }
    assert "internal.model.model.New" in target_names
