from __future__ import annotations

import json
from pathlib import Path

from scs.graph.native import NativeGraph
from scs.providers.base import ProviderMetadata


class FakeHandle:
    pass


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


def test_provider_dimension_mismatch_quarantines_vectors(tmp_path: Path) -> None:
    vector = tmp_path / "index.usearch"
    vector.write_bytes(b"vectors")
    metadata = tmp_path / "provider.json"
    metadata.write_text(json.dumps({"provider": "fake", "model": "v1", "dimension": 3}))

    graph = NativeGraph(
        database_path=tmp_path / "index.db",
        vector_path=vector,
        provider_metadata_path=metadata,
        provider=ProviderMetadata("fake", "v1", 2),
        native_handle=FakeHandle(),
    )

    assert graph.vector_state.reason == "vector provider metadata does not match active provider"
