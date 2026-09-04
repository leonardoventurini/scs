"""Portable single-daemon process ownership."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import BinaryIO


class ProcessLock:
    """Root-scoped exclusive lock preventing duplicate SCS daemons."""

    def __init__(self, path: Path) -> None:
        self._path: Path = path
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
            raise RuntimeError(
                "SCS daemon is already running for this storage root"
            ) from error
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"{os.getpid()}\n".encode())
        lock_file.flush()
        os.fsync(lock_file.fileno())
        self._file = lock_file

    def release(self) -> None:
        """Release ownership without deleting the shared lock inode."""

        lock_file, self._file = self._file, None
        if lock_file is None:
            return
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
