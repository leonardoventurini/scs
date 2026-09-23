"""Pinned, explicitly installed native MLX Laya bundle."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast
from urllib.request import urlopen

MODEL_REPOSITORY = "aac6fef/laya-mlx"
MODEL_REVISION = "20aed815fc6acde75733882e7ec0e3f28aeb9717"
MODEL_CACHE_NAMESPACE = "laya-mlx"
MODEL_LICENSE = "Apache-2.0"
MODEL_DIGEST = "b9c07bf14be2fa5c78a9193a3e6d840ac80e89e62fc40f425834c3d8a6eaa3de"
DOWNLOAD_BASE = (
    f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}"
)


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    """One immutable file in the pinned MLX bundle."""

    path: str
    size: int
    sha256: str


MODEL_ARTIFACTS = (
    ModelArtifact("LICENSE", 10_173, "a6cba85bc92e0cff7a450b1d873c0eaa2e9fc96bf472df0247a26bec77bf3ff9"),
    ModelArtifact("NOTICE", 698, "f23ae8ae701f5a43e186182e387aeef00b666daed18a946fc7c9ea827b00a8fa"),
    ModelArtifact("encoder/config.json", 2_083, "bf3ab80598fdccf414855a2ce80f22859e4492d06ca8a62ddd1cfb63972f8979"),
    ModelArtifact("manifest.json", 1_746, "d8e254b51322fc0462a3cb384bde4d1b449baec9a716cbbc0d0aa99c0cb216b6"),
    ModelArtifact("mlx_config.json", 290, "c022368f128dc9b7f6478a9163c96e0406f1da6a7c6bae660ea8515b016ed7ef"),
    ModelArtifact("model.safetensors", 842_609_225, MODEL_DIGEST),
    ModelArtifact("rl_agent_config.json", 746, "d96dc2cb39d6375e030ff48c9957088f3c52668f45c30c56504f4e801ed3ee62"),
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
    ModelArtifact("validation.json", 18_257, "adc9a4baac1430b58cd8fdaf7c7587b380d4a91834ba405bbdeeabd9d35b5771"),
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

    destination = cache.expanduser().resolve() / MODEL_CACHE_NAMESPACE / MODEL_REVISION
    if verify_bundle(destination):
        return destination
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".laya-mlx-", dir=destination.parent))
    try:
        for artifact in MODEL_ARTIFACTS:
            target = staging / artifact.path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with cast(BinaryIO, urlopen(f"{DOWNLOAD_BASE}/{artifact.path}", timeout=60)) as source:
                with target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1_048_576)
            if not verify_artifact(target, artifact):
                raise ValueError(f"model checksum mismatch: {artifact.path}")
        (staging / "scs-install.json").write_text(
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
