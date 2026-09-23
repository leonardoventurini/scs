"""Generation-safe daemon identity publication and restart contracts."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import cast

import pytest

from scs.config import SCSSettings
from scs.identity import IdentityPublisher
from scs.indexing.runner import IngestionJobRunner
from scs.main import SCSDaemon
from scs.service import ProcessLock


def test_identity_cleanup_preserves_newer_generation(tmp_path: Path) -> None:
    record = tmp_path / "daemon-service.json"
    old = IdentityPublisher(
        record,
        service="scs-daemon",
        generation="old-generation",
        artifact_path=Path(__file__),
    )
    new = IdentityPublisher(
        record,
        service="scs-daemon",
        generation="new-generation",
        artifact_path=Path(__file__),
    )
    old.publish()
    new.publish()

    assert old.remove_owned() is False
    assert (
        json.loads(record.read_text(encoding="utf-8"))["generation"] == "new-generation"
    )
    assert new.remove_owned() is True


@pytest.mark.asyncio
async def test_daemon_restart_replaces_only_daemon_identity(tmp_path: Path) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="scs-identity-", dir="/tmp"))
    settings = SCSSettings(
        decision_model="disabled",
        home=tmp_path / "home",
        model_cache=tmp_path / "models",
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
        embedding_dimension=2,
    )
    daemon_record = runtime / "daemon-service.json"

    try:
        first = SCSDaemon(settings)
        await first.start()
        first_identity = json.loads(daemon_record.read_text(encoding="utf-8"))
        assert set(first_identity) == {
            "service",
            "pid",
            "start_time",
            "generation",
            "artifact_sha256",
            "protocol_min",
            "protocol_max",
        }
        await first.stop()
        assert not daemon_record.exists()
        second = SCSDaemon(settings)
        await second.start()
        try:
            second_identity = json.loads(daemon_record.read_text(encoding="utf-8"))
            assert second_identity["generation"] != first_identity["generation"]
        finally:
            await second.stop()
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


@pytest.mark.asyncio
async def test_daemon_retains_identity_and_writer_lock_until_runner_stops(
    tmp_path: Path,
) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="scs-identity-stop-", dir="/tmp"))
    settings = SCSSettings(
        decision_model="disabled",
        home=tmp_path / "home",
        model_cache=tmp_path / "models",
        runtime_dir=runtime,
        log_dir=tmp_path / "logs",
        embedding_dimension=2,
    )
    daemon = SCSDaemon(settings)
    await daemon.start()
    daemon_record = runtime / "daemon-service.json"
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingRunner:
        async def stop(self) -> None:
            entered.set()
            await release.wait()

    daemon._runner = cast(IngestionJobRunner, BlockingRunner())
    stop_task = asyncio.create_task(daemon.stop())
    await asyncio.wait_for(entered.wait(), timeout=1)
    contender = ProcessLock(settings.paths.home / ".daemon.lock")

    try:
        assert daemon_record.exists()
        with pytest.raises(RuntimeError, match="already running"):
            contender.acquire()
    finally:
        release.set()
        await asyncio.wait_for(stop_task, timeout=2)

    assert not daemon_record.exists()
    contender.acquire()
    contender.release()
    shutil.rmtree(runtime, ignore_errors=True)
