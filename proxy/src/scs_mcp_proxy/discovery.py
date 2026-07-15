"""Generation-safe atomic discovery publication owned by the MCP proxy."""

from __future__ import annotations

import json
import os
import hashlib
from datetime import UTC, datetime
from pathlib import Path

DISCOVERY_MODE = 0o600
PROTOCOL_VERSION = 1


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _artifact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for block in iter(lambda: artifact.read(128 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ServiceIdentityPublisher:
    """Publish the proxy process identity without depending on the daemon package."""

    def __init__(self, path: Path, *, generation: str, artifact_path: Path) -> None:
        self._path = path
        self._generation = generation
        self._payload: dict[str, object] = {
            "service": "scs-proxy",
            "pid": os.getpid(),
            "start_time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "generation": generation,
            "artifact_sha256": _artifact_sha256(artifact_path),
            "protocol_min": PROTOCOL_VERSION,
            "protocol_max": PROTOCOL_VERSION,
        }

    def publish(self) -> None:
        """Atomically persist this proxy generation."""

        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self._payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, DISCOVERY_MODE)
        temporary.replace(self._path)
        _fsync_directory(self._path.parent)

    def remove_owned(self) -> bool:
        """Remove only the matching proxy generation."""

        try:
            if self._path.is_symlink():
                return False
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        if (
            payload.get("service") != "scs-proxy"
            or payload.get("generation") != self._generation
        ):
            return False
        self._path.unlink()
        _fsync_directory(self._path.parent)
        return True


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
        _fsync_directory(self._path.parent)

    def remove_owned(self) -> bool:
        """Remove discovery only when its generation still belongs to this proxy."""

        try:
            if self._path.is_symlink():
                return False
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError, OSError, json.JSONDecodeError:
            return False
        if (
            payload.get("service") != "scs"
            or payload.get("generation") != self._generation
        ):
            return False
        self._path.unlink()
        _fsync_directory(self._path.parent)
        return True
