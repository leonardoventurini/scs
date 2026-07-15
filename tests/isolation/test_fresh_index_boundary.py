"""SCS starts empty and never derives state from legacy External product storage."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import cast

import pytest

from scs.config import SCSSettings
from scs.main import SCSDaemon
from scs.wire.client import SCSClient


def _fingerprint(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


@pytest.mark.asyncio
async def test_daemon_starts_empty_and_indexes_only_after_explicit_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "legacy-external-product"
    legacy.mkdir()
    sentinels = tuple(legacy / name for name in ("brain.db", "brain.db-wal", "brain.usearch"))
    for index, sentinel in enumerate(sentinels):
        sentinel.write_bytes(f"legacy-{index}".encode())
    before = {sentinel: _fingerprint(sentinel) for sentinel in sentinels}
    monkeypatch.setenv("EXTERNAL_PRODUCT_HOME", str(legacy))

    scs_home = tmp_path / "fresh-scs"
    runtime = Path(tempfile.mkdtemp(prefix="scs-test-", dir="/tmp"))
    settings = SCSSettings(
        home=scs_home,
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
        model_cache=tmp_path / "models",
    )
    daemon = SCSDaemon(settings)
    await daemon.start()
    try:
        client = SCSClient(runtime / "scs.sock")
        initial = await client.call("jobs.recent")
        assert initial == {"jobs": []}

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

    assert {sentinel: _fingerprint(sentinel) for sentinel in sentinels} == before
    assert (scs_home / "index.db").exists()
