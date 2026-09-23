"""Pinned bundle verification rejects altered or incomplete artifacts."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from scs.orchestration import bundle
from scs.orchestration.bundle import ModelArtifact, verify_artifact


def test_verify_artifact_checks_bytes_and_size(tmp_path: Path) -> None:
    content = bytes(range(64))
    path = tmp_path / "model.safetensors"
    path.write_bytes(content)
    artifact = ModelArtifact("model.safetensors", len(content), hashlib.sha256(content).hexdigest())

    assert verify_artifact(path, artifact)
    path.write_bytes(content[:-1] + b"x")
    assert not verify_artifact(path, artifact)


def test_installer_publishes_only_verified_local_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contents = {
        "model.safetensors": bytes(range(32)),
        "tokenizer/tokenizer.json": b"{}",
    }
    artifacts = tuple(
        ModelArtifact(name, len(data), hashlib.sha256(data).hexdigest())
        for name, data in contents.items()
    )
    requested: list[str] = []

    def download(url: str, *, timeout: float) -> io.BytesIO:
        assert timeout > 0
        requested.append(url)
        return io.BytesIO(contents[url.removeprefix(bundle.DOWNLOAD_BASE + "/")])

    monkeypatch.setattr(bundle, "MODEL_ARTIFACTS", artifacts)
    monkeypatch.setattr(bundle, "urlopen", download)

    destination = bundle.install_bundle(tmp_path)

    assert destination == tmp_path / "laya-mlx" / bundle.MODEL_REVISION
    assert bundle.verify_bundle(destination)
    assert len(requested) == len(artifacts)
    assert bundle.install_bundle(tmp_path) == destination
    assert len(requested) == len(artifacts)
