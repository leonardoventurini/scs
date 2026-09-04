"""SCS starts empty and indexes only after an explicit request."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import cast

import pytest

from scs.config import SCSSettings
from scs.main import SCSDaemon
from scs.providers.base import ProviderMetadata
from scs.wire.client import SCSClient


class _ImmediateEmbeddings:
    """Keep the storage-isolation test independent from a shared OMLX queue."""

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata("test", "immediate", 2)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0] for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        del text
        return [0.0, 1.0]


@pytest.mark.asyncio
async def test_daemon_starts_empty_and_indexes_only_after_explicit_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scs.main.OpenAICompatibleEmbeddingProvider",
        lambda **_kwargs: _ImmediateEmbeddings(),
    )
    scs_home = tmp_path / "fresh-scs"
    runtime = Path(tempfile.mkdtemp(prefix="scs-test-", dir="/tmp"))
    settings = SCSSettings(
        home=scs_home,
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
        model_cache=tmp_path / "models",
    )
    daemon = SCSDaemon(settings)
    try:
        await daemon.start()
        client = SCSClient(runtime / "scs.sock")
        initial = await client.call("jobs.recent")
        assert initial == {"jobs": []}
        stats = await client.call("knowledge.stats")
        assert stats["total_nodes"] == 0
        assert stats["ingestion_stats"] == {}

        await daemon.stop()
        daemon = SCSDaemon(settings)
        await daemon.start()
        client = SCSClient(runtime / "scs.sock")
        assert await client.call("jobs.recent") == {"jobs": []}
        assert (await client.call("knowledge.stats"))["total_nodes"] == 0

        repository = tmp_path / "repository"
        repository.mkdir()
        (repository / "main.py").write_text("value = 1\n", encoding="utf-8")
        acknowledgement = await client.call(
            "repository.index",
            {"repo_path": str(repository)},
        )
        assert acknowledgement["accepted"] is True
        recent: list[dict[str, object]] = []
        for _attempt in range(100):
            recent = cast(
                list[dict[str, object]],
                (await client.call("jobs.recent"))["jobs"],
            )
            if recent and recent[0]["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        assert recent[0]["status"] == "completed"
        statuses = await client.call(
            "repositories.status",
            {"repo_paths": [str(repository)]},
        )
        assert statuses["repositories"][0]["state"] == "indexed"
        assert statuses["repositories"][0]["file_count"] == 1
    finally:
        await daemon.stop()
        shutil.rmtree(runtime, ignore_errors=True)

    assert (scs_home / "catalog.db").exists()
    assert len(list((scs_home / "projects").glob("*/generations/*/index.db"))) == 1
