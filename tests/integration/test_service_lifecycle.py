"""Independent launchd lifecycle and process-lock contracts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from scs.service import ProcessLock, ServiceManager, SubprocessRunner


class RecordingRunner:
    """Capture launchctl commands without mutating the host service domain."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.loaded: set[str] = set()

    def run(self, command: tuple[str, ...], *, check: bool = True) -> int:
        self.commands.append(command)
        if command[1] == "print":
            return 0 if command[-1].split("/")[-1] in self.loaded else 1
        if command[1] == "bootstrap":
            self.loaded.add(Path(command[-1]).stem)
        if command[1] == "bootout":
            self.loaded.discard(command[-1].split("/")[-1])
        return 0


def test_install_start_stop_and_uninstall_preserve_scs_home(tmp_path: Path) -> None:
    home = tmp_path / "data"
    home.mkdir()
    sentinel = home / "keep.db"
    sentinel.write_text("preserve", encoding="utf-8")
    launch_agents = tmp_path / "LaunchAgents"
    runner = RecordingRunner()
    manager = ServiceManager(
        launch_agents_dir=launch_agents,
        executable=Path("/opt/scs/bin/scs"),
        log_dir=tmp_path / "logs",
        runner=runner,
        user_id=501,
    )

    manager.install()
    proxy_plist = launch_agents / "com.mentagen.scs.proxy.plist"
    daemon_plist = launch_agents / "com.mentagen.scs.daemon.plist"
    assert proxy_plist.exists()
    assert daemon_plist.exists()
    assert "SCS_HOME" not in daemon_plist.read_text(encoding="utf-8")

    manager.start()
    manager.start()
    manager.stop()
    manager.uninstall()

    bootstraps = [command for command in runner.commands if command[1] == "bootstrap"]
    kickstarts = [command for command in runner.commands if command[1] == "kickstart"]
    assert bootstraps[0][0:3] == ("launchctl", "bootstrap", "gui/501")
    assert "com.mentagen.scs.proxy" in str(bootstraps[0])
    assert "com.mentagen.scs.daemon" in str(bootstraps[1])
    assert len(kickstarts) == 2
    assert not proxy_plist.exists()
    assert not daemon_plist.exists()
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_duplicate_process_lock_is_refused(tmp_path: Path) -> None:
    lock_path = tmp_path / "scs.lock"
    first = ProcessLock(lock_path)
    second = ProcessLock(lock_path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_unloaded_service_probe_suppresses_launchctl_noise() -> None:
    with patch("scs.service.subprocess.run") as run:
        run.return_value.returncode = 1

        assert SubprocessRunner().run(
            ("launchctl", "print", "gui/501/com.mentagen.scs.daemon"),
            check=False,
        ) == 1

    assert run.call_args.kwargs["stdout"] is not None
    assert run.call_args.kwargs["stderr"] is not None
