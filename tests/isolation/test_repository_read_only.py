"""SCS intelligence operations never mutate repository source files."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path

import pytest

from scs.config import SCSSettings
from scs.main import SCSDaemon
from scs.providers.base import ProviderMetadata
from scs.wire.client import SCSClient


class _ImmediateEmbeddings:
    """Keep source-read-only verification independent from a shared OMLX queue."""

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata("test", "immediate", 2)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0] for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        del text
        return [0.0, 1.0]


def _fingerprint(path: Path) -> tuple[int, int, int, int, str]:
    metadata = path.stat()
    return (
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


async def _wait_for_completion(client: SCSClient, repo_path: str) -> None:
    for _attempt in range(200):
        jobs = (await client.call("jobs.recent", {"repo_path": repo_path}))["jobs"]
        if jobs and jobs[0]["status"] == "completed":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("indexing job did not complete")


@pytest.mark.asyncio
async def test_index_search_inspection_and_lsp_preserve_source_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scs.main.OpenAICompatibleEmbeddingProvider",
        lambda **_kwargs: _ImmediateEmbeddings(),
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "module.py"
    source.write_text("def stable_source():\n    return 42\n", encoding="utf-8")
    source.chmod(0o444)
    before = _fingerprint(source)
    runtime = Path(tempfile.mkdtemp(prefix="scs-read-only-", dir="/tmp"))
    settings = SCSSettings(
        home=tmp_path / "home",
        model_cache=tmp_path / "models",
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
    )
    daemon = SCSDaemon(settings)
    await daemon.start()
    try:
        client = SCSClient(runtime / "scs.sock")
        repo_path = str(repository.resolve())
        await client.call("repository.index", {"repo_path": repo_path})
        await _wait_for_completion(client, repo_path)
        await client.call(
            "knowledge.search", {"query": "stable_source", "repo_path": repo_path}
        )
        await client.call(
            "knowledge.inspect_file", {"repo_path": repo_path, "file_path": "module.py"}
        )
        await client.call("lsp.references", {"file_path": str(source), "line": 0})
    finally:
        await daemon.stop()

    assert _fingerprint(source) == before
