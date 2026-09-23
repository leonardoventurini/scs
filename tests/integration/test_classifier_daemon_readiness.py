"""A configured daemon is ready only after its classifier is ready."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Iterator

import pytest

from scs.config import SCSSettings
from scs.main import SCSDaemon
from scs.orchestration.laya_provider import LayaDecisionProvider
from scs.service import ProcessLock
from scs.wire.client import SCSClient


@pytest.fixture
def configured_settings(tmp_path: Path) -> Iterator[SCSSettings]:
    with tempfile.TemporaryDirectory(prefix="scs-classifier-", dir="/tmp") as runtime:
        yield SCSSettings(
            home=tmp_path / "home",
            model_cache=tmp_path / "models",
            runtime_dir=Path(runtime),
            log_dir=tmp_path / "logs",
            embedding_dimension=2,
            decision_model="laya",
        )


@pytest.mark.asyncio
async def test_daemon_does_not_publish_ready_socket_before_classifier(
    configured_settings: SCSSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowProvider(LayaDecisionProvider):
        def __init__(
            self,
            model_path: Path,
            *,
            max_concurrency: int = 1,
            on_unavailable: Callable[[], None] | None = None,
        ) -> None:
            super().__init__(
                model_path,
                max_concurrency=max_concurrency,
                on_unavailable=on_unavailable,
            )
            self.ready = False

        async def start(self) -> None:
            entered.set()
            await release.wait()
            self.ready = True

        @property
        def is_ready(self) -> bool:
            return self.ready

    monkeypatch.setattr("scs.main.LayaDecisionProvider", SlowProvider)
    settings = configured_settings
    daemon = SCSDaemon(settings)
    started = asyncio.create_task(daemon.start())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert started.done() is False
        assert not (settings.paths.runtime / "scs.sock").exists()

        release.set()
        await asyncio.wait_for(started, timeout=2)
        health = await SCSClient(settings.paths.runtime / "scs.sock").call(
            "system.health"
        )
        assert health["ready"] is True

        assert isinstance(daemon._decision_provider, SlowProvider)
        daemon._decision_provider.ready = False
        health = await SCSClient(settings.paths.runtime / "scs.sock").call(
            "system.health"
        )
        assert health["ready"] is False
    finally:
        release.set()
        if started.done() and started.exception() is None:
            await daemon.stop()


@pytest.mark.asyncio
async def test_failed_classifier_start_releases_daemon_ownership(
    configured_settings: SCSSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingProvider(LayaDecisionProvider):
        closed = False

        async def start(self) -> None:
            raise RuntimeError("classifier warmup failed")

        async def close(self) -> None:
            self.closed = True
            await super().close()

    monkeypatch.setattr("scs.main.LayaDecisionProvider", FailingProvider)
    settings = configured_settings
    daemon = SCSDaemon(settings)

    with pytest.raises(RuntimeError, match="classifier warmup failed"):
        await daemon.start()

    assert not (settings.paths.runtime / "scs.sock").exists()
    assert not (settings.paths.runtime / "daemon-service.json").exists()
    assert isinstance(daemon._decision_provider, FailingProvider)
    assert daemon._decision_provider.closed is True
    lock = ProcessLock(settings.paths.home / ".daemon.lock")
    lock.acquire()
    lock.release()
