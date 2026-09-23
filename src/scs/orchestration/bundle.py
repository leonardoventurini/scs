"""Pinned, explicitly installed Laya ONNX bundle."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast
from urllib.request import urlopen

MODEL_REPOSITORY = "receptron/laya-onnx"
MODEL_REVISION = "68f27dfe5a27a54fb2b1fefc432f43f972e90868"
MODEL_LICENSE = "Apache-2.0"
MODEL_DIGEST = "487746363a8da57bcadb4345352997d22a0fb90d70aa22c6856668d023242aba"
DOWNLOAD_BASE = (
    f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}"
)


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    """One immutable file in the pinned ONNX bundle."""

    path: str
    size: int
    sha256: str


MODEL_ARTIFACTS = (
    ModelArtifact(
        "laya.onnx",
        3_807_291,
        "a874eb254b58b0fcb1e7ad56fbb188c29d64e08c9a46b689433e1f52c66dba1e",
    ),
    ModelArtifact("laya.onnx.data", 1_685_258_240, MODEL_DIGEST),
    ModelArtifact(
        "laya_config.json",
        369,
        "5049005dc6ae3ca5e82cc7d85c421357d5c543817300c8e8c5281ddbc69bb561",
    ),
    ModelArtifact(
        "tokenizer/tokenizer.json",
        3_583_228,
        "6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30",
    ),
    ModelArtifact(
        "tokenizer/tokenizer_config.json",
        308,
        "50044de60daaa73df97d262e15a40d4faf0160e7d742df64b377877a1320dd12",
    ),
)


def verify_artifact(path: Path, artifact: ModelArtifact) -> bool:
    """Validate the expected size and SHA-256 without loading weights in memory."""

    if not path.is_file() or path.stat().st_size != artifact.size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest() == artifact.sha256


def verify_bundle(directory: Path) -> bool:
    """Return whether every pinned file matches its published digest."""

    return all(
        verify_artifact(directory / artifact.path, artifact)
        for artifact in MODEL_ARTIFACTS
    )


def install_bundle(cache: Path) -> Path:
    """Explicitly download and atomically publish one verified local bundle."""

    destination = cache.expanduser().resolve() / "laya" / MODEL_REVISION
    if verify_bundle(destination):
        return destination
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".laya-", dir=destination.parent))
    try:
        for artifact in MODEL_ARTIFACTS:
            target = staging / artifact.path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with cast(BinaryIO, urlopen(f"{DOWNLOAD_BASE}/{artifact.path}", timeout=60)) as source:
                with target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1_048_576)
            if not verify_artifact(target, artifact):
                raise ValueError(f"model checksum mismatch: {artifact.path}")
        (staging / "manifest.json").write_text(
            json.dumps(
                {
                    "repository": MODEL_REPOSITORY,
                    "revision": MODEL_REVISION,
                    "license": MODEL_LICENSE,
                    "sha256": {item.path: item.sha256 for item in MODEL_ARTIFACTS},
                },
                sort_keys=True,
            )
        )
        if destination.exists():
            shutil.rmtree(destination)
        staging.rename(destination)
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)
