from __future__ import annotations

import json
from pathlib import Path

import pytest

from scs.graph.native import NativeGraph
from scs.providers.base import ProviderMetadata


class FakeHandle:
    pass


class BatchNodeHandle:
    def __init__(self) -> None:
        self.requested_node_ids: list[str] | None = None

    def batch_get_nodes(self, node_ids: list[str]) -> str:
        self.requested_node_ids = node_ids
        return json.dumps(
            [
                {
                    "id": "node-2",
                    "type": "function",
                    "name": "second",
                    "content": "def second(): pass",
                    "metadata": {"file_path": "second.py"},
                    "repo_id": 7,
                },
                {
                    "id": "node-1",
                    "type": "class",
                    "name": "First",
                    "metadata": {"file_path": "first.py"},
                    "repo_id": 7,
                },
            ]
        )


def test_batch_get_nodes_validates_native_results(tmp_path: Path) -> None:
    handle = BatchNodeHandle()
    graph = NativeGraph(
        database_path=tmp_path / "index.db",
        vector_path=tmp_path / "index.usearch",
        provider_metadata_path=tmp_path / "provider.json",
        provider=ProviderMetadata(
            "disabled", "structural-only", 2, available=False
        ),
        native_handle=handle,
    )

    nodes = graph.batch_get_nodes_sync(["node-2", "missing", "node-1"])

    assert handle.requested_node_ids == ["node-2", "missing", "node-1"]
    assert [node.id for node in nodes] == ["node-2", "node-1"]
    assert nodes[0].metadata == {"file_path": "second.py"}
    assert nodes[1].content == ""


def test_ambiguous_vector_sidecar_is_quarantined(tmp_path: Path) -> None:
    vector = tmp_path / "index.usearch"
    vector.write_bytes(b"torn")

    graph = NativeGraph(
        database_path=tmp_path / "index.db",
        vector_path=vector,
        provider_metadata_path=tmp_path / "provider.json",
        provider=ProviderMetadata("fake", "v1", 2),
        native_handle=FakeHandle(),
    )

    assert not graph.vector_state.available
    assert graph.vector_state.quarantined_path is not None
    assert graph.vector_state.quarantined_path.read_bytes() == b"torn"
    assert not vector.exists()


@pytest.mark.parametrize(
    ("persisted", "active"),
    [
        (
            ProviderMetadata("openai-compatible", "v1", 2),
            ProviderMetadata("openai", "v1", 2),
        ),
        (ProviderMetadata("openai", "v1", 2), ProviderMetadata("openai", "v2", 2)),
        (ProviderMetadata("openai", "v1", 3), ProviderMetadata("openai", "v1", 2)),
    ],
)
def test_provider_identity_mismatch_quarantines_vectors(
    tmp_path: Path, persisted: ProviderMetadata, active: ProviderMetadata
) -> None:
    vector = tmp_path / "index.usearch"
    vector.write_bytes(b"vectors")
    metadata = tmp_path / "provider.json"
    metadata.write_text(json.dumps(persisted.to_dict()))

    graph = NativeGraph(
        database_path=tmp_path / "index.db",
        vector_path=vector,
        provider_metadata_path=metadata,
        provider=active,
        native_handle=FakeHandle(),
    )

    assert (
        graph.vector_state.reason
        == "vector provider metadata does not match active provider"
    )
    assert graph.vector_state.quarantined_path is not None
    assert not vector.exists()


def test_non_object_provider_metadata_does_not_mutate_vectors(tmp_path: Path) -> None:
    """Valid non-object JSON preserves the historical fail-without-quarantine rule."""

    vector = tmp_path / "index.usearch"
    vector.write_bytes(b"vectors")
    metadata = tmp_path / "provider.json"
    metadata.write_text("[]")

    with pytest.raises(AttributeError):
        NativeGraph(
            database_path=tmp_path / "index.db",
            vector_path=vector,
            provider_metadata_path=metadata,
            provider=ProviderMetadata("fake", "v1", 2),
            native_handle=FakeHandle(),
        )

    assert vector.read_bytes() == b"vectors"
