"""Generation-safe atomic discovery publication owned by the MCP proxy."""

from __future__ import annotations

import json
import os
from pathlib import Path

DISCOVERY_MODE = 0o600


class DiscoveryPublisher:
    """Publish and remove only the discovery generation owned by this proxy."""

    def __init__(self, path: Path, *, generation: str) -> None:
        self._path = path
        self._generation = generation

    def publish(self, *, url: str) -> None:
        """Atomically replace discovery after flushing file and directory metadata."""

        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        payload = {"service": "scs", "generation": self._generation, "url": url}
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, DISCOVERY_MODE)
        temporary.replace(self._path)
        directory_fd = os.open(self._path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def remove_owned(self) -> bool:
        """Remove discovery only when its generation still belongs to this proxy."""

        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError, OSError, json.JSONDecodeError:
            return False
        if (
            payload.get("service") != "scs"
            or payload.get("generation") != self._generation
        ):
            return False
        self._path.unlink()
        return True
