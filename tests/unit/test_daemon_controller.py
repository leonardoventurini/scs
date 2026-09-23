"""Cross-platform lazy daemon controller behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scs.config import SCSSettings
from scs.daemon import DaemonController, DaemonStatus


def _settings(tmp_path: Path) -> SCSSettings:
    return SCSSettings(
        home=tmp_path / "home",
        runtime_dir=tmp_path / "runtime",
        log_dir=tmp_path / "logs",
        model_cache=tmp_path / "models",
        embedding_dimension=2,
    )


@pytest.mark.asyncio
async def test_status_combines_health_with_generation_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    settings.paths.ensure()
    (settings.paths.runtime / "daemon-service.json").write_text(
        json.dumps({"pid": 321}), encoding="utf-8"
    )

    class HealthyClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def call(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {"ready": True, "generation": "generation-1", "version": "1.2.3"}

    monkeypatch.setattr("scs.daemon.SCSClient", HealthyClient)

    assert await DaemonController(settings).status() == DaemonStatus(
        available=True,
        ready=True,
        pid=321,
        generation="generation-1",
        version="1.2.3",
    )


@pytest.mark.asyncio
async def test_ensure_started_serializes_spawn_and_waits_for_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = DaemonController(_settings(tmp_path))
    states = iter(
        [
            DaemonStatus(False, False),
            DaemonStatus(False, False),
            DaemonStatus(False, False),
            DaemonStatus(True, True, pid=42, generation="winner"),
        ]
    )
    spawns: list[bool] = []

    async def status(_self: DaemonController) -> DaemonStatus:
        return next(states)

    monkeypatch.setattr(DaemonController, "status", status)
    monkeypatch.setattr(DaemonController, "_spawn", lambda _self: spawns.append(True))
    monkeypatch.setattr("scs.daemon.DAEMON_POLL_SECONDS", 0.0)

    result = await controller.ensure_started()

    assert result.generation == "winner"
    assert spawns == [True]


@pytest.mark.asyncio
async def test_ensure_started_waits_for_recovering_daemon_without_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = DaemonController(_settings(tmp_path))
    states = iter(
        [
            DaemonStatus(True, False, pid=42, generation="recovering"),
            DaemonStatus(True, False, pid=42, generation="recovering"),
            DaemonStatus(True, True, pid=42, generation="recovering"),
        ]
    )

    async def status(_self: DaemonController) -> DaemonStatus:
        return next(states)

    monkeypatch.setattr(DaemonController, "status", status)
    monkeypatch.setattr(
        DaemonController,
        "_spawn",
        lambda _self: pytest.fail("a recovering daemon must not be replaced"),
    )
    monkeypatch.setattr("scs.daemon.DAEMON_POLL_SECONDS", 0.0)

    result = await controller.ensure_started()
    assert result.generation == "recovering"


@pytest.mark.asyncio
async def test_ensure_started_reports_failed_child_before_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = DaemonController(_settings(tmp_path))

    async def absent(_self: DaemonController) -> DaemonStatus:
        return DaemonStatus(False, False)

    class ExitedProcess:
        def poll(self) -> int:
            return 2

    monkeypatch.setattr(DaemonController, "status", absent)
    monkeypatch.setattr(DaemonController, "_spawn", lambda _self: ExitedProcess())

    with pytest.raises(RuntimeError, match="exited before readiness"):
        await controller.ensure_started()


@pytest.mark.asyncio
async def test_stop_is_idempotent_when_daemon_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = DaemonController(_settings(tmp_path))

    async def absent(_self: DaemonController) -> DaemonStatus:
        return DaemonStatus(False, False, error="FileNotFoundError")

    monkeypatch.setattr(DaemonController, "status", absent)

    assert await controller.stop() is False


@pytest.mark.asyncio
async def test_stop_waits_for_writer_lock_after_socket_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = DaemonController(_settings(tmp_path))
    states = iter(
        [
            DaemonStatus(True, True, pid=321, generation="generation-1"),
            DaemonStatus(False, False, error="FileNotFoundError"),
        ]
    )
    lock_states = iter([False, False, True])
    calls: list[tuple[str, dict[str, object] | None]] = []

    async def status(_self: DaemonController) -> DaemonStatus:
        return next(states)

    class Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def call(
            self, method: str, params: dict[str, object] | None = None
        ) -> dict[str, object]:
            calls.append((method, params))
            return {"accepted": True}

    monkeypatch.setattr(DaemonController, "status", status)
    monkeypatch.setattr("scs.daemon.SCSClient", Client)
    monkeypatch.setattr(controller, "_writer_lock_available", lambda: next(lock_states))
    monkeypatch.setattr("scs.daemon.DAEMON_POLL_SECONDS", 0.0)

    assert await controller.stop(cancel_active=True) is True
    assert calls == [
        (
            "system.shutdown",
            {"generation": "generation-1", "cancel_active": True},
        )
    ]


@pytest.mark.asyncio
async def test_stop_rejects_unreachable_unidentified_lock_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = DaemonController(_settings(tmp_path))

    async def absent(_self: DaemonController) -> DaemonStatus:
        return DaemonStatus(False, False, error="FileNotFoundError")

    monkeypatch.setattr(DaemonController, "status", absent)
    monkeypatch.setattr(controller, "_writer_lock_available", lambda: False)

    with pytest.raises(RuntimeError, match="unreachable daemon retains"):
        await controller.stop(cancel_active=True)
