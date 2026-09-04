"""Generation-safe daemon identity publication and restart contracts."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from scs.config import SCSSettings
from scs.identity import IdentityPublisher
from scs.main import SCSDaemon


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
    assert json.loads(record.read_text(encoding="utf-8"))["generation"] == "new-generation"
    assert new.remove_owned() is True


@pytest.mark.asyncio
async def test_daemon_restart_replaces_only_daemon_identity(tmp_path: Path) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="scs-identity-", dir="/tmp"))
    settings = SCSSettings(
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
