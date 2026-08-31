"""Git-aware adaptive polling for automatic repository reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from scs.indexing.jobs import IngestionJobMode

logger = logging.getLogger(__name__)

GIT_HEAD_COMMAND: tuple[str, ...] = ("git", "rev-parse", "--verify", "HEAD")
GIT_STATUS_COMMAND: tuple[str, ...] = (
    "git",
    "status",
    "--porcelain=v1",
    "-z",
    "--untracked-files=all",
)


@dataclass(frozen=True, slots=True)
class GitFingerprint:
    """Opaque digest of one repository's Git-visible state."""

    digest: str


class JobQueue(Protocol):
    """Durable enqueue operation required by automatic reconciliation."""

    def enqueue(
        self,
        *,
        repo_path: str,
        store_id: str | None = None,
        store_generation: str | None = None,
        mode: IngestionJobMode,
        reason: str,
        payload: dict[str, object] | None = None,
        max_attempts: int = 3,
    ) -> object: ...


FingerprintReader = Callable[[Path, float], GitFingerprint | None]


def git_fingerprint(repo_path: Path, timeout_seconds: float) -> GitFingerprint | None:
    """Return a digest covering HEAD and every non-ignored working-tree change.

    Git's porcelain status owns ignore semantics and reports staged, unstaged,
    deleted, renamed, and untracked paths. An unborn repository has an empty
    HEAD but remains observable through its status output.
    """

    try:
        head = subprocess.run(
            GIT_HEAD_COMMAND,
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
        )
        status = subprocess.run(
            GIT_STATUS_COMMAND,
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Git state polling failed for %s: %s", repo_path, exc)
        return None
    if status.returncode != 0:
        message = status.stderr.decode("utf-8", errors="replace").strip()
        logger.warning(
            "Git status failed for %s with exit code %d: %s",
            repo_path,
            status.returncode,
            message or "<no stderr>",
        )
        return None
    head_bytes = head.stdout.strip() if head.returncode == 0 else b""
    digest = hashlib.sha256(head_bytes + b"\0" + status.stdout).hexdigest()
    return GitFingerprint(digest)


class RepositoryWatcher:
    """Reconcile one enrolled project from adaptively polled Git state."""

    def __init__(
        self,
        *,
        jobs: JobQueue,
        repo_path: Path,
        store_id: str,
        store_generation: str,
        fingerprint: FingerprintReader = git_fingerprint,
        active_interval_seconds: float = 2.0,
        idle_interval_seconds: float = 30.0,
        debounce_seconds: float = 0.5,
        git_timeout_seconds: float = 10.0,
    ) -> None:
        self._jobs: JobQueue = jobs
        self._repo_path: Path = repo_path.expanduser().resolve()
        self._store_id: str = store_id
        self._store_generation: str = store_generation
        self._fingerprint: FingerprintReader = fingerprint
        self._active_interval: float = active_interval_seconds
        self._idle_interval: float = idle_interval_seconds
        self._debounce: float = debounce_seconds
        self._git_timeout: float = git_timeout_seconds
        self._current_interval: float = active_interval_seconds
        self._baseline: GitFingerprint | None = None
        self._pending: bool = False
        self._task: asyncio.Task[None] | None = None
        self._timer: asyncio.Task[None] | None = None

    @property
    def current_interval_seconds(self) -> float:
        """Return the current adaptive interval for diagnostics and tests."""

        return self._current_interval

    async def start(self) -> None:
        """Recover missed work, establish a baseline, and start adaptive polling."""

        if self._task is not None:
            return
        await self.reconcile_startup()
        await self.observe_once()
        self._task = asyncio.create_task(self._poll(), name="scs-git-state-poller")

    async def stop(self) -> None:
        """Stop polling and discard any unflushed debounce timer."""

        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        self._pending = False

    async def reconcile_startup(self) -> None:
        """Queue hash-incremental full discovery to recover downtime changes."""

        await self._enqueue("git-poller-startup")

    async def observe_once(self) -> None:
        """Observe one Git state and update cadence without raising on failures."""

        observation = await asyncio.to_thread(
            self._fingerprint, self._repo_path, self._git_timeout
        )
        if observation is None:
            self._back_off()
            return
        if self._baseline is None:
            self._baseline = observation
            return
        if observation == self._baseline:
            self._back_off()
            return
        self._baseline = observation
        self._current_interval = self._active_interval
        self._pending = True
        previous = self._timer
        if previous is not None:
            previous.cancel()
        self._timer = asyncio.create_task(self._flush_later())

    async def flush_pending(self) -> None:
        """Flush one coalesced changed-state reconciliation if pending."""

        if not self._pending:
            return
        self._pending = False
        await self._enqueue("git-poller-change")

    def _back_off(self) -> None:
        """Exponentially reduce idle polling frequency within its hard cap."""

        self._current_interval = min(
            self._idle_interval, self._current_interval * 2
        )

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self._current_interval)
            await self.observe_once()

    async def _flush_later(self) -> None:
        try:
            await asyncio.sleep(self._debounce)
            await self.flush_pending()
        except asyncio.CancelledError:
            raise
        finally:
            if self._timer is asyncio.current_task():
                self._timer = None

    async def _enqueue(self, reason: str) -> None:
        await asyncio.to_thread(
            self._jobs.enqueue,
            repo_path=str(self._repo_path),
            store_id=self._store_id,
            store_generation=self._store_generation,
            mode="full",
            reason=reason,
        )
