"""Atomic, generation-scoped daemon ownership records."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from scs.models import PROTOCOL_VERSION, ServiceIdentity

IDENTITY_MODE = 0o600


def _artifact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for block in iter(lambda: artifact.read(128 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class IdentityPublisher:
    """Publish and remove only one daemon process generation."""

    def __init__(
        self,
        path: Path,
        *,
        service: str,
        generation: str,
        artifact_path: Path,
    ) -> None:
        self._path = path
        self._identity = ServiceIdentity(
            service=service,
            pid=os.getpid(),
            start_time=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            generation=generation,
            artifact_sha256=_artifact_sha256(artifact_path),
            protocol_min=PROTOCOL_VERSION,
            protocol_max=PROTOCOL_VERSION,
        )

    @property
    def identity(self) -> ServiceIdentity:
        """Return the immutable identity written by this process."""

        return self._identity

    def publish(self) -> None:
        """Atomically publish the identity after durable file and directory flushes."""

        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self._identity.model_dump(mode="json"), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, IDENTITY_MODE)
        temporary.replace(self._path)
        _fsync_directory(self._path.parent)

    def remove_owned(self) -> bool:
        """Remove the record only when its service and generation still match."""

        try:
            if self._path.is_symlink():
                return False
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        if (
            payload.get("service") != self._identity.service
            or payload.get("generation") != self._identity.generation
        ):
            return False
        self._path.unlink()
        _fsync_directory(self._path.parent)
        return True
