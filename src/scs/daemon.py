"""Race-safe lazy lifecycle for the shared SCS daemon."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scs.config import SCSSettings
from scs.service import ProcessLock
from scs.wire.client import SCSClient

DAEMON_START_TIMEOUT_SECONDS = 15.0
DAEMON_POLL_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class DaemonStatus:
    """Observable state of the single daemon for one SCS root."""

    available: bool
    ready: bool
    pid: int | None = None
    generation: str | None = None
    version: str | None = None
    error: str | None = None


class DaemonController:
    """Start, inspect, and stop the daemon without a platform service manager."""

    def __init__(self, settings: SCSSettings | None = None) -> None:
        self.settings: SCSSettings = settings or SCSSettings()
        self._socket_path: Path = self.settings.paths.runtime / "scs.sock"

    async def status(self) -> DaemonStatus:
        """Return daemon readiness without changing process state."""

        try:
            health = await SCSClient(
                self._socket_path, timeout_seconds=1.0
            ).call("system.health")
        except Exception as error:
            return DaemonStatus(
                available=False,
                ready=False,
                error=type(error).__name__,
            )
        identity = self._read_identity()
        pid = identity.get("pid")
        generation = health.get("generation")
        version = health.get("version")
        return DaemonStatus(
            available=True,
            ready=health.get("ready") is True,
            pid=pid if isinstance(pid, int) else None,
            generation=generation if isinstance(generation, str) else None,
            version=version if isinstance(version, str) else None,
        )

    async def ensure_started(self) -> DaemonStatus:
        """Return the live daemon, spawning exactly one contender when absent."""

        current = await self.status()
        if current.ready:
            return current
        self.settings.paths.ensure()
        deadline = time.monotonic() + DAEMON_START_TIMEOUT_SECONDS
        bootstrap_lock = ProcessLock(self.settings.paths.runtime / ".bootstrap.lock")
        while True:
            try:
                bootstrap_lock.acquire()
                break
            except RuntimeError:
                current = await self.status()
                if current.ready:
                    return current
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for SCS daemon bootstrap")
                await asyncio.sleep(DAEMON_POLL_SECONDS)
        try:
            current = await self.status()
            if current.ready:
                return current
            self._spawn()
            while time.monotonic() < deadline:
                current = await self.status()
                if current.ready:
                    return current
                await asyncio.sleep(DAEMON_POLL_SECONDS)
            raise TimeoutError("SCS daemon did not become ready before timeout")
        finally:
            bootstrap_lock.release()

    async def stop(self) -> bool:
        """Request generation-scoped graceful shutdown when a daemon is live."""

        current = await self.status()
        if not current.available:
            return False
        await SCSClient(self._socket_path).call(
            "system.shutdown", {"generation": current.generation}
        )
        deadline = time.monotonic() + DAEMON_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not (await self.status()).available:
                return True
            await asyncio.sleep(DAEMON_POLL_SECONDS)
        raise TimeoutError("SCS daemon did not stop before timeout")

    def _spawn(self) -> None:
        log_path = self.settings.paths.logs / "daemon.log"
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with log_path.open("ab", buffering=0) as log:
            subprocess.Popen(
                (sys.executable, "-m", "scs.cli", "serve"),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )

    def _read_identity(self) -> dict[str, object]:
        path = self.settings.paths.runtime / "daemon-service.json"
        try:
            payload = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return cast(dict[str, object], payload) if isinstance(payload, dict) else {}
