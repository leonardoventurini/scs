"""Pinned bundle verification rejects altered or incomplete artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path

from scs.orchestration.bundle import ModelArtifact, verify_artifact


def test_verify_artifact_checks_bytes_and_size(tmp_path: Path) -> None:
    content = bytes(range(64))
    path = tmp_path / "model.onnx"
    path.write_bytes(content)
    artifact = ModelArtifact("model.onnx", len(content), hashlib.sha256(content).hexdigest())

    assert verify_artifact(path, artifact)
    path.write_bytes(content[:-1] + b"x")
    assert not verify_artifact(path, artifact)
