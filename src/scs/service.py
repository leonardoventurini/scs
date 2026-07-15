"""SCS-owned process locking and macOS user-service lifecycle."""

from __future__ import annotations

import fcntl
import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

DAEMON_LABEL = "com.mentagen.scs.daemon"
PROXY_LABEL = "com.mentagen.scs.proxy"
SERVICE_LABELS = (PROXY_LABEL, DAEMON_LABEL)
LAUNCHD_THROTTLE_SECONDS = 10


class CommandRunner(Protocol):
    """Run one launchctl command behind an injectable system boundary."""

    def run(self, command: tuple[str, ...], *, check: bool = True) -> int:
        """Return the process exit code or raise when required."""


class SubprocessRunner:
    """Production command runner that never invokes a shell."""

    def run(self, command: tuple[str, ...], *, check: bool = True) -> int:
        """Run a command with inherited output for transparent diagnostics."""

        completed = subprocess.run(command, check=check)
        return completed.returncode


class ProcessLock:
    """Root-scoped exclusive lock preventing duplicate SCS daemons."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        """Acquire the lock without waiting behind another daemon."""

        if self._file is not None:
            raise RuntimeError("SCS process lock is already acquired")
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_file = self._path.open("a+b")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock_file.close()
            raise RuntimeError("SCS daemon is already running for this storage root") from error
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"{os.getpid()}\n".encode())
        lock_file.flush()
        os.fsync(lock_file.fileno())
        self._file = lock_file

    def release(self) -> None:
        """Release this process's lock without deleting a shared lock inode."""

        lock_file = self._file
        self._file = None
        if lock_file is None:
            return
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    """Current launchd registration status for both SCS processes."""

    proxy_loaded: bool
    daemon_loaded: bool


class ServiceManager:
    """Install and operate the two SCS-owned user launchd agents."""

    def __init__(
        self,
        *,
        launch_agents_dir: Path | None = None,
        executable: Path | None = None,
        log_dir: Path | None = None,
        runner: CommandRunner | None = None,
        user_id: int | None = None,
    ) -> None:
        self._launch_agents_dir = launch_agents_dir or Path.home() / "Library" / "LaunchAgents"
        discovered_executable = shutil.which("scs") or sys.argv[0]
        self._executable = executable or Path(
            os.environ.get("SCS_EXECUTABLE", discovered_executable)
        ).resolve()
        self._log_dir = log_dir or Path.home() / "Library" / "Logs" / "SCS"
        self._runner = runner or SubprocessRunner()
        self._user_id = os.getuid() if user_id is None else user_id

    @property
    def domain(self) -> str:
        """Return the current user's launchd domain."""

        return f"gui/{self._user_id}"

    def install(self) -> None:
        """Atomically install proxy and daemon plists without starting them."""

        self._launch_agents_dir.mkdir(parents=True, exist_ok=True)
        self._write_plist(PROXY_LABEL, "proxy")
        self._write_plist(DAEMON_LABEL, "serve")

    def start(self) -> None:
        """Load or restart the proxy before the replaceable daemon."""

        for label in SERVICE_LABELS:
            plist_path = self._plist_path(label)
            if not plist_path.exists():
                raise RuntimeError(f"SCS service is not installed: {plist_path}")
            if self._is_loaded(label):
                self._runner.run(
                    ("launchctl", "kickstart", "-k", f"{self.domain}/{label}")
                )
            else:
                self._runner.run(("launchctl", "bootstrap", self.domain, str(plist_path)))

    def stop(self) -> None:
        """Unload daemon then proxy while preserving all persistent data."""

        for label in reversed(SERVICE_LABELS):
            self._runner.run(
                ("launchctl", "bootout", f"{self.domain}/{label}"),
                check=False,
            )

    def restart(self) -> None:
        """Restart both agents in their defined ownership order."""

        self.stop()
        self.start()

    def status(self) -> ServiceStatus:
        """Inspect launchd registration without changing service state."""

        states = {label: self._is_loaded(label) for label in SERVICE_LABELS}
        return ServiceStatus(
            proxy_loaded=states[PROXY_LABEL],
            daemon_loaded=states[DAEMON_LABEL],
        )

    def uninstall(self) -> None:
        """Unload and remove registrations while deliberately preserving SCS_HOME."""

        self.stop()
        for label in SERVICE_LABELS:
            self._plist_path(label).unlink(missing_ok=True)

    def _write_plist(self, label: str, command: str) -> None:
        plist_path = self._plist_path(label)
        log_directory = self._log_dir
        log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "Label": label,
            "ProgramArguments": [str(self._executable), command],
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": LAUNCHD_THROTTLE_SECONDS,
            "StandardOutPath": str(log_directory / f"{label}.log"),
            "StandardErrorPath": str(log_directory / f"{label}.error.log"),
        }
        body = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
        temporary_path = plist_path.with_suffix(".plist.tmp")
        with temporary_path.open("wb") as temporary:
            temporary.write(body)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(plist_path)
        directory_fd = os.open(plist_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _plist_path(self, label: str) -> Path:
        return self._launch_agents_dir / f"{label}.plist"

    def _is_loaded(self, label: str) -> bool:
        return (
            self._runner.run(
                ("launchctl", "print", f"{self.domain}/{label}"),
                check=False,
            )
            == 0
        )
